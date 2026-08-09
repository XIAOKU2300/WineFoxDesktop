from .common import *


class LookVisionFeature(PluginFeature):
    def _look_vision_chat_completions_url(self) -> str:
        url, _api_key, endpoint_warning = self._look_vision_endpoint("Look Vision")
        if endpoint_warning and url:
            logger.warning(f"[ComfyUI] Look Vision config warning: {self._redact_sensitive_text(endpoint_warning)}")
        return url
    def _look_vision_configured(self) -> bool:
        return bool(
            getattr(self, "look_vision_enabled", True)
            and getattr(self, "look_vision_api_base", "")
            and getattr(self, "look_vision_api_key", "")
            and getattr(self, "look_vision_model", "")
        )
    def _build_look_vision_user_prompt(self, user_note: str, image_count: int) -> str:
        payload = {
            "task": "reverse-engineer the attached image(s) into one detailed reusable composition prompt without source character identity",
            "image_count": int(image_count or 0),
            "user_note": str(user_note or "").strip(),
            "output_format": "one English comma-separated Stable Diffusion prompt line only",
            "detail_requirements": [
                "subject count and broad subject type only, no character names or source names",
                "pose, gesture, body orientation, head angle, torso direction, balance and weight distribution",
                "facial expression category and gaze direction without eye color or pupil details",
                "arm, hand, finger, leg and foot placement as spatial pose information only",
                "generic garment categories, fabric type, silhouette, fit, folds and material behavior without signature outfit details",
                "viewpoint, view angle, perspective, shot size, framing, composition, negative space and depth layering",
                "foreground, midground, background, scene layout, props, environment and spatial relationships",
                "lighting direction, shadow softness, highlights, atmosphere, color palette, art style, linework, texture and rendering style",
                "for sketches or stick figures, infer the intended action and rewrite it as an anatomically plausible finished anime pose",
                "for live-action or real-person references, convert the pose/framing into anime illustration cues instead of photo realism",
                "if the reference head is cropped, hidden, blocked or missing, infer a complete visible anime head and readable face instead of copying the crop",
            ],
            "rules": [
                "be very detailed but do not explain",
                "do not output character names, franchise/source names, LoRA trigger words, cosplay labels, logos, exact hair color, eye color, pupil shape, unique hairstyle, iconic accessories, or signature outfit details",
                "do not preserve the source character identity; describe only reusable visual structure that will not summon the same character when copied",
                "if the source is rough, simplified, distorted, or missing body parts, normalize it into a believable human body pose with attached limbs, natural joints, plausible hand placement, and stable balance",
                "if the source is a photo, output anime illustration, clean anime linework, cel shading or soft anime rendering, stylized proportions, expressive anime face, full-color illustration, and avoid photorealistic/photo/camera-brand terms",
                "do not output headless crop, neck down, cropped head, head out of frame, no head, faceless, body only, torso only, or source-crop-only framing unless the user explicitly asks for it",
                "write eye-level view, low-angle view, high-angle view, viewpoint or perspective; do not write camera angle",
                "do not output physical camera, DSLR, camera lens, camera object, or holding camera unless the user explicitly asks to preserve a real camera prop",
                "do not output headings, markdown, JSON, bullet points, Chinese or multiple alternatives",
                "if multiple images are provided, merge them into one useful prompt and avoid repeating the same tag many times",
            ],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    def _look_vision_generic_identity_markers(self) -> set:
        return {
            "girl", "boy", "woman", "man", "male", "female", "1girl", "1boy", "2girls", "2boys",
            "solo", "person", "people", "character", "animegirl", "animeboy", "dress", "shirt",
            "skirt", "coat", "jacket", "boots", "shoes", "gloves", "socks", "thighhighs",
            "standing", "sitting", "lying", "smile", "lookingatviewer",
        }
    def _look_vision_profile_identity_markers(self) -> set:
        markers = set()
        families = [None] + sorted(SUPPORTED_MODEL_FAMILIES)
        for family in families:
            profiles = self._get_lora_profiles(family=family)
            for key, profile in (profiles or {}).items():
                if not isinstance(profile, dict):
                    continue
                terms = [key, profile.get("character"), profile.get("lora"), Path(str(profile.get("lora") or "")).stem]
                for field in (
                    "aliases", "official_aliases", "nickname_aliases", "match_terms",
                    "search_terms", "search_aliases", "activation_tags", "identity_appearance",
                    "default_outfit", "source_title", "source_name", "model_name",
                ):
                    terms.extend(self._profile_tag_values(profile.get(field), limit=80))
                terms.extend(self._split_prompt_tags(str(profile.get("trigger_prompt") or "")))
                terms.extend(self._lora_stem_trigger_candidates(str(profile.get("lora") or "")))
                for term in terms:
                    for part in re.split(r"[/|\\]+", str(term or "")):
                        part = part.strip(" ,;()[]{}")
                        marker = self._normalize_lora_match_key(part)
                        if not marker:
                            continue
                        has_cjk = bool(re.search(r"[\u3400-\u9fff]", marker))
                        if (has_cjk and len(marker) < 2) or (not has_cjk and len(marker) < 4):
                            continue
                        if marker in self._look_vision_generic_identity_markers():
                            continue
                        markers.add(marker)
        return markers
    def _look_vision_tag_is_identity_specific(self, tag: str, profile_markers: set) -> bool:
        raw = str(tag or "").strip()
        if not raw:
            return True
        lower = raw.lower()
        marker = self._normalize_lora_match_key(raw)
        if not marker:
            return True
        crop_markers = (
            "headless", "headlesscrop", "neckdown", "croppedhead", "facecropped",
            "headoutofframe", "headoffscreen", "offscreenhead", "missinghead",
            "coveredhead", "hiddenhead", "nohead", "faceless", "bodyonly",
            "torsoonly", "torsocrop", "waistdown", "sourcecroponly",
            "torsoandlegsvisible",
        )
        if any(item in marker for item in crop_markers):
            return True
        if re.search(
            r"\b(?:headless|neck\s*down|cropped\s+head|face\s+cropped|head\s+(?:out\s+of\s+frame|offscreen|missing|covered|hidden)|"
            r"no\s+head|faceless|body\s+only|torso\s+only|torso\s+and\s+legs\s+visible|waist\s+down|source\s+crop)\b",
            lower,
            re.I,
        ):
            return True
        if re.search(
            r"\b(?:camera|dslr|mirrorless\s+camera|camera\s+(?:object|prop|body|lens|in\s+hand)|"
            r"holding\s+(?:a\s+)?camera|physical\s+camera|photographic\s+camera|vintage\s+camera)\b",
            lower,
            re.I,
        ):
            return True
        identity_markers = (
            "pupil", "pupils", "iris", "eyelash", "eyelashes", "ahoge", "bangs",
            "sidelocks", "twintails", "ponytail", "braid", "hairornament", "hairclip",
            "hairribbon", "hairbow", "animalears", "catears", "foxears", "bunnyears",
            "horns", "halo", "cosplay", "franchise", "officialoutfit",
            "canonoutfit", "signatureoutfit", "characterdesign", "logo", "brand",
            "celebrity", "ethnicity",
        )
        if any(item in marker for item in identity_markers):
            return True
        if re.search(r"\bhair(?:ed)?\b|\bhair[-\s]*(?:ornament|clip|ribbon|bow|color|colour|style)\b", lower, re.I):
            return True
        if re.search(r"\b(?:tail|animal\s+tail|cat\s+tail|fox\s+tail|bunny\s+tail)\b", lower, re.I):
            return True
        color_words = (
            "black", "white", "blue", "red", "green", "yellow", "gold", "golden", "pink",
            "purple", "violet", "grey", "gray", "silver", "brown", "orange", "cyan",
            "teal", "aqua", "blonde", "blond", "multicolored", "gradient", "two-tone",
        )
        color_re = r"(?:%s)" % "|".join(color_words)
        if re.search(rf"\b{color_re}\s+(?:eyes?|iris|pupils?|hair|haired|fur)\b", lower, re.I):
            return True
        if re.search(r"\b(?:eyes?|iris|pupils?|hair|fur)\s+(?:color|colour)\b", lower, re.I):
            return True
        if re.search(r"\b(?:from|source|series|franchise)\s*[:=]", lower, re.I):
            return True
        if re.search(
            r"\b(?:photo|photograph|photorealistic|realistic photo|live[-\s]*action|real person|selfie|snapshot|"
            r"dslr|iphone photo|camera photo|stick figure|rough sketch|simple sketch|doodle|line drawing|"
            r"unfinished sketch|source image|pose guide)\b",
            lower,
            re.I,
        ):
            return True
        for item in profile_markers or set():
            if not item:
                continue
            min_len = 2 if re.search(r"[\u3400-\u9fff]", item) else 4
            if len(item) < min_len:
                continue
            if marker == item:
                return True
            if len(item) >= min_len + 2 and item in marker:
                return True
            if len(marker) >= min_len + 2 and marker in item:
                return True
        return False
    def _normalize_look_vision_prompt_tag(self, tag: str) -> str:
        text = str(tag or "").strip(" \t\r\n,;.!?。！？")
        if not text:
            return ""
        replacements = (
            (r"\beye[-\s]*level\s+camera\s+angle\b", "eye-level view"),
            (r"\blow\s+camera\s+angle\b", "low-angle view"),
            (r"\bhigh\s+camera\s+angle\b", "high-angle view"),
            (r"\boverhead\s+camera\s+angle\b", "overhead view"),
            (r"\bcamera\s+angle\b", "view angle"),
            (r"\bcamera\s+view\b", "viewpoint"),
            (r"\bcamera\s+perspective\b", "perspective"),
        )
        for pattern, repl in replacements:
            text = re.sub(pattern, repl, text, flags=re.I)
        text = re.sub(r"\s+", " ", text).strip(" \t\r\n,;.!?。！？")
        return text
    def _sanitize_look_vision_prompt(self, text: str) -> str:
        cleaned = self._sanitize_llm_prompt_text(text)
        profile_markers = self._look_vision_profile_identity_markers()
        tags = [
            "anime illustration",
            "clean anime linework",
            "soft anime rendering",
            "complete head visible",
            "face visible",
            "readable face",
            "stylized proportions",
            "full color illustration",
        ]
        seen = set()
        for tag in tags:
            marker = self._normalize_lora_match_key(tag)
            if marker:
                seen.add(marker)
        for part in [self._normalize_look_vision_prompt_tag(part) for part in cleaned.split(",")]:
            if not part:
                continue
            if self._look_vision_tag_is_identity_specific(part, profile_markers):
                continue
            marker = self._normalize_lora_match_key(part)
            if marker and marker not in seen:
                seen.add(marker)
                tags.append(part)
        return ", ".join(tags[:240]).strip(" ,")
    def _validate_look_vision_prompt(self, raw_content: str, prompt: str) -> tuple:
        text = str(prompt or "").strip()
        if not text:
            return "", "视觉模型空输出"
        if self._looks_like_llm_refusal_text(raw_content) or self._looks_like_llm_refusal_text(text):
            return "", "视觉模型返回拒绝文本"
        if self._looks_like_prompt_meta_text(text):
            return "", "视觉模型返回了分析过程"
        tags = [part.strip() for part in text.split(",") if part.strip()]
        if len(tags) < 6:
            return "", "有效提示词标签过少"
        return ", ".join(tags[:180]).strip(" ,"), ""
    def _build_look_vision_safe_user_prompt(self, user_note: str, image_count: int) -> str:
        payload = {
            "task": "extract a safe reusable anime pose and composition prompt from the attached image(s)",
            "image_count": int(image_count or 0),
            "user_note": str(user_note or "").strip(),
            "output_format": "one English comma-separated Stable Diffusion prompt line only",
            "safety_mode": "SFW abstraction only",
            "rules": [
                "describe only non-explicit reusable structure: subject count, pose, body orientation, limb placement, viewpoint, framing, scene layout, lighting, color palette and anime rendering style",
                "if the source contains nudity, erotic content, fetish content, exposed intimate areas, or suggestive focus, ignore those details and rewrite the image as a non-explicit clothed anime pose reference",
                "do not output sexual words, nudity words, intimate body part words, fetish words, age-sensitive words, violence, gore, or medical terms",
                "do not identify characters, franchises, real people, celebrities, names, LoRA triggers, hair color, eye color, unique hairstyle, logos or signature outfits",
                "if a body part is cropped or missing in the source, infer a complete visible anime head and a plausible healthy full body unless the user explicitly asks otherwise",
                "for sketches, stick figures, or rough drawings, infer the intended action as a natural anime pose with attached limbs and stable balance",
                "output only comma-separated prompt tags; no markdown, no explanation, no Chinese",
            ],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    async def _call_look_vision_safe_retry(
        self,
        valid_paths: list,
        user_note: str,
        headers: dict,
        timeout: aiohttp.ClientTimeout,
    ) -> tuple:
        url, api_key, endpoint_warning = self._look_vision_endpoint("JiuHu Look Vision Safe")
        if endpoint_warning:
            safe_warning = self._redact_sensitive_text(endpoint_warning)
            if not url:
                return "", safe_warning
            logger.warning(f"[ComfyUI] JiuHu Look Vision Safe config warning: {safe_warning}")
        headers = dict(headers or {})
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        safe_system = (
            "You are a safety-conscious anime composition prompt writer. "
            "You must output only safe-for-work reusable pose and composition tags. "
            "Do not describe nudity, sexual acts, erotic focus, intimate body parts, fetish content, violence, gore, "
            "minors, real person identity, character identity, franchise names, or LoRA trigger words. "
            "If the image contains sensitive content, abstract it into a non-explicit clothed anime pose reference: "
            "pose, gesture, body orientation, limb placement, viewpoint, framing, scene layout, lighting, palette, and rendering style. "
            "Output one English comma-separated prompt line only."
        )
        content = [{"type": "text", "text": self._build_look_vision_safe_user_prompt(user_note, len(valid_paths))}]
        for _path, data_url in valid_paths:
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        payload = {
            "model": self.look_vision_model,
            "messages": [
                {"role": "system", "content": safe_system},
                {"role": "user", "content": content},
            ],
            "temperature": 0.05,
            "max_tokens": min(max(int(getattr(self, "look_vision_max_tokens", 1600) or 1600), 256), 900),
            "stream": False,
        }
        _data, content_text, _raw, error = await self._post_llm_chat_with_retry(
            url,
            headers,
            payload,
            timeout,
            "JiuHu Look Vision Safe",
            attempts=1,
        )
        if not content_text:
            return "", error or "safe retry empty"
        prompt = self._sanitize_look_vision_prompt(content_text)
        prompt, invalid_reason = self._validate_look_vision_prompt(content_text, prompt)
        if prompt:
            return prompt, ""
        return "", invalid_reason or "safe retry invalid"
    async def _call_look_vision_llm(self, image_paths: list, user_note: str = "") -> tuple:
        valid_paths = []
        for path in image_paths or []:
            data_url = self._image_file_to_data_url(path)
            if data_url:
                valid_paths.append((path, data_url))
        if not valid_paths:
            return "", "图片读取失败"

        content = [{"type": "text", "text": self._build_look_vision_user_prompt(user_note, len(valid_paths))}]
        for _path, data_url in valid_paths:
            content.append({"type": "image_url", "image_url": {"url": data_url}})

        payload = {
            "model": self.look_vision_model,
            "messages": [
                {"role": "system", "content": self.look_vision_system_prompt},
                {"role": "user", "content": content},
            ],
            "temperature": 0.12,
            "max_tokens": max(int(getattr(self, "look_vision_max_tokens", 1600) or 1600), 256),
            "stream": False,
        }
        url, api_key, endpoint_warning = self._look_vision_endpoint("JiuHu Look Vision")
        if endpoint_warning:
            safe_warning = self._redact_sensitive_text(endpoint_warning)
            if not url:
                return "", safe_warning
            logger.warning(f"[ComfyUI] JiuHu Look Vision config warning: {safe_warning}")
        if not api_key:
            return "", "JiuHu Look Vision api_key is empty"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=max(int(getattr(self, "look_vision_timeout", 60) or 60), 10))
        _data, content_text, raw, error = await self._post_llm_chat_with_retry(
            url,
            headers,
            payload,
            timeout,
            "JiuHu Look Vision",
            attempts=2,
        )
        if not content_text:
            if self._llm_error_is_policy_block(error):
                logger.warning("[ComfyUI] 酒狐看看普通识图被安全策略拦截，尝试 SFW 构图反推重试")
                safe_prompt, safe_error = await self._call_look_vision_safe_retry(
                    valid_paths,
                    user_note,
                    headers,
                    timeout,
                )
                if safe_prompt:
                    return safe_prompt, ""
                logger.warning(f"[ComfyUI] 酒狐看看 SFW 重试仍失败: {safe_error}")
                return "", "这张图被视觉模型安全策略拦截了，换一张更温和的图，或把酒狐看看换成更宽松的视觉模型"
            return "", error or "视觉模型空输出"
        prompt = self._sanitize_look_vision_prompt(content_text)
        prompt, invalid_reason = self._validate_look_vision_prompt(content_text, prompt)
        if prompt:
            return prompt, ""
        return "", invalid_reason or "视觉模型输出无效"
    def _build_look_result(self, event: AstrMessageEvent, prompt: str):
        sender_name = self._sender_display_name(event)
        sender_id = str(event.get_sender_id() or "").strip()
        node_name = f"酒狐看看 | {sender_name}/{sender_id}" if sender_id else f"酒狐看看 | {sender_name}"
        node = Node(
            uin=self._safe_self_id_int(event),
            name=node_name,
            content=[Plain(str(prompt or "").strip())],
        )
        return event.chain_result([Nodes([node])])
    async def _resolve_look_command_image_paths(self, event: AstrMessageEvent, image_paths: list, limit: int = 8) -> list:
        paths = [str(path or "").strip() for path in (image_paths or []) if str(path or "").strip()]
        if paths:
            return paths[:max(int(limit or 1), 1)]
        paths = await self._get_look_recent_session_images(event, limit=limit, max_age_seconds=180)
        if paths:
            logger.info(f"[ComfyUI] 酒狐看看使用会话最近图: images={len(paths)} user={event.get_sender_id()}")
            return paths
        if not self._is_group_message(event):
            paths = await self._wait_for_look_private_images(event, limit=limit, wait_seconds=10)
            if paths:
                logger.info(f"[ComfyUI] 酒狐看看使用私聊补充图: images={len(paths)} user={event.get_sender_id()}")
                return paths
        return []
    async def _handle_look_command(self, event: AstrMessageEvent, raw: str = ""):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        if not getattr(self, "look_vision_enabled", True):
            yield event.plain_result("酒狐看看功能未开启，请先在 llm_settings.look_vision_enable 打开")
            return
        if not self._look_vision_configured():
            yield event.plain_result("酒狐看看视觉模型未配置：请填写 look_vision_api_base / api_key / model，或先配置图生图视觉模型")
            return
        limit = max(1, min(int(getattr(self, "look_vision_max_images", 6) or 6), 8))
        image_paths = await self._extract_source_image_paths(event, limit=limit, reply_first=True)
        image_paths = await self._resolve_look_command_image_paths(event, image_paths, limit=limit)
        if not image_paths and self._is_group_message(event):
            yield event.plain_result(self._notice("look_no_image", "请引用/附带图片，或先发图再发酒狐看看；酒狐会自动读取本群最近一张图。"))
            return
        if not image_paths:
            yield event.plain_result(self._notice("look_no_image", "请引用/附带图片，或先发图再发酒狐看看；也可以发酒狐看看后 10 秒内补图。"))
            return
        if not image_paths:
            image_paths = await self._get_look_recent_session_images(event, limit=limit, max_age_seconds=180)
            if image_paths:
                logger.info(f"[ComfyUI] 酒狐看看使用私聊补充图片: images={len(image_paths)} user={event.get_sender_id()}")
            else:
                yield event.plain_result(self._notice("look_no_image", "请引用/附带图片，或发送酒狐看看后 10 秒内私聊 bot 发图"))
                return
        user_note = self._look_command_body(raw or getattr(event, "message_str", ""))
        logger.info(f"[ComfyUI] 酒狐看看开始识图: images={len(image_paths)} user={event.get_sender_id()}")
        prompt, error = await self._call_look_vision_llm(image_paths, user_note=user_note)
        if error:
            logger.warning(f"[ComfyUI] 酒狐看看失败: {error}")
            yield event.plain_result(self._notice("look_failed", "酒狐看看失败：{error}", error=self._short_user_error(error, max_len=80)))
            return
        logger.info(f"[ComfyUI] 酒狐看看完成: {prompt[:120]}...")
        yield self._build_look_result(event, prompt)
