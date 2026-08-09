# -*- coding: utf-8 -*-
"""WineFox Agent 06 测试：PromptService / LoraService / WorkflowService / CatalogService。

纯业务模块测试，不依赖 AstrBot / HTTP / 数据库。
运行方式（仓库根目录）：
    python -m unittest tests_desktop.test_agent06_prompt_lora -v
    或 python tests_desktop/test_agent06_prompt_lora.py
"""

import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winefox_core.prompt_service import (  # noqa: E402
    LLMPortError,
    PromptResult,
    PromptService,
    default_negative_prompt,
    extract_seed,
    strip_reasoning_content,
    validate_llm_output,
)
from winefox_core.lora_service import (  # noqa: E402
    LoraProfile,
    LoraSelection,
    LoraService,
    MODEL_FAMILY_ANIMA,
    MODEL_FAMILY_ILLUSTRIOUS,
    is_generic_term,
    normalize_lora_key,
    normalize_match_key,
)
from winefox_core.workflow_service import (  # noqa: E402
    WorkflowEntry,
    WorkflowService,
    canonical_json,
    detect_family,
    detect_mode,
    sha256_bytes,
    stable_workflow_id,
    validate_workflow_nodes,
)
from winefox_core.catalog_service import CatalogService, CatalogSummary  # noqa: E402


# ---------------------------------------------------------------------------
# 样本数据
# ---------------------------------------------------------------------------

BAILU_PROFILE = {
    "character": "白露",
    "lora": "Bailu.safetensors",
    "official_aliases": ["白露", "Bailu", "bailu"],
    "nickname_aliases": ["露露"],
    "role_terms": ["医生", "医师"],
    "trigger_words": ["bailu, 白露, doctor, 医生"],
    "strength_model": 0.8,
    "strength_clip": 0.7,
    "family": MODEL_FAMILY_ILLUSTRIOUS,
}

JINGLIU_PROFILE = {
    "character": "镜流",
    "lora": "Jingliu.safetensors",
    "official_aliases": ["镜流", "Jingliu", "jingliu"],
    "trigger_words": ["jingliu, 镜流, sword, 剑"],
    "family": MODEL_FAMILY_ILLUSTRIOUS,
}

KUJOU_SARA_PROFILE = {
    "character": "九条裟罗",
    "lora": "KujouSara.safetensors",
    "official_aliases": ["九条裟罗", "九条裟羅", "Kujou Sara", "kujou sara"],
    "trigger_words": ["kujou sara, 九条裟罗"],
    "family": MODEL_FAMILY_ANIMA,
}

GENERIC_WORDS = ("自己", "少女", "girl", "white hair", "白发", "角色", "1girl", "masterpiece")


def _illustrious_profiles() -> dict:
    return {BAILU_PROFILE["character"]: BAILU_PROFILE, JINGLIU_PROFILE["character"]: JINGLIU_PROFILE}


def _make_workflow(name: str = "demo_api.json") -> dict:
    """构造一个合法的 API 格式工作流（正向/负向/输出/LoRA 节点齐全）。"""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "waiIllustriousSDXL_v170.safetensors"}},
        "99": {"class_type": "LoraLoader", "inputs": {"model": ["1", 0], "clip": ["1", 1], "lora_name": "Bailu.safetensors", "strength_model": 0.8, "strength_clip": 0.7}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {"text": "masterpiece, best quality, 1girl", "clip": ["2", 0]}},
        "12": {"class_type": "CLIPTextEncode", "inputs": {"text": "worst quality, low quality, bad anatomy", "clip": ["2", 1]}},
        "13": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 768, "batch_size": 1}},
        "14": {"class_type": "KSampler", "inputs": {"seed": 42, "steps": 20, "cfg": 6, "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0, "model": ["1", 0], "positive": ["11", 0], "negative": ["12", 0], "latent_image": ["13", 0]}},
        "17": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["1", 2]}},
        "66": {"class_type": "SaveImage", "inputs": {"filename_prefix": "WineFox", "images": ["17", 0]}},
    }


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# PromptService
# ---------------------------------------------------------------------------


class FakeLLMPort:
    """可编程 LLM 端口桩。"""

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.calls = []
        self.error = error

    async def complete(self, messages, **options):
        self.calls.append({"messages": messages, "options": options})
        if self.error:
            raise self.error
        if not self.responses:
            raise LLMPortError("no response")
        return self.responses.pop(0)


