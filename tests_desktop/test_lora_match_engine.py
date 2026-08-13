# -*- coding: utf-8 -*-
"""LoraService 匹配引擎移植测试：评分阈值 / 多角色连接符 / 多角色意图 / 跨族建议 / 本地同步。

覆盖原插件移植的四块算法：
1) _score_lora_profile_match（exact>=58 / fuzzy>=78 阈值行为）
2) _multi_lora_matches_have_connector（连接词证据的有/无）
3) has_multi_character_intent（显式多角色意图判定）
4) suggest_cross_family_switch（跨族临时建议返回结构，不持久修改）
5) sync_local_files / _find_local_lora_file（本地文件存在 / 缺失 / 目录不可用）

运行：``python -m unittest tests_desktop.test_lora_match_engine -v``
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winefox_core.lora_service import (  # noqa: E402
    LoraService,
    MODEL_FAMILY_ANIMA,
    MODEL_FAMILY_ILLUSTRIOUS,
    has_multi_character_intent,
)


def _profiles() -> dict:
    return {
        MODEL_FAMILY_ILLUSTRIOUS: {
            "白露": {
                "character": "白露",
                "lora": "Bailu.safetensors",
                "official_aliases": ["白露", "Bailu", "bailu", "露露"],
                "role_terms": ["医生", "医师"],
                "trigger_words": ["bailu, 白露, doctor, 医生"],
                "strength_model": 0.8,
                "strength_clip": 0.7,
            },
            "镜流": {
                "character": "镜流",
                "lora": "Jingliu.safetensors",
                "official_aliases": ["镜流", "Jingliu", "jingliu"],
                "trigger_words": ["jingliu, 镜流, sword, 剑"],
            },
        },
        MODEL_FAMILY_ANIMA: {
            "九条裟罗": {
                "character": "九条裟罗",
                "lora": "KujouSara.safetensors",
                "official_aliases": ["九条裟罗", "九条裟羅", "Kujou Sara", "kujou sara"],
                "trigger_words": ["kujou sara, 九条裟罗"],
            },
        },
    }


class MatchScoringTests(unittest.TestCase):
    def setUp(self):
        self.service = LoraService(_profiles())

    def test_exact_match_meets_exact_threshold(self):
        result = self.service.match("画一个白露", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertEqual(len(result.matches), 1)
        match = result.matches[0]
        self.assertEqual(match.match_kind, "exact")
        self.assertGreaterEqual(match.score, 58.0)
        self.assertEqual(match.matched_term, "白露")
        self.assertEqual(match.lora_name, "Bailu.safetensors")

    def test_fuzzy_match_meets_fuzzy_threshold(self):
        # 近似拼写：九条裟罗 vs 九条裟萝 → fuzzy，score >= 78
        result = self.service.match("九条裟萝", family=MODEL_FAMILY_ANIMA)
        self.assertEqual(len(result.matches), 1)
        match = result.matches[0]
        self.assertEqual(match.match_kind, "fuzzy")
        self.assertGreaterEqual(match.score, 78.0)

    def test_fuzzy_below_threshold_is_rejected(self):
        # Bailun 与 Bailu 相似度不足阈值 → 无命中
        result = self.service.match("Bailun 城市", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertEqual(len(result.matches), 0)
        ratio, fragment = self.service._profile_best_fuzzy_prompt_fragment(
            "Bailun 城市", "bailu", "Bailu"
        )
        self.assertEqual(ratio, 0.0)
        self.assertEqual(fragment, "")

    def test_generic_and_bad_terms_never_match(self):
        for text in ("画一个女孩", "画一个 master-piece", "solo"):
            result = self.service.match(text, family=MODEL_FAMILY_ILLUSTRIOUS)
            self.assertEqual(result.matches, [], text)


class MultiRoleConnectorTests(unittest.TestCase):
    def setUp(self):
        self.service = LoraService(_profiles())

    def test_connector_between_hits_is_found(self):
        result = self.service.match("白露和镜流", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertEqual(len(result.matches), 2)
        self.assertTrue(result.connector_found)
        self.assertTrue(result.multi)

    def test_connector_between_missing_is_not_found(self):
        result = self.service.match("白露 镜流", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertGreaterEqual(len(result.matches), 1)
        self.assertFalse(result.connector_found)
        self.assertFalse(result.multi)

    def test_connector_requires_two_distinct_spans(self):
        # 单个角色即使文本里出现连接词，也不判定为多角色
        result = self.service.match("画个白露和背景", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertEqual(len(result.matches), 1)
        self.assertFalse(result.multi)


class MultiCharacterIntentTests(unittest.TestCase):
    def setUp(self):
        self.service = LoraService(_profiles())

    def test_explicit_pair_intent_true(self):
        self.assertTrue(has_multi_character_intent("白露和镜流"))
        self.assertTrue(has_multi_character_intent("白露 and Jingliu"))
        self.assertTrue(has_multi_character_intent("两个角色:白露和镜流"))

    def test_single_subject_intent_false(self):
        self.assertFalse(has_multi_character_intent("白露 镜流"))
        self.assertFalse(has_multi_character_intent("画一个白露"))
        self.assertFalse(has_multi_character_intent("一个女孩独自站着"))

    def test_service_method_delegates_to_intent(self):
        self.assertTrue(self.service.has_multi_character_intent("白露和镜流"))
        self.assertFalse(self.service.has_multi_character_intent("白露 镜流"))


class CrossFamilySuggestionTests(unittest.TestCase):
    def setUp(self):
        self.service = LoraService(_profiles())

    def test_suggestion_return_structure(self):
        info = self.service.suggest_cross_family_switch(
            "画一个九条裟罗", current_family=MODEL_FAMILY_ILLUSTRIOUS
        )
        self.assertIsNotNone(info)
        self.assertEqual(info["from_family"], MODEL_FAMILY_ILLUSTRIOUS)
        self.assertEqual(info["family"], MODEL_FAMILY_ANIMA)
        self.assertEqual(info["key"], "九条裟罗")
        self.assertEqual(info["character"], "九条裟罗")
        self.assertIn("lora", info)
        self.assertIn("selection", info)
        self.assertEqual(info["restore_family"], MODEL_FAMILY_ILLUSTRIOUS)
        selection = info["selection"]
        self.assertTrue(selection.suggestion_only)
        self.assertEqual(selection.family, MODEL_FAMILY_ANIMA)
        self.assertIn("临时建议", selection.notice)

    def test_suggestion_none_when_current_family_hits(self):
        info = self.service.suggest_cross_family_switch(
            "画一个白露", current_family=MODEL_FAMILY_ILLUSTRIOUS
        )
        self.assertIsNone(info)

    def test_suggestion_does_not_mutate_family_state(self):
        before = dict(self.service.profiles_for(MODEL_FAMILY_ILLUSTRIOUS))
        self.service.suggest_cross_family_switch(
            "画一个九条裟罗", current_family=MODEL_FAMILY_ILLUSTRIOUS
        )
        after = dict(self.service.profiles_for(MODEL_FAMILY_ILLUSTRIOUS))
        self.assertEqual(before, after)


class LocalFileSyncTests(unittest.TestCase):
    def setUp(self):
        self.service = LoraService(_profiles())

    def test_sync_present_and_missing(self):
        with tempfile.TemporaryDirectory(prefix="winefox_sync_") as temp:
            root = Path(temp)
            (root / "Bailu.safetensors").write_bytes(b"x")
            items = self.service.sync_local_files(str(root), family=MODEL_FAMILY_ILLUSTRIOUS)
            self.assertEqual(len(items), 2)
            by_name = {item["lora_name"]: item for item in items}
            self.assertEqual(by_name["Bailu.safetensors"]["status"], "present")
            self.assertTrue(by_name["Bailu.safetensors"]["exists"])
            self.assertEqual(by_name["Jingliu.safetensors"]["status"], "missing")
            self.assertFalse(by_name["Jingliu.safetensors"]["exists"])

    def test_sync_fuzzy_nested_match(self):
        with tempfile.TemporaryDirectory(prefix="winefox_sync_") as temp:
            root = Path(temp)
            (root / "nested").mkdir()
            (root / "nested" / "Jingliu_v2.safetensors").write_bytes(b"x")
            items = self.service.sync_local_files(str(root), family=MODEL_FAMILY_ILLUSTRIOUS)
            jingliu = next(item for item in items if item["lora_name"] == "Jingliu.safetensors")
            self.assertEqual(jingliu["status"], "present")
            self.assertIn("Jingliu_v2.safetensors", jingliu["matched"])

    def test_sync_unavailable_dir_returns_empty(self):
        self.assertEqual(self.service.sync_local_files(""), [])
        self.assertEqual(self.service.sync_local_files("Z:/not/here/xyz_zzz"), [])
        with tempfile.TemporaryDirectory(prefix="winefox_sync_") as temp:
            file_path = Path(temp) / "a.txt"
            file_path.write_text("hi")
            self.assertEqual(self.service.sync_local_files(str(file_path)), [])


class MultiLoraAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.service = LoraService(_profiles())

    def test_resolve_multi_loras_applies_safe_chain_caps(self):
        result = self.service.match("白露和镜流", family=MODEL_FAMILY_ILLUSTRIOUS)
        selections = self.service.resolve_multi_loras(
            result.matches, family=MODEL_FAMILY_ILLUSTRIOUS, mode="safe_chain"
        )
        self.assertEqual(len(selections), 2)
        for selection in selections:
            self.assertLessEqual(selection.strength_model, 0.65)
            self.assertLessEqual(selection.strength_clip, 0.72)

    def test_resolve_multi_loras_staged_keeps_below_high_cap(self):
        result = self.service.match("白露和镜流", family=MODEL_FAMILY_ILLUSTRIOUS)
        selections = self.service.resolve_multi_loras(
            result.matches, family=MODEL_FAMILY_ILLUSTRIOUS, mode="staged_multi_lora"
        )
        self.assertEqual(selections[0].strength_model, 0.8)
        self.assertEqual(selections[1].strength_model, 0.75)


class MultiCharacterPackRoutingTests(unittest.TestCase):
    def test_route_keeps_only_matched_trigger(self):
        service = LoraService({
            MODEL_FAMILY_ILLUSTRIOUS: {
                "双人包": {
                    "character": "character_a",
                    "lora": "DoublePack.safetensors",
                    "official_aliases": ["character_a"],
                    "trigger_words": ["character_a, character_b"],
                    "version_name": "character collection",
                },
            }
        })
        result = service.match("character_a", family=MODEL_FAMILY_ILLUSTRIOUS)
        self.assertEqual(len(result.matches), 1)
        selections = service.resolve_multi_loras(
            result.matches, family=MODEL_FAMILY_ILLUSTRIOUS
        )
        self.assertEqual(len(selections[0].trigger_words), 1)
        self.assertEqual(selections[0].trigger_words[0], "character_a")


if __name__ == "__main__":
    unittest.main()
