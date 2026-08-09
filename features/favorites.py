from .common import *


class FavoritesFeature(PluginFeature):
    def _is_favorite_trigger(self, raw: str, event: AstrMessageEvent = None) -> bool:
        clean = self._plain_trigger_text(raw)
        if not clean or clean.startswith("/"):
            return False
        compact = re.sub(r"\s+", "", clean).lower()
        prefix = str(getattr(self, "command_prefix", "jh") or "jh").strip().lower() or "jh"
        return compact in {
            "酒狐收藏",
            f"{prefix}收藏",
            "jh收藏",
            "jiuhufavorite",
            "jhfavorite",
        }
    def _delegate_magic_heads(self) -> tuple:
        prefix = str(getattr(self, "command_prefix", "jh") or "jh").strip().lower() or "jh"
        return (
            "酒狐传授魔法",
            "酒狐授予魔法",
            "酒狐收回魔法",
            "酒狐管理员",
            f"{prefix}传授魔法",
            f"{prefix}授予魔法",
            f"{prefix}收回魔法",
            f"{prefix}管理员",
            "jh传授魔法",
            "jh授予魔法",
            "jh收回魔法",
            "jh管理员",
        )
    def _delegate_magic_command_body(self, raw: str) -> tuple:
        clean = self._plain_trigger_text(raw)
        if not clean or clean.startswith("/"):
            return "", ""
        command_view = re.sub(r"\[At:\d+\]|\[CQ:at,qq=\d+\]", " ", clean, flags=re.I).strip()
        command_view = re.sub(r"\s+", " ", command_view).strip()
        for head in sorted(self._delegate_magic_heads(), key=len, reverse=True):
            pattern = r"^\s*" + r"\s*".join(re.escape(ch) for ch in head) + r"\s*(.*)$"
            match = re.match(pattern, command_view, flags=re.I | re.S)
            if match:
                return match.group(1).strip(), head
        return "", ""
    def _is_delegate_magic_trigger(self, raw: str, event: AstrMessageEvent = None) -> bool:
        _rest, head = self._delegate_magic_command_body(raw)
        return bool(head)
    def _favorite_text_from_onebot_segments(self, segments) -> str:
        parts = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "").lower()
            data = seg.get("data", {}) or {}
            if seg_type == "text":
                text = str(data.get("text") or data.get("content") or "").strip()
                if text:
                    parts.append(text)
            elif seg_type == "node":
                content = data.get("content") or data.get("message") or []
                text = self._favorite_text_from_onebot_segments(content)
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    def _favorite_segment_summary(self, segments) -> list:
        summary = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "")
            data = seg.get("data", {}) or {}
            item = {"type": seg_type}
            if seg_type == "text":
                item["text"] = str(data.get("text") or data.get("content") or "")[:2000]
            elif seg_type == "image":
                item["file"] = str(data.get("file") or "")[:500]
                item["file_id"] = str(data.get("file_id") or data.get("fileId") or "")[:500]
                item["path"] = str(data.get("path") or data.get("sourcePath") or "")[:500]
                item["url"] = str(data.get("url") or "")[:1000]
                item["summary"] = str(data.get("summary") or "")[:500]
            elif seg_type == "reply":
                item["id"] = str(data.get("id") or data.get("message_id") or "")[:100]
            else:
                item["data"] = {
                    str(k): str(v)[:500]
                    for k, v in list(data.items())[:12]
                    if isinstance(k, str)
                }
            summary.append(item)
            if len(summary) >= 50:
                break
        return summary
    async def _favorite_reply_context(self, event: AstrMessageEvent) -> dict:
        components = self._iter_event_components(event)
        context = {
            "reply_message_id": "",
            "reply_message_text": "",
            "reply_raw_message": "",
            "reply_segments": [],
        }
        for reply in self._iter_reply_components(components):
            reply_id = str(getattr(reply, "id", "") or "").strip()
            context["reply_message_id"] = reply_id
            chain_texts = []
            for item in getattr(reply, "chain", None) or []:
                text = self._extract_plain_text_from_component(item).strip()
                if text:
                    chain_texts.append(text)
            if event.get_platform_name() == "aiocqhttp" and reply_id:
                try:
                    msg_id = int(reply_id) if reply_id.isdigit() else reply_id
                    data = await self._call_onebot_action(getattr(event, "bot", None), "get_msg", message_id=msg_id)
                    if isinstance(data, dict):
                        segments = data.get("message", []) or []
                        context["reply_raw_message"] = str(data.get("raw_message") or "")[:6000]
                        context["reply_segments"] = self._favorite_segment_summary(segments)
                        seg_text = self._favorite_text_from_onebot_segments(segments)
                        if seg_text:
                            chain_texts.append(seg_text)
                        if context["reply_raw_message"]:
                            chain_texts.append(context["reply_raw_message"])
                except Exception as e:
                    logger.warning(f"[ComfyUI] 酒狐收藏获取引用消息失败: {e}")
            seen = set()
            merged = []
            for text in chain_texts:
                key = text.strip()
                if key and key not in seen:
                    seen.add(key)
                    merged.append(key)
            context["reply_message_text"] = "\n".join(merged).strip()[:8000]
            break
        return context
    def _extract_favorite_prompt(self, text: str) -> str:
        text = str(text or "").strip()
        if not text:
            return ""
        patterns = (
            r"(?:📝\s*)?Prompt\s*[:：]\s*(.+)",
            r"提示词\s*[:：]\s*(.+)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.I | re.S)
            if not match:
                continue
            prompt = match.group(1).strip()
            prompt = re.split(
                r"\n\s*(?:⚠|本地图片\s*[:：]|Seed\s*[:：]|耗时\s*[:：]|工作流\s*[:：]|模型\s*[:：])",
                prompt,
                maxsplit=1,
            )[0].strip()
            return prompt[:6000]
        return ""
    def _favorite_safe_path_part(self, value: str, fallback: str = "未分类") -> str:
        text = str(value or "").strip() or fallback
        text = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", text)
        text = re.sub(r"\s+", " ", text).strip(" ._")
        return (text or fallback)[:80]
    def _favorite_clean_character_label(self, value: str, require_cjk: bool = False) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"\[CQ:[^\]]+\]", "", text)
        text = re.sub(r"<lora:[^>]+>", "", text, flags=re.I)
        text = re.sub(r"(?i)\b(?:lora|locon|lycoris|safetensors|checkpoint|model)\b", " ", text)
        text = re.sub(r"[\r\n\t]+", " ", text)
        text = re.split(r"[|｜/／,，;；]+", text, maxsplit=1)[0].strip()
        text = re.sub(r"[\(（]\s*(?:wuthering\s*waves|wuwa|鸣潮|原神|genshin|honkai|崩坏|star\s*rail|official|illustrious|anima|sdxl|pony)[^）)]*[\)）]", "", text, flags=re.I)
        text = re.sub(r"[\(（]\s*[\)）]", "", text).strip()
        if require_cjk:
            cjk_chunks = re.findall(r"[\u3400-\u9fff][\u3400-\u9fff·・ーA-Za-z0-9]{0,24}", text)
            for chunk in cjk_chunks:
                chunk = chunk.strip(" _-")
                if chunk and not self._is_generic_profile_match_term(chunk) and not self._is_lora_profile_bad_match_term(chunk):
                    return chunk[:24]
            return ""
        text = re.sub(r"[^\u3400-\u9fff\u3040-\u30ffA-Za-z0-9·・ー _.-]+", "", text).strip(" ._-")
        if not text or self._is_generic_profile_match_term(text) or self._is_lora_profile_bad_match_term(text):
            return ""
        return text[:40]
    def _favorite_profile_lookup_candidates(self, key: str, profile: dict) -> list:
        values = [key]
        if isinstance(profile, dict):
            values.extend([
                profile.get("character", ""),
                profile.get("lora", ""),
                Path(str(profile.get("lora", "") or "")).stem,
            ])
            for field in ("match_terms", "official_aliases", "nickname_aliases", "aliases", "search_aliases"):
                raw = profile.get(field) or []
                if isinstance(raw, str):
                    raw = re.split(r"[,，;；\n\r]+", raw)
                values.extend(raw)
        result = []
        seen = set()
        for item in values:
            item = str(item or "").strip()
            if not item:
                continue
            marker = self._normalize_profile_match_key(item)
            if marker and marker not in seen:
                seen.add(marker)
                result.append(item)
        return result
    def _favorite_profile_chinese_name(self, key: str, profile: dict) -> str:
        if not isinstance(profile, dict):
            return ""
        priority_groups = [
            [profile.get("character", "")],
            profile.get("official_aliases") or [],
            profile.get("aliases") or [],
            profile.get("match_terms") or [],
            profile.get("nickname_aliases") or [],
            profile.get("search_aliases") or [],
        ]
        for group in priority_groups:
            if isinstance(group, str):
                group = re.split(r"[,，;；\n\r]+", group)
            for item in group or []:
                name = self._favorite_clean_character_label(item, require_cjk=True)
                if name:
                    return name
        return ""
    def _favorite_profile_display_name(self, key: str, profile: dict, fallback: str = "") -> str:
        chinese = self._favorite_profile_chinese_name(key, profile)
        if chinese:
            return chinese
        for item in [profile.get("character", "") if isinstance(profile, dict) else "", fallback, key]:
            name = self._favorite_clean_character_label(item, require_cjk=False)
            if name:
                return name
        return ""
    def _find_favorite_lora_profile(self, character_hint: str = "", lora_name: str = "") -> tuple:
        hints = [str(character_hint or "").strip(), str(lora_name or "").strip()]
        norm_hints = {self._normalize_profile_match_key(x) for x in hints if x}
        lora_hints = {self._normalize_lora_lookup_key(x) for x in hints if x}
        if not norm_hints and not lora_hints:
            return "", {}
        families = [
            self._normalize_model_family(getattr(self, "active_model_family", MODEL_FAMILY_ANIMA)),
            MODEL_FAMILY_ILLUSTRIOUS,
            MODEL_FAMILY_ANIMA,
        ]
        seen_families = set()
        for family in families:
            if not family or family in seen_families:
                continue
            seen_families.add(family)
            for key, profile in (self._get_lora_profiles(family) or {}).items():
                candidates = self._favorite_profile_lookup_candidates(key, profile)
                profile_norms = {self._normalize_profile_match_key(x) for x in candidates if x}
                profile_loras = {self._normalize_lora_lookup_key(x) for x in candidates if x}
                if (norm_hints & profile_norms) or (lora_hints & profile_loras):
                    return str(key), profile
        return "", {}
    def _favorite_character_hint_from_text(self, text: str) -> str:
        text = str(text or "")
        if not text.strip():
            return ""
        patterns = (
            r"角色档案\s*[:：]\s*([^\n\r]+)",
            r"自动匹配\s*LoRA\s*角色\s*[:：]\s*([^\n\r]+)",
            r"角色\s*[:：]\s*([^\n\r]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.I)
            if match:
                value = self._favorite_clean_character_label(match.group(1), require_cjk=False)
                if value:
                    return value
        return ""
    def _favorite_resolve_character_name(self, related_text: str = "", generated_meta: dict = None) -> str:
        generated_meta = generated_meta or {}
        hints = [
            generated_meta.get("profile_character", ""),
            self._favorite_character_hint_from_text(related_text),
        ]
        lora_name = str(generated_meta.get("lora_name") or "").strip()
        for hint in hints:
            hint = str(hint or "").strip()
            if not hint:
                continue
            key, profile = self._find_favorite_lora_profile(hint, lora_name=lora_name)
            if profile:
                name = self._favorite_profile_display_name(key, profile, fallback=hint)
                if name:
                    return name
            name = self._favorite_clean_character_label(hint, require_cjk=True) or self._favorite_clean_character_label(hint, require_cjk=False)
            if name:
                return name
        if lora_name:
            key, profile = self._find_favorite_lora_profile("", lora_name=lora_name)
            if profile:
                name = self._favorite_profile_display_name(key, profile, fallback="")
                if name:
                    return name
        return "未分类"
    def _favorite_unique_path(self, target: Path) -> Path:
        if not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix
        for index in range(2, 1000):
            candidate = target.with_name(f"{stem}_{index}{suffix}")
            if not candidate.exists():
                return candidate
        return target.with_name(f"{stem}_{uuid.uuid4().hex[:6]}{suffix}")
    def _copy_favorite_image(self, source_path: str, character_name: str = "未分类") -> Path:
        source = Path(str(source_path or "")).expanduser()
        if not source.exists() or not source.is_file():
            raise RuntimeError("引用图片文件不存在，无法收藏")
        suffix = source.suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
            suffix = ".png"
        safe_character = self._favorite_safe_path_part(character_name or "未分类", fallback="未分类")
        target_dir = Path(getattr(self, "favorite_dir", self.data_dir / "favorites")) / safe_character
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{safe_character}_{time.strftime('%Y%m%d_%H%M%S')}{suffix}"
        target = self._favorite_unique_path(target)
        shutil.copy2(source, target)
        return target
    def _favorite_file_sha256(self, path: str) -> str:
        try:
            digest = hashlib.sha256()
            with Path(path).open("rb") as fp:
                for chunk in iter(lambda: fp.read(1024 * 1024), b""):
                    if not chunk:
                        break
                    digest.update(chunk)
            return digest.hexdigest()
        except Exception:
            return ""
    def _find_favorite_image_by_sha256(self, image_sha256: str) -> tuple:
        target_hash = str(image_sha256 or "").strip().lower()
        if not target_hash:
            return None, {}
        root = Path(getattr(self, "favorite_dir", self.data_dir / "favorites"))
        if not root.exists():
            return None, {}
        image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
        try:
            paths = [
                path
                for path in root.rglob("*")
                if path.is_file() and path.suffix.lower() in image_exts
            ]
            paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            return None, {}
        for path in paths:
            meta = self._load_json_file_safe(self._favorite_sidecar_path(path), default={})
            if not isinstance(meta, dict):
                meta = {}
            existing_hash = str(meta.get("image_sha256") or "").strip().lower()
            if not existing_hash:
                existing_hash = self._favorite_file_sha256(str(path)).lower()
            if existing_hash and existing_hash == target_hash:
                return path, meta
        return None, {}
    def _favorite_collector_record(self, event: AstrMessageEvent) -> dict:
        return {
            "sender_id": str(event.get_sender_id()) if event else "",
            "sender_name": self._sender_display_name(event) if event else "",
            "group_id": self._get_group_id(event) if event else None,
            "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    def _merge_favorite_collectors(self, meta: dict, collector: dict) -> dict:
        meta = dict(meta or {})
        collector = dict(collector or {})
        collectors = []
        seen = set()

        def add_collector(item: dict):
            if not isinstance(item, dict):
                return
            user_id = str(item.get("sender_id") or item.get("user_id") or "").strip()
            group_id = str(item.get("group_id") or "").strip()
            name = str(item.get("sender_name") or item.get("name") or "").strip()
            key = (user_id, group_id)
            if not user_id and not name:
                return
            record = {
                "sender_id": user_id,
                "sender_name": name,
                "group_id": group_id or None,
                "collected_at": str(item.get("collected_at") or time.strftime("%Y-%m-%d %H:%M:%S")),
            }
            if key in seen:
                for old in collectors:
                    old_key = (str(old.get("sender_id") or ""), str(old.get("group_id") or ""))
                    if old_key == key:
                        if record.get("sender_name"):
                            old["sender_name"] = record["sender_name"]
                        old["collected_at"] = record["collected_at"]
                        break
                return
            seen.add(key)
            collectors.append(record)

        legacy_sender_id = str(meta.get("sender_id") or "").strip()
        legacy_sender_name = str(meta.get("sender_name") or "").strip()
        if legacy_sender_id or legacy_sender_name:
            add_collector({
                "sender_id": legacy_sender_id,
                "sender_name": legacy_sender_name,
                "group_id": meta.get("group_id"),
                "collected_at": meta.get("created_at") or meta.get("updated_at"),
            })
        for item in meta.get("collectors") or []:
            add_collector(item)
        add_collector(collector)
        meta["collectors"] = collectors
        meta["collector_count"] = len(collectors)
        if collector.get("sender_id") or collector.get("sender_name"):
            meta["last_collector_id"] = str(collector.get("sender_id") or "")
            meta["last_collector_name"] = str(collector.get("sender_name") or "")
            meta["last_collected_at"] = str(collector.get("collected_at") or time.strftime("%Y-%m-%d %H:%M:%S"))
        return meta
    def _merge_duplicate_favorite_meta(self, existing_meta: dict, new_meta: dict, collector: dict) -> dict:
        merged = dict(existing_meta or {})
        new_meta = dict(new_meta or {})
        for key, value in new_meta.items():
            if key in {"created_at", "saved_image_path", "image_sha256", "collectors", "collector_count"}:
                continue
            value_text = str(value or "").strip() if not isinstance(value, (dict, list)) else value
            if not value_text:
                continue
            if key == "favorite_character":
                current = str(merged.get(key) or "").strip()
                if current and current != "未分类":
                    continue
            elif str(merged.get(key) or "").strip():
                continue
            merged[key] = value
        merged.setdefault("created_at", new_meta.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"))
        merged["duplicate_merged"] = True
        merged["duplicate_merged_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        merged = self._merge_favorite_collectors(merged, collector)
        return merged
    def _generated_favorite_sidecar_path(self, image_path: Path) -> Path:
        image_path = Path(image_path)
        return image_path.with_name(f"{image_path.stem}.favorite.json")
    def _favorite_sidecar_path(self, image_path: Path) -> Path:
        image_path = Path(image_path)
        return image_path.with_name(f"{image_path.stem}.favorite.json")
    def _save_favorite_sidecar(self, image_path: Path, meta: dict) -> bool:
        try:
            image_path = Path(image_path)
            payload = dict(meta or {})
            payload["saved_image_path"] = str(image_path)
            payload["image_sha256"] = self._favorite_file_sha256(str(image_path))
            payload["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return self._save_json_file_safe(self._favorite_sidecar_path(image_path), payload)
        except Exception as e:
            logger.debug(f"[ComfyUI] 写入酒狐收藏 sidecar 失败: {e}")
            return False
    def _remember_generated_image_for_favorite(self, event: AstrMessageEvent, image_path: Path, prompt: str, meta: dict = None, source: str = "comfyui") -> None:
        if not getattr(self, "favorite_enabled", False):
            return
        try:
            image_path = Path(image_path)
            if not image_path.exists():
                return
            meta = dict(meta or {})
            image_sha256 = self._favorite_file_sha256(str(image_path))
            payload = {
                "version": PLUGIN_VERSION,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": source,
                "image_path": str(image_path),
                "image_sha256": image_sha256,
                "seed": meta.get("seed"),
                "workflow": meta.get("workflow"),
                "model": meta.get("model") or meta.get("checkpoint"),
                "lora_name": meta.get("lora_name"),
                "profile_character": meta.get("profile_character"),
                "prompt": str(prompt or "")[:3000],
                "maker_name": self._sender_display_name(event) if event else "",
                "sender_id": str(event.get_sender_id()) if event else "",
                "sender_name": self._sender_display_name(event) if event else "",
                "group_id": self._get_group_id(event) if event else None,
            }
            runtime_index = getattr(self, "_generated_favorite_runtime_index", None)
            if not isinstance(runtime_index, dict):
                runtime_index = {}
                self._generated_favorite_runtime_index = runtime_index
            if image_sha256:
                runtime_index[image_sha256] = payload
            runtime_index[str(image_path.resolve())] = payload
            if len(runtime_index) > 1000:
                for old_key in list(runtime_index.keys())[: max(1, len(runtime_index) - 800)]:
                    runtime_index.pop(old_key, None)
        except Exception as e:
            logger.debug(f"[ComfyUI] 写入酒狐收藏生成索引失败: {e}")
    def _lookup_generated_favorite_metadata(self, image_path: str) -> dict:
        target_hash = self._favorite_file_sha256(image_path)
        runtime_index = getattr(self, "_generated_favorite_runtime_index", None)
        if isinstance(runtime_index, dict):
            if target_hash and isinstance(runtime_index.get(target_hash), dict):
                return runtime_index.get(target_hash) or {}
            try:
                resolved = str(Path(image_path).resolve())
                if isinstance(runtime_index.get(resolved), dict):
                    return runtime_index.get(resolved) or {}
            except Exception:
                pass
        if not target_hash:
            return {}
        try:
            sidecars = sorted(
                Path(getattr(self, "output_dir", self.data_dir / "output")).glob("*.favorite.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            sidecars = []
        for sidecar in sidecars[:500]:
            data = self._load_json_file_safe(sidecar, default={})
            if isinstance(data, dict) and str(data.get("image_sha256") or "") == target_hash:
                return data
        return {}
    def _build_favorite_owner_text(self, meta: dict) -> str:
        source_kind = "群聊" if meta.get("group_id") else "私聊"
        source_id = meta.get("group_id") or meta.get("sender_id") or "-"
        sender_name = meta.get("sender_name") or "-"
        sender_id = meta.get("sender_id") or "-"
        text = f"收藏人：{sender_name} / {sender_id}\n来源：{source_kind} {source_id}"
        if meta.get("duplicate_merged"):
            text += "\n状态：同图已存在，已合并收藏人"
        collector_count = int(meta.get("collector_count") or 0)
        if collector_count > 1:
            text += f"\n累计收藏：{collector_count} 人"
        return self._zayu_translate_text(text)
    async def _send_favorite_to_owner(self, event: AstrMessageEvent, image_path: Path, meta: dict) -> tuple:
        owner = str(getattr(self, "favorite_owner_user_id", "") or "").strip()
        if not owner:
            return False, "owner_empty"
        if not owner.isdigit():
            logger.warning(f"[ComfyUI] 酒狐收藏主人 QQ 配置无效: {owner}")
            return False, "owner_invalid"
        bot = getattr(event, "bot", None)
        if not bot:
            logger.warning("[ComfyUI] 酒狐收藏私聊发送失败：当前事件没有 bot 实例")
            return False, "bot_missing"

        text = self._build_favorite_owner_text(meta)
        image_uri = self._onebot_file_uri(Path(image_path))
        message = [
            {"type": "text", "data": {"text": text + "\n"}},
            {"type": "image", "data": {"file": image_uri}},
        ]
        try:
            await asyncio.wait_for(
                self._call_onebot_action(bot, "send_private_msg", user_id=int(owner), message=message),
                timeout=15,
            )
            return True, "sent"
        except Exception as e:
            logger.warning(f"[ComfyUI] 酒狐收藏私聊发送失败: {e}")
            return False, self._short_user_error(e)
    async def _handle_favorite_command(self, event: AstrMessageEvent, raw: str = ""):
        user_id = str(event.get_sender_id())
        if not self._can_use_favorite(user_id):
            yield event.plain_result("🚫 权限不足，仅管理员可使用酒狐收藏")
            return
        if not getattr(self, "favorite_enabled", False):
            yield event.plain_result("酒狐收藏功能未开启，请先在插件配置 favorite_settings.enable 中打开")
            return

        source_paths = await self._extract_source_image_paths(event, limit=1, reply_first=True)
        if not source_paths:
            yield event.plain_result("请引用或附带一张图片后发送：酒狐收藏")
            return

        reply_context = await self._favorite_reply_context(event)
        command_text = self._plain_trigger_text(raw or getattr(event, "message_str", ""))
        related_text = reply_context.get("reply_message_text") or ""
        generated_meta = self._lookup_generated_favorite_metadata(source_paths[0])
        character_name = self._favorite_resolve_character_name(related_text=related_text, generated_meta=generated_meta)
        source_hash = self._favorite_file_sha256(source_paths[0])
        existing_image, existing_meta = self._find_favorite_image_by_sha256(source_hash)
        duplicate_merged = bool(existing_image)
        meta = {
            "version": PLUGIN_VERSION,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sender_id": user_id,
            "sender_name": self._sender_display_name(event),
            "maker_name": str((generated_meta or {}).get("maker_name") or (generated_meta or {}).get("sender_name") or self._sender_display_name(event) or ""),
            "group_id": self._get_group_id(event),
            "favorite_character": character_name,
            "command_text": command_text,
            "source_image_path": str(source_paths[0]),
            "source_image_sha256": source_hash,
            "source": str((generated_meta or {}).get("source") or ""),
            "model": str((generated_meta or {}).get("model") or (generated_meta or {}).get("checkpoint") or ""),
            "workflow": str((generated_meta or {}).get("workflow") or ""),
            "lora_name": str((generated_meta or {}).get("lora_name") or ""),
            "prompt_preview": self._clip_favorite_text(
                str((generated_meta or {}).get("prompt") or related_text or ""),
                limit=500,
            ),
            "reply_text": self._clip_favorite_text(related_text, limit=500),
        }
        collector = self._favorite_collector_record(event)
        if duplicate_merged:
            saved_image = Path(existing_image)
            meta = self._merge_duplicate_favorite_meta(existing_meta, meta, collector)
        else:
            saved_image = self._copy_favorite_image(source_paths[0], character_name=character_name)
            meta = self._merge_favorite_collectors(meta, collector)
        meta["saved_image_path"] = str(saved_image)
        self._save_favorite_sidecar(saved_image, meta)

        await self._send_favorite_to_owner(event, saved_image, meta)
        yield event.plain_result("已收藏（同图已存在，已合并收藏人）" if duplicate_merged else "已收藏")
    def _can_use_favorite(self, user_id: str) -> bool:
        return self._can_manage_lora(user_id)
