# -*- coding: utf-8 -*-
"""LoRA 身份/别名提取引擎测试：清洗 / 提取 / 展开 / 合并。

核心断言：clean_alias 覆盖引号/括号/版本后缀/空白清洗规则；
extract_identity_candidates 能提取主角色并给出 0~1 置信度；
expand_aliases 去重并按 COMMON_CHARACTER_ALIASES 扩展、过滤非平凡触发词；
upsert_merge 遵循身份打分更高者胜出，且纯 dict 操作不落盘。

运行：``python -m unittest tests_desktop.test_lora_identity -v``
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winefox_core.lora_identity import (  # noqa: E402
    IdentityCandidates,
    clean_alias,
    expand_aliases,
    extract_identity_candidates,
    upsert_merge,
)


class CleanAliasTest(unittest.TestCase):
    def test_clean_removes_quotes_and_whitespace(self):
        self.assertEqual(clean_alias('  "Miku"  '), "Miku")
        self.assertEqual(clean_alias("  'Yae Miko'  "), "Yae Miko")

    def test_clean_removes_source_bracket(self):
        self.assertEqual(clean_alias("Yae Miko (Genshin Impact)"), "Yae Miko")

    def test_clean_removes_version_suffix(self):
        self.assertEqual(clean_alias("Yae Miko v2.0"), "Yae Miko")
        self.assertEqual(clean_alias("Yae Miko version 2.0"), "Yae Miko")

    def test_clean_empty_and_none(self):
        self.assertEqual(clean_alias(""), "")
        self.assertEqual(clean_alias("   "), "")
        self.assertEqual(clean_alias(None), "")


class ExtractIdentityTest(unittest.TestCase):
    def test_extract_main_character_and_confidence(self):
        profile = {
            "name": "Yae Miko [Genshin Impact] Illustrious XL",
            "version_name": "v1.0",
            "trained_words": ["yae miko", "genshin impact"],
            "tags": ["character"],
        }
        result = extract_identity_candidates(profile)
        self.assertIsInstance(result, IdentityCandidates)
        self.assertEqual(result.character, "Yae Miko")
        self.assertIn("Yae Miko", result.aliases)
        self.assertGreater(result.confidence, 0.8)
        self.assertTrue(any("model_title" in reason for reason in result.reasons))

    def test_extract_confidence_scales_with_evidence(self):
        strong = extract_identity_candidates({
            "name": "Yae Miko [Genshin Impact] Illustrious XL",
            "version_name": "v1.0",
            "trained_words": ["yae miko"],
        })
        weak = extract_identity_candidates({"name": "", "version_name": "", "trained_words": ["air"]})
        self.assertGreater(strong.confidence, weak.confidence)
        self.assertEqual(weak.confidence, 0.0)
        self.assertEqual(weak.character, "")

    def test_extract_empty_profile(self):
        result = extract_identity_candidates({})
        self.assertEqual(result.character, "")
        self.assertEqual(result.aliases, [])
        self.assertEqual(result.confidence, 0.0)
        self.assertEqual(result.reasons, [])


class ExpandAliasesTest(unittest.TestCase):
    def test_expand_dedupes_case_insensitive(self):
        self.assertEqual(expand_aliases(["Miku", "miku", " Miku "], "", []), ["Miku"])

    def test_expand_uses_character_alias_map(self):
        result = expand_aliases(["雷电将军"], "", [])
        self.assertIn("Raiden Shogun", result)
        self.assertIn("Ei", result)
        self.assertIn("影", result)
        self.assertEqual(len(result), 4)

    def test_expand_merges_profile_and_trained_words(self):
        result = expand_aliases(
            ["Miku"],
            "Hatsune Miku [VOCALOID]",
            ["miku", "masterpiece", "best quality"],
        )
        self.assertIn("Miku", result)
        self.assertIn("Hatsune Miku", result)
        self.assertNotIn("masterpiece", result)
        self.assertNotIn("best quality", result)


class UpsertMergeTest(unittest.TestCase):
    def test_incoming_high_score_overrides_base(self):
        base = {
            "lora": "yae_miko.safetensors",
            "character": "Miko",
            "aliases": ["Miko"],
            "identity_evidence": [{"source": "trigger_word", "value": "miko", "score": 62}],
            "confidence": 0.62,
        }
        incoming = {
            "name": "Yae Miko [Genshin Impact] Illustrious XL",
            "version_name": "v1.0",
            "trained_words": ["yae miko"],
            "lora": "yae_miko.safetensors",
        }
        merged = upsert_merge(base, incoming)
        self.assertEqual(merged["character"], "Yae Miko")
        self.assertGreater(merged["confidence"], base["confidence"])
        self.assertEqual(merged["lora"], "yae_miko.safetensors")

    def test_incoming_low_score_keeps_base(self):
        base = {
            "lora": "raiden.safetensors",
            "character": "Raiden Shogun",
            "aliases": ["Raiden Shogun"],
            "identity_evidence": [{"source": "model_title", "value": "Raiden Shogun", "score": 85}],
            "confidence": 0.85,
        }
        incoming = {"name": "", "version_name": "", "trained_words": ["raiden"]}
        merged = upsert_merge(base, incoming)
        self.assertEqual(merged["character"], "Raiden Shogun")
        self.assertEqual(merged["confidence"], base["confidence"])
        self.assertEqual(merged["lora"], "raiden.safetensors")

    def test_incoming_brings_own_identity_when_base_empty(self):
        incoming = {
            "name": "Raiden Shogun [Genshin Impact]",
            "version_name": "v1.0",
            "trained_words": ["raiden"],
        }
        merged = upsert_merge({}, incoming)
        self.assertEqual(merged["character"], "Raiden Shogun")
        self.assertGreater(merged["confidence"], 0.0)
        self.assertIn("Raiden Shogun", merged["aliases"])

    def test_merge_pure_dict_no_file_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            os.chdir(tmp)
            try:
                upsert_merge({}, {"name": "Miku [VOCALOID]", "trained_words": ["miku"]})
                extract_identity_candidates({"name": "Miku [VOCALOID]"})
                expand_aliases(["Miku"], "Miku [VOCALOID]", [])
                self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), [])
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
