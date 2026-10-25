"""Regressions for failures observed in the actual portable generation path."""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web

from desktop_runtime.llm_client import OpenAICompatibleLLMClient
from desktop_runtime.engine import DesktopEngine
from desktop_runtime.workflow_builder import prepare_workflow
from desktop_runtime.lora_profiles import profile_from_comfy_metadata, resolve_selected_lora_triggers
from desktop_runtime.lora_import import provider_lora_profile
from winefox_core.lora_service import LoraService
from winefox_core.storage import Database
from desktop_backend.errors import AppError


class ChunkedProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        async def completion(request):
            self.requests.append(await request.json())
            response = web.StreamResponse(headers={"Content-Type":"application/json; charset=utf-8"})
            await response.prepare(request)
            payload = json.dumps({"choices":[{"message":{"content":"1girl, full body, dancing, beach, ocean"}}]}).encode()
            for piece in (payload[:7], payload[7:31], payload[31:]):
                await response.write(piece)
                await asyncio.sleep(.01)
            await response.write_eof()
            return response
        async def models(request):
            response = web.StreamResponse(headers={"Content-Type":"application/json"})
            await response.prepare(request)
            await response.write(b'{"data":[')
            await asyncio.sleep(.01)
            await response.write(b'{"id":"provider-model"}]}')
            await response.write_eof()
            return response
        app = web.Application()
        app.router.add_post('/v1/chat/completions',completion)
        app.router.add_get('/v1/models',models)
        self.runner=web.AppRunner(app)
        await self.runner.setup()
        site=web.TCPSite(self.runner,'127.0.0.1',0)
        await site.start()
        self.client=OpenAICompatibleLLMClient(f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1')

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_translation_reads_complete_chunked_json(self):
        result=await self.client.complete([{"role":"user","content":"在沙滩上跳舞"}],model="provider-model",max_tokens=800)
        self.assertIn('dancing',result['choices'][0]['message']['content'])
        self.assertFalse(self.requests[0]['stream'])

    async def test_model_list_reads_complete_chunked_json(self):
        self.assertEqual(await self.client.list_models(),['provider-model'])


class GenerationSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_chinese_translation_never_submits_gpu_work(self):
        engine=object.__new__(DesktopEngine)
        engine.db=Database(':memory:'); engine.db.initialize()
        try:
            engine._cancel_requested=set()
            engine._prepare_prompt=AsyncMock(return_value=SimpleNamespace(source='fallback',cleaned_prompt='在沙滩上跳舞',warnings=['AI 请求失败']))
            engine._attach_prompt_debug=lambda *args: None
            engine._select_workflow=lambda *args: self.fail('must not select or submit a GPU workflow')
            cur=engine.db.connection.execute("INSERT INTO jobs(client_request_id,mode,state,request_json,created_at,updated_at) VALUES ('fixture','txt2img','queued',?, 'now','now')",(json.dumps({'prompt':'在沙滩上跳舞'}),))
            with self.assertRaises(AppError) as caught:
                await engine._run_job(cur.lastrowid)
            self.assertEqual(caught.exception.code,'PROMPT_TRANSLATION_FAILED')
        finally:
            engine.db.close()


class SelectedModelAndWorkflowTests(unittest.TestCase):
    def test_sdxl_header_identifies_family_without_trigger_metadata(self):
        family,profile=profile_from_comfy_metadata('unknown-name.safetensors',{'ss_base_model_version':'sdxl_a1111_compat'},default_family='anima')
        self.assertEqual(family,'illustrious')
        self.assertEqual(profile['trigger_words'],[])

    def test_provider_character_trigger_is_applied_after_renaming_file(self):
        profile=provider_lora_profile({'model_name':'Character','trainedWords':['feranmut proxy (arknights), green hair, dragon horns,']},'renamed.safetensors','illustrious')
        service=LoraService({'illustrious':{'role':profile}})
        triggers=resolve_selected_lora_triggers(service,'illustrious',[{'name':'renamed.safetensors'}])
        self.assertIn('feranmut proxy (arknights)',triggers)
        self.assertIn('green hair',triggers)

    def test_default_illustrious_workflow_is_single_pass_at_requested_size(self):
        root=Path(__file__).resolve().parents[1]
        source=json.loads((root/'workflow/finish_illustrious_fast_api.json').read_text(encoding='utf-8'))
        workflow,_=prepare_workflow(source,{'prompt':'full body, dancing, beach','seed':777,'width':832,'height':1216,'loras':[{'name':'role.safetensors','strength':.8}]})
        samplers=[n for n in workflow.values() if n['class_type']=='KSampler']
        self.assertEqual(len(samplers),1); self.assertEqual(samplers[0]['inputs']['steps'],20)
        self.assertNotIn('LatentUpscaleBy',[n['class_type'] for n in workflow.values()])
        self.assertEqual(samplers[0]['inputs']['model'],['99',0])
        self.assertEqual(workflow['99']['inputs']['lora_name'],'role.safetensors')