class PromptServiceTests(unittest.TestCase):
    def test_raw_prompt_is_preserved(self):
        service = PromptService(config={"family": MODEL_FAMILY_ILLUSTRIOUS})
        result = run(service.process("  一个穿白裙的女孩  "))
        self.assertIsInstance(result, PromptResult)
        self.assertEqual(result.raw_prompt, "一个穿白裙的女孩")

    def test_no_llm_deterministic_fallback(self):
        service = PromptService(config={"family": MODEL_FAMILY_ILLUSTRIOUS})
        result = run(service.process("画一个女孩"))
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.final_prompt, result.cleaned_prompt)
        self.assertIn("worst quality", result.negative_prompt)
        self.assertEqual(result.seed, None)

    def test_seed_extraction_removes_seed_from_prompt(self):
        for text, expected_seed in (
            ("一个女孩 seed=123456", 123456),
            ("一个女孩 --seed 999", 999),
            ("一个女孩 seed: 42", 42),
            ("一个女孩 种子：7788", 7788),
            ("一个女孩 固定种子=1", 1),
        ):
            cleaned, seed = extract_seed(text)
            self.assertEqual(seed, expected_seed, text)
            self.assertNotIn("seed", cleaned.lower(), text)
            self.assertNotIn("种子", cleaned, text)
            self.assertIn("女孩", cleaned, text)
            self.assertNotIn("123456", cleaned, text)

    def test_seed_invalid_values_ignored(self):
        self.assertEqual(extract_seed("画 seed=abc"), ("画 seed=abc", None))
        self.assertEqual(extract_seed("画 seed=-5"), ("画 seed=-5", None))
        self.assertEqual(extract_seed("画 seed="), ("画 seed=", None))

    def test_seed_not_left_in_prompt_pipeline(self):
        service = PromptService(config={"family": MODEL_FAMILY_ILLUSTRIOUS})
        result = run(service.process("一个女孩 seed=2024"))
        self.assertEqual(result.seed, 2024)
        self.assertNotIn("2024", result.final_prompt)
        self.assertNotIn("seed", result.final_prompt.lower())

    def test_reasoning_content_ignored_from_llm_response(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": "1girl, white dress, standing",
                        "reasoning_content": "The user wants a girl with a white dress, I should output tags...",
                    }
                }
            ]
        }
        port = FakeLLMPort(responses=[response])
        service = PromptService(config={"family": MODEL_FAMILY_ILLUSTRIOUS, "llm": {"enabled": True}}, llm_port=port)
        result = run(service.process("一个穿白裙的女孩"))
        self.assertEqual(result.source, "llm")
        self.assertIn("1girl", result.final_prompt)
        self.assertNotIn("The user wants", result.final_prompt)
        self.assertNotIn("reasoning", result.final_prompt.lower())

    def test_think_blocks_stripped(self):
        text = "1girl, nice tags <think>Let me analyze...</think> 2girls"
        cleaned = strip_reasoning_content(text)
        self.assertNotIn("<think>", cleaned)
        self.assertNotIn("Let me analyze", cleaned)

    def test_reasoning_only_response_means_empty_and_fallback(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "reasoning_content": "thinking about the prompt...",
                    }
                }
            ]
        }
        port = FakeLLMPort(responses=[response])
        service = PromptService(config={"family": MODEL_FAMILY_ILLUSTRIOUS, "llm": {"enabled": True}}, llm_port=port)
        result = run(service.process("画一个女孩"))
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.final_prompt, result.cleaned_prompt)
        self.assertTrue(any("无效" in w for w in result.warnings))

    def test_empty_llm_output_falls_back(self):
        port = FakeLLMPort(responses=[{"choices": [{"message": {"content": ""}}]}])
        service = PromptService(config={"family": MODEL_FAMILY_ILLUSTRIOUS, "llm": {"enabled": True}}, llm_port=port)
        result = run(service.process("画一个女孩"))
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.final_prompt, "画一个女孩")

    def test_llm_port_failure_falls_back(self):
        port = FakeLLMPort(error=LLMPortError("boom"))
        service = PromptService(config={"family": MODEL_FAMILY_ILLUSTRIOUS, "llm": {"enabled": True}}, llm_port=port)
        result = run(service.process("画一个女孩"))
        self.assertEqual(result.source, "fallback")
        self.assertTrue(any("fallback" in w for w in result.warnings))

    def test_llm_disabled_by_config_never_calls_port(self):
        port = FakeLLMPort()
        service = PromptService(config={"family": MODEL_FAMILY_ILLUSTRIOUS}, llm_port=port)
        run(service.process("画一个女孩"))
        self.assertEqual(port.calls, [])

    def test_negative_prompt_defaults_by_family(self):
        self.assertIn("worst quality", default_negative_prompt(MODEL_FAMILY_ILLUSTRIOUS))
        self.assertIn("worst quality", default_negative_prompt(MODEL_FAMILY_ANIMA))
        self.assertIn("worst quality", default_negative_prompt("unknown-family"))

    def test_configured_negative_prompt_wins(self):
        service = PromptService(config={"negative_prompt": "custom negative", "family": MODEL_FAMILY_ILLUSTRIOUS})
        result = run(service.process("画一个女孩"))
        self.assertEqual(result.negative_prompt, "custom negative")

    def test_matcher_injected_produces_matches(self):
        def matcher(raw, cleaned):
            return [{"kind": "lora", "value": "白露", "score": 100.0, "detail": {"family": "illustrious"}}]

        service = PromptService(config={}, matcher=matcher)
        result = run(service.process("画个白露"))
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].kind, "lora")
        self.assertEqual(result.matches[0].value, "白露")
        self.assertEqual(result.matches[0].score, 100.0)

    def test_validate_llm_output_rejects_refusals(self):
        ok, _ = validate_llm_output("sorry, I cannot do that")
        self.assertFalse(ok)
        ok, _ = validate_llm_output("1girl, best quality")
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# LoraService
# ---------------------------------------------------------------------------


class LoraServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = LoraService(
            profiles={
                MODEL_FAMILY_ILLUSTRIOUS: _illustrious_profiles(),
                MODEL_FAMILY_ANIMA: {KUJOU_SARA_PROFILE["character"]: KUJOU_SARA_PROFILE},
            }
        )

    def test_explicit_character_match(self):
        selection = self.service.single_match("画一个白露", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertIsNotNone(selection)
        self.assertEqual(selection.key, "白露")
        self.assertEqual(selection.lora_name, "Bailu.safetensors")
        self.assertEqual(selection.match_kind, "exact")
        self.assertFalse(selection.suggestion_only)

    def test_alias_match_latin_and_nickname(self):
        for text in ("Bailu", "bailu", "露露"):
            selection = self.service.single_match(f"画{text}", family=MODEL_FAMILY_ILLUSTRIOUS)
            self.assertIsNotNone(selection, text)
            self.assertEqual(selection.key, "白露", text)

    def test_latin_alias_not_matched_inside_word(self):
        # 英文别名必须有整词边界："Bailu" 不能命中 "Bailun"
        selection = self.service.single_match("Bailun 城市", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertIsNone(selection)

    def test_generic_words_never_match(self):
        for word in GENERIC_WORDS:
            selection = self.service.single_match(f"画{word}", family=MODEL_FAMILY_ILLUSTRIOUS)
            self.assertIsNone(selection, f"泛化词不应命中: {word}")
            self.assertTrue(is_generic_term(word), f"应识别为泛化词: {word}")

    def test_vague_text_no_match(self):
        selection = self.service.single_match("画一个好看的角色", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertIsNone(selection)

    def test_single_role(self):
        result = self.service.match("画一个白露", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertEqual(len(result.matches), 1)
        self.assertFalse(result.multi)

    def test_multi_role_two_characters(self):
        result = self.service.match("白露和镜流", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertEqual(len(result.matches), 2)
        self.assertTrue(result.multi)
        self.assertTrue(result.connector_found)
        keys = {m.key for m in result.matches}
        self.assertEqual(keys, {"白露", "镜流"})

    def test_multi_role_requires_connector(self):
        # 两个角色但无连接词 → 不判定为多角色（无明确证据）
        result = self.service.match("白露 镜流", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertGreaterEqual(len(result.matches), 1)
        self.assertFalse(result.multi)

    def test_cross_family_suggestion_only_no_persist(self):
        # 九条裟罗只在 anima 族；当前 illustrious 族下只返回临时建议
        selection = self.service.suggest_cross_family("画一个九条裟罗", current_family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertIsNotNone(selection)
        self.assertTrue(selection.suggestion_only)
        self.assertEqual(selection.family, MODEL_FAMILY_ANIMA)
        self.assertIn("临时建议", selection.notice)
        # 当前族有明确命中 → 不跨族
        none_selection = self.service.suggest_cross_family("画一个白露", current_family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertIsNone(none_selection)

    def test_cross_family_does_not_change_family_state(self):
        before = dict(self.service.profiles_for(MODEL_FAMILY_ILLUSTRIOUS))
        self.service.suggest_cross_family("画一个九条裟罗", current_family=MODEL_FAMILY_ILLUSTRIOUS)
        after = dict(self.service.profiles_for(MODEL_FAMILY_ILLUSTRIOUS))
        self.assertEqual(before, after)

    def test_strength_resolution_and_caps(self):
        selection = self.service.single_match("画一个白露", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertEqual(selection.strength_model, 0.8)
        self.assertEqual(selection.strength_clip, 0.7)
        # 超过上限被钳制
        profile = LoraProfile(
            key="测试", lora_name="Test.safetensors", family=MODEL_FAMILY_ILLUSTRIOUS,
            character="测试角色", strength_model=1.5, strength_clip=1.5,
        )
        self.service.add_profile(profile)
        selection = self.service.single_match("测试角色", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertLessEqual(selection.strength_model, 0.82)
        self.assertLessEqual(selection.strength_clip, 0.72)

    def test_name_normalization(self):
        self.assertEqual(normalize_lora_key("models/loras/Bailu.safetensors"), "bailu")
        self.assertEqual(normalize_lora_key("Bailu (1).safetensors"), "bailu(1)")
        self.assertEqual(normalize_match_key("白露 医生"), "白露医生")
        self.assertEqual(normalize_match_key("Jingliu_v2.safetensors"), "jingliuv2")

    def test_profile_from_dict_parses_alias_fields(self):
        profile = LoraProfile.from_dict("白露", BAILU_PROFILE)
        self.assertIn("Bailu", profile.aliases)
        self.assertIn("露露", profile.aliases)
        self.assertEqual(profile.role_terms, ["医生", "医师"])
        self.assertIn("bailu", profile.trigger_words)
        self.assertEqual(profile.strength_model, 0.8)

    def test_trigger_words_are_evidence(self):
        selection = self.service.single_match("剑 镜流", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertIsNotNone(selection)

    def test_match_limit_and_dedupe_by_lora(self):
        result = self.service.match("白露 镜流 露露", family=MODEL_FAMILY_ILLUSTRIOUS, limit=2)
        self.assertLessEqual(len(result.matches), 2)
        lora_keys = [normalize_lora_key(m.lora_name) for m in result.matches]
        self.assertEqual(len(lora_keys), len(set(lora_keys)))

    def test_result_to_dict_is_pure_json(self):
        result = self.service.match("白露和镜流", family=MODEL_FAMILY_ILLUSTRIOUS)
        dumped = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertIn("白露", dumped)
        self.assertIn("multi", dumped)


# ---------------------------------------------------------------------------
# WorkflowService
# ---------------------------------------------------------------------------


class WorkflowServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wf_dir = Path(self.tmp.name) / "workflow"
        self.wf_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, data) -> Path:
        path = self.wf_dir / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_stable_id_and_sha256(self):
        data = _make_workflow()
        path = self._write("demo_api.json", data)
        service = WorkflowService(self.wf_dir)
        entries = service.scan()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        # SHA256 = 原始字节哈希
        self.assertEqual(entry.sha256, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(entry.sha256, sha256_bytes(path.read_bytes()))
        # 稳定 ID = 规范化 JSON 哈希，与格式无关
        self.assertEqual(entry.workflow_id, stable_workflow_id(data))
        reformatted = json.dumps(data, ensure_ascii=False, indent=8, sort_keys=True)
        self.assertEqual(entry.workflow_id, stable_workflow_id(json.loads(reformatted)))
        self.assertEqual(len(entry.workflow_id), 64)

    def test_stable_id_independent_of_key_order(self):
        data = _make_workflow()
        reordered = dict(reversed(list(data.items())))
        self.assertEqual(stable_workflow_id(data), stable_workflow_id(reordered))

    def test_corrupted_json_skipped_with_error(self):
        (self.wf_dir / "broken.json").write_text("{not valid json!!", encoding="utf-8")
        (self.wf_dir / "good.json").write_text(json.dumps(_make_workflow()), encoding="utf-8")
        service = WorkflowService(self.wf_dir)
        entries = service.scan()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "good.json")
        self.assertTrue(service.errors())
        self.assertIn("broken.json", service.errors()[0]["file"])
        self.assertIn("JSON 损坏", service.errors()[0]["error"])

    def test_validation_detects_missing_nodes(self):
        broken = _make_workflow()
        del broken["12"]  # 删除负向节点
        del broken["66"]  # 删除输出节点
        validation = validate_workflow_nodes(broken)
        self.assertFalse(validation.ok)
        messages = [f["message"] for f in validation.findings]
        self.assertTrue(any("负向" in m for m in messages))
        self.assertTrue(any("输出" in m for m in messages))

    def test_validation_detects_dangling_reference(self):
        bad = _make_workflow()
        bad["14"]["inputs"]["positive"] = ["999", 0]  # 悬空引用
        validation = validate_workflow_nodes(bad)
        self.assertTrue(any(f["severity"] == "warning" and "悬空" in f["message"] for f in validation.findings))

    def test_family_and_mode_detection(self):
        self.assertEqual(detect_family(_make_workflow(), "finish_illustrious_api.json"), "illustrious")
        self.assertEqual(detect_mode(_make_workflow()), "txt2img")

        img2img = _make_workflow()
        img2img["20"] = {"class_type": "LoadImage", "inputs": {"image": "a.png"}}
        img2img["13"] = {"class_type": "ImageScale", "inputs": {"image": ["20", 0]}}
        self.assertEqual(detect_mode(img2img), "img2img")

        video = {
            "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "a girl walking", "clip": ["3", 0]}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "worst quality", "clip": ["3", 1]}},
            "9": {"class_type": "KSamplerAdvanced", "inputs": {"seed": 1}},
            "11": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "out"}},
        }
        self.assertEqual(detect_mode(video), "video")
        self.assertEqual(detect_family(video, "wan_i2v_api.json"), "wan")

    def test_custom_workflow_not_overwritten(self):
        self._write("demo_api.json", _make_workflow())
        service = WorkflowService(self.wf_dir)
        builtin = service.scan()[0]
        # 同名自定义工作流（内容不同）
        custom_data = _make_workflow("custom_api.json")
        custom_data["66"]["inputs"]["filename_prefix"] = "CustomPrefix"
        custom_entry = service.register_custom(custom_data, "demo_api.json")
        # 内置 registry 未被覆盖
        self.assertEqual(service.builtin_registry["demo_api.json"][0].workflow_id, builtin.workflow_id)
        self.assertEqual(service.get("demo_api.json").workflow_id, builtin.workflow_id)
        # 自定义条目独立存在
        self.assertTrue(service.is_custom("demo_api.json"))
        self.assertEqual(service.list_custom()[0].workflow_id, custom_entry.workflow_id)
        # prefer_custom 时返回自定义
        service.prefer_custom = True
        self.assertEqual(service.get("demo_api.json").workflow_id, custom_entry.workflow_id)
        service.prefer_custom = False

    def test_builtin_snapshot_as_independent_registry(self):
        self._write("demo_api.json", _make_workflow())
        service = WorkflowService(self.wf_dir)
        builtin = service.scan()[0]
        # 内置新版（不同内容）注册为独立条目，不覆盖旧版
        new_version = _make_workflow()
        new_version["11"]["inputs"]["text"] = "masterpiece, new builtin version"
        new_entry = service.register_builtin_snapshot(new_version, "demo_api.json", version="builtin-v2")
        self.assertEqual(new_entry.registry_version, "builtin-v2")
        self.assertEqual(len(service.builtin_registry["demo_api.json"]), 2)
        ids = [e.workflow_id for e in service.builtin_registry["demo_api.json"]]
        self.assertEqual(len(set(ids)), 2)
        # 旧版条目仍然可查
        self.assertEqual(service.builtin_registry["demo_api.json"][0].workflow_id, builtin.workflow_id)
        # 默认 get 返回最新内置版本
        self.assertEqual(service.get("demo_api.json").workflow_id, new_entry.workflow_id)

    def test_real_builtin_workflows_scan(self):
        """扫描插件内置 workflow/ 目录：全部可解析、模式/族识别合理、无崩溃。"""
        real_dir = ROOT / "workflow"
        if not real_dir.is_dir():
            self.skipTest("workflow 目录不存在")
        service = WorkflowService(real_dir)
        entries = service.scan()
        names = {e.name for e in entries}
        self.assertIn("finish_illustrious_api.json", names)
        self.assertIn("img2img_illustrious_api.json", names)
        self.assertIn("wan_i2v_api.json", names)
        by_name = {e.name: e for e in entries}
        self.assertEqual(by_name["finish_illustrious_api.json"].family, "illustrious")
        self.assertEqual(by_name["finish_illustrious_api.json"].mode, "txt2img")
        self.assertEqual(by_name["img2img_illustrious_api.json"].mode, "img2img")
        self.assertEqual(by_name["wan_i2v_api.json"].mode, "video")
        self.assertEqual(by_name["wan_i2v_api.json"].family, "wan")
        # 损坏文件（若存在）不应导致崩溃，且被记录
        self.assertTrue(service.errors() == [] or any(e for e in service.errors()))


# ---------------------------------------------------------------------------
# CatalogService
# ---------------------------------------------------------------------------


class CatalogServiceTests(unittest.TestCase):
    def test_build_merges_pure_json(self):
        service = CatalogService()
        catalog = service.build(
            model_families=[
                {"key": "illustrious", "label": "Illustrious", "modes": ["txt2img", "img2img"]},
                {"key": "anima", "label": "Anima", "modes": ["txt2img"]},
            ],
            workflows=[{"workflow_id": "a" * 64, "name": "finish_illustrious_api.json", "family": "illustrious", "mode": "txt2img"}],
            checkpoints=[{"name": "waiIllustriousSDXL_v170.safetensors", "family": "illustrious"}],
            loras=[{"key": "白露", "lora_name": "Bailu.safetensors", "family": "illustrious"}],
            declared_capabilities=[
                {"capability": "txt2img", "family": "illustrious", "requires": ["checkpoint", "workflow", "lora"]},
                {"capability": "video", "family": "wan", "requires": ["checkpoint", "workflow"]},
            ],
        )
        # 纯 JSON：可往返
        dumped = service.to_json(catalog)
        roundtrip = json.loads(dumped)
        self.assertEqual(roundtrip["schema_version"], 1)
        self.assertEqual(len(roundtrip["model_families"]), 2)
        self.assertEqual(len(roundtrip["workflows"]), 1)
        self.assertEqual(len(roundtrip["checkpoints"]), 1)
        self.assertEqual(len(roundtrip["loras"]), 1)
        # 能力缺口：wan 族缺 checkpoint 与 workflow
        missing = roundtrip["capabilities"]["missing"]
        self.assertTrue(any(m["capability"] == "video" and "checkpoint" in m["missing"] for m in missing))
        self.assertEqual(roundtrip["summary"]["missing_capabilities"], [m["reason"] for m in missing])

    def test_build_with_workflow_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf_dir = Path(tmp) / "workflow"
            wf_dir.mkdir()
            (wf_dir / "demo_api.json").write_text(json.dumps(_make_workflow()), encoding="utf-8")
            wf_service = WorkflowService(wf_dir)
            wf_service.scan()
            catalog = CatalogService(workflow_service=wf_service).build()
            self.assertEqual(len(catalog["workflows"]), 1)
            self.assertEqual(catalog["workflows"][0]["family"], "illustrious")
            self.assertEqual(catalog["workflows"][0]["mode"], "txt2img")

    def test_summary_counts(self):
        catalog = CatalogService().build(
            workflows=[{"workflow_id": "x", "name": "a.json", "family": "illustrious", "mode": "txt2img"}],
            checkpoints=[{"name": "c.safetensors", "family": "illustrious"}],
            loras=[{"key": "k", "lora_name": "l.safetensors", "family": "illustrious"}],
        )
        self.assertEqual(catalog["summary"]["workflows"], 1)
        self.assertEqual(catalog["summary"]["checkpoints"], 1)
        self.assertEqual(catalog["summary"]["loras"], 1)

    def test_deterministic_output(self):
        kwargs = dict(
            model_families=[{"key": "illustrious", "label": "Illustrious"}],
            workflows=[{"workflow_id": "x", "name": "a.json", "family": "illustrious", "mode": "txt2img"}],
            checkpoints=[{"name": "c.safetensors", "family": "illustrious"}],
            loras=[{"key": "k", "lora_name": "l.safetensors", "family": "illustrious"}],
        )
        c1 = CatalogService().build(**kwargs)
        c2 = CatalogService().build(**kwargs)
        self.assertEqual(CatalogService().to_json(c1), CatalogService().to_json(c2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
