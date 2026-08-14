import asyncio
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _tag_count(text: str) -> int:
    return len([tag for tag in str(text or "").split(",") if tag.strip()])


def _feature_text() -> str:
    feature_dir = ROOT / "features"
    if not feature_dir.exists():
        return ""
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(feature_dir.glob("*.py"))
    )


def _plugin_text() -> str:
    return (ROOT / "main.py").read_text(encoding="utf-8") + "\n" + _feature_text()


def _load_gpt_magic_for_store_tests():
    if "astrbot.api" not in sys.modules:
        astrbot_mod = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
        api_mod = types.ModuleType("astrbot.api")

        class DummyLogger:
            def debug(self, *args, **kwargs):
                pass

            def info(self, *args, **kwargs):
                pass

            def warning(self, *args, **kwargs):
                pass

            def error(self, *args, **kwargs):
                pass

        api_mod.logger = DummyLogger()
        astrbot_mod.api = api_mod
        sys.modules["astrbot.api"] = api_mod
    import gpt_magic

    return gpt_magic


class StabilityStaticTests(unittest.TestCase):
    def test_version_is_consistent(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        version = re.search(r"(?m)^version:\s*(.+?)\s*$", metadata).group(1).strip()
        literal = re.search(r'PLUGIN_VERSION\s*=\s*"([^"]+)"', main_text)
        if literal:
            self.assertEqual(version, literal.group(1))
        else:
            self.assertIn("def _metadata_version(", main_text)
            self.assertIn("PLUGIN_VERSION = _metadata_version(", main_text)
        self.assertIn(f"## {version}", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))

    def test_self_update_version_reader_prefers_metadata_and_blocks_downgrade(self):
        from features.self_update import SelfUpdateFeature

        feature = SelfUpdateFeature(object())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "metadata.yaml").write_text("name: demo\nversion: 8.5.3\n", encoding="utf-8")
            (root / "main.py").write_text('PLUGIN_VERSION = "8.5.0"\n', encoding="utf-8")

            self.assertEqual(feature._read_plugin_version_from_dir(root), "8.5.3")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text('PLUGIN_VERSION = "8.4.9"\n', encoding="utf-8")

            self.assertEqual(feature._read_plugin_version_from_dir(root), "8.4.9")

        self.assertTrue(feature._self_update_is_downgrade("8.5.2", "8.5.0"))
        self.assertFalse(feature._self_update_is_downgrade("8.5.0", "8.5.2"))
        self.assertEqual(feature._compare_self_update_versions("8.5", "8.5.0"), 0)

    def test_zayu_notice_ids_exist(self):
        data = json.loads((ROOT / "zayu_messages_template.json").read_text(encoding="utf-8"))
        ids = {str(item.get("id") or "") for item in data.get("entries", []) if isinstance(item, dict)}
        self.assertIn("missing_prompt", ids)
        self.assertIn("cooldown_short", ids)
        self.assertIn("look_no_image", ids)
        self.assertIn("look_failed", ids)
        self.assertIn("comfyui_offline_short", ids)
        self.assertIn("generation_queue_ready", ids)
        self.assertIn("generation_queue_waiting", ids)

    def test_generation_seed_control_is_wired(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        comfy_text = (ROOT / "comfyui_api.py").read_text(encoding="utf-8")
        schema_text = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        required = [
            "def _extract_prompt_seed_override(",
            "async def _handle_seed_command(",
            "seed_override",
            "runtime_seed_override",
            "fixed_seed",
            "fixed_seed_enabled",
            "def _apply_generation_seed(",
            "seed_source",
            "seed_override is not None",
            "meta.get(\"seed\") is not None",
            "已恢复随机种子",
            "酒狐来点 seed=0 <提示词>",
            "\"seed\": {",
            "sub_conf.get(\"seed\", 0)",
            "generate_staged_pass(",
        ]
        haystack = main_text + comfy_text + schema_text
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, haystack)

    def test_sensitive_ascii_cut_terms_are_not_hard_blocked(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("SENSITIVE_AMBIGUOUS_ASCII_TERMS", main_text)
        self.assertIn("normalized_term in SENSITIVE_AMBIGUOUS_ASCII_TERMS", main_text)

        lexicon = json.loads((ROOT / "sensitive_words.json").read_text(encoding="utf-8"))
        skipped = {"cut", "cuts", "cutting"}
        word_terms = []
        phrase_terms = []
        for terms in lexicon.values():
            if not isinstance(terms, list):
                continue
            for term in terms:
                term = str(term or "").strip()
                if not term or any(ord(ch) >= 128 for ch in term) or term.lower() in skipped:
                    continue
                if " " in term:
                    phrase_terms.append(re.escape(term))
                else:
                    word_terms.append(re.escape(term))
        parts = []
        if word_terms:
            parts.append(r"(?<![A-Za-z0-9_])(?:" + "|".join(dict.fromkeys(word_terms)) + r")(?![A-Za-z0-9_])")
        if phrase_terms:
            parts.append("|".join(dict.fromkeys(phrase_terms)))
        pattern = re.compile("|".join(parts), re.I)

        for prompt in ("cute smile", "hair cut", "cut out dress", "cinematic cut", "self cutting"):
            with self.subTest(prompt=prompt):
                self.assertNotIn("cut", [m.group(0).lower() for m in pattern.finditer(prompt)])
        self.assertIn("slit wrists", [m.group(0).lower() for m in pattern.finditer("slit wrists")])
        self.assertIn("stabbed", [m.group(0).lower() for m in pattern.finditer("stabbed scene")])

    def test_stability_helpers_are_wired(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        plugin_text = main_text + "\n" + _feature_text()
        gpt_magic_text = (ROOT / "gpt_magic.py").read_text(encoding="utf-8")
        comfy_text = (ROOT / "comfyui_api.py").read_text(encoding="utf-8")
        required = [
            "def _notice(",
            "def _illustrious_checkpoint_blocking_notice(",
            "def _apply_perfect_realism_limb_foreground_guard(",
            "PERFECT_REALISM_LIMB_FOREGROUND_REMOVE_MARKERS",
            "perfect_realism_limb_foreground_guard",
            "ILLUSTRIOUS_EXTERNAL_HAND_NEGATIVE",
            "ILLUSTRIOUS_SELF_TOUCH_HAND_REMOVE_MARKERS",
            "ILLUSTRIOUS_FOOT_EXTERNAL_HAND_NEGATIVE",
            "ILLUSTRIOUS_PHONE_HAND_ANCHOR_POSITIVE",
            "def _apply_illustrious_external_hand_guard(",
            "illustrious_armpit_self_hand_guard",
            "illustrious_phone_hand_anchor_guard",
            "another person's hand",
            "hand entering from frame edge",
            "hands away from feet",
            "both hands holding smartphone near chest",
            "def _illustrious_high_risk_foot_prompt(",
            "def _apply_illustrious_foot_perspective_guard(",
            "ILLUSTRIOUS_BAREFOOT_FOOTWEAR_REMOVE_MARKERS",
            "illustrious_foot_perspective_guard",
            "illustrious_barefoot_conflict_removed",
            "incorrect toe count",
            "feet attached to ankles",
            "_json_file_cache",
            "_checkpoint_names_runtime_cache",
            "avatar_redraw_settings",
            "def _extract_onebot_at_user_ids(",
            "def _download_qq_avatar_image(",
            "avatar_redraw_prompt",
            "prompt_note_settings",
            "template_llm_enabled",
            "saved_prompts.json",
            "def _handle_prompt_note_command(",
            "def _handle_prompt_note_pending_title(",
            "def _normalize_prompt_note_template_with_llm(",
            "def _handle_prompt_notebook_command(",
            "draw_prompt",
            "酒狐看本本 [标题/触发词]",
            "generation_stats_avatars",
            "async def _handle_webui_pose_generate(",
            "async def _fetch_paintable_role_live_loras(",
            "available_lora_keys = self._lora_key_set(available_loras)",
            "_build_paintable_role_rows(family=family, available_loras=available_loras)",
            "实时未匹配 {int(unavailable or 0)} 个 LoRA，已保留显示",
            "跨设备部署时不再自动删除档案",
            "def _send_failure_is_forward_message_failure(",
            "def _send_failure_is_likely_late_media_timeout(",
            "def _send_failure_is_unknown_onebot_action(",
            "async def _send_forward_fallback_safely(",
            "def _should_suppress_background_forward_fallback(",
            "\"paint_background\", \"video_background\"",
            "已停止普通消息降级以避免重复发送",
            "_flatten_forward_components_for_plain_send(",
            "def _prepend_forward_intro_to_result(",
            "_prepend_forward_intro_to_result(event, imported, intro, name=\"LoRA 导入\")",
            "async def _send_onebot_forward_nodes(",
            "\"send_forward_msg\"",
            "\"message_type\": \"group\" if is_group else \"private\"",
            "合并转发降级普通消息返回 OneBot 超时",
            "def _generation_queue_average_seconds(",
            "def _build_generation_queue_notice(",
            "def _mark_generation_queue_entry_ready(",
            "\"status\": \"preparing\"",
            "\"ready_at\": 0.0",
            "preparing_count",
            "生成任务预处理完成，进入 ComfyUI 提交队列",
            "def _track_background_task(",
            "后台任务异常退出",
            "def _comfyui_bridge_fetch_circuit_block_message(",
            "bypass_circuit: bool = False",
            "external_status in {408, 429, 500, 502, 503, 504}",
            "\"http 429\"",
            "Bridge 代理出网暂不可用",
            "queue_entry_id=queue_entry_id",
            "generation_queue_waiting",
            "画好我直接发出来",
            "def _build_civitai_import_profile_payload(",
            "event.chain_result([Nodes(nodes)])",
            "_prepare_prompt_with_lora_profile(",
            "webui_pose_lora_matching",
            "def _webui_pose_camera_prompt(",
            "def _webui_pose_generation_preset(",
            "def _webui_pose_auto_control_options(",
            "def _webui_pose_single_subject_prompt(",
            "def _webui_pose_duplicate_negative_prompt(",
            "webui_pose_auto_control",
            "webui_pose_duplicate_extra_negative",
            "def _enrich_generation_stats_rank_view(",
            "_look_recent_session_image_cache",
            "def _onebot_image_segments_from_event(",
            "def _extract_direct_image_paths_from_event(",
            "def _resolve_onebot_image_segment_path(",
            "def _onebot_action_image_data_to_path(",
            "get_image",
            "get_file",
            "onebot_image_cache",
            "def _remember_look_recent_images_from_event(",
            "def _get_look_recent_session_images(",
            "def _resolve_look_command_image_paths(",
            "酒狐看看使用会话最近图",
            "酒狐排行 [今日|昨日|总榜]",
        ]
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, plugin_text)
        webui_required = [
            "def list_favorites(",
            "collector_count",
            "image_sha256",
            "/api/favorites/image/{item_id}",
            "data-tab=\"favorites\"",
            "data-tab=\"comfy\"",
            "function renderFavorites(",
            "function renderFavoriteRoles(",
            "favorite-grid",
            "role-button",
            "function setComfyPage(",
            "comfy-page-img2img",
            "data-tab=\"pose\"",
            "pose_presets.json",
            "function generatePosePrompt",
            "TransformControls",
            "/api/pose/presets",
            "RIG_JOINTS",
            "function rigSolveTwoBoneIK",
            "poseHumanLimits",
            "function applyPoseTemplate",
            "function mirrorPoseState",
            "/api/pose/generate",
            "function buildPoseControlImageDataUrl",
            "coco18BodyLines",
            "function poseAutoGenerationPreset",
            "function syncPoseAutoGenerateSettings",
            "poseManualControl",
            "poseAutoSummary",
            "single person",
            "profile_lora",
            "profile_character",
            "poseGenerateBtn",
            "function saveComfySettings(",
            "save_comfy_settings",
            "FavoriteGalleryWebUI",
            "FAVORITE_GALLERY_HTML_PATH",
            "def _load_favorite_gallery_html(",
            "start_gallery_webui",
            "WineFox",
            "Gallery 管理员初始密码已随机生成",
            "6666",
            "/gallery.html",
            "/draw.html",
            "/api/meta",
            "/api/draw",
            "/api/draw/{job_id}",
            "/api/draw/image/{job_id}",
            "/api/draw/history",
            "/api/draw/history/{item_id}/image",
            "/api/draw/history/{item_id}/download",
            "/api/draw/loras",
            "/api/gallery",
            "/api/gallery/{item_id}",
            "/api/roles",
            "/api/makers",
            "/api/rank",
            "/api/image/{item_id}",
            "def _api_manifest(",
            "def _draw_owner(",
            "queue_status",
            "def _run_draw_job(",
            "async def draw(",
            "async def draw_status(",
            "async def draw_history(",
            "async def draw_history_image(",
            "async def draw_history_download(",
            "async def draw_loras(",
            "async def draw_image(",
            "def gallery_item(",
            "def roles(",
            "def makers(",
            "def list_gallery_favorites(",
            "def generation_rank(",
            "收藏图库",
            "WINEFOX_DRAW_HTML_PATH",
            "def _load_winefox_draw_html(",
            "def web_draw_history(",
            "def add_web_draw_history(",
            "def list_web_draw_history(",
            "def get_web_draw_history_image_path(",
            "def _draw_safety_payload(",
            "draw_history_ttl_hours",
        ]
        for marker in webui_required:
            with self.subTest(marker=marker):
                self.assertIn(marker, gpt_magic_text)
        gallery_template = (ROOT / "templates" / "favorite_gallery.html").read_text(encoding="utf-8")
        gallery_template_required = [
            "__GALLERY_API_MANIFEST_JSON__",
            "__GALLERY_NAME_JSON__",
            "const GALLERY_API",
            "href=\"/draw.html\"",
            "draw-nav-item",
            "winefox_gallery_age_confirmed_v1",
            "请确认你已满 18 岁",
            "免责声明",
            "function loadGallery()",
            "function renderGallery()",
            "function loadRank()",
            "制作人",
            "提示词",
        ]
        for marker in gallery_template_required:
            with self.subTest(marker=marker):
                self.assertIn(marker, gallery_template)
        draw_template = (ROOT / "templates" / "winefox_draw.html").read_text(encoding="utf-8")
        draw_template_required = [
            "__GALLERY_API_MANIFEST_JSON__",
            "__GALLERY_NAME_JSON__",
            "const GALLERY_API",
            "id=\"prompt\"",
            "endpoint('draw')",
            "endpoint('draw_job')",
            "endpoint('draw_history')",
            "thinking-bars",
            "thinkingPhrases",
            "phraseDrift",
            "酒狐正在疯狂画图中",
            "sensitive-mask",
            "revealSensitive",
            "ageGate",
            "请确认你已满 18 岁",
            "免责声明",
            "drawCount",
            "previewOverlay",
            "openHistoryPreview",
            "endpoint('draw_loras')",
            "data.status === 'preparing'",
            "loraList",
            "resultImage",
            "downloadBtn",
        ]
        for marker in draw_template_required:
            with self.subTest(marker=marker):
                self.assertIn(marker, draw_template)
        self.assertNotIn("single person pose reference", gpt_magic_text)
        comfy_required = [
            "DEFAULT_POSE_CONTROLNET_NAME",
            "def _inject_pose_control_params",
            "ControlNetApplyAdvanced",
            "pose_controlnet_name",
            '"mode": "pose_control"',
            "def _register_webui_draw_queue_entry(",
            "async def _handle_webui_draw_generate(",
            "draw_generate",
            "webui_draw_elapsed_seconds",
            "self._generate_staged_multi_lora_image_file(",
            "source=\"webui_draw\", force=True",
            "def _apply_output_watermark(self, image_path: Path, source: str = \"comfyui\", force: bool = False)",
        ]
        for marker in comfy_required:
            with self.subTest(marker=marker):
                self.assertIn(marker, comfy_text + plugin_text)

    def test_default_illustrious_stabilizer_stays_minimal(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        match = re.search(
            r"def _apply_default_illustrious_prompt_stabilizers\(.*?\):(?P<body>.*?)\n\s+def _append_api_runtime_negative_prompt",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertNotIn("positive.extend(", body)
        self.assertNotIn("negative.extend(", body)
        after_multi = body.split("if multi_intent:", 1)[1]
        multi_block, single_block = after_multi.split("else:", 1)
        self.assertNotIn('"multiple people, duplicate character"', multi_block)
        self.assertNotIn("_default_illustrious_single_subject_negative()", multi_block)
        self.assertIn("_default_illustrious_single_subject_positive(raw_prompt, text, profile_meta)", single_block)
        self.assertIn("_default_illustrious_single_subject_negative()", single_block)
        for forbidden in (
            "balanced group composition",
            "each character has distinct readable face",
            "clear separation between characters",
            "limbs attached to the correct body",
        ):
            with self.subTest(single_forbidden=forbidden):
                self.assertNotIn(forbidden, single_block)
        positive_match = re.search(
            r"def _default_illustrious_single_subject_positive\(.*?\) -> list:(?P<body>.*?)\n\s+def _default_illustrious_single_subject_negative",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(positive_match)
        positive_body = positive_match.group("body")
        self.assertIn('tags.append("solo")', positive_body)
        self.assertIn("_single_character_profile_count_tag(raw_prompt, prompt, profile_meta or {})", positive_body)
        self.assertIn('profile_meta.get("profile_default_outfit")', main_text)
        self.assertIn('"whitedress"', main_text)
        for forbidden in (
            "(solo:",
            "(single character:",
            "one character only",
            "one visible character",
            "one body only",
            "solo focus",
        ):
            with self.subTest(positive_forbidden=forbidden):
                self.assertNotIn(forbidden, positive_body)

        negative_match = re.search(
            r"def _default_illustrious_single_subject_negative\(self\) -> list:(?P<body>.*?)\n\s+def _perfect_realism_limb_foreground_risk",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(negative_match)
        negative_body = negative_match.group("body")
        for marker in (
            '"2girls"',
            '"2boys"',
            '"multiple people"',
            '"second character"',
            '"duplicate character"',
            '"same character twice"',
            '"mirrored duplicate"',
            '"twins"',
            '"cloned body"',
            '"extra person"',
            '"extra body"',
            '"background character"',
        ):
            with self.subTest(negative_marker=marker):
                self.assertIn(marker, negative_body)
        for forbidden in (
            "(multiple characters:",
            "(duplicate character:",
            "picture-in-picture",
            "chibi inset",
            "partial extra body",
            "background duplicate",
            "shadow clone",
            "hand from outside frame",
            "wrong toe count",
            "edge spikes",
            "zipper artifacts",
        ):
            with self.subTest(negative_forbidden=forbidden):
                self.assertNotIn(forbidden, negative_body)

    def test_prompt_tag_dedupe_accepts_string_inputs(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        match = re.search(
            r"def _dedupe_prompt_tag_list_preserve_weights\(self, tags\) -> str:(?P<body>.*?)\n\s+def _apply_default_illustrious_prompt_stabilizers",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertIn("if isinstance(tags, str):", body)
        self.assertIn("source_tags = self._split_prompt_tags(tags)", body)
        self.assertIn("for tag in source_tags:", body)

    def test_illustrious_multi_character_intent_algorithm(self):
        from intent_utils import illustrious_has_multi_character_intent

        single_subject_cases = [
            "一只手拿着帽子，一只手扶住自己的头发，独自坐着的妮基塔",
            "one hand holding her hat, other hand touching own hair, Nikketa alone",
            "a girl with blue hair and red dress, looking at viewer",
            "cat girl with rabbit ears, solo portrait",
            "one person with left hand and right hand raised, solo",
            "solo, full body, head-to-toe, feet visible",
            "one hand in own hair, other hand holding own hat, solo",
            "1girl, one hand raised, other hand on own chest, alone",
            "\u5355\u4eba\uff0c\u5168\u8eab\uff0c\u5c55\u793a\u5b8c\u6574\u8db3\u5e95",
            "\u81ea\u5df1\u62ac\u8d77\u4e00\u53ea\u624b\uff0c\u53e6\u4e00\u53ea\u624b\u653e\u5728\u80f8\u524d\uff0c\u5355\u4eba",
            "自己整理头发，另一只手扶住帽子，单人半身像",
        ]
        multi_subject_cases = [
            "爱弥斯和圣娅相互拥抱",
            "Nikketa and Denia standing together",
            "两个少女同框聊天",
            "another person standing behind her",
            "2个角色在教室里互动",
            "两位女生牵手看向镜头",
            "一男一女坐在咖啡店里",
            "角色A：白发少女；角色B：黑发少女",
            "左边女孩是白发，右边女孩是黑发",
            "a girl and a boy sitting together",
            "white-haired girl, black-haired girl standing together",
            "left character has white hair; right character has black hair, same room",
            "alice and bob sitting together at a cafe",
            "a maid beside a knight talking",
            "red-haired OC + blue-haired OC hugging",
        ]
        for prompt in single_subject_cases:
            with self.subTest(prompt=prompt):
                self.assertFalse(illustrious_has_multi_character_intent(prompt, prompt))
        raw = "\u7231\u5f25\u65af\u5012\u7acb\u5927\u817f\u5f20\u5f00"
        translated = (
            "aemeath (wuthering waves), sidelocks, short hair with long locks, "
            "uncropped body, full body, handstand, legs apart, spread legs, solo"
        )
        self.assertFalse(illustrious_has_multi_character_intent(raw, translated))
        for prompt in multi_subject_cases:
            with self.subTest(prompt=prompt):
                self.assertTrue(illustrious_has_multi_character_intent(prompt, prompt))

    def test_default_illustrious_clean_neutral_defaults_are_wired(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        short_positive = "masterpiece, best quality, high quality, anime illustration, detailed eyes, clean eye shape"
        short_negative = (
            "worst quality, low quality, bad anatomy, bad hands, bad feet, extra fingers, "
            "missing fingers, extra limbs, distorted face, blurry, watermark, text, signature"
        )
        required_main_markers = [
            '"sampler_name": "euler_ancestral"',
            '"scheduler": "normal"',
            '"steps": ILLUSTRIOUS_DEFAULT_STEPS',
            '"cfg": ILLUSTRIOUS_DEFAULT_CFG',
            'ILLUSTRIOUS_DEFAULT_WIDTH = 896',
            'ILLUSTRIOUS_DEFAULT_HEIGHT = 1344',
            'ILLUSTRIOUS_DEFAULT_LANDSCAPE_WIDTH = 1344',
            'ILLUSTRIOUS_DEFAULT_LANDSCAPE_HEIGHT = 896',
            'ILLUSTRIOUS_DEFAULT_WIDE_WIDTH = 1344',
            'ILLUSTRIOUS_DEFAULT_WIDE_HEIGHT = 768',
            'ILLUSTRIOUS_DEFAULT_SQUARE_SIZE = 1152',
            'ILLUSTRIOUS_DEFAULT_REFINE_STEPS = 24',
            'ILLUSTRIOUS_DEFAULT_REFINE_CFG = 5.0',
            'ILLUSTRIOUS_DEFAULT_REFINE_DENOISE = 0.45',
            'ILLUSTRIOUS_DEFAULT_LATENT_UPSCALE_BY = 1.5',
            '"lora_strength_model_cap": 0.68',
            '"lora_strength_clip_cap": 0.45',
            'key != "checkpoint_name"',
            '"refine_steps", "refine_cfg", "refine_denoise", "width", "height", "latent_upscale_by",',
            '"38", "38.0"',
            '"6", "6.0", "6.00"',
            'if _safe_float_value(ill.get("width"), ILLUSTRIOUS_DEFAULT_WIDTH) >= _safe_float_value(ill.get("height"), ILLUSTRIOUS_DEFAULT_HEIGHT):',
            'def _illustrious_oriented_size(',
            'if aspect == "portrait" and width >= height:',
            f'"{short_positive}"',
            '"worst quality, low quality, bad anatomy, bad hands, bad feet, "',
            '"extra fingers, missing fingers, extra limbs, distorted face, blurry, watermark, text, signature"',
            'getattr(self, "enable_final_prompt_optimize", False)',
        ]
        for marker in required_main_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, main_text)

        workflow = json.loads((ROOT / "workflow/finish_illustrious_api.json").read_text(encoding="utf-8"))
        ksamplers = [node for node in workflow.values() if isinstance(node, dict) and node.get("class_type") == "KSampler"]
        self.assertEqual(len(ksamplers), 2)
        class_types = [node.get("class_type") for node in workflow.values() if isinstance(node, dict)]
        self.assertIn("LatentUpscaleBy", class_types)
        self.assertEqual(workflow["13"]["inputs"]["width"], 896)
        self.assertEqual(workflow["13"]["inputs"]["height"], 1344)
        self.assertEqual(workflow["14"]["inputs"]["steps"], 60)
        self.assertEqual(workflow["14"]["inputs"]["cfg"], 5.15)
        self.assertEqual(workflow["14"]["inputs"]["sampler_name"], "euler_ancestral")
        self.assertEqual(workflow["14"]["inputs"]["scheduler"], "normal")
        self.assertEqual(workflow["14"]["inputs"]["denoise"], 1.0)
        self.assertEqual(workflow["15"]["inputs"]["upscale_method"], "nearest-exact")
        self.assertEqual(workflow["15"]["inputs"]["scale_by"], 1.5)
        self.assertEqual(workflow["15"]["inputs"]["samples"], ["14", 0])
        self.assertEqual(workflow["16"]["inputs"]["latent_image"], ["15", 0])
        self.assertEqual(workflow["16"]["inputs"]["denoise"], 0.45)
        self.assertEqual(workflow["16"]["inputs"]["steps"], 24)
        self.assertIn("refine", workflow["16"]["_meta"]["title"].lower())
        self.assertEqual(workflow["17"]["inputs"]["samples"], ["16", 0])
        workflow_positive = workflow["11"]["inputs"]["text"]
        self.assertEqual(workflow_positive, short_positive)
        self.assertLessEqual(_tag_count(workflow_positive), 8)
        for forbidden_positive in (
            "defined clean linework",
            "clear subject focus",
            "crisp subject details",
            "smooth shading",
            "vibrant but stable colors",
            "balanced color palette",
            "solo",
            "soft clean shading",
            "soft focus",
            "sharp focus",
        ):
            with self.subTest(forbidden_positive=forbidden_positive):
                self.assertNotIn(forbidden_positive, workflow_positive)
        self.assertEqual(workflow["12"]["inputs"]["text"], short_negative)
        self.assertLessEqual(_tag_count(workflow["12"]["inputs"]["text"]), 16)
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        schema_ill = schema["model_family_settings"]["items"]["illustrious"]["items"]
        self.assertEqual(schema_ill["sampler"]["default"], "euler_ancestral")
        self.assertEqual(schema_ill["scheduler"]["default"], "normal")
        self.assertEqual(schema_ill["steps"]["default"], 60)
        self.assertEqual(schema_ill["cfg"]["default"], 5.15)
        self.assertEqual(schema_ill["width"]["default"], 896)
        self.assertEqual(schema_ill["height"]["default"], 1344)
        self.assertNotIn("aspect_mode", schema_ill)
        self.assertTrue(schema_ill["enable_prompt_aspect_detect"]["default"])
        self.assertEqual(schema_ill["landscape_width"]["default"], 1344)
        self.assertEqual(schema_ill["landscape_height"]["default"], 896)
        self.assertEqual(schema_ill["wide_width"]["default"], 1344)
        self.assertEqual(schema_ill["wide_height"]["default"], 768)
        self.assertEqual(schema_ill["square_size"]["default"], 1152)
        self.assertEqual(schema_ill["refine_steps"]["default"], 24)
        self.assertEqual(schema_ill["refine_denoise"]["default"], 0.45)
        self.assertEqual(schema_ill["latent_upscale_by"]["default"], 1.5)
        self.assertTrue(schema_ill["enable_single_subject_foot_detailer"]["default"])
        self.assertEqual(schema_ill["foot_detailer_detector_model"]["default"], "foot-yolov8l.pt")
        self.assertEqual(schema_ill["foot_detailer_steps"]["default"], 26)
        self.assertEqual(schema_ill["foot_detailer_cfg"]["default"], 4.6)
        self.assertEqual(schema_ill["foot_detailer_denoise"]["default"], 0.24)
        self.assertTrue(schema_ill["enable_single_subject_output_audit"]["default"])
        self.assertEqual(schema_ill["subject_audit_face_detector_model"]["default"], "face_yolov8m.pt")
        self.assertEqual(schema_ill["subject_audit_person_detector_model"]["default"], "person_yolov8s-seg.pt")
        self.assertEqual(schema_ill["subject_audit_max_retries"]["default"], 2)
        self.assertEqual(schema_ill["default_strength_model"]["default"], 0.68)
        self.assertEqual(schema_ill["default_strength_clip"]["default"], 0.45)
        schema_positive = schema["model_family_settings"]["items"]["illustrious"]["items"]["quality_prefix"]["default"]
        self.assertEqual(schema_positive, short_positive)
        for forbidden_positive in (
            "defined clean linework",
            "clear subject focus",
            "crisp subject details",
            "smooth shading",
            "vibrant but stable colors",
            "balanced color palette",
            "solo",
            "soft clean shading",
            "soft focus",
            "sharp focus",
        ):
            with self.subTest(schema_forbidden_positive=forbidden_positive):
                self.assertNotIn(forbidden_positive, schema_positive)
        schema_negative = schema["model_family_settings"]["items"]["illustrious"]["items"]["negative_prompt"]["default"]
        self.assertEqual(schema_negative, short_negative)
        self.assertLessEqual(_tag_count(schema_negative), 16)
        for forbidden_negative in (
            "partial extra body",
            "duplicate body",
            "same character twice",
            "background duplicate",
            "hand from outside frame",
            "hand entering from frame edge",
            "wrong toe count",
            "soft focus",
            "haze",
            "motion blur",
            "neon color cast",
            "plastic skin",
            "waxy skin",
            "glossy skin",
        ):
            with self.subTest(forbidden_negative=forbidden_negative):
                self.assertNotIn(forbidden_negative, workflow["12"]["inputs"]["text"])
                self.assertNotIn(forbidden_negative, schema_negative)
        schema_output = schema["output_settings"]["items"]
        self.assertTrue(schema_output["enable_black_screen_retry"]["default"])
        self.assertEqual(schema_output["black_screen_max_retries"]["default"], 2)

    def test_illustrious_underwear_barefoot_prompt_case_stays_user_led(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        workflow = json.loads((ROOT / "workflow/finish_illustrious_api.json").read_text(encoding="utf-8"))
        raw_case = "穿内衣 展示裸足的爱弥斯"

        outfit_match = re.search(
            r"def _user_prompt_has_outfit_intent\(.*?\) -> bool:(?P<body>.*?)\n\s+def _user_prompt_has_lora_detail_override_intent",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(outfit_match)
        outfit_body = outfit_match.group("body")
        for marker in ("内衣", "lingerie", "underwear", "bra", "panties"):
            with self.subTest(outfit_marker=marker):
                self.assertIn(marker, outfit_body)
        self.assertIn(raw_case[:3], raw_case)

        lora_match = re.search(
            r"def _build_lora_smart_prompt\(.*?\) -> str:(?P<body>.*?)\n\s+def _build_lora_identity_prompt",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(lora_match)
        lora_body = lora_match.group("body")
        self.assertIn("activation = self._profile_tag_values(profile.get(\"activation_tags\"), role_prompt, limit=8)", lora_body)
        self.assertIn("appearance = self._profile_tag_values(profile.get(\"identity_appearance\"), limit=8)", lora_body)
        self.assertIn("include_default_outfit = self._user_prompt_wants_default_outfit(user_prompt)", lora_body)
        self.assertIn("outfit[:6] if include_default_outfit else []", lora_body)
        self.assertIn("max_tags=6", lora_body)
        self.assertNotIn("not self._user_prompt_has_outfit_intent(user_prompt)", lora_body)
        self.assertNotIn("or not self._user_prompt_has_outfit_intent(user_prompt)", lora_body)

        ensure_outfit_match = re.search(
            r"def _ensure_outfit_in_prompt\(.*?\) -> str:(?P<body>.*?)\n\s+def _single_character_profile_count_tag",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(ensure_outfit_match)
        ensure_outfit_body = ensure_outfit_match.group("body")
        self.assertIn("if not self._user_prompt_wants_default_outfit(raw_prompt):", ensure_outfit_body)
        self.assertIn("return text", ensure_outfit_body)

        positive_match = re.search(
            r"def _default_illustrious_single_subject_positive\(.*?\) -> list:(?P<body>.*?)\n\s+def _default_illustrious_single_subject_negative",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(positive_match)
        positive_body = positive_match.group("body")
        self.assertEqual(positive_body.count('tags.append("solo")'), 1)
        for forbidden in ("(solo:", "(single character:", "one visible character", "solo focus"):
            with self.subTest(single_subject_forbidden=forbidden):
                self.assertNotIn(forbidden, positive_body)

        workflow_positive = workflow["11"]["inputs"]["text"]
        for forbidden in (
            "white dress",
            "default outfit",
            "defined clean linework",
            "vibrant but stable colors",
            "balanced color palette",
            "background",
            "solo",
        ):
            with self.subTest(workflow_positive_forbidden=forbidden):
                self.assertNotIn(forbidden, workflow_positive)
        self.assertLessEqual(_tag_count(workflow_positive), 8)

        base_negative = workflow["12"]["inputs"]["text"]
        foot_risk_pack = [
            "bad toes",
            "fused toes",
            "extra toes",
            "missing toes",
            "wrong toe count",
            "twisted foot",
            "backward foot",
            "broken ankle",
            "detached foot",
            "extra foot",
            "extra feet",
            "foot from nowhere",
            "incorrect toe count",
            "impossible leg bend",
            "broken leg",
            "dislocated knee",
            "extra leg",
            "extra legs",
        ]
        self.assertLessEqual(_tag_count(base_negative) + len(foot_risk_pack), 35)
        for forbidden in ("neon color cast", "plastic skin", "glossy skin", "over-sharpened"):
            with self.subTest(foot_pack_forbidden=forbidden):
                self.assertNotIn(forbidden, base_negative)
                self.assertNotIn(forbidden, ", ".join(foot_risk_pack))

        sampler = workflow["14"]["inputs"]
        self.assertEqual(sampler["sampler_name"], "euler_ancestral")
        self.assertEqual(sampler["scheduler"], "normal")
        self.assertEqual(sampler["steps"], 60)
        self.assertEqual(sampler["cfg"], 5.15)
        self.assertIn('"lora_strength_model_cap": 0.68', main_text)
        self.assertIn('"lora_strength_clip_cap": 0.45', main_text)

    def test_illustrious_landscape_runtime_dimensions_are_wired(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        required = [
            "def _illustrious_explicit_dimension_has_context(",
            "def _extract_illustrious_explicit_dimensions(",
            "def _illustrious_prompt_aspect_key(",
            "def _illustrious_size_for_aspect(",
            "def _illustrious_generation_request_dimensions(",
            "def _apply_illustrious_generation_request_runtime(",
            "def _webui_explicit_illustrious_payload_dimensions(",
            "prepared[\"img_width\"] = img_width",
            "prepared[\"img_height\"] = img_height",
            "payload_width, payload_height = self._webui_explicit_illustrious_payload_dimensions(payload)",
            "profile_meta[\"illustrious_aspect\"]",
            "profile_meta[\"illustrious_width\"]",
            "profile_meta[\"illustrious_height\"]",
            "current[\"aspect_source\"]",
            '"raw_prompt": raw_prompt',
        ]
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, main_text)
        self.assertNotIn('"raw_prompt": original', main_text)
        self.assertNotIn('prepared["img_width"] = payload.get("width") or payload.get("img_width")', main_text)
        self.assertNotIn('prepared["img_height"] = payload.get("height") or payload.get("img_height")', main_text)
        self.assertIn('"explicit_size"', main_text)
        self.assertIn('"enable_custom_size"', main_text)
        self.assertIn('if aspect and aspect not in {"auto", "default", "none"}:', main_text)

        aspect_match = re.search(
            r"def _illustrious_prompt_aspect_key\(.*?\) -> str:(?P<body>.*?)\n\s+def _illustrious_size_for_aspect",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(aspect_match)
        aspect_body = aspect_match.group("body")
        for marker in (
            "横图",
            "横屏",
            "landscape",
            "horizontal",
            "宽屏",
            "16\\s*[:：]\\s*9",
            "竖图",
            "竖屏",
            "portrait",
            "square\\s+(?:image|format|canvas)",
            "方图",
        ):
            with self.subTest(aspect_marker=marker):
                self.assertIn(marker, aspect_body)
        for forbidden in (
            "电脑壁纸",
            "桌面壁纸",
            "story",
            "cinematic\\s+wide",
            "电影横幅",
            "horizontal|",
            "|wide|",
            "landscape\\)",
            "wide\\)",
        ):
            with self.subTest(aspect_forbidden=forbidden):
                self.assertNotIn(forbidden, aspect_body)

        request_match = re.search(
            r"def _illustrious_generation_request_dimensions\(.*?\) -> dict:(?P<body>.*?)\n\s+def _apply_illustrious_generation_request_runtime",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(request_match)
        request_body = request_match.group("body")
        self.assertIn('"aspect_source": "args"', request_body)
        self.assertIn('"aspect_source": "prompt_size"', request_body)
        self.assertIn('"aspect_source": "prompt" if detected else "default"', request_body)
        self.assertIn('detected = self._illustrious_prompt_aspect_key(raw_prompt)', request_body)
        self.assertIn('aspect = detected or "portrait"', request_body)
        self.assertNotIn("aspect_mode", request_body)
        self.assertIn("if explicit_width > 0 and explicit_height > 0:", request_body)
        self.assertIn("not self._illustrious_explicit_dimension_has_context(raw, match)", main_text)

        oriented_match = re.search(
            r"def _illustrious_oriented_size\(.*?\) -> tuple:(?P<body>.*?)\n\s+def _illustrious_profile_number",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(oriented_match)
        oriented_body = oriented_match.group("body")
        self.assertIn('if aspect == "portrait" and width >= height:', oriented_body)
        self.assertIn('if aspect in {"landscape", "wide"} and width <= height:', oriented_body)
        self.assertIn("return default_width, default_height", oriented_body)

        self.assertIn("if multi_intent and len(multi_matches) >= 2:", main_text)
        self.assertNotIn("if len(multi_matches) >= 2:", main_text)

    def test_illustrious_foot_detailer_is_mandatory_and_setup_installs_dependencies(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        comfy_text = (ROOT / "comfyui_api.py").read_text(encoding="utf-8")
        setup_bat_text = (ROOT / "scripts" / "astrbot_comfyui_setup.bat").read_text(encoding="utf-8")

        main_required = [
            'ILLUSTRIOUS_FOOT_DETAILER_DETECTOR_MODEL = "foot-yolov8l.pt"',
            "ILLUSTRIOUS_FOOT_DETAILER_STEPS = 26",
            "ILLUSTRIOUS_FOOT_DETAILER_CFG = 4.6",
            "ILLUSTRIOUS_FOOT_DETAILER_DENOISE = 0.24",
            'ILLUSTRIOUS_SUBJECT_AUDIT_FACE_MODEL = "face_yolov8m.pt"',
            'ILLUSTRIOUS_SUBJECT_AUDIT_PERSON_MODEL = "person_yolov8s-seg.pt"',
            '"enable_single_subject_foot_detailer", True',
            '"enable_single_subject_output_audit", True',
            "def _illustrious_single_subject_foot_detailer_options(",
            "def _illustrious_single_subject_audit_options(",
            "def _illustrious_foot_detailer_missing_detector_model(",
            "def _download_illustrious_foot_detailer_model_via_bridge(",
            "def _download_illustrious_subject_audit_models_via_bridge(",
            "def _preflight_illustrious_foot_detailer_if_needed(",
            "def _preflight_illustrious_subject_audit_if_needed(",
            "def _audit_illustrious_single_subject_image(",
            "await self.api.check_illustrious_foot_detailer_ready(",
            "await self.api.apply_illustrious_foot_detailer(",
            "ILLUSTRIOUS_FOOT_DETAILER_DETECTOR_URL",
            "ILLUSTRIOUS_SUBJECT_AUDIT_RETRY_PROFILES",
            "ultralytics_bbox",
            "ultralytics_segm",
            "foot_detailer_remote_download_attempted",
            "foot_detailer_remote_downloaded",
            "Bridge >= 1.4.0",
            "缺少足部 Detailer 预检方法",
            "return None, foot_preflight_error, foot_preflight_meta",
            "return None, audit_preflight_error, audit_preflight_meta",
            'meta["foot_detailer_applied"] = True',
            'meta["foot_detailer_regions"]',
            '"single_subject_audit_passed"',
            "BLACK_SCREEN_RETRY_ENABLED",
            "BLACK_SCREEN_MAX_RETRIES",
            "def _black_screen_retry_settings",
            'self.api._is_blank_or_black_image(img_data)',
            'meta["black_screen_detected"] = True',
            'meta["foot_detailer_black_screen_skipped"] = True',
        ]
        for marker in main_required:
            with self.subTest(main_marker=marker):
                self.assertIn(marker, main_text)
        self.assertNotIn('hasattr(self.api, "apply_illustrious_foot_detailer")', main_text)

        comfy_required = [
            'DEFAULT_ILLUSTRIOUS_FOOT_DETECTOR_MODEL = "foot-yolov8l.pt"',
            "def check_illustrious_foot_detailer_ready(",
            "def apply_illustrious_foot_detailer(",
            "def _build_illustrious_foot_detailer_workflow(",
            "def _format_illustrious_foot_detailer_preflight_error(",
            "/object_info 不可用",
            "ComfyUI-Impact-Pack",
            "ComfyUI-Impact-Subpack",
            "UltralyticsDetectorProvider",
            "ImpactSimpleDetectorSEGS",
            "DetailerForEach",
            "ComfyUI 服务器端的 ComfyUI/models/ultralytics/bbox",
            "bbox/\" + Path(raw).name",
            '"guide_size": 768',
            '"max_size": 1216',
            '"bbox_dilation": 12',
            '"crop_factor": 3.5',
            '"feather": 10',
            "def _is_blank_or_black_image",
        ]
        for marker in comfy_required:
            with self.subTest(comfy_marker=marker):
                self.assertIn(marker, comfy_text)

        setup_required = [
            "ASTRBOT_SETUP_VERSION=",
            'Join-Path $Root "models\\ultralytics\\bbox"',
            'Join-Path $Root "models\\ultralytics\\segm"',
            "$script:DetailerFiles",
            "foot-yolov8l.pt",
            "face_yolov8m.pt",
            "person_yolov8s-seg.pt",
            "https://huggingface.co/AunyMoons/loras-pack/resolve/main/foot-yolov8l.pt?download=true",
            "https://huggingface.co/Bingsu/adetailer/resolve/main/face_yolov8m.pt?download=true",
            "https://huggingface.co/Bingsu/adetailer/resolve/main/person_yolov8s-seg.pt?download=true",
            "MinGB = 0.05",
            "$script:MinimalFiles = @($script:CoreFiles + $script:ModelFiles + $script:DetailerFiles)",
            "function Add-RunningComfyPythonCandidates",
            "function Add-CondaEnvPythonCandidates",
            "function Test-ComfyPythonCandidate",
            "ComfyUI 依赖需要 Python 3.10+",
            "Anaconda base",
            "COMFYUI_PYTHON",
            "function Resolve-GitCommand",
            "function Install-PortableGit",
            "function Ensure-GitForRequirements",
            "PortableGit-.*-64-bit\\.7z\\.exe",
            "tools\\PortableGit\\cmd\\git.exe",
            "function Resolve-ComfyPython",
            "function Install-NodeRequirements",
            "function Install-SubjectAuditRequirements",
            "New-GitlessRequirementsFile $requirements",
            "pip install ultralytics",
            "Ensure-GitForRequirements $requirements",
            "ComfyUI-Impact-Pack",
            "ComfyUI-Impact-Subpack",
            "Install-NodeRequirements \"ComfyUI-Impact-Pack\"",
            "Install-NodeRequirements \"ComfyUI-Impact-Subpack\"",
            "Install-SubjectAuditRequirements",
        ]
        for marker in setup_required:
            with self.subTest(setup_marker=marker):
                self.assertIn(marker, setup_bat_text)

    def test_illustrious_runtime_prompt_sanitizer_is_wired(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        match = re.search(
            r"def _sanitize_illustrious_runtime_prompt\(.*?\) -> str:(?P<body>.*?)\n\s+def _default_illustrious_plain_aircraft_cabin_intent",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        for marker in (
            'text.replace("\\\\\\\\(", "(")',
            '"naturalbodyproportions"',
            '"balancedanatomy"',
            '"cleanlineart"',
            '"softlighting"',
            '"naturalshadows"',
            '"highdetail"',
            '"softfocus"',
            '"sharpfocus"',
            "self._dedupe_prompt_tag_list_preserve_weights(text)",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)
        self.assertGreaterEqual(main_text.count("_sanitize_illustrious_runtime_prompt("), 5)
        self.assertIn("_default_illustrious_subject_intent(", main_text)
        self.assertIn('profile_meta["illustrious_subject_mode"] = "multi" if multi_intent else "single"', main_text)
        self.assertIn('"warmlighting"', body)
        self.assertIn('"indoor"', body)
        self.assertIn("raw_text = str(raw_prompt or \"\").lower()", body)
        self.assertIn('remove_markers.update({"indoor", "indoors", "interior", "indoorscene"})', body)
        self.assertIn('remove_markers.update({"warmlighting", "warmambientlighting", "softwarmlighting"})', body)

        framing_match = re.search(
            r"def _prompt_framing_intent\(.*?\) -> str:(?P<body>.*?)\n\s+def _cleanup_prompt_framing_conflicts",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(framing_match)
        framing_body = framing_match.group("body")
        for marker in ("全身", "从头到脚", "full\\s*body", "head[-\\s]*to[-\\s]*toe"):
            with self.subTest(full_body_marker=marker):
                self.assertIn(marker, framing_body)
        for forbidden in (
            "足底",
            "脚底",
            "脚趾",
            "泡脚",
            "feet\\s*visible",
            "showing\\s*soles",
            "bare\\s*feet",
            "foot\\s*focus",
            "toes",
        ):
            with self.subTest(framing_forbidden=forbidden):
                self.assertNotIn(forbidden, framing_body)

    def test_illustrious_color_tone_prompt_guard_was_removed(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        for marker in (
            "def _illustrious_indoor_white_tile_color_risk(",
            "def _apply_illustrious_indoor_color_tone_guard(",
            "_apply_illustrious_indoor_color_tone_guard(raw_prompt, text)",
            "def _user_prompt_wants_stylized_color_lighting(",
            "def _remove_default_illustrious_color_cast_tags(",
            "_remove_default_illustrious_color_cast_tags(text, raw_prompt)",
            '"illustrious_indoor_color_tone_guard": True',
            '"neutral white balance"',
            '"natural color balance"',
            '"natural skin tones"',
            '"cyan color cast"',
            '"teal color cast"',
            '"green color cast"',
            '"fluorescent color cast"',
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, main_text)

    def test_illustrious_single_runtime_guards_are_active_and_concise(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        hand_match = re.search(
            r"def _apply_illustrious_external_hand_guard\(.*?\n\s+\) -> tuple:\n(?P<body>.*?)\n\s+def _illustrious_restraint_chain_risk",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(hand_match)
        hand_body = hand_match.group("body")
        self.assertNotIn("intentionally minimal", hand_body)
        self.assertIn("if not text or multi_intent:", hand_body)
        self.assertIn("ILLUSTRIOUS_EXTERNAL_HAND_NEGATIVE", hand_body)
        self.assertIn("ILLUSTRIOUS_FOOT_EXTERNAL_HAND_NEGATIVE", hand_body)
        self.assertIn("ILLUSTRIOUS_RESTRAINT_CHAIN_NEGATIVE", hand_body)
        self.assertIn("ILLUSTRIOUS_SELF_TOUCH_HAND_NEGATIVE", hand_body)
        for marker in (
            '"extra hands"',
            '"third hand"',
            '"phantom hand"',
            '"hand from nowhere"',
            '"hand not attached to body"',
            '"phone near feet"',
            '"smartphone near feet"',
        ):
            with self.subTest(hand_marker=marker):
                self.assertIn(marker, hand_body)
        for marker in (
            "large hands from outside frame",
            "hands behind legs",
            "hands holding legs",
            "disembodied arms behind subject",
            "helper hands touching body",
            "arm from outside frame",
            "solid color cast",
            "neon color cast",
        ):
            with self.subTest(hand_forbidden_marker=marker):
                self.assertNotIn(marker, hand_body)
        self.assertIn("illustrious_armpit_self_hand_guard", hand_body)
        self.assertIn("illustrious_phone_hand_anchor_guard", hand_body)
        self.assertNotIn("_merge_prompt_tags", hand_body)

        foot_match = re.search(
            r"def _apply_illustrious_foot_perspective_guard\(.*?\n\s+\) -> tuple:\n(?P<body>.*?)\n\s+def _user_prompt_wants_silhouette_or_shadow",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(foot_match)
        foot_body = foot_match.group("body")
        self.assertNotIn('return str(prompt or "").strip(), "", {}', foot_body)
        self.assertIn("_remove_barefoot_footwear_conflicts", foot_body)
        self.assertIn("_illustrious_high_risk_foot_prompt", foot_body)
        self.assertIn("ILLUSTRIOUS_FOOT_PERSPECTIVE_NEGATIVE", foot_body)
        self.assertIn('"wrong toe count"', foot_body)
        self.assertIn('"fused toes"', foot_body)
        self.assertNotIn('"mutated feet"', foot_body)
        self.assertNotIn('"feet not attached to ankles"', foot_body)
        self.assertNotIn('"skin gloss"', foot_body)
        self.assertNotIn('"neon color cast"', foot_body)
        self.assertNotIn("_merge_prompt_tags", foot_body)

        self.assertGreaterEqual(main_text.count("_apply_illustrious_external_hand_guard("), 3)
        self.assertIn("self._normalize_model_family(family) == MODEL_FAMILY_ILLUSTRIOUS", main_text)
        self.assertIn('profile_meta.get("illustrious_subject_mode") == "multi"', main_text)

    def test_illustrious_foot_guard_trigger_is_targeted(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        high_risk_match = re.search(
            r"def _illustrious_high_risk_foot_prompt\([^\n]*\) -> bool:\n(?P<body>.*?)\n\s+def _apply_illustrious_foot_perspective_guard",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(high_risk_match)
        body = high_risk_match.group("body")
        for marker in (
            "full_body_framing",
            "head[-\\s]*to[-\\s]*toe",
            "showing\\s*soles",
            "spread\\s*legs",
            "\\u5b8c\\u6574\\u8db3\\u5e95",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)
        self.assertIn("if not has_foot_intent:", body)
        self.assertIn("return False", body)

    def test_lora_profile_cleanup_is_non_destructive(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        match = re.search(
            r"def _cleanup_stale_lora_mappings\(self, loras: list\) -> int:(?P<body>.*?)\n\s+def _is_no_cache_lora_fetch_failure",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertNotIn("profiles.pop(", body)
        self.assertNotIn("alias_data.pop(", body)
        self.assertNotIn("prompt_data.pop(", body)
        self.assertNotIn("_get_lora_profile_store().save", body)
        self.assertIn("已保留不删除", body)

    def test_single_llm_draw_tool_is_wired(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertEqual(1, len(re.findall(r"@filter\.llm_tool", main_text)))
        required = [
            '@filter.llm_tool(name="jiuhu_draw_image")',
            'async def jiuhu_draw_image(self, event: AstrMessageEvent, prompt: str = "", message: str = "")',
            "prompt(string): 完整图片提示词",
            "message(string): 要展示给用户的短回复文本",
            "self.context.deactivate_llm_tool(\"comfyui_txt2img\")",
            "self.context.activate_llm_tool(\"jiuhu_draw_image\")",
            "def _ensure_llm_draw_tool_registered(self):",
            "def _ensure_llm_draw_tool_in_request(self, req) -> bool:",
            "tool_mgr.add_func(\"jiuhu_draw_image\", args, desc, self.jiuhu_draw_image)",
            "req.func_tool.add_tool(tool)",
            "req.func_tool.get_tool(\"jiuhu_draw_image\")",
            "self._ensure_llm_draw_tool_in_request(req)",
            "def _fold_llm_draw_text_into_tool_args(",
            "resp.completion_text = \"\"",
            "resp.result_chain = None",
            "compact_text=intro_text",
            "compact_text: str = None",
            "工具会把 message 和图片放在同一条绘图结果里发送",
            "不要在普通回复里输出 <pic>",
            "调用 `jiuhu_draw_image`",
            "intro_text=intro_text",
            "def _start_llm_draw_background_task(",
            "self._start_llm_draw_background_task(event, raw_prompt, intro_text=intro_text)",
            "event.set_extra(\"comfy_llm_draw_background\", True)",
            "event.stop_event()",
        ]
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, main_text)
        self.assertNotIn('请在 <pic prompt="...">', main_text)
        self.assertNotIn('再写入 <pic prompt="...">', main_text)

    def test_llm_draw_tool_does_not_wait_for_generation(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        helper = re.search(
            r"def _start_llm_draw_background_task\(self, event: AstrMessageEvent, prompt: str, intro_text: str = \"\"\):(?P<body>.*?)\n\s+@filter\.llm_tool",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(helper)
        helper_body = helper.group("body")
        self.assertIn("asyncio.create_task(", helper_body)
        self.assertIn("self._register_generation_queue_entry(event, prompt, kind=\"llm_tool\")", helper_body)
        self.assertIn("self._send_image_async(event, prompt, intro_text=intro_text, compact_text=intro_text, queue_entry_id=queue_entry_id)", helper_body)
        self.assertIn("_paint_background_tasks", helper_body)

        tool = re.search(
            r"async def jiuhu_draw_image\(self, event: AstrMessageEvent, prompt: str = \"\", message: str = \"\"\):(?P<body>.*?)\n\s+# ====== 修改提取逻辑",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(tool)
        tool_body = tool.group("body")
        self.assertIn("self._start_llm_draw_background_task(event, raw_prompt, intro_text=intro_text)", tool_body)
        self.assertNotIn("await self._send_image_async", tool_body)

    def test_generation_queue_orphan_watchdog_and_task_binding(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        gpt_magic_text = (ROOT / "gpt_magic.py").read_text(encoding="utf-8")
        schema_text = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        required = [
            "_generation_queue_task_by_entry_id",
            "generation_queue_ready_stale_seconds",
            "generation_lock_acquire_timeout_seconds",
            "generation_lock_remote_idle_timeout_seconds",
            "def _bind_generation_queue_task(",
            "已清理无后台任务绑定的生成队列孤儿条目",
            "head_ready_age=",
            "lock_owner=(",
            "def _generation_lock_owner_text(",
            "def _generation_lock_owner_task_alive(",
            "def _recover_remote_idle_generation_lock_if_needed(",
            "def _recover_stale_generation_lock_if_needed(",
            "def _release_generation_state_lock(",
            "async def _finalize_generation_queue_and_lock(",
            "asyncio.wait_for(lock.acquire(), timeout=2.0)",
            "stale generation lock",
            "stale_generation_lock_recovery",
            "remote_idle_generation_lock_recovery",
            "\"task\": current_task",
            "self._bind_generation_queue_task(queue_entry_id, task, \"paint_background\")",
            "self._bind_generation_queue_task(queue_entry_id, task, \"video_background\")",
            "self._bind_generation_queue_task(queue_entry_id, task, \"llm_draw_tool\")",
            "self._bind_generation_queue_task(queue_entry_id, task, \"llm_pic_tag\")",
        ]
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, main_text)
        self.assertIn("owner._bind_generation_queue_task(queue_entry_id, task, \"web_draw\")", gpt_magic_text)
        self.assertIn("generation_queue_ready_stale_seconds", schema_text)
        self.assertIn("generation_lock_acquire_timeout_seconds", schema_text)
        self.assertIn("generation_lock_remote_idle_timeout_seconds", schema_text)
        comfy_text = (ROOT / "comfyui_api.py").read_text(encoding="utf-8")
        for marker in (
            "def _fetch_queue_snapshot(",
            "def _check_prompt_disappeared_from_queue(",
            "queue_missing_grace_seconds",
            "queue_missing_confirmations",
            "ComfyUI 远端队列中找不到本次 prompt_id",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, comfy_text)

    def test_generation_queue_finalize_release_is_exception_safe(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        match = re.search(
            r"async def _finalize_generation_queue_and_lock\((?P<body>.*?)\n\s+def _generation_queue_active_entries",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        required = [
            "cleanup_errors = []",
            "cleanup_errors.append(f\"restore_auto_switch:{e}\")",
            "cleanup_errors.append(f\"finish_queue:{e}\")",
            "cleanup_errors.append(f\"reset_runtime:{e}\")",
            "cleanup_errors.append(f\"release_lock:{e}\")",
            "self._release_generation_state_lock(context=context, queue_entry_id=queue_entry_id)",
            "锁释放流程已保护",
        ]
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, body)
        self.assertLess(
            body.index("self._configure_api_for_current_family"),
            body.index("self._release_generation_state_lock(context=context, queue_entry_id=queue_entry_id)"),
        )

    def test_generation_queue_high_concurrency_simulation_does_not_deadlock(self):
        class MiniQueue:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.finished = []
                self.released = 0

            async def acquire(self):
                await self.lock.acquire()

            def _release_generation_state_lock(self):
                if self.lock.locked():
                    self.lock.release()
                    self.released += 1
                    return True
                return False

            async def finalize(self, entry_id: int, fail_cleanup: bool):
                cleanup_errors = []
                try:
                    try:
                        if fail_cleanup:
                            raise RuntimeError("simulated cleanup failure")
                        self.finished.append(entry_id)
                    except Exception as e:
                        cleanup_errors.append(str(e))
                finally:
                    self._release_generation_state_lock()
                return cleanup_errors

        async def run_simulation():
            queue = MiniQueue()
            errors = []

            async def worker(index: int):
                await queue.acquire()
                await asyncio.sleep(0)
                errors.extend(await queue.finalize(index, fail_cleanup=(index % 7 == 0)))

            await asyncio.wait_for(
                asyncio.gather(*(worker(index) for index in range(50))),
                timeout=5,
            )
            return queue, errors

        queue, errors = asyncio.run(run_simulation())
        self.assertFalse(queue.lock.locked())
        self.assertEqual(queue.released, 50)
        self.assertGreaterEqual(len(errors), 7)

    def test_generation_lock_is_released_before_send_paths(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        llm = re.search(
            r"async def _send_image_async\(self, event: AstrMessageEvent, prompt: str, intro_text: str = None, compact_text: str = None, queue_entry_id: str = \"\"\):(?P<body>.*?)\n\s+@filter\.on_decorating_result\(priority=5\)",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(llm)
        llm_body = llm.group("body")
        self.assertRegex(
            llm_body,
            r"(?s)img_path, error_msg, meta = await self\._generate_image_file\(\s*prepared_prompt.*?await self\._finalize_generation_queue_and_lock\(.*?audit_ok, audit_message, meta = await self\._audit_generated_output_before_send",
        )

        txt2img = re.search(
            r"async def comfyui_txt2img\(self, event: AstrMessageEvent,.*?(?P<body>try:\n\s+lock_started = await self\._acquire_generation_state_lock.*)$",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(txt2img)
        txt2img_body = txt2img.group("body")
        self.assertRegex(
            txt2img_body,
            r"(?s)img_path, error_msg, meta = await self\._generate.*?await self\._finalize_generation_queue_and_lock\(.*?if error_msg:",
        )
        self.assertLess(
            txt2img_body.index("await self._finalize_generation_queue_and_lock("),
            txt2img_body.index("audit_ok, audit_message, meta = await self._audit_generated_output_before_send"),
        )

    def test_web_draw_history_is_account_scoped(self):
        gpt_magic_text = (ROOT / "gpt_magic.py").read_text(encoding="utf-8")
        required = [
            "def _web_draw_history_allowed(",
            "def list_web_draw_history_for_context(",
            "def _draw_job_allowed(",
            "\"account_key\": context.get(\"account_key\")",
            "\"owner_user_id\": context.get(\"owner_user_id\")",
            "list_web_draw_history_for_context(context, limit=limit)",
            "get_web_draw_history_item(item_id, context=context)",
            "get_web_draw_history_image_path(item_id, context=context)",
            "not self._draw_job_allowed(job, context)",
        ]
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, gpt_magic_text)
        self.assertNotIn(
            "items = [self._draw_history_payload(item) for item in self.service.store.list_web_draw_history(limit=limit)]",
            gpt_magic_text,
        )

    def test_favorite_gallery_admin_batch_editing_is_wired(self):
        gpt_magic_text = (ROOT / "gpt_magic.py").read_text(encoding="utf-8")
        gallery_template = (ROOT / "templates" / "favorite_gallery.html").read_text(encoding="utf-8")
        backend_required = [
            "def _normalize_favorite_batch_ids(",
            "def update_favorite_metadata_batch(",
            "def delete_favorite_items_batch(",
            "async def admin_batch_gallery_items(",
            "\"admin_gallery_batch\"",
            "app.router.add_post(\"/api/admin/gallery/batch\", self.admin_batch_gallery_items)",
            "admin_gallery_batch_update",
            "admin_gallery_batch_delete",
        ]
        for marker in backend_required:
            with self.subTest(marker=marker):
                self.assertIn(marker, gpt_magic_text)
        template_required = [
            "adminGallerySelectedIds: new Set()",
            "function adminGallerySelection()",
            "function adminVisibleGalleryIdSet()",
            "function adminSelectVisibleGalleryItems()",
            "function adminInvertVisibleGalleryItems()",
            "function adminBatchUpdateSelected()",
            "function adminBatchDeleteSelected()",
            "admin_gallery_batch",
            "adminBatchCharacter",
            "adminGallerySelectionCount",
            "adminSelectedGalleryIds(true)",
            "adminSyncGallerySelection(true)",
            "adminGallerySelection().clear()",
        ]
        for marker in template_required:
            with self.subTest(marker=marker):
                self.assertIn(marker, gallery_template)
        self.assertNotIn("const ids = adminSelectedGalleryIds();", gallery_template)

    def test_favorite_gallery_batch_store_updates_and_deletes_items(self):
        gpt_magic = _load_gpt_magic_for_store_tests()
        with tempfile.TemporaryDirectory() as tmp:
            store = gpt_magic.GptMagicStore(Path(tmp) / "data")
            favorite_root = store.favorite_dir()
            role_dir = favorite_root / "old-role"
            role_dir.mkdir(parents=True, exist_ok=True)
            first = role_dir / "first.png"
            second = role_dir / "second.png"
            first.write_bytes(b"first-image")
            second.write_bytes(b"second-image")
            first_id = store._favorite_item_id(first)
            second_id = store._favorite_item_id(second)

            normalized = store._normalize_favorite_batch_ids(
                f"{first_id}, {second_id} {first_id} stale-id",
                limit=3,
            )
            self.assertEqual(normalized, [first_id, second_id, "stale-id"])
            self.assertEqual(
                store._normalize_favorite_batch_ids([first_id, second_id], limit="bad-limit"),
                [first_id, second_id],
            )
            empty_update = store.update_favorite_metadata_batch([first_id], ["not", "a", "patch"])
            self.assertEqual(empty_update["requested"], 1)
            self.assertEqual(empty_update["updated"], 0)

            update = store.update_favorite_metadata_batch(
                [first_id, second_id, first_id, "missing-id"],
                {
                    "character": "new/role",
                    "maker": "  maker name  ",
                    "prompt": "  prompt text  ",
                    "summary": "  summary text  ",
                },
                limit=10,
            )
            self.assertEqual(update["requested"], 3)
            self.assertEqual(update["updated"], 2)
            self.assertEqual(update["not_found"], ["missing-id"])
            first_meta = json.loads(store._favorite_sidecar_path(first).read_text(encoding="utf-8"))
            self.assertEqual(first_meta["favorite_character"], "new_role")
            self.assertEqual(first_meta["maker_name"], "maker name")
            self.assertEqual(first_meta["prompt_preview"], "prompt text")
            self.assertEqual(first_meta["design_summary"], "summary text")

            delete = store.delete_favorite_items_batch([first_id, "missing-id"], limit=10)
            self.assertEqual(delete["requested"], 2)
            self.assertEqual(delete["deleted"], 1)
            self.assertEqual(delete["not_found"], ["missing-id"])
            self.assertFalse(first.exists())
            self.assertFalse(store._favorite_sidecar_path(first).exists())
            self.assertTrue(second.exists())

    def test_gallery_admin_password_is_generated_and_stable(self):
        gpt_magic = _load_gpt_magic_for_store_tests()
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = gpt_magic.GptMagicStore(data_dir)
            password = store.config()["gallery"]["admin_password"]
            account = store.gallery_accounts()["admin"]

            self.assertEqual(len(password), 16)
            self.assertNotIn(password, ("", "114514", "admin"))
            self.assertEqual(account["credential_epoch"], 1)
            self.assertTrue(
                store._verify_gallery_password(
                    password,
                    account["salt"],
                    account["password_hash"],
                    account["password_algo"],
                    account["password_iterations"],
                )
            )

            reloaded = gpt_magic.GptMagicStore(data_dir)
            self.assertEqual(reloaded.config()["gallery"]["admin_password"], password)

    def test_legacy_builtin_gallery_admin_rotates_only_once(self):
        gpt_magic = _load_gpt_magic_for_store_tests()
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = gpt_magic.GptMagicStore(data_dir)
            old_password = store.config()["gallery"]["admin_password"]
            store.gallery_accounts()["admin"].pop("credential_epoch")
            store.save()

            migrated = gpt_magic.GptMagicStore(data_dir)
            new_password = migrated.config()["gallery"]["admin_password"]
            account = migrated.gallery_accounts()["admin"]
            self.assertNotEqual(new_password, old_password)
            self.assertEqual(account["credential_epoch"], 1)
            self.assertFalse(
                migrated._verify_gallery_password(
                    old_password,
                    account["salt"],
                    account["password_hash"],
                    account["password_algo"],
                    account["password_iterations"],
                )
            )

            reloaded = gpt_magic.GptMagicStore(data_dir)
            self.assertEqual(reloaded.config()["gallery"]["admin_password"], new_password)

    def test_gallery_video_balance_billing_lifecycle(self):
        gpt_magic = _load_gpt_magic_for_store_tests()
        with tempfile.TemporaryDirectory() as tmp:
            store = gpt_magic.GptMagicStore(Path(tmp) / "data")
            # 注册一个待激活账号，再用 token 激活并绑定 QQ
            reg = store.create_pending_gallery_registration("videouser", "password123", owner_name="VU")
            token = reg["token"]
            account, status = store.activate_gallery_registration_token(token, owner_user_id="10001", owner_name="VU")
            self.assertEqual(status, "activated")
            # 激活即赠送默认试用额度
            self.assertAlmostEqual(account["balance"], gpt_magic.GALLERY_DEFAULT_TRIAL_BALANCE, places=2)
            self.assertTrue(account["trial_granted"])

            # 未注册的 QQ 扣费应失败
            ok, bal, code = store.consume_gallery_balance_by_owner("99999", 0.5, reason="t")
            self.assertFalse(ok)
            self.assertEqual(code, "not_registered")

            # 正常扣费 0.5
            ok, bal, code = store.consume_gallery_balance_by_owner("10001", 0.5, reason="local video")
            self.assertTrue(ok)
            self.assertEqual(code, "ok")
            self.assertAlmostEqual(bal, gpt_magic.GALLERY_DEFAULT_TRIAL_BALANCE - 0.5, places=2)

            # 失败退回
            refunded = store.refund_gallery_balance_by_owner("10001", 0.5, reason="refund")
            self.assertAlmostEqual(refunded, gpt_magic.GALLERY_DEFAULT_TRIAL_BALANCE, places=2)

            # 余额不足拦截：把价格设成超过余额
            ok, bal, code = store.consume_gallery_balance_by_owner("10001", 999.0, reason="too big")
            self.assertFalse(ok)
            self.assertEqual(code, "insufficient")

            # 管理员充值与设额度
            updated = store.adjust_gallery_balance("videouser", 10.0, reason="admin recharge")
            self.assertAlmostEqual(updated["balance"], gpt_magic.GALLERY_DEFAULT_TRIAL_BALANCE + 10.0, places=2)
            updated = store.set_gallery_balance("videouser", 3.0, reason="admin set")
            self.assertAlmostEqual(updated["balance"], 3.0, places=2)

            # 账号不能扣成负数
            ok, bal, code = store.consume_gallery_balance_by_owner("10001", 5.0, reason="over")
            self.assertFalse(ok)
            self.assertEqual(code, "insufficient")
            self.assertAlmostEqual(store.gallery_balance_by_owner("10001"), 3.0, places=2)

            # 流水有记录（试用赠送 + 消费 + 退回 + 充值 + 设置）
            ledger = store.gallery_balance_ledger("videouser", limit=50)
            kinds = {row["kind"] for row in ledger}
            self.assertIn("trial", kinds)
            self.assertIn("consume", kinds)
            self.assertIn("refund", kinds)
            self.assertIn("set", kinds)

            # 试用额度只发一次
            again = store.grant_gallery_trial_if_needed("videouser")
            self.assertAlmostEqual(again["balance"], 3.0, places=2)

    def test_api_video_config_store_roundtrip(self):
        gpt_magic = _load_gpt_magic_for_store_tests()
        with tempfile.TemporaryDirectory() as tmp:
            store = gpt_magic.GptMagicStore(Path(tmp) / "data")
            # 默认值
            conf = store.get_api_video_config()
            self.assertFalse(conf["enable"])
            self.assertEqual(conf["base_url"], "https://api.dealonhorizon.us")
            self.assertEqual(conf["model"], "grok-imagine-video-10s-720p")
            self.assertEqual(conf["aspect_ratio"], "auto")
            # 写入并回读
            updated = store.update_api_video_config({
                "enable": True,
                "base_url": "https://api.dealonhorizon.us",
                "api_key": "sk-secret-123456",
                "model": "grok-imagine-video-10s-720p",
                "max_images": 5,
                "timeout": 300,
                "duration": 8,
                "aspect_ratio": "9:16",
                "proxy": "http://127.0.0.1:7890",
                "prompt_enhance": "none",
                "default_prompt": "cinematic",
            })
            self.assertTrue(updated["enable"])
            self.assertEqual(updated["max_images"], 5)
            self.assertEqual(updated["duration"], 8)
            self.assertEqual(updated["aspect_ratio"], "9:16")
            self.assertEqual(updated["proxy"], "http://127.0.0.1:7890")
            self.assertEqual(updated["prompt_enhance"], "none")
            self.assertEqual(updated["api_key"], "sk-secret-123456")
            # 留空 api_key 不应清除（通过 patch 不含 api_key 模拟）
            again = store.update_api_video_config({"enable": False})
            self.assertEqual(again["api_key"], "sk-secret-123456")
            self.assertFalse(again["enable"])
            # 配置确实落到 gallery.api_video，运行时读取路径一致
            self.assertEqual(
                store.config()["gallery"]["api_video"]["base_url"],
                "https://api.dealonhorizon.us",
            )

    def test_video_billing_and_api_video_are_wired(self):
        text = _plugin_text()
        # 触发词与命令
        self.assertIn("酒狐来点魔法视频", text)
        self.assertIn("酒狐来点视频魔法", text)
        self.assertIn("_extract_plain_video_request", text)
        # 计费流程
        self.assertIn("_video_billing_precheck", text)
        self.assertIn("_video_billing_consume", text)
        self.assertIn("_video_billing_refund", text)
        # Grok / API 1.8 视频客户端
        self.assertIn("_generate_api_video_file", text)
        self.assertIn("_run_openai_video_job", text)
        self.assertIn("/v1/generate", text)
        self.assertIn("/v1/tasks", text)
        self.assertIn("/file", text)
        self.assertIn("task_id", text)
        self.assertIn("file_url", text)
        self.assertIn("input_reference", text)
        # API 1.8：Grok 参考图用顶层 input_reference，非 Grok 参考图放 messages.image_url。
        self.assertIn('payload["input_reference"] = data_urls[0]', text)
        self.assertIn('[{"url": u} for u in data_urls]', text)
        self.assertIn("_api_video_messages", text)
        self.assertIn('"messages": self._api_video_messages(prompt, data_urls)', text)
        self.assertIn("_api_video_aspect_ratio", text)
        self.assertIn("_api_video_oriented_model", text)
        self.assertIn('"aspect_ratio"', text)
        self.assertIn('"video_config"', text)
        self.assertIn('"720x1280"', text)
        self.assertIn('"1280x720"', text)
        self.assertIn("source_image_paths=api_source_paths if is_api_mode else None", text)
        self.assertIn('"source_image_count": len(source_image_paths)', text)
        self.assertIn("_poll_openai_video_job", text)
        self.assertIn("_api_video_join_download_url", text)
        self.assertIn("_api_video_proxy", text)
        self.assertIn("_build_api_video_prompt", text)
        self.assertIn("api_video_path_fallback", text)
        # 防回归：API 视频解析依赖的方法必须真实定义（曾因重构丢失 def 导致每次调用 AttributeError）
        self.assertIn("def _extract_grok_video_url", text)
        self.assertIn("def _extract_chat_text_content", text)
        self.assertNotIn("_finalize_generation_queue_entry", text)
        self.assertIn("_finish_generation_queue_entry", text)
        # 失败时必须记录第三方原始响应，便于诊断
        self.assertIn("/v1/generate 创建响应", text)
        # 余额查询命令
        self.assertIn("_handle_gallery_balance_query", text)

    def test_video_send_inline_with_fallback_is_wired(self):
        text = _plugin_text()
        # 统一发送入口 + 内联视频段 + 多级降级
        self.assertIn("_deliver_video", text)
        self.assertIn("_send_video_inline_aiocqhttp", text)
        self.assertIn("_video_inline_send_enabled", text)
        self.assertIn('"type": "video"', text)
        self.assertIn("video_i2v_inline_send", text)
        # 降级仍保留文件上传与本地路径兜底
        self.assertIn("_build_video_generation_chain", text)
        self.assertIn("文件已保存在本机", text)
        # schema 暴露开关
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("inline_send", schema["video_settings"]["items"])
        # API 视频使用独立信息卡，不复用本地 Wan 工作流/模型字段
        self.assertIn("_format_api_video_generation_info", text)
        self.assertIn("魔法视频结果", text)
        self.assertIn("prompt_enhance", text)

    def test_wan_i2v_safe_defaults_and_burn_guards_are_wired(self):
        schema_text = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        schema = json.loads(schema_text)
        video_items = schema["video_settings"]["items"]
        video_block = schema_text[
            schema_text.index('"video_settings"'):
            schema_text.index('"model_family_settings"')
        ]
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        common_text = (ROOT / "features" / "common.py").read_text(encoding="utf-8")
        image_audit_text = (ROOT / "features" / "image_audit.py").read_text(encoding="utf-8")

        self.assertEqual(video_items["workflow"]["default"], "wan_i2v_api.json")
        self.assertEqual(video_block.count('"default_prompt"'), 1)
        self.assertIn("镜头缓缓推进", video_items["default_prompt"]["default"])

        negative_prompt = video_items["negative_prompt"]["default"]
        for marker in ("过饱和", "色彩断层", "色彩漂移", "荧光伪影"):
            with self.subTest(marker=marker):
                self.assertIn(marker, negative_prompt)
        self.assertIn("WAN_I2V_NEGATIVE_PROMPT", common_text)
        self.assertIn("self.video_i2v_negative_prompt", image_audit_text)
        self.assertIn('"negative_prompt": self.video_i2v_negative_prompt', image_audit_text)

        for text in (main_text, common_text):
            self.assertIn('WAN_I2V_WORKFLOW = "wan_i2v_api.json"', text)
        self.assertIn("def _stabilize_i2v_feedback_frame(", main_text)
        self.assertIn("feedback_frames_clamped", main_text)
        self.assertIn("self._stabilize_i2v_feedback_frame", main_text)
        self.assertIn("wan_i2v_quality_defaults_8_5_4", main_text)
        self.assertIn("wan_i2v_safe_defaults_8_5_5", main_text)
        self.assertIn('current_workflow == "wan_i2v_quality.json"', main_text)
        self.assertIn('video_conf["workflow"] = _video_default("workflow", WAN_I2V_WORKFLOW)', main_text)
        self.assertIn('"41", "41.0", "49", "49.0"', main_text)
        self.assertIn('video_conf["fps"] = _video_default("fps", 16)', main_text)

    def test_favorite_gallery_balance_admin_ui_is_wired(self):
        html = (ROOT / "templates" / "favorite_gallery.html").read_text(encoding="utf-8")
        self.assertIn('data-admin-tab="balance"', html)
        self.assertIn("renderAdminBalance", html)
        self.assertIn("adminRecharge", html)
        self.assertIn("adminSaveBilling", html)
        self.assertIn("adminBatchBalance", html)
        self.assertIn("billingTrialBalance", html)
        self.assertIn("adminBalanceBatchUsers", html)
        self.assertIn("/balance", html)
        self.assertIn("adminSaveVideoConfig", html)
        self.assertIn("videoCfgApiKey", html)
        self.assertIn("videoCfgAspectRatio", html)
        gpt_text = (ROOT / "gpt_magic.py").read_text(encoding="utf-8")
        self.assertIn("admin_adjust_balance", gpt_text)
        self.assertIn("admin_batch_balance", gpt_text)
        self.assertIn("admin_save_billing", gpt_text)
        self.assertIn("/api/admin/accounts/{username}/balance", gpt_text)
        self.assertIn("/api/admin/balance/batch", gpt_text)
        self.assertIn("/api/admin/billing", gpt_text)
        self.assertIn("admin_save_video_config", gpt_text)
        self.assertIn("update_api_video_config", gpt_text)


    def test_lora_admin_unrecognized_and_batch_management_are_wired(self):
        gpt_magic_text = (ROOT / "gpt_magic.py").read_text(encoding="utf-8")
        gallery_template = (ROOT / "templates" / "favorite_gallery.html").read_text(encoding="utf-8")
        backend_required = [
            "def _admin_lora_virtual_profile(",
            "def _admin_lora_rows_for_family(",
            "def _normalize_admin_lora_batch_items(",
            "async def admin_batch_lora_profiles(",
            "\"admin_lora_batch\"",
            "app.router.add_post(\"/api/admin/lora/profiles/batch\", self.admin_batch_lora_profiles)",
            "created_from\": \"web_admin_unrecognized\"",
            "admin_lora_batch_update",
            "admin_lora_batch_delete",
        ]
        for marker in backend_required:
            with self.subTest(marker=marker):
                self.assertIn(marker, gpt_magic_text)
        template_required = [
            "adminLoraSelectedIds: new Set()",
            "adminRefreshLoraProfiles()",
            "adminSelectedLoraIds(true)",
            "adminSyncLoraSelection(true)",
            "adminSelectedLoraBatchItems()",
            "adminBatchUpdateSelectedLoras()",
            "adminBatchDeleteSelectedLoras()",
            "admin-lora-status",
            "未识别",
            "refresh=1",
            "adminLoraSelection().clear()",
        ]
        for marker in template_required:
            with self.subTest(marker=marker):
                self.assertIn(marker, gallery_template)

    def test_lora_admin_can_materialize_unrecognized_profile(self):
        gpt_magic = _load_gpt_magic_for_store_tests()

        class DummyProfileStore:
            def __init__(self):
                self.data = {}

            def load(self):
                return dict(self.data)

            def save(self, data):
                self.data = dict(data or {})

        class DummyOwner:
            active_model_family = "illustrious"

            def __init__(self):
                self.store = DummyProfileStore()
                self.alias_map = {}
                self.prompt_map = {}

            def _get_lora_profile_store(self, family=None):
                return self.store

            def _normalize_model_family(self, family):
                return "illustrious" if str(family or "").lower().startswith("ill") else "anima"

            def _family_label(self, family):
                return "Illustrious" if family == "illustrious" else "Anima"

            def _normalize_profile_match_key(self, value):
                return re.sub(r"[^0-9a-z]+", "", str(value or "").lower())

            def _normalize_lora_lookup_key(self, value):
                return re.sub(r"\.(safetensors|ckpt|pt|pth|bin)$", "", Path(str(value or "")).name, flags=re.I).lower()

            def _normalize_alias_key(self, value):
                return str(value or "").strip().lower()

            def _normalize_lora_profile_runtime(self, profile, family=None, all_profiles=None):
                return dict(profile or {})

            def _paintable_role_cjk_display_name(self, key, profile, fallback):
                return fallback or key

            def _profile_identity_conflict_reason(self, key, profile):
                return ""

            def _all_profile_lora_keys(self):
                keys = set()
                for profile in self.store.data.values():
                    key = self._normalize_lora_lookup_key(profile.get("lora") or "")
                    if key:
                        keys.add(key)
                return keys

            def _is_non_character_lora_model_name(self, name):
                return False

            def _lora_alias_store_for_family(self, family=None):
                return Path("dummy_aliases.json")

            def _lora_prompt_store_for_family(self, family=None):
                return Path("dummy_prompts.json")

            def _load_json_map_file(self, path):
                return dict(self.prompt_map if "prompt" in str(path) else self.alias_map)

            def _save_json_map_file(self, path, data):
                if "prompt" in str(path):
                    self.prompt_map = dict(data or {})
                else:
                    self.alias_map = dict(data or {})

        owner = DummyOwner()
        webui = gpt_magic.FavoriteGalleryWebUI(types.SimpleNamespace(store=types.SimpleNamespace(), owner=owner))
        rows = webui._admin_lora_rows_for_family(owner, "illustrious", {}, live_loras=["LingyangIllus.safetensors"])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["unrecognized"])
        self.assertEqual(rows[0]["status_label"], "未识别")

        updated, error = webui._apply_admin_lora_profile_patch(
            owner,
            "illustrious",
            rows[0]["key"],
            {"character": "Lingyang", "disabled": True, "disabled_reason": "整理中"},
            context={"username": "admin"},
            lora_name="LingyangIllus.safetensors",
        )
        self.assertFalse(error)
        self.assertTrue(updated["created"])
        self.assertIn("lingyang", owner.store.data)
        self.assertEqual(owner.store.data["lingyang"]["lora"], "LingyangIllus.safetensors")
        self.assertTrue(owner.store.data["lingyang"]["disabled"])

    def test_self_update_request_hook_sends_feedback(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        match = re.search(
            r"async def inject_system_prompt\(self, event: AstrMessageEvent, req\):(?P<body>.*?)\n\s+def _clean_pic_tags_from_req",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        required = [
            "self_update_raw = next(",
            "event.set_extra(\"comfy_self_update_handled\", True)",
            "self._handle_self_update_command(event, self_update_raw)",
            "context=\"self_update_llm_request\"",
        ]
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, body)
        early_block = body.split("if (", 1)[1].split("):", 1)[0]
        self.assertNotIn("_is_self_update_trigger", early_block)

    def test_targeted_self_update_version_is_wired(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        feature_text = (ROOT / "features" / "self_update.py").read_text(encoding="utf-8")
        combined = main_text + "\n" + feature_text

        required = [
            "def _parse_self_update_target_version(",
            "def _self_update_target_ref_candidates(",
            'target_version=target_version',
            '"target_version": target_version',
            'f"v{version}"',
            '"target_ref_candidates"',
            'target_version = str(target_version or "").strip()',
            'last_result["target_version"] = target_version',
            'last_result["target_ref"] = target_ref',
            'target_ref=branch',
            '"酒狐强制更新"',
            '"jh强制更新"',
            "酒狐强制更新 8.0.0",
            r"v?\d+(?:\.\d+){1,5}",
        ]
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, combined)

        image_audit_match = re.search(
            r"def _is_image_audit_trigger\(self, raw: str, event: AstrMessageEvent = None\) -> bool:(?P<body>.*?)\n\s+def _is_self_update_tail",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(image_audit_match)
        self.assertNotIn("酒狐强制更新", image_audit_match.group("body"))

        self_update_match = re.search(
            r"def _is_self_update_trigger\(self, raw: str, event: AstrMessageEvent = None\) -> bool:(?P<body>.*?)\n\s+def _is_wake_stripped_gpt_magic_trigger",
            main_text,
            flags=re.S,
        )
        self.assertIsNotNone(self_update_match)
        self.assertIn("酒狐强制更新", self_update_match.group("body"))
        self.assertIn("jh强制更新", self_update_match.group("body"))

    def test_llm_endpoint_resolution_rejects_key_as_url(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        plugin_text = main_text + "\n" + _feature_text()
        required = [
            "from urllib.parse import quote, urlparse",
            "def _resolve_llm_chat_endpoint(",
            "def _normalize_configured_llm_pair(",
            "api_base/api_key appear swapped; using runtime swap",
            "config auto-corrected",
            "api_base looks like an API key",
            '"/v1/chat/completions"',
            "if not self._valid_http_url(url):",
            "invalid LLM endpoint",
            "def _llm_empty_response_should_retry(",
            '"choices=0"',
            "returned non-retryable empty response",
            'self._primary_llm_endpoint("Wan I2V Prompt Text")',
            'self._vision_llm_endpoint("Wan I2V Source Vision")',
            'self._utility_llm_endpoint("LLM JSON")',
            'self._look_vision_endpoint("JiuHu Look Vision")',
        ]
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, plugin_text)
        stale_direct_calls = [
            "session.post(self._vision_chat_completions_url()",
            "self._utility_llm_chat_completions_url(),",
            "self._look_vision_chat_completions_url(),",
        ]
        for marker in stale_direct_calls:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, main_text)

    def test_key_json_files_parse(self):
        paths = [
            "_conf_schema.json",
            "zayu_messages_template.json",
            "workflow/finish_illustrious_api.json",
            "workflow/finish_illustrious_hq_safe_api.json",
            "workflow/finish_illustrious_multi_lora_regional_api.json",
            "workflow/finish_illustrious_multi_lora_api.json",
            "workflow/finish_illustrious_multi_lora_staged_base_api.json",
            "workflow/img2img_illustrious_multi_lora_staged_api.json",
        ]
        for rel in paths:
            with self.subTest(path=rel):
                json.loads((ROOT / rel).read_text(encoding="utf-8"))

    def test_regional_multi_lora_hook_topology_is_isolated(self):
        workflow = json.loads((ROOT / "workflow" / "finish_illustrious_multi_lora_regional_api.json").read_text(encoding="utf-8"))
        comfy_text = (ROOT / "comfyui_api.py").read_text(encoding="utf-8")
        schema_text = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")

        self.assertIn("v6.0.2", json.dumps(workflow.get("1", {}), ensure_ascii=False))
        role_prompts = [("33", "111", "101"), ("34", "112", "102"), ("35", "113", "103")]
        for prompt_id, clip_hook_id, hook_id in role_prompts:
            with self.subTest(prompt=prompt_id):
                self.assertEqual(workflow[prompt_id]["inputs"]["clip"], [clip_hook_id, 0])
                self.assertEqual(workflow[clip_hook_id]["class_type"], "SetClipHooks")
                self.assertEqual(workflow[clip_hook_id]["inputs"]["clip"], ["2", 0])
                self.assertEqual(workflow[clip_hook_id]["inputs"]["hooks"], [hook_id, 0])
                self.assertTrue(workflow[clip_hook_id]["inputs"]["apply_to_conds"])
                self.assertFalse(workflow[clip_hook_id]["inputs"]["schedule_clip"])

        pair_nodes = [("50", "33", "36", "101"), ("51", "34", "37", "102"), ("52", "35", "38", "103")]
        for pair_id, pos_id, neg_id, hook_id in pair_nodes:
            with self.subTest(pair=pair_id):
                inputs = workflow[pair_id]["inputs"]
                self.assertEqual(inputs["positive_NEW"], [pos_id, 0])
                self.assertEqual(inputs["negative_NEW"], [neg_id, 0])
                self.assertEqual(inputs["hooks"], [hook_id, 0])
                self.assertEqual(inputs["strength"], 1.0)
                self.assertEqual(inputs["set_cond_area"], "default")

        required_code_markers = [
            "overlap_ratio=0.18 if interaction_intent else 0.08",
            "blur_radius=16.0",
            "binary=False",
            "multi_lora_role_bindings",
            "negative_prompt_node_ids",
            "set_clip_inputs[\"schedule_clip\"] = False",
            "inputs[\"negative_NEW\"] = [str(neg_id), 0]",
            "inputs[\"set_cond_area\"] = \"default\"",
            "multi_lora_debug_masks",
            "multi_lora_debug_workflow_path",
        ]
        for marker in required_code_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, comfy_text + schema_text)

    def test_densediffusion_multi_lora_default_is_wired(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        comfy_text = (ROOT / "comfyui_api.py").read_text(encoding="utf-8")
        schema_text = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")

        required_markers = [
            "async def _apply_multi_lora_densediffusion(",
            "\"DenseDiffusionAddCondNode\"",
            "\"DenseDiffusionApplyNode\"",
            "meta[\"multi_lora_densediffusion\"] = True",
            "ComfyUI_densediffusion",
            "不会自动回退到 Safe Chain",
            "if mode in {\"auto\", \"densediffusion\"}:",
            "if mode == \"regional_lora\":",
            "if mode == \"safe_chain\":",
            "\"dd\": \"densediffusion\"",
            "multi_lora_densediffusion_expected",
            "def _restore_safe_chain_role_prompts(",
            "multi_lora_safe_chain_role_prompts_restored",
            "self._restore_safe_chain_role_prompts(workflow, meta, valid_entries)",
            "def _prune_multi_lora_legacy_regional_nodes(",
            "\"ConditioningSetMaskAndCombine\"",
            "self._prune_multi_lora_legacy_regional_nodes(workflow, meta, reason=\"densediffusion\")",
            "self._prune_multi_lora_legacy_regional_nodes(workflow, meta, reason=\"safe_chain\")",
            "\"ConditioningConcat\"",
            "role_identity_prompt",
            "role_identity_prompt = char_prompt",
            "role_aux_prompt",
            "dd_mask_overlap_ratio = 0.0",
            "global_dd_strength = 1.0",
            "binary=True",
            "AstrBot DD Global Prompt",
            "multi_lora_dd_global_prompt_node",
            "multi_lora_dd_global_prompt",
            "multi_lora_dd_global_strength",
            "multi_lora_dd_mask_overlap_ratio",
            "\"conditioning\": [str(global_prompt_id), 0]",
            "AstrBot DD Role Aux Prompt",
            "AstrBot DD Role Split Conditioning",
            "multi_lora_dd_role_prompt_mode",
            "\"split_conditioning_concat\"",
            "conditioning_ref = (",
            "\"strength\": 1.0",
            "multi_lora_dd_anti_sheet_negative",
            "dd_anti_sheet_negative",
            "side by side comparison",
            "single coherent scene, characters interacting in same space, shared environment",
            "strip_profile_outfit",
            "if multi_intent and len(multi_matches) >= 2:",
            "or len(early_multi_matches) >= 2",
            "AstrBot DD Compact Negative",
            "dd_compact_negative",
            "def _densediffusion_base_sampler_node_ids(",
            "def _repeat_conditioning_with_concat(",
            "AstrBot DD Repeated Negative",
            "dd_sampler_negative_ref",
            "dd_sampler_ids = self._densediffusion_base_sampler_node_ids(workflow)",
            "inputs[\"negative\"] = copy.deepcopy(dd_sampler_negative_ref)",
            "multi_lora_dd_negative_node",
            "multi_lora_dd_sampler_ids",
            "def _workflow_has_layerdiffuse_attention(",
            "layerdiffuse_attention_sharing",
            "multi_lora_mode_requested",
            "mode = \"safe_chain\"",
        ]
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, main_text + comfy_text + schema_text)
        self.assertNotIn("multi_lora_dd_fallback", comfy_text)
        self.assertNotIn("DenseDiffusion fallback to Safe Chain", comfy_text)
        self.assertNotIn("hook_meta[\"multi_lora_regional_fallback\"]", comfy_text)

        dd_block = comfy_text[
            comfy_text.index("async def _apply_multi_lora_densediffusion("):
            comfy_text.index("async def _apply_multi_lora_safe_chain_override(")
        ]
        self.assertNotIn("shared interaction pose", dd_block)
        self.assertNotIn("close to the other character", dd_block)
        self.assertNotIn("distinct character design", dd_block)
        self.assertNotIn("distinct outfit", dd_block)
        self.assertNotIn("only this character in this region", dd_block)
        self.assertNotIn("single character area", dd_block)
        self.assertNotIn("({char_prompt}:1.1)", dd_block)

    def test_llm_composition_director_is_wired_for_multi_lora(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        llm_items = schema["llm_settings"]["items"]

        self.assertTrue(llm_items["enable_llm_composition_director"]["default"])
        self.assertTrue(llm_items["llm_composition_director_multi_lora_only"]["default"])
        self.assertEqual(llm_items["llm_composition_director_max_tokens"]["default"], 900)

        required_markers = [
            "def _llm_composition_director_available(",
            "async def _run_llm_composition_director(",
            "def _apply_llm_composition_director_plan_to_multi_lora(",
            "role_prompts:array of {index:number, position:string, prompt:string}",
            "director_plan = await self._run_llm_composition_director(",
            "global_prompt, _director_applied = self._apply_llm_composition_director_plan_to_multi_lora(",
            "profile_meta[\"llm_composition_negative_prompt\"]",
            "\"interaction_intent\": bool(",
            "\"global_prompt\": str((profile_meta or {}).get(\"multi_lora_global_prompt\") or \"\").strip()",
        ]
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, main_text)

    def test_feature_common_exports_internal_helpers(self):
        common_text = (ROOT / "features" / "common.py").read_text(encoding="utf-8")
        image_audit_text = (ROOT / "features" / "image_audit.py").read_text(encoding="utf-8")

        self.assertIn("__all__", common_text)
        self.assertIn("def _safe_float_value(", common_text)
        self.assertIn("def _safe_probability_value(", common_text)
        self.assertIn("_safe_float_value(", image_audit_text)
        self.assertIn("_safe_probability_value(", image_audit_text)

    def test_comfyui_self_signed_https_bypass_is_wired(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        comfy_text = (ROOT / "comfyui_api.py").read_text(encoding="utf-8")

        self.assertIn("server_ssl_verify", schema)
        self.assertFalse(schema["server_ssl_verify"]["default"])
        self.assertIn("def _comfyui_client_session(", main_text)
        self.assertIn("aiohttp.TCPConnector(ssl=False)", main_text)
        self.assertIn("async with self._comfyui_client_session(timeout) as session:", main_text)
        self.assertIn("def _client_session(", comfy_text)
        self.assertIn("self.server_ssl_verify = _safe_bool(self.config.get(\"server_ssl_verify\", False), False)", comfy_text)
        self.assertIn("async with self._client_session(timeout) as session:", comfy_text)

    def test_comfyui_lora_bridge_is_wired(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        comfy_text = (ROOT / "comfyui_api.py").read_text(encoding="utf-8")
        bridge_text = (ROOT / "comfyui_custom_nodes" / "ComfyUI-AstrBot-LoraBridge" / "__init__.py").read_text(encoding="utf-8")
        setup_bat_text = (ROOT / "scripts" / "astrbot_comfyui_setup.bat").read_text(encoding="utf-8")
        script_names = sorted(path.name for path in (ROOT / "scripts").glob("*") if path.is_file())
        docs_text = (ROOT / "docs" / "comfyui_migration_extras.md").read_text(encoding="utf-8")

        civitai_items = schema["civitai_settings"]["items"]
        self.assertIn("lora_download_mode", civitai_items)
        self.assertIn("comfyui_bridge_token", civitai_items)
        self.assertIn("comfyui_bridge_endpoint", civitai_items)
        self.assertIn("comfyui_bridge_proxy", civitai_items)
        self.assertIn("comfyui_bridge_timeout", civitai_items)
        self.assertIn("comfyui_bridge_connect_timeout", civitai_items)
        self.assertIn("comfyui_bridge_sock_read_timeout", civitai_items)
        self.assertIn("comfyui_bridge_fetch_timeout", civitai_items)
        self.assertIn("network_max_retries", civitai_items)
        self.assertIn("proxy_circuit_breaker", civitai_items)
        self.assertIn("proxy_circuit_breaker_cooldown", civitai_items)
        self.assertIn("def _format_comfyui_bridge_error(", main_text)
        self.assertIn("def _normalize_comfyui_bridge_proxy(", main_text)
        self.assertIn("def _probe_comfyui_lora_bridge_external_fetch(", main_text)
        self.assertIn("self.comfyui_bridge_proxy", main_text)
        self.assertIn("def _download_lora_via_comfyui_bridge(", main_text)
        self.assertIn("def _check_comfyui_lora_bridge_status(", main_text)
        self.assertIn("def _should_route_lora_source_requests_via_bridge(", main_text)
        self.assertIn("def _fetch_lora_source_via_comfyui_bridge(", main_text)
        self.assertIn("def _lora_download_dir_unavailable_note(", main_text)
        self.assertIn("using cached checkpoint list", main_text)
        self.assertIn("checkpoint_check_skipped", main_text)
        self.assertIn("代理出网", main_text)
        self.assertIn("\"X-AstrBot-Bridge-Token\"", main_text)
        self.assertIn("def _comfyui_bridge_headers(", main_text)
        self.assertIn("request_headers = self._comfyui_bridge_headers()", main_text)
        self.assertIn("headers = self._comfyui_bridge_headers()", main_text)
        self.assertIn("if token:", main_text)
        self.assertNotIn("未配置 civitai_settings.comfyui_bridge_token", main_text)
        self.assertIn("civitai_settings.comfyui_bridge_proxy", main_text + docs_text)
        self.assertIn("http://127.0.0.1:7890", main_text + docs_text)
        self.assertIn("http://CLASH_LAN_IP:7890", main_text + docs_text)
        self.assertIn("/comfy_civitai bridge", main_text)
        self.assertIn("BRIDGE_VERSION = \"1.4.2\"", bridge_text)
        self.assertIn("import ipaddress", bridge_text)
        self.assertIn("@routes.post(\"/astrbot/lora/fetch\")", bridge_text)
        self.assertIn("@routes.post(\"/astrbot/lora/download\")", bridge_text)
        self.assertIn("@routes.post(\"/astrbot/subject-audit\")", bridge_text)
        self.assertIn("@routes.get(\"/astrbot/lora/task/{task_id}\")", bridge_text)
        self.assertIn("DEFAULT_ALLOWED_HOSTS", bridge_text)
        self.assertIn("github-releases.githubusercontent.com", bridge_text)
        self.assertIn("request_resource_download_supported", bridge_text)
        self.assertIn("subject_audit_supported", bridge_text)
        self.assertIn("subject_audit_endpoint", bridge_text)
        self.assertIn("allow_local_requests_without_token", bridge_text)
        self.assertIn("def _request_is_local_or_private(", bridge_text)
        self.assertIn("return bool(ip.is_loopback)", bridge_text)
        self.assertNotIn('request.query.get("token")', bridge_text)
        self.assertNotIn('payload.get("token")', bridge_text)
        self.assertIn("if not _host_resolves_public(url):", bridge_text)
        self.assertIn(
            "trace_configs=_redirect_trace_configs(require_allowlisted=True)",
            bridge_text,
        )
        self.assertIn("local_auth_bypass", bridge_text)
        self.assertIn("TARGET_TYPE_ALIASES", bridge_text)
        self.assertIn("controlnet_aux_ckpts", bridge_text)
        self.assertIn("ultralytics_bbox", bridge_text)
        self.assertIn("ultralytics_segm", bridge_text)
        self.assertIn('"ultralytics_bbox": ("ultralytics/bbox",)', bridge_text)
        self.assertIn('"ultralytics_segm": ("ultralytics/segm",)', bridge_text)
        self.assertIn("YOLO_MODEL_CACHE", bridge_text)
        self.assertIn("face_yolov8m.pt", bridge_text)
        self.assertIn("person_yolov8s-seg.pt", bridge_text)
        self.assertIn("target_type", main_text + bridge_text)
        self.assertIn("def _safe_int(", bridge_text)
        self.assertIn("proxy must be an http(s) URL or host:port", bridge_text)
        self.assertIn("proxy_configured", bridge_text)
        self.assertIn("request_proxy_supported", bridge_text)
        self.assertIn("request_fetch_supported", bridge_text)
        self.assertIn("max_concurrent_downloads", bridge_text)
        self.assertIn("proxy_cooldown", bridge_text)
        self.assertIn("_generation_poll_delay", comfy_text)
        self.assertIn("view_download_retries", comfy_text)
        self.assertIn('encoding="utf-8-sig"', bridge_text)
        self.assertIn("X-AstrBot-Bridge-Token", bridge_text)
        self.assertEqual(["astrbot_comfyui_setup.bat"], script_names)
        self.assertIn("ComfyUI-AstrBot-LoraBridge", setup_bat_text)
        self.assertIn("function Write-EmbeddedLoraBridge", setup_bat_text)
        self.assertIn("[内置] 未找到本地 Bridge 源码，写入内嵌版本", setup_bat_text)
        self.assertNotIn("fetching from GitHub", setup_bat_text)
        self.assertIn("__ASTRBOT_POWERSHELL_BELOW__", setup_bat_text)
        self.assertIn(".LastIndexOf($marker)", setup_bat_text)
        self.assertIn("[Text.UTF8Encoding]::new($true)", setup_bat_text)
        self.assertIn('-File "%ASTRBOT_SETUP_TMP%" %*', setup_bat_text)
        self.assertIn("scripts\\astrbot_comfyui_setup.bat", docs_text)
        self.assertIn("[switch]$UseClashLan", setup_bat_text)
        self.assertIn("[string]$ClashHost", setup_bat_text)
        self.assertIn("[int]$ClashPort = 7890", setup_bat_text)
        self.assertIn("function Resolve-BridgeProxy", setup_bat_text)
        self.assertIn("[switch]$InstallLoraBridge", setup_bat_text)
        self.assertIn("[string]$BridgeProxy", setup_bat_text)
        self.assertIn("function Resolve-DownloadProxy", setup_bat_text)
        self.assertIn("git -c \"http.proxy=$proxy\" -c \"https.proxy=$proxy\" clone", setup_bat_text)
        self.assertIn("request_proxy_supported", setup_bat_text)
        self.assertIn("request_fetch_supported", setup_bat_text)
        self.assertIn("max_concurrent_downloads", setup_bat_text)
        self.assertIn("function Install-LoraBridge", setup_bat_text)
        self.assertIn("function Install-LitePackage", setup_bat_text)
        self.assertIn("function Install-FullPackage", setup_bat_text)
        self.assertIn("function Invoke-MainMenu", setup_bat_text)
        self.assertIn("按 Enter 返回主菜单", setup_bat_text)
        self.assertIn("Get-SafetensorsFileProblem", setup_bat_text)
        self.assertIn("Get-GgufFileProblem", setup_bat_text)
        self.assertIn("自动尝试 hf-mirror.com 镜像", setup_bat_text)
        self.assertIn("自动尝试 Hugging Face 直连", setup_bat_text)
        self.assertIn("--fail", setup_bat_text)
        self.assertIn("MinGB = 8.5", setup_bat_text)
        self.assertIn("下面是最终检测汇总", setup_bat_text)
        self.assertIn("按 Enter 返回主菜单", setup_bat_text)
        self.assertIn("安装/刷新 Resource Bridge", setup_bat_text)
        self.assertIn("工作流校验失败：缺少模型文件", comfy_text)
        self.assertIn("_format_validation_missing_model_error", comfy_text)
        self.assertIn("Depth-Anything-V2-Large", setup_bat_text + docs_text)
        self.assertIn("depth_anything_v2_vitl.pth", setup_bat_text + docs_text)
        self.assertIn("BRIDGE_VERSION = \"1.4.1\"", setup_bat_text)
        self.assertIn("import ipaddress", setup_bat_text)
        self.assertIn("request_resource_download_supported", setup_bat_text)
        self.assertIn("subject_audit_supported", setup_bat_text)
        self.assertIn("allow_local_requests_without_token", setup_bat_text)
        self.assertIn("def _request_is_local_or_private(", setup_bat_text)
        self.assertIn("local_auth_bypass", setup_bat_text)
        self.assertIn("controlnet_aux_ckpts", setup_bat_text)
        self.assertIn("ultralytics_bbox", setup_bat_text)
        self.assertIn("ultralytics_segm", setup_bat_text)
        self.assertIn('"ultralytics_bbox": ("ultralytics/bbox",)', setup_bat_text)
        self.assertIn('"ultralytics_segm": ("ultralytics/segm",)', setup_bat_text)
        self.assertIn('encoding="utf-8-sig"', setup_bat_text)
        self.assertIn("安装/刷新 Resource Bridge", setup_bat_text)
        self.assertIn("civitai_settings.lora_download_mode = comfyui_bridge", docs_text)
        self.assertIn("-UseClashLan -ClashHost", docs_text)

    def test_staged_multi_lora_wiring_is_current(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        comfy_text = (ROOT / "comfyui_api.py").read_text(encoding="utf-8")
        schema_text = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        base_workflow = (ROOT / "workflow" / "finish_illustrious_multi_lora_staged_base_api.json").read_text(encoding="utf-8")
        inpaint_workflow = (ROOT / "workflow" / "img2img_illustrious_multi_lora_staged_api.json").read_text(encoding="utf-8")

        required_main_markers = [
            "ILLUSTRIOUS_MULTI_LORA_STAGED_INPAINT_DENOISE = 0.90",
            "ILLUSTRIOUS_MULTI_LORA_STAGED_CONTROL_STRENGTH = 0.16",
            "ILLUSTRIOUS_MULTI_LORA_STAGED_MASK_OVERLAP = 0.06",
            "ILLUSTRIOUS_MULTI_LORA_STAGED_MASK_BLUR = 6",
            "ill.setdefault(\"multi_lora_mode\", \"densediffusion\")",
            "migration_marker = \"densediffusion_default_7_1_7\"",
            "ill_conf[\"multi_lora_mode\"] = \"densediffusion\"",
            "mode = \"densediffusion\"",
            "def _migrate_illustrious_multi_lora_staged_defaults_if_stale(",
            "\"multi_lora_staged_inpaint_denoise\": ((0.46, 0.52, 0.70), ILLUSTRIOUS_MULTI_LORA_STAGED_INPAINT_DENOISE)",
            "boundary_chars = set(\"的了着給给畫画绘繪看要想找搜选選用换換抽点點来來和跟与與及同\")",
            "def _staged_multi_lora_base_dimensions(",
            "width, height = self._staged_multi_lora_base_dimensions(base_workflow)",
            "def _composite_staged_pass_onto_base(",
            "PILImage.composite(pass_img, base_img, mask_img)",
            "\"staged_pixel_composite\"",
            "def _select_available_illustrious_checkpoint(self, names: list, candidates: list = None) -> str:",
            "async def _ensure_illustrious_runtime_checkpoint_available(self, source_image_path: str = None) -> str:",
            "await self._ensure_illustrious_runtime_checkpoint_available(source_image_path=source_image_path)",
            "await self._ensure_illustrious_runtime_checkpoint_available(source_image_path=None)",
            "def _staged_multi_lora_prompt_parts_v2(self, prompt: str, profile_meta: dict, entries: list) -> tuple:",
            "\"empty scene, background only, environment only, unobstructed background\"",
            "\"(solo:1.3)\"",
            "\"(single character in assigned masked region:1.25)\"",
            "f\"({role_prompt}:1.15)\" if role_prompt else \"\"",
            "fully inside the left half, face centered in the left third",
            "one complete separate body",
            "user_action_tags = []",
            "\"staged_multi_lora_base\": True",
            "\"staged_multi_lora_base_background_only\": True",
            "\"staged_multi_lora_base_regional\": False",
            "\"base_region_prompts\": role_prompts",
            "async def _apply_staged_multi_lora_base_regional_conditioning(",
            "\"ConditioningSetMask\"",
            "\"ConditioningCombine\"",
            "staged_base_regional_conditioning",
            "staged_base_background_only",
            "staged_base_global_scene_prompt",
            "self._staged_multi_lora_base_negative_prompt(negative_prompt)",
            "single centered body, one centered subject, merged face, fused face",
            "for attempt in range(2):",
            "\"control_strength\": control_strength",
            "multi_lora_staged_control_strength",
            "def _set_controlnet_strength(self, workflow: dict, strength: float) -> int:",
            "control_strength_nodes = self._set_controlnet_strength(workflow, control_strength)",
            "composited_path = self._composite_staged_pass_onto_base(",
            "stage_summaries[-1][\"pixel_composite\"] = True",
            "role_prompts.append(self._merge_prompt_tags(role_base, scene_tags, extra_first=False))",
            "多人 LoRA {cap_label} 自动限制强度",
        ]
        for marker in required_main_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, main_text + comfy_text)
        self.assertIn('"default": "densediffusion"', schema_text)
        self.assertIn("densediffusion is the default path matching the reference two-character workflow", schema_text)

        prewired_match = re.search(
            r"if prewired_sources:(?P<body>.*?)\n\s*load_id = self\._next_node_id\(workflow\)",
            comfy_text,
            flags=re.S,
        )
        self.assertIsNotNone(prewired_match)
        self.assertNotIn("latent_ref_override=latent_ref", prewired_match.group("body"))
        self.assertNotIn("self._merge_prompt_tags(*base_parts, extra_first=False)", main_text)
        self.assertIn("blur = max(12.0, _safe_float(blur_radius, 12.0))", comfy_text)

        self.assertIn("AstrBot Illustrious Staged Multi-LoRA Base v1.3.0", base_workflow)
        self.assertIn("AstrBot Illustrious Staged Multi-LoRA Inpaint v1.3.0", inpaint_workflow)
        self.assertIn('"sampler_name": "euler_ancestral"', base_workflow)
        self.assertIn('"strength": 0.16', inpaint_workflow)
        self.assertIn('"denoise": 0.9', inpaint_workflow)
        self.assertNotIn("solo, single character", base_workflow)


if __name__ == "__main__":
    unittest.main()
