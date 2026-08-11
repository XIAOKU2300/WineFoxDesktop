from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop_runtime.engine import DesktopEngine
from desktop_runtime.lora_profiles import (
    cache_generated_lora_profiles,
    lora_metadata_cache_key,
    load_lora_profiles,
    merge_prompt_tags,
    profile_from_comfy_metadata,
    resolve_prompt_loras,
)
from desktop_runtime.workflow_builder import prepare_workflow
from winefox_core.lora_service import LoraService


class DesktopLoraProfileTests(unittest.TestCase):
    def test_metadata_profile_uses_default_family_and_high_confidence_trigger(self) -> None:
        parsed = profile_from_comfy_metadata(
            "Adult_Nahida_from_Genshin_Impact_PDIL.safetensors",
            {
                "modelspec.title": "Adult_Nahida_from_Genshin_Impact_PDIL",
                "ss_output_name": "Adult_Nahida_from_Genshin_Impact_PDIL",
                "ss_base_model_version": "sdxl_base_v1-0",
                "ss_tag_frequency": json.dumps(
                    {
                        "img": {
                            "aunahida": 296,
                            "genshin impact": 296,
                            "1girl": 296,
                            "white hair": 280,
                            "looking at viewer": 260,
                        }
                    }
                ),
            },
            default_family="illustrious",
        )

        self.assertIsNotNone(parsed)
        family, profile = parsed
        self.assertEqual(family, "illustrious")
        self.assertEqual(profile["trigger_words"], ["aunahida"])
        self.assertFalse(any("\u3400" <= char <= "\u9fff" for char in profile["character"]))

    def test_metadata_title_can_preserve_a_specific_chinese_alias(self) -> None:
        parsed = profile_from_comfy_metadata(
            "characters/Nahida.safetensors",
            {
                "modelspec.title": "纳西妲 Nahida",
                "ss_output_name": "Nahida",
                "ss_tag_frequency": json.dumps({"set": {"aunahida": 90, "1girl": 90}}),
            },
            default_family="illustrious",
        )

        self.assertIsNotNone(parsed)
        _, profile = parsed
        self.assertEqual(profile["character"], "纳西妲 Nahida")
        self.assertIn("aunahida", profile["trigger_words"])

    def test_metadata_does_not_turn_franchise_only_terms_into_profile(self) -> None:
        parsed = profile_from_comfy_metadata(
            "wuthering_waves.safetensors",
            {
                "modelspec.title": "Wuthering Waves",
                "ss_base_model_version": "sdxl_base_v1-0",
                "ss_tag_frequency": json.dumps({"data": {"wuthering waves": 100}}),
            },
        )

        self.assertIsNone(parsed)

    def test_imported_profile_wins_over_generated_cache_for_same_lora(self) -> None:
        with tempfile.TemporaryDirectory(prefix="winefox_cache_priority_") as temp:
            root = Path(temp)
            cache_generated_lora_profiles(
                root,
                "server-key",
                {
                    "anima": {},
                    "illustrious": {
                        "auto-nahida": {
                            "lora": "Adult_Nahida.safetensors",
                            "character": "Auto Nahida",
                            "aliases": ["auto_nahida"],
                            "trigger_words": ["aunahida"],
                            "source": "comfyui_metadata",
                        },
                        "auto-aris": {
                            "lora": "Aris_Illustrious.safetensors",
                            "character": "Aris",
                            "aliases": [],
                            "trigger_words": ["aris_bluearchive"],
                            "source": "comfyui_metadata",
                        },
                    },
                },
            )
            imported = root / "legacy" / "lora_profiles"
            imported.mkdir(parents=True)
            (imported / "lora_profiles_illustrious.json").write_text(
                json.dumps(
                    {
                        "\u7eb3\u897f\u59b2": {
                            "lora": "Adult_Nahida.safetensors",
                            "character": "\u7eb3\u897f\u59b2",
                            "aliases": ["\u5c0f\u8349\u795e"],
                            "trigger_prompt": "nahida_character",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            loaded = load_lora_profiles(root, env={}, auto_cache_key="server-key")

            self.assertEqual(loaded.count, 2)
            self.assertEqual(loaded.profiles["illustrious"]["\u7eb3\u897f\u59b2"]["character"], "\u7eb3\u897f\u59b2")
            self.assertNotIn("auto-nahida", loaded.profiles["illustrious"])
            self.assertIn("auto-aris", loaded.profiles["illustrious"])
            self.assertFalse(list((root / "cache").glob("*.tmp")))

    def test_importer_nested_profiles_aliases_and_prompts_are_loaded_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="winefox_nested_lora_") as temp:
            root = Path(temp)
            profile_dir = root / "legacy" / "lora_profiles"
            alias_dir = root / "imports" / "batch-001" / "lora_aliases"
            prompt_dir = root / "imports" / "batch-001" / "lora_character_prompts"
            ignored_dir = root / "legacy" / "unrelated" / "deep"
            for directory in (profile_dir, alias_dir, prompt_dir, ignored_dir):
                directory.mkdir(parents=True)

            (profile_dir / "lora_profiles.json").write_text(
                json.dumps(
                    {
                        "bailu": {
                            "lora": "characters/Bailu.safetensors",
                            "character": "\u767d\u9732",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (alias_dir / "lora_aliases.json").write_text(
                json.dumps({"\u5c0f\u767d\u9732": "characters/Bailu.safetensors"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (prompt_dir / "lora_character_prompts.json").write_text(
                json.dumps({"characters/Bailu.safetensors": "bailu_character, white hair"}),
                encoding="utf-8",
            )
            (ignored_dir / "lora_profiles.json").write_text(
                json.dumps({"rogue": {"lora": "Rogue.safetensors"}}),
                encoding="utf-8",
            )

            loaded = load_lora_profiles(root, env={})

            self.assertEqual(loaded.count, 1)
            self.assertEqual(loaded.sources.count("lora_profiles.json"), 1)
            profile = loaded.profiles["anima"]["bailu"]
            self.assertIn("\u5c0f\u767d\u9732", profile["aliases"])
            self.assertEqual(profile["trigger_prompt"], "bailu_character, white hair")
            self.assertNotIn("rogue", loaded.profiles["anima"])

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


class _MetadataConfig:
    def __init__(self, server_address: str) -> None:
        self.server_address = server_address

    def snapshot(self) -> dict:
        return {
            "comfyui": {"server_address": self.server_address, "ssl_verify": True},
            "auth": {"mode": "none"},
            "defaults": {"family": "illustrious"},
        }


class _MetadataExecutor:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def fetch_lora_metadata_batch(self, filenames, **_kwargs):
        return {
            filename: {
                "modelspec.title": "Adult Nahida",
                "ss_base_model_version": "sdxl_base_v1-0",
                "ss_tag_frequency": json.dumps(
                    {"dataset": {"aunahida": 120, "genshin impact": 120, "1girl": 120}}
                ),
            }
            for filename in filenames
        }


class DesktopLoraMetadataRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_updates_cache_and_live_profile_index(self) -> None:
        with tempfile.TemporaryDirectory(prefix="winefox_metadata_refresh_") as temp:
            root = Path(temp)
            server_address = "http://127.0.0.1:8188"
            engine = object.__new__(DesktopEngine)
            engine.data_root = root
            engine.config = _MetadataConfig(server_address)
            engine._lora_metadata_cache_key = lora_metadata_cache_key(server_address)
            engine._lora_metadata_attempted = set()
            engine._reload_lora_profiles()

            with patch("desktop_runtime.engine.ComfyUIExecutor", _MetadataExecutor):
                await engine._refresh_lora_metadata_profiles(
                    ["characters/Adult_Nahida.safetensors"]
                )

            cache_path = root / "cache" / "lora_profiles_metadata.json"
            self.assertTrue(cache_path.is_file())
            self.assertIn("adultnahida", engine._lora_profile_index)
            profile = engine._lora_profile_index["adultnahida"][0]
            self.assertEqual(profile.trigger_words, ["aunahida"])
            self.assertIn("lora_profiles_metadata.json", engine.loaded_lora_profiles.sources)


if __name__ == "__main__":
    unittest.main()
