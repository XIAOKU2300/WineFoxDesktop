from .common import *


class ImageAuditFeature(PluginFeature):
    def _image_audit_model_root(self) -> Path:
        configured = str(getattr(self, "image_audit_model_dir", "") or "").strip()
        return Path(configured).expanduser() if configured else self.data_dir / "image_audit_models"
    def _find_nsfw_detector_model_path(self, root: Path) -> str:
        try:
            root = Path(root).expanduser()
            if root.is_file() and root.suffix.lower() in {".h5", ".keras"}:
                return str(root)
            if root.is_dir() and (root / "saved_model.pb").exists():
                return str(root)
            if root.is_dir():
                for pattern in ("*.h5", "*.keras"):
                    matches = sorted(root.rglob(pattern), key=lambda p: (len(p.parts), str(p).lower()))
                    if matches:
                        return str(matches[0])
                saved_models = sorted(root.rglob("saved_model.pb"), key=lambda p: (len(p.parts), str(p).lower()))
                if saved_models:
                    return str(saved_models[0].parent)
        except Exception:
            pass
        return ""
    def _resolve_image_audit_model_path(self) -> str:
        for raw in (getattr(self, "image_audit_model_path", ""), os.environ.get("JIUHU_NSFW_MODEL", "")):
            raw = str(raw or "").strip()
            if raw:
                found = self._find_nsfw_detector_model_path(Path(raw).expanduser())
                if found:
                    return found
        return self._find_nsfw_detector_model_path(self._image_audit_model_root())
    def _safe_extract_zip(self, archive_path: Path, target_dir: Path):
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_root = target_dir.resolve(strict=False)
        with zipfile.ZipFile(archive_path) as zf:
            for member in zf.infolist():
                member_path = target_dir / member.filename
                resolved = member_path.resolve(strict=False)
                if target_root != resolved and target_root not in resolved.parents:
                    raise RuntimeError(f"压缩包包含非法路径：{member.filename}")
            zf.extractall(target_dir)
    async def _download_image_audit_model_task(self):
        from ..civitai_api import CivitaiClient, safe_filename

        started = time.perf_counter()
        root = self._image_audit_model_root()
        root.mkdir(parents=True, exist_ok=True)
        url = str(getattr(self, "image_audit_model_url", "") or IMAGE_AUDIT_MODEL_URL).strip()
        filename = safe_filename(Path(url.split("?", 1)[0]).name or IMAGE_AUDIT_MODEL_ARCHIVE_NAME)
        if not filename.lower().endswith(".zip"):
            filename = IMAGE_AUDIT_MODEL_ARCHIVE_NAME
        archive_path = root / filename
        try:
            self._image_audit_download_status.update({
                "running": True,
                "done": 0,
                "total": 0,
                "message": "downloading",
                "error": "",
                "target": str(archive_path),
                "model_path": "",
            })

            found = self._resolve_image_audit_model_path()
            if found:
                self.image_audit_model_path = found
                self._image_audit_download_status.update({
                    "running": False,
                    "message": "already exists",
                    "model_path": found,
                })
                return

            async def progress(done, total):
                elapsed = max(time.perf_counter() - started, 0.001)
                self._image_audit_download_status.update({
                    "running": True,
                    "done": int(done or 0),
                    "total": int(total or 0),
                    "speed_bps": int((done or 0) / elapsed),
                    "message": "downloading",
                    "target": str(archive_path),
                    "updated_at": time.time(),
                })

            client = CivitaiClient(
                proxy=str(getattr(self, "image_audit_proxy", "") or "").strip(),
                timeout=max(int(getattr(self, "image_audit_timeout", 30) or 30), 60),
                downloader="multithread",
                split=getattr(self, "image_audit_download_split", 8),
            )
            await client.download_file(url, archive_path, progress_cb=progress)

            extract_dir = root / "nsfw_mobilenet_v2_140_224"
            self._safe_extract_zip(archive_path, extract_dir)
            found = self._find_nsfw_detector_model_path(extract_dir) or self._find_nsfw_detector_model_path(root)
            if not found:
                raise RuntimeError(f"模型已下载但未找到 .h5/.keras 或 saved_model.pb：{extract_dir}")
            self.image_audit_model_path = found
            self.config.setdefault("image_audit_settings", {})["nsfw_detector_model_path"] = found
            self._image_audit_download_status.update({
                "running": False,
                "done": archive_path.stat().st_size if archive_path.exists() else 0,
                "total": archive_path.stat().st_size if archive_path.exists() else 0,
                "message": "ready",
                "error": "",
                "target": str(archive_path),
                "model_path": found,
                "updated_at": time.time(),
            })
            logger.info(f"[ComfyUI] 酒狐审核模型已部署: {found}")
        except Exception as e:
            self._image_audit_download_status.update({
                "running": False,
                "message": "failed",
                "error": str(e),
                "updated_at": time.time(),
            })
            logger.warning(f"[ComfyUI] 酒狐审核模型自动部署失败: {e}")
    def _start_image_audit_model_download(self) -> bool:
        if self._image_audit_download_task and not self._image_audit_download_task.done():
            return False
        self._image_audit_download_task = asyncio.create_task(self._download_image_audit_model_task())
        return True
    def _maybe_start_image_audit_auto_deploy(self) -> bool:
        backend = str(getattr(self, "image_audit_backend", "auto") or "auto").lower()
        if backend in {"http", "api", "nsfwjs_http"}:
            return False
        if not getattr(self, "image_audit_auto_download_model", False):
            return False
        if self._resolve_image_audit_model_path():
            return False
        return self._start_image_audit_model_download()
    def _refresh_image_audit_settings(self):
        audit_conf = self.config.get("image_audit_settings", {}) if isinstance(self.config.get("image_audit_settings", {}), dict) else {}
        self.image_audit_backend = str(audit_conf.get("backend", "auto") or "auto").strip().lower()
        self.image_audit_model_path = str(audit_conf.get("nsfw_detector_model_path", "") or "").strip()
        self.image_audit_api_url = str(audit_conf.get("api_url", "") or "").strip()
        try:
            self.image_audit_timeout = max(int(audit_conf.get("timeout", 30) or 30), 5)
        except Exception:
            self.image_audit_timeout = 30
        self.image_audit_auto_download_model = bool(audit_conf.get("auto_download_model", False))
        self.image_audit_model_url = str(audit_conf.get("model_url", IMAGE_AUDIT_MODEL_URL) or IMAGE_AUDIT_MODEL_URL).strip()
        self.image_audit_model_dir = str(audit_conf.get("model_dir", "") or "").strip()
        self.image_audit_proxy = (
            str(audit_conf.get("proxy", "") or "").strip()
            or str(self.config.get("civitai_settings", {}).get("proxy", "") if isinstance(self.config.get("civitai_settings", {}), dict) else "").strip()
        )
        try:
            self.image_audit_download_split = max(1, min(int(audit_conf.get("download_split", 8) or 8), 16))
        except Exception:
            self.image_audit_download_split = 8
        self.image_audit_fail_hentai = _safe_probability_value(audit_conf.get("fail_hentai", 0.55), 0.55)
        self.image_audit_fail_porn = _safe_probability_value(audit_conf.get("fail_porn", 0.55), 0.55)
        self.image_audit_fail_sexy = _safe_probability_value(audit_conf.get("fail_sexy", 0.75), 0.75)
        self.image_audit_fail_explicit_sum = _safe_probability_value(audit_conf.get("fail_explicit_sum", 0.60), 0.60)
        self.image_audit_fail_total = _safe_probability_value(audit_conf.get("fail_total", 0.80), 0.80)
        self.image_audit_fail_binary_nsfw = _safe_probability_value(audit_conf.get("fail_binary_nsfw", 0.85), 0.85)
        self.generated_output_audit_enabled = bool(audit_conf.get("generated_output_audit_enabled", True))
        self.generated_output_audit_threshold = _safe_probability_value(
            audit_conf.get("generated_output_audit_threshold", 0.90),
            0.90,
        )
        self.generated_output_audit_fail_closed = bool(audit_conf.get("generated_output_audit_fail_closed", True))
        self.generated_output_audit_forward_to_special_whitelist = bool(
            audit_conf.get("generated_output_audit_forward_to_special_whitelist", True)
        )
    def _refresh_video_settings(self):
        video_conf = self.config.get("video_settings", {}) if isinstance(self.config.get("video_settings", {}), dict) else {}
        self.video_i2v_enabled = bool(video_conf.get("enable", True))
        self.video_i2v_workflow = str(video_conf.get("workflow", WAN_I2V_WORKFLOW) or WAN_I2V_WORKFLOW).strip()
        self.video_i2v_input_node_id = str(video_conf.get("input_node_id", WAN_I2V_INPUT_NODE_ID) or WAN_I2V_INPUT_NODE_ID).strip()
        self.video_i2v_neg_node_id = str(video_conf.get("neg_node_id", WAN_I2V_NEG_NODE_ID) or WAN_I2V_NEG_NODE_ID).strip()
        self.video_i2v_output_node_id = str(video_conf.get("output_node_id", WAN_I2V_OUTPUT_NODE_ID) or WAN_I2V_OUTPUT_NODE_ID).strip()
        self.video_i2v_width = max(int(_safe_float_value(video_conf.get("width", 720), 720)), 16)
        self.video_i2v_height = max(int(_safe_float_value(video_conf.get("height", 480), 480)), 16)
        self.video_i2v_frames = max(int(_safe_float_value(video_conf.get("frames", 81), 81)), 1)
        self.video_i2v_fps = max(int(_safe_float_value(video_conf.get("fps", 16), 16)), 1)
        self.video_i2v_segments = min(max(int(_safe_float_value(video_conf.get("segments", 4), 4)), 1), 12)
        self.video_i2v_interpolate_enable = bool(video_conf.get("interpolate_enable", True))
        self.video_i2v_target_fps = max(int(_safe_float_value(video_conf.get("target_fps", 32), 32)), 1)
        # 8.5.0：柔和插帧(blend)默认开启，避免 mci 运动补偿在快速动作下的糊化/重影；
        # 视频分段色彩锚定(colormatch 到原始源图)默认开启，抑制段间偏色/闪烁。
        self.video_i2v_interpolate_mci = bool(video_conf.get("interpolate_mci", False))
        self.video_i2v_color_match = bool(video_conf.get("color_match", True))
        self.video_i2v_steps = max(int(_safe_float_value(video_conf.get("steps", 4), 4)), 1)
        self.video_i2v_cfg_high = max(float(_safe_float_value(video_conf.get("cfg_high", 3.0), 3.0)), 0.0)
        self.video_i2v_cfg_low = max(float(_safe_float_value(video_conf.get("cfg_low", 3.0), 3.0)), 0.0)
        self.video_i2v_boundary = min(max(float(_safe_float_value(video_conf.get("boundary", 0.9), 0.9)), 0.0), 1.0)
        self.video_i2v_sigma_shift = max(float(_safe_float_value(video_conf.get("sigma_shift", 5.0), 5.0)), 0.0)
        self.video_i2v_sampler = str(video_conf.get("sampler_name", "lcm") or "lcm").strip() or "lcm"
        self.video_i2v_scheduler = str(video_conf.get("scheduler", "simple") or "simple").strip() or "simple"
        self.video_i2v_crf = min(max(int(_safe_float_value(video_conf.get("crf", 19), 19)), 0), 100)
        self.video_i2v_ffmpeg_path = str(video_conf.get("ffmpeg_path", "") or "").strip()
        self.video_i2v_inline_send = bool(video_conf.get("inline_send", True))
        self.video_i2v_send_as_zip = bool(video_conf.get("send_as_password_zip", True))
        self.video_i2v_archive_password = str(video_conf.get("archive_password", "114514") or "114514").strip() or "114514"
        self.video_i2v_file_upload_timeout = max(int(_safe_float_value(video_conf.get("file_upload_timeout", 90), 90)), 5)
        self.video_i2v_ai_prompt_enabled = bool(video_conf.get("ai_prompt_enabled", True))
        self.video_i2v_ai_prompt_max_tokens = max(int(_safe_float_value(video_conf.get("ai_prompt_max_tokens", 700), 700)), 128)
        self.video_i2v_free_memory_before = bool(video_conf.get("free_memory_before", True))
        self.video_i2v_free_memory_after = bool(video_conf.get("free_memory_after", True))
        self.video_i2v_oom_retry = bool(video_conf.get("oom_retry_after_free", True))
        self.video_i2v_fallback_width = max(int(_safe_float_value(video_conf.get("fallback_width", 448), 448)), 16)
        self.video_i2v_fallback_height = max(int(_safe_float_value(video_conf.get("fallback_height", 448), 448)), 16)
        self.video_i2v_fallback_frames = max(int(_safe_float_value(video_conf.get("fallback_frames", 33), 33)), 1)
        self.video_i2v_fallback_steps = max(int(_safe_float_value(video_conf.get("fallback_steps", 4), 4)), 1)
        # 8.5.1：图生视频画质修复——
        # (1) clip_vision_model：WanImageToVideo 的 clip_vision_output 语义引导模型，放 models/clip_vision/。
        #     历史工作流缺少 CLIPVisionEncode 管线，导致模型拿不到源图语义、画质塌方。
        # (2) sampler_preset：采样预设，提供一整套采样参数，避免逐字段默认值打架。
        #     auto(默认) 按工作流文件名推断；fast_4step=Q4 4步；quality_20step=FP8 20步。
        self.video_i2v_clip_vision_model = str(video_conf.get("clip_vision_model", WAN_I2V_CLIP_VISION_MODEL) or WAN_I2V_CLIP_VISION_MODEL).strip()
        self.video_i2v_sampler_preset = str(video_conf.get("sampler_preset", "auto") or "auto").strip().lower() or "auto"
        self.video_i2v_default_prompt = str(
            video_conf.get("default_prompt")
            or "镜头缓缓推进，角色保持自然姿态，头发与衣摆随轻风微微飘动，目光注视镜头，柔和光影，细腻肌肤质感，电影级画质，高细节"
        ).strip()
        self.video_i2v_negative_prompt = str(
            video_conf.get("negative_prompt")
            or WAN_I2V_NEGATIVE_PROMPT
        ).strip()
        # ====== API 视频（官方兼容 API 1.8）======
        # 配置统一放在「超级管理员 WebUI」的 gallery 配置块里（gallery.api_video），
        # 不再依赖 AstrBot 配置页；找不到时回退到 video_settings.api_video 兼容旧版。
        api_conf = {}
        try:
            gpt_magic = getattr(self, "gpt_magic", None)
            if gpt_magic is not None:
                gallery_conf = gpt_magic.store.config().get("gallery", {})
                if isinstance(gallery_conf, dict) and isinstance(gallery_conf.get("api_video"), dict):
                    api_conf = gallery_conf.get("api_video") or {}
        except Exception:
            api_conf = {}
        if not api_conf and isinstance(video_conf.get("api_video", {}), dict):
            api_conf = video_conf.get("api_video", {})
        self.api_video_enabled = bool(api_conf.get("enable", False))
        self.api_video_base_url = str(api_conf.get("base_url", "https://api.dealonhorizon.us") or "https://api.dealonhorizon.us").strip().rstrip("/")
        self.api_video_api_key = str(api_conf.get("api_key", "") or "").strip()
        self.api_video_model = str(api_conf.get("model", "grok-imagine-video-10s-720p") or "grok-imagine-video-10s-720p").strip()
        self.api_video_max_images = min(max(int(_safe_float_value(api_conf.get("max_images", 1), 1)), 1), 7)
        self.api_video_poll_timeout = min(max(int(_safe_float_value(api_conf.get("timeout", 900), 900)), 30), 3600)
        self.api_video_duration = min(max(int(_safe_float_value(api_conf.get("duration", 6), 6)), 1), 30)
        self.api_video_poll_interval = min(max(float(_safe_float_value(api_conf.get("poll_interval", 6), 6)), 3.0), 30.0)
        aspect_ratio = str(api_conf.get("aspect_ratio", "auto") or "auto").strip().lower()
        if aspect_ratio in ("16:9", "landscape", "horizontal"):
            self.api_video_aspect_ratio = "16:9"
        elif aspect_ratio in ("9:16", "portrait", "vertical"):
            self.api_video_aspect_ratio = "9:16"
        else:
            self.api_video_aspect_ratio = "auto"
        self.api_video_proxy = str(api_conf.get("proxy", "") or "").strip()
        self.api_video_default_prompt = str(
            api_conf.get("default_prompt")
            or self.video_i2v_default_prompt
            or "subtle natural motion, slow cinematic camera movement"
        ).strip()

    def _wan_i2v_options(self) -> dict:
        self._refresh_video_settings()
        return {
            "workflow": self.video_i2v_workflow,
            "input_node_id": self.video_i2v_input_node_id,
            "neg_node_id": self.video_i2v_neg_node_id,
            "output_node_id": self.video_i2v_output_node_id,
            "width": self.video_i2v_width,
            "height": self.video_i2v_height,
            "frames": self.video_i2v_frames,
            "fps": self.video_i2v_fps,
            "segments": self.video_i2v_segments,
            "interpolate_enable": self.video_i2v_interpolate_enable,
            "interpolate_mci": self.video_i2v_interpolate_mci,
            "color_match": self.video_i2v_color_match,
            "target_fps": self.video_i2v_target_fps,
            "steps": self.video_i2v_steps,
            "cfg_high": self.video_i2v_cfg_high,
            "cfg_low": self.video_i2v_cfg_low,
            "boundary": self.video_i2v_boundary,
            "sigma_shift": self.video_i2v_sigma_shift,
            "sampler_name": self.video_i2v_sampler,
            "scheduler": self.video_i2v_scheduler,
            "crf": self.video_i2v_crf,
            "free_memory_before": self.video_i2v_free_memory_before,
            "free_memory_after": self.video_i2v_free_memory_after,
            "oom_retry_after_free": self.video_i2v_oom_retry,
            "fallback_width": self.video_i2v_fallback_width,
            "fallback_height": self.video_i2v_fallback_height,
            "fallback_frames": self.video_i2v_fallback_frames,
            "fallback_steps": self.video_i2v_fallback_steps,
            "clip_vision_model": self.video_i2v_clip_vision_model,
            "sampler_preset": self.video_i2v_sampler_preset,
            "negative_prompt": self.video_i2v_negative_prompt,
        }
    def _normalize_image_audit_class_name(self, value: str) -> str:
        key = re.sub(r"[^a-z]+", "", str(value or "").strip().lower())
        return IMAGE_AUDIT_ALIASES.get(key, key)
    def _normalize_image_audit_scores(self, raw) -> dict:
        if isinstance(raw, dict):
            if "predictions" in raw:
                raw = raw.get("predictions")
            elif "classes" in raw:
                raw = raw.get("classes")
            elif "scores" in raw:
                raw = raw.get("scores")
            elif not any(self._normalize_image_audit_class_name(k) in IMAGE_AUDIT_SCORE_KEYS for k in raw.keys()):
                nested = next((v for v in raw.values() if isinstance(v, (dict, list))), None)
                if nested is not None:
                    raw = nested

        scores = {key: 0.0 for key in IMAGE_AUDIT_SCORE_KEYS}
        if isinstance(raw, dict):
            for key, value in raw.items():
                norm = self._normalize_image_audit_class_name(key)
                if norm in scores:
                    scores[norm] = self._normalize_probability(value)
        elif isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = item.get("className", item.get("class_name", item.get("label", item.get("name", ""))))
                norm = self._normalize_image_audit_class_name(name)
                if norm in scores:
                    scores[norm] = self._normalize_probability(item.get("probability", item.get("score", item.get("value", 0))))
        return scores
    def _normalize_probability(self, value) -> float:
        try:
            number = float(value)
        except Exception:
            return 0.0
        if number > 1.0 and number <= 100.0:
            number = number / 100.0
        return min(max(number, 0.0), 1.0)
    async def _image_audit_with_http(self, image_path: str) -> dict:
        api_url = str(getattr(self, "image_audit_api_url", "") or "").strip()
        if not api_url:
            raise RuntimeError("未配置 image_audit_settings.api_url")
        path = Path(image_path)
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        form = aiohttp.FormData()
        with path.open("rb") as fp:
            form.add_field("image", fp, filename=path.name, content_type=content_type)
            timeout = aiohttp.ClientTimeout(total=self.image_audit_timeout, connect=5, sock_read=self.image_audit_timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(api_url, data=form) as resp:
                    raw_text = await resp.text()
                    if resp.status >= 400:
                        raise RuntimeError(f"HTTP 审核服务失败: {resp.status} {raw_text[:160]}")
                    try:
                        data = json.loads(raw_text)
                    except Exception as e:
                        raise RuntimeError(f"HTTP 审核服务返回非 JSON: {e}") from e
        scores = self._normalize_image_audit_scores(data)
        scores["_backend"] = "http"
        return scores
    def _load_local_image_audit_model_sync(self, model_path: str):
        errors = []
        try:
            from nsfw_detector import predict

            if (
                self._image_audit_model is not None
                and self._image_audit_model_loaded_path == model_path
                and self._image_audit_model_backend == "nsfw_detector"
            ):
                return "nsfw_detector", predict, self._image_audit_model
            model = predict.load_model(model_path)
            self._image_audit_model = model
            self._image_audit_model_loaded_path = model_path
            self._image_audit_model_backend = "nsfw_detector"
            return "nsfw_detector", predict, model
        except Exception as e:
            errors.append(f"nsfw_detector: {self._short_user_error(e)}")

        tfhub_custom_objects = None
        tfhub_error = None
        try:
            import tensorflow_hub as hub
            tfhub_custom_objects = {"KerasLayer": hub.KerasLayer}
        except Exception as e:
            tfhub_error = e

        try:
            import tensorflow as tf

            if (
                self._image_audit_model is not None
                and self._image_audit_model_loaded_path == model_path
                and self._image_audit_model_backend == "tensorflow"
            ):
                return "tensorflow", None, self._image_audit_model
            model = tf.keras.models.load_model(model_path, custom_objects=tfhub_custom_objects, compile=False)
            self._image_audit_model = model
            self._image_audit_model_loaded_path = model_path
            self._image_audit_model_backend = "tensorflow"
            return "tensorflow", None, model
        except Exception as tf_error:
            if tfhub_error:
                errors.append(f"tensorflow_hub: {self._short_user_error(tfhub_error)}")
            errors.append(f"tensorflow: {self._short_user_error(tf_error)}")

        try:
            import tensorflow_hub as hub
            import tf_keras

            if (
                self._image_audit_model is not None
                and self._image_audit_model_loaded_path == model_path
                and self._image_audit_model_backend == "tf_keras"
            ):
                return "tensorflow", None, self._image_audit_model
            model = tf_keras.models.load_model(
                model_path,
                custom_objects={"KerasLayer": hub.KerasLayer},
                compile=False,
            )
            self._image_audit_model = model
            self._image_audit_model_loaded_path = model_path
            self._image_audit_model_backend = "tf_keras"
            return "tensorflow", None, model
        except Exception as legacy_error:
            errors.append(f"tf_keras: {self._short_user_error(legacy_error)}")

        raise RuntimeError("本地审核模型加载失败。" + "；".join(errors))
    def _classify_image_with_tensorflow_sync(self, model, image_path: str) -> dict:
        import numpy as np
        import tensorflow as tf
        from tensorflow.keras.applications.mobilenet_v2 import preprocess_input

        image = tf.keras.utils.load_img(str(image_path), target_size=(224, 224))
        array = tf.keras.utils.img_to_array(image)
        array = np.expand_dims(array, axis=0)
        array = preprocess_input(array)
        predictions = model.predict(array, verbose=0)
        if isinstance(predictions, (list, tuple)):
            predictions = predictions[0]
        values = np.asarray(predictions).reshape(-1).astype(float).tolist()
        return {
            key: (values[index] if index < len(values) else 0.0)
            for index, key in enumerate(IMAGE_AUDIT_SCORE_KEYS)
        }
    def _classify_image_with_opennsfw2_sync(self, image_path: str) -> dict:
        import opennsfw2 as n2

        probability = n2.predict_image(str(image_path))
        if isinstance(probability, (list, tuple)):
            probability = probability[0] if probability else 0.0
        try:
            probability = float(probability)
        except Exception:
            probability = 0.0
        probability = min(max(probability, 0.0), 1.0)
        return {
            "drawings": 0.0,
            "hentai": 0.0,
            "neutral": 1.0 - probability,
            "porn": probability,
            "sexy": 0.0,
            "_backend": "opennsfw2",
            "_binary": True,
            "_binary_nsfw": probability,
        }
    async def _image_audit_with_opennsfw2(self, image_path: str) -> dict:
        raw = await asyncio.to_thread(self._classify_image_with_opennsfw2_sync, str(image_path))
        scores = self._normalize_image_audit_scores(raw)
        scores.update({
            "_backend": "opennsfw2",
            "_binary": True,
            "_binary_nsfw": float(raw.get("_binary_nsfw", scores.get("porn", 0.0)) or 0.0),
        })
        return scores
    async def _image_audit_with_nsfw_detector(self, image_path: str) -> dict:
        model_path = self._resolve_image_audit_model_path()
        if not model_path:
            if self._module_available("opennsfw2"):
                return await self._image_audit_with_opennsfw2(image_path)
            if getattr(self, "image_audit_auto_download_model", False):
                started = self._maybe_start_image_audit_auto_deploy()
                status = getattr(self, "_image_audit_download_status", {}) or {}
                if started or status.get("running"):
                    raise RuntimeError(
                        "酒狐审核模型正在自动部署，请稍后再试。"
                        f"进度：{status.get('done', 0)}/{status.get('total', 0) or '?'}"
                    )
                if status.get("error"):
                    raise RuntimeError(f"酒狐审核模型自动部署失败：{status.get('error')}")
            raise RuntimeError(
                "未配置本地鉴黄模型。请安装 nsfw_detector，并在 image_audit_settings.nsfw_detector_model_path "
                "填写 GantMan/nsfw_model 的模型文件或目录；也可以开启 image_audit_settings.auto_download_model 自动下载模型包"
            )
        model_path_obj = Path(model_path).expanduser()
        if not model_path_obj.exists():
            raise RuntimeError(f"本地鉴黄模型不存在：{model_path}")

        try:
            async with self._image_audit_lock:
                def run():
                    backend, predict, model = self._load_local_image_audit_model_sync(str(model_path_obj))
                    if backend == "tensorflow":
                        return backend, self._classify_image_with_tensorflow_sync(model, str(image_path))
                    result = predict.classify(model, str(image_path))
                    if isinstance(result, dict):
                        return backend, (next(iter(result.values())) if result else {})
                    return backend, result

                backend_used, raw = await asyncio.to_thread(run)
        except Exception:
            if self._module_available("opennsfw2"):
                logger.warning("[ComfyUI] GantMan 五分类模型加载失败，已切换 OpenNSFW2 二分类兜底")
                return await self._image_audit_with_opennsfw2(image_path)
            raise
        scores = self._normalize_image_audit_scores(raw)
        scores["_backend"] = backend_used
        return scores
    async def _audit_image_file(self, image_path: str) -> dict:
        self._refresh_image_audit_settings()
        backend = str(getattr(self, "image_audit_backend", "auto") or "auto").strip().lower()
        if backend in ("http", "api", "nsfwjs_http"):
            scores = await self._image_audit_with_http(image_path)
        elif backend in ("local", "nsfw_detector", "auto"):
            if backend == "auto" and getattr(self, "image_audit_api_url", "") and not getattr(self, "image_audit_model_path", ""):
                scores = await self._image_audit_with_http(image_path)
            else:
                scores = await self._image_audit_with_nsfw_detector(image_path)
        else:
            raise RuntimeError(f"未知图片审核后端：{backend}")
        passed, reason = self._judge_image_audit_scores(scores)
        return {
            "scores": scores,
            "passed": passed,
            "reason": reason,
            "backend": str(scores.get("_backend") or backend),
            "binary": bool(scores.get("_binary")),
            "binary_nsfw": float(scores.get("_binary_nsfw", scores.get("porn", 0.0)) or 0.0),
        }
    def _should_audit_generated_output(self, event: AstrMessageEvent) -> bool:
        self._refresh_image_audit_settings()
        if not getattr(self, "generated_output_audit_enabled", True):
            return False
        if self._bypass_generation_controls(event):
            return False
        return True
    def _generated_image_audit_risk_score(self, audit: dict) -> float:
        audit = audit or {}
        if audit.get("binary"):
            try:
                return min(max(float(audit.get("binary_nsfw", 0.0) or 0.0), 0.0), 1.0)
            except Exception:
                return 0.0
        scores = audit.get("scores") or {}
        try:
            hentai = float(scores.get("hentai", 0.0) or 0.0)
            porn = float(scores.get("porn", 0.0) or 0.0)
            sexy = float(scores.get("sexy", 0.0) or 0.0)
        except Exception:
            return 0.0
        risk = max(hentai, porn, sexy, hentai + porn, hentai + porn + sexy)
        return min(max(risk, 0.0), 1.0)
    def _generated_output_forward_targets(self, event: AstrMessageEvent) -> list:
        if not getattr(self, "generated_output_audit_forward_to_special_whitelist", True):
            return []
        if not getattr(self, "special_whitelist_enabled", True):
            return []
        source_gid = self._get_group_id(event) if self._is_group_message(event) else ""
        targets = []
        for gid in sorted(getattr(self, "special_whitelist_group_ids", set()) or []):
            gid = self._normalize_group_id(gid)
            if gid and gid != source_gid:
                targets.append(gid)
        return targets
    def _event_source_label(self, event: AstrMessageEvent) -> str:
        sender_id = str(event.get_sender_id() or "").strip()
        sender_name = self._sender_display_name(event)
        if self._is_group_message(event):
            gid = self._get_group_id(event) or "unknown"
            group_name = ""
            for getter in (
                lambda: getattr(getattr(event, "message_obj", None), "group_name", ""),
                lambda: getattr(event, "group_name", ""),
            ):
                try:
                    group_name = str(getter() or "").strip()
                    if group_name:
                        break
                except Exception:
                    pass
            group_text = f"{group_name}({gid})" if group_name else f"群 {gid}"
            user_text = f"{sender_name}({sender_id})" if sender_id else sender_name
            return f"{group_text} / {user_text}"
        user_text = f"{sender_name}({sender_id})" if sender_id else sender_name
        return f"私聊 / {user_text}"
    def _generated_output_prompt_text(self, meta: dict) -> str:
        meta = meta or {}
        for key in ("raw_prompt", "request_prompt", "user_prompt", "prompt", "translated_prompt"):
            value = str(meta.get(key) or "").strip()
            if value:
                return value
        return ""
    def _generated_output_vip_hint(self) -> str:
        return "想无限制：联系群主星河绘梦(QQ1982975523)，10r进特殊VIP群。"
    def _with_generated_output_vip_hint(self, message: str) -> str:
        message = str(message or "").strip()
        hint = self._generated_output_vip_hint()
        if hint in message:
            return message
        return (message + "\n" + hint).strip()
    def _build_blocked_generated_output_forward_nodes(
        self,
        event: AstrMessageEvent,
        image_path,
        meta: dict,
        audit_message: str,
        status: str = "blocked",
    ) -> list:
        meta = meta or {}
        risk = meta.get("generated_output_audit_risk")
        risk_text = self._format_probability_percent(risk) if risk is not None else "-"
        threshold = self._format_probability_percent(getattr(self, "generated_output_audit_threshold", 0.90))
        backend = str(meta.get("generated_output_audit_backend") or "-").strip()
        reason = str(meta.get("generated_output_audit_reason") or meta.get("generated_output_audit_error") or "").strip()
        source = self._event_source_label(event)
        lines = [
            "🚨 出图审核拦截转发",
            "━━━━━━━━━━━━━━━━━━",
            f"来源：{source}",
            f"状态：{'审核失败' if status == 'error' else '审核未通过'}",
            f"风险：{risk_text} / 阈值 {threshold}",
            f"后端：{backend}",
            f"图片：{Path(image_path).name}",
        ]
        if reason:
            lines.append(f"原因：{reason}")
        if audit_message:
            lines.extend(["", str(audit_message).strip()])
        notice_node = Node(
            uin=self._safe_self_id_int(event),
            name="ComfyUI Audit",
            content=[Plain("\n".join(lines).strip())],
        )
        request_node = self._build_generation_request_node(event, meta=meta)
        prompt = self._generated_output_prompt_text(meta)
        info_text = self._format_generation_info(prompt, meta=meta) if prompt else "\n".join(lines).strip()
        image_node = Node(
            uin=self._safe_self_id_int(event),
            name="ComfyUI",
            content=[Plain(info_text + "\n"), Image.fromFileSystem(str(image_path))],
        )
        return [node for node in (notice_node, request_node, image_node) if node]
    async def _send_blocked_generated_output_to_special_whitelist(
        self,
        event: AstrMessageEvent,
        image_path,
        meta: dict,
        audit_message: str,
        status: str = "blocked",
    ) -> bool:
        targets = self._generated_output_forward_targets(event)
        if not targets:
            return False
        bot = getattr(event, "bot", None)
        if not bot:
            logger.warning("[ComfyUI] 出图审核拦截转发失败：当前事件没有 bot 实例，无法发送到特殊白名单群")
            return False
        nodes = self._build_blocked_generated_output_forward_nodes(event, image_path, meta, audit_message, status=status)
        if not nodes:
            return False
        sent_any = False
        for gid in targets:
            payload_gid = int(gid) if str(gid).isdigit() else gid
            try:
                await self._send_onebot_forward_nodes(bot, Nodes(nodes), True, payload_gid)
                sent_any = True
                logger.info(f"[ComfyUI] 已将审核拦截出图转发到特殊白名单群 {gid}: image={Path(image_path).name}")
            except Exception as e:
                if self._send_failure_is_likely_late_media_timeout(e):
                    sent_any = True
                    logger.warning(
                        f"[ComfyUI] 合并转发审核拦截出图到特殊白名单群返回超时 {gid}，NapCat 可能已延迟送达"
                    )
                    continue
                logger.warning(f"[ComfyUI] 合并转发审核拦截出图到特殊白名单群失败 {gid}: {e}")
                try:
                    fallback_text = "\n\n".join(
                        str(getattr(part, "text", "") or "").strip()
                        for node in nodes
                        for part in (getattr(node, "content", None) or [])
                        if isinstance(part, Plain) and str(getattr(part, "text", "") or "").strip()
                    )
                    await self._call_onebot_action(
                        bot,
                        "send_group_msg",
                        group_id=payload_gid,
                        message=[
                            {"type": "text", "data": {"text": (fallback_text or "出图审核拦截转发") + "\n"}},
                            {"type": "image", "data": {"file": str(Path(image_path).resolve())}},
                        ],
                    )
                    sent_any = True
                    logger.info(f"[ComfyUI] 已用普通消息将审核拦截出图转发到特殊白名单群 {gid}: image={Path(image_path).name}")
                except Exception as fallback_error:
                    if self._send_failure_is_likely_late_media_timeout(fallback_error):
                        sent_any = True
                        logger.warning(
                            f"[ComfyUI] 普通消息转发审核拦截出图到特殊白名单群返回超时 {gid}，NapCat 可能已延迟送达"
                        )
                        continue
                    logger.warning(f"[ComfyUI] 普通消息转发审核拦截出图到特殊白名单群仍失败 {gid}: {fallback_error}")
        return sent_any
    async def _audit_generated_output_before_send(
        self,
        event: AstrMessageEvent,
        image_path,
        meta: dict = None,
        forward_blocked: bool = True,
        include_vip_hint: bool = True,
    ) -> tuple:
        if not self._should_audit_generated_output(event):
            return True, "", meta or {}
        meta = meta if isinstance(meta, dict) else {}
        threshold = min(max(float(getattr(self, "generated_output_audit_threshold", 0.90) or 0.90), 0.0), 1.0)
        try:
            audit = await self._audit_image_file(str(image_path))
        except Exception as e:
            reason = self._short_user_error(e) if hasattr(self, "_short_user_error") else str(e)
            meta.update({
                "generated_output_audit": "error",
                "generated_output_audit_error": reason,
                "generated_output_audit_threshold": threshold,
            })
            logger.warning(f"[ComfyUI] Generated output audit failed before send: image={Path(image_path).name} error={reason}")
            if getattr(self, "generated_output_audit_fail_closed", True):
                message = f"🚫 出图图片审核失败，按安全策略不发送图片：{reason}"
                if include_vip_hint:
                    message = self._with_generated_output_vip_hint(message)
                if forward_blocked:
                    await self._send_blocked_generated_output_to_special_whitelist(
                        event,
                        image_path,
                        meta,
                        message,
                        status="error",
                    )
                return False, message, meta
            return True, "", meta

        risk = self._generated_image_audit_risk_score(audit)
        backend_label = self._image_audit_backend_label(audit)
        meta.update({
            "generated_output_audit": "passed" if risk < threshold else "blocked",
            "generated_output_audit_risk": risk,
            "generated_output_audit_backend": backend_label,
            "generated_output_audit_reason": audit.get("reason", ""),
            "generated_output_audit_threshold": threshold,
        })
        if risk >= threshold:
            reason = str(audit.get("reason") or "").strip()
            message = f"🚫 出图图片审核未通过，风险 {self._format_probability_percent(risk)}，已拦截发送。"
            if reason:
                message += f"\n原因：{reason}"
            if include_vip_hint:
                message = self._with_generated_output_vip_hint(message)
            logger.warning(
                f"[ComfyUI] Generated output blocked by audit: image={Path(image_path).name} "
                f"risk={risk:.4f} threshold={threshold:.4f} backend={backend_label} reason={reason or '-'}"
            )
            if forward_blocked:
                await self._send_blocked_generated_output_to_special_whitelist(
                    event,
                    image_path,
                    meta,
                    message,
                    status="blocked",
                )
            return False, message, meta

        logger.info(
            f"[ComfyUI] Generated output audit passed: image={Path(image_path).name} "
            f"risk={risk:.4f} threshold={threshold:.4f} backend={backend_label}"
        )
        return True, "", meta
    def _judge_image_audit_scores(self, scores: dict) -> tuple:
        scores = scores or {}
        hentai = float(scores.get("hentai", 0.0) or 0.0)
        porn = float(scores.get("porn", 0.0) or 0.0)
        sexy = float(scores.get("sexy", 0.0) or 0.0)
        reasons = []
        if scores.get("_binary"):
            binary_nsfw = float(scores.get("_binary_nsfw", porn) or 0.0)
            if binary_nsfw >= getattr(self, "image_audit_fail_binary_nsfw", 0.85):
                reasons.append("NSFW 二分类风险过高")
            return not reasons, "、".join(reasons)
        if hentai >= self.image_audit_fail_hentai:
            reasons.append("二次元色情过高")
        if porn >= self.image_audit_fail_porn:
            reasons.append("色情过高")
        if sexy >= self.image_audit_fail_sexy:
            reasons.append("性感过高")
        if hentai + porn >= self.image_audit_fail_explicit_sum:
            reasons.append("显性风险过高")
        if hentai + porn + sexy >= self.image_audit_fail_total:
            reasons.append("综合风险过高")
        return not reasons, "、".join(reasons)
    def _image_audit_backend_label(self, audit: dict) -> str:
        backend = str((audit or {}).get("backend") or "").strip().lower()
        if backend == "opennsfw2":
            return "OpenNSFW2 二分类兜底"
        if backend == "nsfw_detector":
            return "GantMan 五分类"
        if backend in {"tensorflow", "tf_keras"}:
            return "GantMan 五分类"
        if backend == "http":
            return "HTTP"
        return backend or "未知"
    def _format_probability_percent(self, value) -> str:
        try:
            return f"{float(value or 0.0) * 100:.2f}%"
        except Exception:
            return "0.00%"
    def _format_image_audit_thresholds(self, binary: bool = False) -> str:
        if binary:
            return f"NSFW {self._format_probability_percent(getattr(self, 'image_audit_fail_binary_nsfw', 0.85))}"
        return (
            f"二次元 {self._format_probability_percent(getattr(self, 'image_audit_fail_hentai', 0.55))} / "
            f"色情 {self._format_probability_percent(getattr(self, 'image_audit_fail_porn', 0.55))} / "
            f"性感 {self._format_probability_percent(getattr(self, 'image_audit_fail_sexy', 0.75))} / "
            f"显性 {self._format_probability_percent(getattr(self, 'image_audit_fail_explicit_sum', 0.60))} / "
            f"综合 {self._format_probability_percent(getattr(self, 'image_audit_fail_total', 0.80))}"
        )
    def _format_image_audit_score_lines(self, audit: dict) -> list:
        scores = audit.get("scores") or {}
        if audit.get("binary"):
            nsfw = float(audit.get("binary_nsfw", scores.get("porn", 0.0)) or 0.0)
            return [
                f"正常：{self._format_probability_percent(max(0.0, 1.0 - nsfw))}",
                f"NSFW：{self._format_probability_percent(nsfw)}",
            ]
        return [
            f"{IMAGE_AUDIT_LABELS[key]}：{scores.get(key, 0.0) * 100:.2f}%"
            for key in IMAGE_AUDIT_SCORE_KEYS
        ]
    def _format_image_audit_result(self, audit: dict, index: int = 1, total: int = 1, compact: bool = False) -> str:
        lines = [] if compact else ["🛡️ 酒狐审核结果", "━━━━━━━━━━━━━━━━━━"]
        if total > 1:
            lines.append(f"图片：{index}/{total}")
        if not compact:
            backend_label = self._image_audit_backend_label(audit)
            if backend_label:
                lines.append(f"后端：{backend_label}")
            lines.append(f"阈值：{self._format_image_audit_thresholds(binary=bool(audit.get('binary')))}")
        lines.extend(self._format_image_audit_score_lines(audit))
        passed = bool(audit.get("passed"))
        lines.append("通过审核" if passed else "未通过审核")
        reason = str(audit.get("reason") or "").strip()
        if reason and not passed:
            lines.append(f"原因：{reason}")
        return "\n".join(lines)
    def _format_image_audit_results(self, audits: list) -> str:
        audits = [item for item in (audits or []) if isinstance(item, dict)]
        total = len(audits)
        lines = ["🛡️ 酒狐审核结果", "━━━━━━━━━━━━━━━━━━"]
        if total <= 0:
            lines.append("未完成任何图片审核。")
            return "\n".join(lines)
        failed = [item for item in audits if not item.get("passed")]
        backends = []
        binary = False
        for item in audits:
            label = self._image_audit_backend_label(item)
            if label and label not in backends:
                backends.append(label)
            binary = binary or bool(item.get("binary"))
        lines.append(f"图片：{total} 张")
        if backends:
            lines.append(f"后端：{'、'.join(backends)}")
        lines.append(f"阈值：{self._format_image_audit_thresholds(binary=binary)}")
        lines.append("总体：通过审核" if not failed else f"总体：未通过审核（{len(failed)}/{total}）")
        if total == 1:
            lines.append("━━━━━━━━━━━━━━━━━━")
            lines.extend(self._format_image_audit_result(audits[0], index=1, total=1, compact=True).splitlines())
            return "\n".join(lines)
        for index, item in enumerate(audits, start=1):
            lines.append("━━━━━━━━━━━━━━━━━━")
            lines.extend(self._format_image_audit_result(item, index=index, total=total, compact=True).splitlines())
        return "\n".join(lines)
    def _image_audit_command_body(self, rest: str) -> str:
        text = self._plain_trigger_text(rest)
        lower = text.lower()
        prefix = str(getattr(self, "command_prefix", "jh") or "jh").strip() or "jh"
        triggers = (
            "酒狐审核",
            "酒狐鉴黄",
            f"{prefix}图片审核",
            f"{prefix}鉴黄",
            f"{prefix} 图片审核",
            f"{prefix} 鉴黄",
            "jh图片审核",
            "jh鉴黄",
            "jh 图片审核",
            "jh 鉴黄",
        )
        for trigger in triggers:
            if lower.startswith(trigger.lower()):
                return self._clean_prompt_after_trigger(text[len(trigger):])
        return text.strip()
    def _dedupe_image_audit_paths(self, paths: list) -> list:
        result = []
        seen = set()
        for item in paths or []:
            path = str(item or "").strip()
            if not path:
                continue
            key = path
            try:
                path_obj = Path(path)
                if path_obj.exists() and path_obj.is_file():
                    digest = hashlib.sha256()
                    with path_obj.open("rb") as fp:
                        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
                            digest.update(chunk)
                    key = f"sha256:{digest.hexdigest()}"
                else:
                    key = str(path_obj.expanduser())
            except Exception:
                key = path
            if key in seen:
                continue
            seen.add(key)
            result.append(path)
        return result
    def _module_available(self, name: str) -> bool:
        try:
            import importlib.util
            return bool(importlib.util.find_spec(str(name)))
        except Exception:
            return False
    def _image_audit_dependency_status(self) -> dict:
        try:
            return {
                "nsfw_detector": self._module_available("nsfw_detector"),
                "tensorflow": self._module_available("tensorflow"),
                "tensorflow_hub": self._module_available("tensorflow_hub"),
                "tf_keras": self._module_available("tf_keras"),
                "opennsfw2": self._module_available("opennsfw2"),
            }
        except Exception:
            return {
                "nsfw_detector": False,
                "tensorflow": False,
                "tensorflow_hub": False,
                "tf_keras": False,
                "opennsfw2": False,
            }
    def _format_image_audit_status(self) -> str:
        self._refresh_image_audit_settings()
        status = dict(getattr(self, "_image_audit_download_status", {}) or {})
        deps = self._image_audit_dependency_status()
        model_path = self._resolve_image_audit_model_path() or str(status.get("model_path") or "").strip()
        total = int(status.get("total") or 0)
        done = int(status.get("done") or 0)
        if total:
            progress = f"{min(100, int(done * 100 / max(total, 1)))}% ({done}/{total})"
        elif done:
            progress = f"{done}/?"
        else:
            progress = "-"
        lines = [
            "🛡️ 酒狐审核状态",
            "━━━━━━━━━━━━━━━━━━",
            f"后端：{getattr(self, 'image_audit_backend', 'auto')}",
            f"模型：{model_path or '未检测到'}",
            f"下载：{status.get('message') or 'idle'}",
            f"进度：{progress}",
            f"目标：{status.get('target') or '-'}",
            f"代理：{getattr(self, 'image_audit_proxy', '') or '未使用'}",
            f"依赖 nsfw_detector：{'已安装' if deps.get('nsfw_detector') else '未安装（可选）'}",
            f"依赖 tensorflow：{'已安装' if deps.get('tensorflow') else '未安装'}",
            f"依赖 tensorflow_hub：{'已安装' if deps.get('tensorflow_hub') else '未安装（GantMan 可选）'}",
            f"依赖 tf_keras：{'已安装' if deps.get('tf_keras') else '未安装（GantMan 可选）'}",
            f"兜底 opennsfw2：{'已安装' if deps.get('opennsfw2') else '未安装'}",
            f"五分类阈值：{self._format_image_audit_thresholds(binary=False)}",
            f"二分类阈值：{self._format_image_audit_thresholds(binary=True)}",
        ]
        if status.get("error"):
            lines.append(f"失败：{status.get('error')}")
        lines.append("命令：酒狐审核初始化 / 酒狐审核状态 / 引用图片+酒狐审核")
        return "\n".join(lines)
    def _build_image_audit_admin_result(self, event: AstrMessageEvent, body: str):
        sub = str(body or "").strip().lower()
        if sub in ("状态", "查看", "status", "show", "进度", "progress"):
            return event.plain_result(self._format_image_audit_status())
        if sub in ("初始化", "init", "deploy", "部署", "下载", "下载模型", "模型", "准备"):
            if str(getattr(self, "image_audit_backend", "auto") or "auto").lower() in {"http", "api", "nsfwjs_http"}:
                return event.plain_result("ℹ️ 当前是 HTTP 审核后端，不需要下载本地模型。")
            found = self._resolve_image_audit_model_path()
            if found:
                self.image_audit_model_path = found
                return event.plain_result(f"✅ 酒狐审核模型已存在：{found}\n{self._format_image_audit_status()}")
            started = self._start_image_audit_model_download()
            if started:
                return event.plain_result("✅ 已开始部署酒狐审核模型，稍后用：酒狐审核状态")
            return event.plain_result("ℹ️ 酒狐审核模型部署任务已在进行中，稍后用：酒狐审核状态")
        if sub in ("帮助", "help", "h", "?"):
            return event.plain_result(
                "🛡️ 酒狐审核\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "酒狐审核初始化：下载并解压本地五分类模型\n"
                "酒狐审核状态：查看模型、下载和依赖状态\n"
                "引用图片 + 酒狐审核：审核图片\n"
                "配置：image_audit_settings.auto_download_model 可控制启动时自动部署"
            )
        return None
    async def _handle_image_audit_command(self, event: AstrMessageEvent, rest: str = ""):
        self._refresh_image_audit_settings()
        allowed, reason = self._check_access(event)
        if not allowed:
            await self._trace_test_mode(event, "image_audit_access_denied", reason)
            yield event.plain_result(reason)
            return
        body = self._image_audit_command_body(rest)
        admin_result = self._build_image_audit_admin_result(event, body)
        if admin_result:
            yield admin_result
            return
        paths = self._dedupe_image_audit_paths(await self._extract_source_image_paths(event, limit=6, reply_first=True))
        if not paths:
            yield event.plain_result("❌ 请引用图片或随消息发送图片，然后使用：酒狐审核\n初始化：酒狐审核初始化\n状态：酒狐审核状态")
            return
        audits = []
        errors = []
        for path in paths:
            try:
                audit = await self._audit_image_file(path)
                audits.append(audit)
            except Exception as e:
                logger.warning(f"[ComfyUI] 酒狐审核失败: {e}")
                errors.append(self._short_user_error(e))
        if audits:
            text = self._format_image_audit_results(audits)
            if errors:
                text += "\n━━━━━━━━━━━━━━━━━━\n失败：" + "；".join(errors[:3])
            yield event.plain_result(text)
            return
        yield event.plain_result(f"❌ 酒狐审核失败：{'；'.join(errors[:3]) if errors else '未知错误'}")
