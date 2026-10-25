from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from desktop_runtime.lora_library import LoraLibraryService
from desktop_runtime.lora_profiles import merge_prompt_tags, profile_from_comfy_metadata, resolve_prompt_loras
from desktop_runtime.workflow_builder import prepare_workflow
from desktop_runtime.lora_import import provider_lora_profile, local_lora_profile
from desktop_runtime.lora_workflow import active_nodes
from winefox_core.lora_binding import LoraNameIndex, lora_path_key, lora_is_excluded
from winefox_core.lora_service import LoraProfile, LoraService
from winefox_core.storage import Database, LoraAssetStore, LoraProfileStore


def simple_workflow():
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "base.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": "old"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": "bad"}, "_meta": {"title": "negative"}},
        "4": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "seed": 1}},
        "5": {"class_type": "VAEDecode", "inputs": {"samples": ["4", 0], "vae": ["1", 2]}},
        "6": {"class_type": "SaveImage", "inputs": {"images": ["5", 0]}},
    }


def ancestors(workflow, node_id):
    seen = set()
    def visit(current):
        if current in seen or current not in workflow:
            return
        seen.add(current)
        for value in workflow[current].get("inputs", {}).values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
                visit(str(value[0]))
    visit(node_id)
    return seen


class WorkflowBindingTests(unittest.TestCase):
    def test_every_bundled_image_template_keeps_all_selected_loras_reachable(self):
        root = Path(__file__).resolve().parents[1] / "workflow"
        for path in root.glob("*.json"):
            if path.name.startswith("wan_"):
                continue
            source = json.loads(path.read_text(encoding="utf-8-sig"))
            for count in range(4):
                with self.subTest(template=path.name, count=count):
                    selections = [{"name": f"selected-{index}.safetensors"} for index in range(count)]
                    workflow, _ = prepare_workflow(source, {"prompt": "portrait", "loras": selections})
                    active = active_nodes(workflow)
                    applied = {workflow[key].get("inputs", {}).get("lora_name") for key in active}
                    self.assertTrue({entry["name"] for entry in selections}.issubset(applied))
                    for key, node in workflow.items():
                        for value in node.get("inputs", {}).values():
                            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                                self.assertIn(value[0], workflow, (key, value))

    def test_standard_http_workflow_does_not_require_websocket_example_node(self):
        source = simple_workflow()
        source["ws"] = {"class_type": "SaveImageWebsocket", "inputs": {"images": ["5", 0]}}
        workflow, _ = prepare_workflow(source, {})
        self.assertNotIn("ws", workflow)
        self.assertIn("6", workflow)

    def test_graph_connections_determine_positive_and_negative_not_node_numbers(self):
        source = simple_workflow()
        source["12"] = source.pop("2")
        source["12"]["_meta"] = {"title": "unlabeled"}
        source["3"]["_meta"] = {"title": "unlabeled"}
        source["4"]["inputs"]["positive"] = ["12", 0]
        workflow, _ = prepare_workflow(source, {"prompt": "actual positive", "negative_prompt": "actual negative"})
        self.assertEqual(workflow["12"]["inputs"]["text"], "actual positive")
        self.assertEqual(workflow["3"]["inputs"]["text"], "actual negative")

    def test_regional_capacity_overflow_is_reported_not_silently_dropped(self):
        source = simple_workflow()
        source["7"] = {"class_type": "CreateHookLora", "inputs": {"lora_name": "old", "strength_model": 0, "strength_clip": 0}}
        source["8"] = {"class_type": "SetClipHooks", "inputs": {"clip": ["1", 1], "hooks": ["7", 0]}}
        source["2"]["inputs"]["clip"] = ["8", 0]
        with self.assertRaisesRegex(ValueError, "角色槽"):
            prepare_workflow(source, {"loras": [{"name": "a"}, {"name": "b"}]})

    def test_native_chain_is_inserted_when_template_has_no_lora_slots(self):
        source = simple_workflow()
        original = copy.deepcopy(source)
        workflow, _ = prepare_workflow(source, {"prompt": "portrait", "loras": [
            {"name": "characters/a.safetensors", "strength_model": .8, "strength_clip": 0},
            {"name": "styles/b.safetensors", "strength_model": -.2, "strength_clip": .3},
        ]})
        active = ancestors(workflow, "6")
        loaders = [workflow[key] for key in active if workflow[key]["class_type"] == "LoraLoader"]
        self.assertEqual({node["inputs"]["lora_name"] for node in loaders}, {"characters/a.safetensors", "styles/b.safetensors"})
        self.assertEqual(source, original)
        model_path = ancestors(workflow, str(workflow["4"]["inputs"]["model"][0]))
        clip_path = ancestors(workflow, str(workflow["2"]["inputs"]["clip"][0]))
        self.assertEqual(len([key for key in model_path & clip_path if workflow[key]["class_type"] == "LoraLoader"]), 2)

    def test_more_selections_than_slots_are_all_connected(self):
        source = simple_workflow()
        source["7"] = {"class_type": "LoraLoader", "inputs": {"model": ["1", 0], "clip": ["1", 1], "lora_name": "placeholder", "strength_model": 0, "strength_clip": 0}}
        source["4"]["inputs"]["model"] = ["7", 0]
        source["2"]["inputs"]["clip"] = ["7", 1]
        workflow, _ = prepare_workflow(source, {"loras": [{"name": f"{i}.safetensors"} for i in range(3)]})
        self.assertEqual({workflow[k]["inputs"]["lora_name"] for k in ancestors(workflow, "6") if workflow[k]["class_type"] == "LoraLoader"}, {"0.safetensors", "1.safetensors", "2.safetensors"})

    def test_fixed_template_loras_are_preserved(self):
        source = simple_workflow()
        source["7"] = {"class_type": "LoraLoader", "inputs": {"model": ["1", 0], "clip": ["1", 1], "lora_name": "quality.safetensors", "strength_model": .25, "strength_clip": .25}, "_meta": {"title": "Fixed quality style"}}
        source["4"]["inputs"]["model"] = ["7", 0]
        source["2"]["inputs"]["clip"] = ["7", 1]
        workflow, _ = prepare_workflow(source, {"loras": []})
        self.assertEqual(workflow["7"]["inputs"]["lora_name"], "quality.safetensors")
        self.assertEqual(workflow["7"]["inputs"]["strength_model"], .25)

    def test_zero_clip_and_negative_model_strength_are_preserved(self):
        source = simple_workflow()
        source["7"] = {"class_type": "LoraLoader", "inputs": {"model": ["1", 0], "clip": ["1", 1], "lora_name": "old", "strength_model": 0, "strength_clip": 0}}
        source["4"]["inputs"]["model"] = ["7", 0]
        workflow, _ = prepare_workflow(source, {"loras": [{"name": "a", "strength_model": -.5, "strength_clip": 0}]})
        self.assertEqual(workflow["7"]["inputs"]["strength_model"], -.5)
        self.assertEqual(workflow["7"]["inputs"]["strength_clip"], 0)

    def test_unused_regional_hooks_leave_no_dangling_references(self):
        source = simple_workflow()
        source["7"] = {"class_type": "CreateHookLora", "inputs": {"lora_name": "placeholder", "strength_model": 0, "strength_clip": 0}}
        source["8"] = {"class_type": "SetClipHooks", "inputs": {"clip": ["1", 1], "hooks": ["7", 0], "apply_to_conds": True, "schedule_clip": False}}
        source["2"]["inputs"]["clip"] = ["8", 0]
        workflow, _ = prepare_workflow(source, {"loras": []})
        for key, node in workflow.items():
            for value in node.get("inputs", {}).values():
                if isinstance(value, list) and len(value) == 2:
                    self.assertIn(str(value[0]), workflow, (key, value))


