from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from desktop_runtime.lora_profiles import load_lora_profiles, merge_prompt_tags, resolve_prompt_loras
from desktop_runtime.workflow_builder import prepare_workflow
from winefox_core.lora_service import LoraService


class DesktopLoraProfileTests(unittest.TestCase):
    def test_legacy_alias_and_prompt_are_used_for_inventory_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="winefox_lora_") as temp:
            root = Path(temp)
            (root / "lora_profiles_anima.json").write_text(
                json.dumps({
                    "白露": {
                        "lora": "characters/Bailu.safetensors",
                        "character": "白露",
                        "official_aliases": ["Bailu"],
                        "strength_model": 0.72,
                        "strength_clip": 0.66,
                    }
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "lora_aliases_anima.json").write_text(
                json.dumps({"小白露": "characters/Bailu.safetensors"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "lora_character_prompts_anima.json").write_text(
                json.dumps({"characters/Bailu.safetensors": "bailu outfit, white hair"}),
                encoding="utf-8",
            )

            loaded = load_lora_profiles(root, env={})
            service = LoraService(loaded.profiles)
            resolved = resolve_prompt_loras(
                service,
                "画一张小白露在雪地里",
                "anima",
                ["models/loras/Bailu.safetensors"],
            )

            self.assertEqual(loaded.count, 1)
            self.assertEqual(resolved.loras[0]["name"], "models/loras/Bailu.safetensors")
            self.assertEqual(resolved.loras[0]["strength_model"], 0.72)
            self.assertIn("bailu outfit", resolved.triggers)
            self.assertEqual(resolved.metadata["matches"][0]["matched_term"], "小白露")

    def test_prompt_tags_are_appended_once(self) -> None:
        self.assertEqual(
            merge_prompt_tags("portrait, white hair", ["white hair", "blue eyes"]),
            "portrait, white hair, blue eyes",
        )

    def test_multiple_loras_fill_separate_workflow_nodes(self) -> None:
        source = {
            "1": {"class_type": "ModelLoader", "inputs": {}},
            "2": {"class_type": "LoraLoader", "inputs": {"model": ["1", 0], "clip": ["1", 1], "lora_name": "old-a", "strength_model": 1.0, "strength_clip": 1.0}},
            "3": {"class_type": "LoraLoader", "inputs": {"model": ["2", 0], "clip": ["2", 1], "lora_name": "old-b", "strength_model": 1.0, "strength_clip": 1.0}},
        }
        workflow, _ = prepare_workflow(source, {
            "prompt": "two characters",
            "seed": 0,
            "loras": [
                {"name": "a.safetensors", "strength_model": 0.7, "strength_clip": 0.6},
                {"name": "b.safetensors", "strength_model": 0.8, "strength_clip": 0.75},
            ],
        })
        self.assertEqual(workflow["2"]["inputs"]["lora_name"], "a.safetensors")
        self.assertEqual(workflow["3"]["inputs"]["lora_name"], "b.safetensors")
        self.assertEqual(workflow["3"]["inputs"]["strength_clip"], 0.75)

    def test_sampler_settings_remain_from_workflow_without_explicit_override(self) -> None:
        source = {
            "1": {
                "class_type": "KSampler",
                "inputs": {"seed": 9, "steps": 31, "cfg": 5.5, "sampler_name": "dpmpp_2m_sde", "scheduler": "karras"},
            }
        }
        workflow, _ = prepare_workflow(source, {"prompt": "portrait", "seed": 0})
        self.assertEqual(workflow["1"]["inputs"]["steps"], 31)
        self.assertEqual(workflow["1"]["inputs"]["cfg"], 5.5)
        self.assertEqual(workflow["1"]["inputs"]["sampler_name"], "dpmpp_2m_sde")
        self.assertEqual(workflow["1"]["inputs"]["scheduler"], "karras")


if __name__ == "__main__":
    unittest.main()