class AliasAndTriggerTests(unittest.TestCase):
    def test_ascii_substring_is_not_a_silent_exact_character_match(self):
        service = LoraService({"illustrious": {"Alexander": {"character": "Alexander", "lora": "a.safetensors"}}})
        self.assertEqual(service.match("Alexanderplatz street", include_fuzzy=False).matches, [])

    def test_fullwidth_latin_name_matches_its_normalized_alias(self):
        service = LoraService({"illustrious": {"Alpha": {"character": "Alpha", "lora": "a.safetensors"}}})
        self.assertEqual(len(service.match("Ａｌｐｈａ portrait", include_fuzzy=False).matches), 1)

    def test_removed_alias_does_not_match_via_an_old_internal_profile_key(self):
        service = LoraService({"illustrious": {"obsolete_alias": {"character": "Alpha", "aliases": [], "aliases_authoritative": True, "lora": "a.safetensors"}}})
        self.assertEqual(service.match("obsolete_alias", include_fuzzy=False).matches, [])
        self.assertEqual(len(service.match("Alpha", include_fuzzy=False).matches), 1)

    def test_provider_import_keeps_declared_words_but_not_discovery_query_as_alias(self):
        profile = provider_lora_profile({"model_name": "Hatsune Miku", "trained_words": ["unrelated_unique_token"], "search_query": "红头发少女", "aliases": ["初音未来"]}, "miku.safetensors", "illustrious", "初音")
        self.assertEqual(profile["trigger_words"], ["unrelated_unique_token"])
        self.assertIn("初音", profile["aliases"])
        self.assertNotIn("红头发少女", profile["aliases"])
        self.assertNotIn("unrelated_unique_token", profile["aliases"])

    def test_training_frequencies_are_suggestions_not_automatic_triggers(self):
        parsed = profile_from_comfy_metadata("Nahida.safetensors", {"ss_tag_frequency": json.dumps({"data": {"aunahida": 100, "1girl": 100}})})
        self.assertEqual(parsed[1]["trigger_words"], [])
        self.assertEqual(parsed[1]["suggested_trigger_words"], ["aunahida"])

    def test_declared_sdxl_architecture_wins_over_anima_filename_and_default(self):
        parsed = profile_from_comfy_metadata("anima_style.safetensors", {"ss_base_model_version": "sdxl_base_v1-0", "trainedWords": ["style_token"]}, default_family="anima")
        self.assertEqual(parsed[0], "illustrious")

    def test_disabling_one_subfolder_does_not_disable_another(self):
        self.assertTrue(lora_is_excluded("a/hero.safetensors", {"a/hero.safetensors"}))
        self.assertFalse(lora_is_excluded("b/hero.safetensors", {"a/hero.safetensors"}))
        self.assertTrue(LoraNameIndex(["a/hero.safetensors", "b/hero.safetensors"]).ambiguous("hero.safetensors"))

    def test_explicit_empty_triggers_do_not_fall_back_to_legacy_fields(self):
        profile = LoraProfile.from_dict("fox", {"lora": "fox.safetensors", "trigger_words": [], "activation_tags": ["old_trigger"]})
        self.assertEqual(profile.trigger_words, [])

    def test_weighted_existing_trigger_is_not_injected_twice(self):
        self.assertEqual(merge_prompt_tags("portrait, (fox_token:1.2)", ["fox_token"]), "portrait, (fox_token:1.2)")

    def test_explicit_metadata_trigger_does_not_require_a_name_substring(self):
        parsed = profile_from_comfy_metadata("arbitrary-v2.safetensors", {"modelspec.title": "角色甲", "trainedWords": ["unique_activation_token"]})
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[1]["trigger_words"], ["unique_activation_token"])

    def test_subfolder_identity_is_resolved_exactly(self):
        service = LoraService({"illustrious": {"alpha": {"character": "Alpha", "lora": "a/hero.safetensors", "trigger_words": ["alpha_token"]}}})
        result = resolve_prompt_loras(service, "Alpha", "illustrious", ["a/hero.safetensors", "b/hero.safetensors"])
        self.assertEqual(result.loras[0]["name"], "a/hero.safetensors")

    def test_ambiguous_legacy_basename_does_not_choose_an_arbitrary_file(self):
        service = LoraService({"illustrious": {"alpha": {"character": "Alpha", "lora": "hero.safetensors"}}})
        result = resolve_prompt_loras(service, "Alpha", "illustrious", ["a/hero.safetensors", "b/hero.safetensors"])
        self.assertEqual(result.loras, ())


class LibraryImportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="winefox_lora_reliability_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(":memory:"); self.db.initialize(); self.addCleanup(self.db.close)
        self.assets = LoraAssetStore(self.db); self.profiles = LoraProfileStore(self.db)
        self.service = LoraLibraryService(db=self.db, data_root=self.root, asset_store=self.assets, profile_store=self.profiles)

    async def test_nested_weights_and_rgthree_metadata_are_imported(self):
        directory = self.root / "models/loras/characters"; directory.mkdir(parents=True)
        (directory / "fox.safetensors").write_bytes(b"test fixture")
        (directory / "fox.safetensors.rgthree-info.json").write_text(json.dumps({"name": "Fox", "trainedWords": [{"word": "fox_token", "user": True}], "aliases": ["小狐狸"]}), encoding="utf-8")
        result = await self.service.scan_local_dir()
        self.assertEqual(result["added"], 1)
        item = self.service.list()[0]
        self.assertEqual(item["comfy_key"], "characters/fox.safetensors")
        self.assertIn("小狐狸", item["aliases"])
        self.assertEqual(item["triggers"], ["fox_token"])

    async def test_clearing_aliases_and_triggers_is_authoritative(self):
        asset = self.assets.register("illustrious", "fox.safetensors", source="local")
        self.profiles.save("illustrious", "Fox", {"lora": "fox.safetensors", "character": "Fox", "aliases": ["old"], "official_aliases": ["old"], "trigger_words": ["old_token"], "activation_tags": ["old_token"]})
        self.service.update(asset.id, aliases=[], triggers=[])
        item = self.service.get(asset.id)
        self.assertEqual(item["aliases"], [])
        self.assertEqual(item["triggers"], [])

    async def test_same_basename_in_two_subfolders_keeps_distinct_assets_and_aliases(self):
        for folder, alias, trigger in (("a", "角色甲", "token_a"), ("b", "角色乙", "token_b")):
            directory = self.root / "models/loras" / folder; directory.mkdir(parents=True)
            (directory / "hero.safetensors").write_bytes(b"fixture")
            (directory / "hero.civitai.info").write_text(json.dumps({"name": alias, "trainedWords": [trigger]}), encoding="utf-8")
        self.assertEqual((await self.service.scan_local_dir())["added"], 2)
        items = {item["filename"]: item for item in self.service.list()}
        self.assertEqual(items["a/hero.safetensors"]["triggers"], ["token_a"])
        self.assertEqual(items["b/hero.safetensors"]["triggers"], ["token_b"])
        self.assertEqual((await self.service.scan_local_dir())["added"], 0)

    async def test_adopting_suggested_triggers_is_explicit_and_persisted(self):
        asset = self.assets.register("illustrious", "fox.safetensors", source="local")
        self.profiles.save("illustrious", "Fox", {"lora": "fox.safetensors", "character": "Fox", "trigger_words": [], "suggested_trigger_words": ["fox_token"]})
        self.assertEqual(self.service.get(asset.id)["trigger_candidates"], ["fox_token"])
        self.assertEqual(self.service.get(asset.id)["triggers"], [])
        self.service.update(asset.id, triggers=["fox_token"])
        self.assertEqual(self.service.get(asset.id)["triggers"], ["fox_token"])
        self.assertEqual(self.profiles.get("illustrious", "Fox").profile["trigger_source"], "user")

    async def test_legacy_flat_asset_is_not_duplicated_for_a_unique_nested_file(self):
        self.assets.register("illustrious", "hero.safetensors", source="local")
        directory = self.root / "models/loras/characters"; directory.mkdir(parents=True)
        (directory / "hero.safetensors").write_bytes(b"fixture")
        self.assertEqual((await self.service.scan_local_dir())["added"], 0)
        self.assertEqual(len(self.service.list()), 1)
