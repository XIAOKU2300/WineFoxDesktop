from .common import *


class PromptNotebookFeature(PluginFeature):
    def _prompt_note_heads(self) -> tuple:
        prefix = str(getattr(self, "command_prefix", "jh") or "jh").strip() or "jh"
        return (
            "酒狐记下来",
            "酒狐记一下",
            "酒狐记住",
            "酒库记下来",
            f"{prefix}记下来",
            f"{prefix}记一下",
            f"{prefix}记住",
            "jh记下来",
            "jh记一下",
            "jh记住",
        )
    def _prompt_note_command_body(self, raw: str) -> tuple:
        clean = self._plain_trigger_text(raw)
        if not clean or clean.startswith("/"):
            return "", ""
        lower = clean.lower()
        for head in self._prompt_note_heads():
            head = str(head or "").strip()
            if lower.startswith(head.lower()):
                return self._clean_prompt_after_trigger(clean[len(head):]), head
        return "", ""
    def _is_prompt_note_trigger(self, raw: str, event: AstrMessageEvent = None) -> bool:
        _body, head = self._prompt_note_command_body(raw)
        return bool(head)
    def _prompt_notebook_heads(self) -> tuple:
        prefix = str(getattr(self, "command_prefix", "jh") or "jh").strip() or "jh"
        return (
            "酒狐看本本",
            "酒狐本本",
            "酒狐翻本本",
            "酒库看本本",
            "酒库本本",
            f"{prefix}看本本",
            f"{prefix}本本",
            f"{prefix}提示词本",
            "jh看本本",
            "jh本本",
            "jh提示词本",
        )
    def _prompt_notebook_command_body(self, raw: str) -> tuple:
        clean = self._plain_trigger_text(raw)
        if not clean or clean.startswith("/"):
            return "", ""
        lower = clean.lower()
        for head in self._prompt_notebook_heads():
            head = str(head or "").strip()
            if lower.startswith(head.lower()):
                return self._clean_prompt_after_trigger(clean[len(head):]), head
        return "", ""
    def _is_prompt_notebook_trigger(self, raw: str, event: AstrMessageEvent = None) -> bool:
        _body, head = self._prompt_notebook_command_body(raw)
        return bool(head)
    def _prompt_note_session_key(self, event: AstrMessageEvent) -> str:
        user_id = str(event.get_sender_id() or "").strip()
        group_id = self._get_group_id(event)
        if group_id:
            return f"group:{group_id}:user:{user_id}"
        return f"private:user:{user_id}"
    def _has_prompt_note_pending(self, event: AstrMessageEvent) -> bool:
        try:
            self._purge_expired_prompt_note_pending()
            return self._prompt_note_session_key(event) in (getattr(self, "_saved_prompt_pending", {}) or {})
        except Exception:
            return False
    def _prompt_note_scope_meta(self, event: AstrMessageEvent) -> dict:
        group_id = self._get_group_id(event)
        if group_id:
            return {"scope_type": "group", "group_id": str(group_id), "scope_key": f"group:{group_id}"}
        return {"scope_type": "private", "group_id": "", "scope_key": "private"}
    def _prompt_note_message_id(self, event: AstrMessageEvent) -> str:
        for value in (
            getattr(event, "message_id", None),
            getattr(getattr(event, "message_obj", None), "message_id", None),
            (getattr(event, "raw", {}) or {}).get("message_id") if isinstance(getattr(event, "raw", None), dict) else None,
            (getattr(getattr(event, "message_obj", None), "raw", {}) or {}).get("message_id")
            if isinstance(getattr(getattr(event, "message_obj", None), "raw", None), dict) else None,
        ):
            text = str(value or "").strip()
            if text:
                return text
        return ""
    def _clean_prompt_note_text(self, text: str) -> str:
        text = html_lib.unescape(str(text or ""))
        text = re.sub(r"[\u061c\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]", "", text)
        text = re.sub(r"\[CQ:reply[^\]]*\]", "", text, flags=re.I)
        text = re.sub(r"\[CQ:at,[^\]]+\]", "", text, flags=re.I)
        text = re.sub(r"\[CQ:image[^\]]*\]", "", text, flags=re.I)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text
    def _looks_like_prompt_note_text(self, text: str) -> bool:
        text = self._clean_prompt_note_text(text)
        if len(text) < 2:
            return False
        if len(text) > int(getattr(self, "prompt_note_max_prompt_length", 6000) or 6000):
            return False
        lowered = text.lower()
        blocked_heads = (
            "酒狐来点", "酒狐魔法", "酒狐看看", "酒狐收藏", "酒狐记下来", "酒库记下来",
            "酒狐看本本", "酒狐本本", "酒库看本本", "酒库本本",
            "jh", "/jh", "/comfy", "comfy",
        )
        if any(lowered == head.lower() for head in blocked_heads):
            return False
        return True
    def _onebot_text_from_raw_message(self, raw_message: str) -> str:
        text = str(raw_message or "")
        text = re.sub(r"\[CQ:(?:image|record|video|file),[^\]]+\]", " ", text, flags=re.I)
        text = re.sub(r"\[CQ:reply[^\]]*\]", " ", text, flags=re.I)
        text = re.sub(r"\[CQ:at,[^\]]+\]", " ", text, flags=re.I)
        return self._clean_prompt_note_text(text)
    def _onebot_text_from_message(self, msg: dict) -> str:
        texts = []

        def add(value):
            text = self._clean_prompt_note_text(value)
            if text:
                texts.append(text)

        def scan_segment(seg):
            if isinstance(seg, str):
                add(self._onebot_text_from_raw_message(seg))
                return
            if not isinstance(seg, dict):
                return
            seg_type = str(seg.get("type") or "").lower()
            if seg_type == "text":
                data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
                add(data.get("text") or data.get("content") or seg.get("text") or seg.get("content"))
                return
            if isinstance(seg.get("textElement"), dict):
                add(seg.get("textElement", {}).get("content"))
            if isinstance(seg.get("data"), dict) and isinstance(seg.get("data", {}).get("textElement"), dict):
                add(seg.get("data", {}).get("textElement", {}).get("content"))

        if not isinstance(msg, dict):
            return ""
        for key in ("sourceMsgText", "raw_message", "message_text", "text", "content"):
            add(self._onebot_text_from_raw_message(msg.get(key)) if key == "raw_message" else msg.get(key))
        message = msg.get("message")
        if isinstance(message, list):
            for seg in message:
                scan_segment(seg)
        elif isinstance(message, str):
            add(self._onebot_text_from_raw_message(message))
        raw = msg.get("raw")
        if isinstance(raw, dict):
            for key in ("raw_message", "message"):
                value = raw.get(key)
                if isinstance(value, str):
                    add(self._onebot_text_from_raw_message(value))
                elif isinstance(value, list):
                    for seg in value:
                        scan_segment(seg)
            for elem in raw.get("elements") or []:
                scan_segment(elem)
        for elem in msg.get("elements") or []:
            scan_segment(elem)
        return "\n".join(dict.fromkeys(texts)).strip()
    def _prompt_note_reply_component_text(self, reply) -> str:
        chain = (
            getattr(reply, "chain", None)
            or getattr(reply, "message", None)
            or getattr(reply, "messages", None)
            or getattr(reply, "content", None)
            or []
        )
        if isinstance(chain, (str, dict)):
            chain = [chain]
        texts = []
        for item in chain or []:
            text = self._extract_plain_text_from_component(item)
            text = self._clean_prompt_note_text(text)
            if text:
                texts.append(text)
        return "\n".join(texts).strip()
    async def _prompt_note_reply_text_via_onebot(self, event: AstrMessageEvent, reply) -> str:
        if event.get_platform_name() != "aiocqhttp":
            return ""
        reply_id = str(getattr(reply, "id", "") or "").strip()
        if not reply_id:
            return ""
        try:
            msg_id = int(reply_id) if reply_id.isdigit() else reply_id
            data = await asyncio.wait_for(
                self._call_onebot_action(getattr(event, "bot", None), "get_msg", message_id=msg_id),
                timeout=4,
            )
            return self._onebot_text_from_message(data if isinstance(data, dict) else {})
        except Exception as e:
            logger.debug(f"[ComfyUI] 酒狐记下来通过 OneBot 获取引用文字失败: {e}")
            return ""
    async def _prompt_note_raw_reply_text(self, event: AstrMessageEvent) -> str:
        raw = self._event_raw_dict(event)
        if not isinstance(raw, dict):
            return ""
        texts = []
        seen_reply_ids = set()
        for reply in self._walk_nested_reply_elements(raw):
            if not isinstance(reply, dict):
                continue
            for key in ("sourceMsgText", "text", "content", "raw_message"):
                text = self._onebot_text_from_raw_message(reply.get(key)) if key == "raw_message" else self._clean_prompt_note_text(reply.get(key))
                if text:
                    texts.append(text)
            source = reply.get("sourceMsg")
            if isinstance(source, dict):
                text = self._onebot_text_from_message(source)
                if text:
                    texts.append(text)
            for reply_id in self._reply_message_ids_from_raw_reply(reply):
                if reply_id in seen_reply_ids:
                    continue
                seen_reply_ids.add(reply_id)
                try:
                    payload_id = int(reply_id) if str(reply_id).isdigit() else reply_id
                    data = await asyncio.wait_for(
                        self._call_onebot_action(getattr(event, "bot", None), "get_msg", message_id=payload_id),
                        timeout=4,
                    )
                    text = self._onebot_text_from_message(data if isinstance(data, dict) else {})
                    if text:
                        texts.append(text)
                except Exception:
                    continue
        return "\n".join(dict.fromkeys(t for t in texts if t)).strip()
    async def _extract_prompt_note_reply_text(self, event: AstrMessageEvent) -> str:
        texts = []
        components = self._iter_event_components(event)
        for reply in self._iter_reply_components(components):
            text = self._prompt_note_reply_component_text(reply)
            if not text:
                text = await self._prompt_note_reply_text_via_onebot(event, reply)
            if text:
                texts.append(text)
        raw_text = await self._prompt_note_raw_reply_text(event)
        if raw_text:
            texts.append(raw_text)
        return "\n".join(dict.fromkeys(t for t in texts if t)).strip()
    async def _extract_prompt_note_source(self, event: AstrMessageEvent, raw: str) -> tuple:
        body, _head = self._prompt_note_command_body(raw)
        body = self._clean_prompt_note_text(body)
        if body:
            return body, "direct"
        reply_text = await self._extract_prompt_note_reply_text(event)
        reply_text = self._clean_prompt_note_text(reply_text)
        if reply_text:
            return reply_text, "reply"
        return "", ""
    def _prompt_note_pending_preview(self, prompt: str, limit: int = 180) -> str:
        prompt = self._clean_prompt_note_text(prompt)
        prompt = re.sub(r"\s+", " ", prompt)
        if len(prompt) <= limit:
            return prompt
        return prompt[: max(20, limit - 10)].rstrip() + "..."
    def _purge_expired_prompt_note_pending(self) -> None:
        pending = getattr(self, "_saved_prompt_pending", None)
        if not isinstance(pending, dict):
            self._saved_prompt_pending = {}
            return
        now = time.time()
        ttl = max(int(getattr(self, "prompt_note_pending_timeout", 180) or 180), 30)
        for key in list(pending.keys()):
            if now - float((pending.get(key) or {}).get("created_ts") or 0) > ttl:
                pending.pop(key, None)
    async def _handle_prompt_note_command(self, event: AstrMessageEvent, raw: str):
        if not getattr(self, "prompt_note_enabled", True):
            yield event.plain_result("❌ 提示词记忆功能未开启，请在 WebUI 的 prompt_note_settings.enable 打开")
            return
        prompt, source_mode = await self._extract_prompt_note_source(event, raw)
        if not self._looks_like_prompt_note_text(prompt):
            yield event.plain_result(
                "请引用一段文字提示词，或直接发送：酒狐记下来 <提示词>\n"
                "仅支持文字；读取到提示词后我会再让你命名。"
            )
            return
        prompt = self._clean_prompt_note_text(prompt)
        scope = self._prompt_note_scope_meta(event)
        session_key = self._prompt_note_session_key(event)
        self._purge_expired_prompt_note_pending()
        self._saved_prompt_pending[session_key] = {
            "prompt": prompt,
            "source_mode": source_mode,
            "command_text": self._plain_trigger_text(raw),
            "created_ts": time.time(),
            "sender_id": str(event.get_sender_id() or ""),
            "sender_name": self._sender_display_name(event),
            "message_id": self._prompt_note_message_id(event),
            **scope,
        }
        yield event.plain_result(
            "读到了。请为这条提示词起一个名字/标签。\n"
            "格式：标题：你的名字\n"
            "也可以直接发名字；发送“取消”放弃。\n"
            f"预览：{self._prompt_note_pending_preview(prompt)}"
        )
    def _prompt_note_title_from_text(self, text: str) -> str:
        text = self._clean_prompt_note_text(text)
        if not text:
            return ""
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        first_line = re.sub(r"^(?:标题|题目|名称|名字|命名|标签|tag|title|name)\s*[:：=]\s*", "", first_line, flags=re.I).strip()
        first_line = first_line.strip("「」『』《》“”\"'` ")
        first_line = re.sub(r"\s+", " ", first_line)
        max_len = int(getattr(self, "prompt_note_max_title_length", 60) or 60)
        if len(first_line) > max_len:
            first_line = first_line[:max_len].rstrip()
        return first_line
    def _prompt_note_key_from_title(self, title: str) -> str:
        key = str(title or "").strip().lower()
        key = re.sub(r"[^\u3400-\u9fff\u3040-\u30ffA-Za-z0-9]+", "", key)
        if key:
            return key[:80]
        return hashlib.sha256(str(title or "").encode("utf-8")).hexdigest()[:16]
    def _prompt_note_cancel_text(self, text: str) -> bool:
        clean = re.sub(r"\s+", "", self._clean_prompt_note_text(text)).lower()
        return clean in {"取消", "算了", "不记了", "cancel", "c"}
    def _prompt_note_store_default(self) -> dict:
        return {"version": PLUGIN_VERSION, "entries": {}, "updated_at": ""}
    def _prompt_note_entry_matches(self, record: dict, pending: dict, key: str) -> bool:
        return (
            isinstance(record, dict)
            and str(record.get("owner_user_id") or "") == str(pending.get("sender_id") or "")
            and str(record.get("scope_type") or "") == str(pending.get("scope_type") or "")
            and str(record.get("group_id") or "") == str(pending.get("group_id") or "")
            and str(record.get("key") or "") == str(key or "")
        )
    def _prompt_note_dedupe_texts(self, values, limit: int = 12, max_len: int = 60) -> list:
        result = []
        seen = set()
        if isinstance(values, (str, int, float)):
            raw_values = re.split(r"[,，、\n]+", str(values))
        elif isinstance(values, (list, tuple, set)):
            raw_values = values
        else:
            raw_values = []
        for value in raw_values:
            text = self._clean_prompt_note_text(value)
            text = text.strip("「」『』“”\"'` ")
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                continue
            if len(text) > max_len:
                text = text[:max_len].rstrip()
            key = self._prompt_note_key_from_title(text)
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(text)
            if len(result) >= limit:
                break
        return result
    def _fallback_prompt_note_template(self, title: str, raw_prompt: str, reason: str = "") -> dict:
        raw_prompt = self._clean_prompt_note_text(raw_prompt)
        summary = self._prompt_note_pending_preview(raw_prompt, 80) or title
        return {
            "template_version": 1,
            "draw_prompt": raw_prompt,
            "template_text": raw_prompt,
            "summary": summary,
            "aliases": self._prompt_note_dedupe_texts([title], limit=4),
            "trigger_words": self._prompt_note_dedupe_texts([title], limit=4),
            "llm_normalized": False,
            "normalize_error": reason,
        }
    async def _normalize_prompt_note_template_with_llm(self, event: AstrMessageEvent, title: str, raw_prompt: str) -> dict:
        raw_prompt = self._clean_prompt_note_text(raw_prompt)
        fallback = self._fallback_prompt_note_template(title, raw_prompt)
        if not raw_prompt:
            fallback["normalize_error"] = "empty_prompt"
            return fallback
        if not getattr(self, "prompt_note_template_llm_enabled", True):
            fallback["normalize_error"] = "template_llm_disabled"
            return fallback
        if not self._utility_llm_configured():
            fallback["normalize_error"] = "utility_llm_not_configured"
            return fallback
        system_prompt = (
            "You normalize user-saved drawing prompts for an AstrBot ComfyUI plugin. "
            "Return exactly one JSON object. Do not call tools, do not mention ComfyUI nodes, and do not add anything to a workflow. "
            "Keep the user's intent, characters, LoRA trigger words, style words, composition, and constraints if present. "
            "Do not invent unrelated characters, scenes, poses, or model names. "
            "The field draw_prompt must be one reusable Stable Diffusion prompt text. Prefer concise English comma-separated tags when you can, "
            "but preserve Chinese or special trigger words when translation would damage meaning. "
            "Also return a short Chinese summary, aliases, and trigger_words for lookup. "
            "Schema: {\"draw_prompt\":\"...\",\"summary\":\"...\",\"aliases\":[\"...\"],\"trigger_words\":[\"...\"]}"
        )
        user_prompt = json.dumps(
            {
                "title": title,
                "raw_prompt": raw_prompt,
                "rules": [
                    "draw_prompt is plain text only, not markdown and not JSON inside a string",
                    "do not add quality boilerplate unless the user already supplied it",
                    "aliases and trigger_words should be short lookup names that the same user may type after 酒狐看本本",
                ],
            },
            ensure_ascii=False,
        )
        try:
            data = await self._call_plugin_llm_json(system_prompt, user_prompt, temperature=0.1, max_tokens=700)
            if not isinstance(data, dict):
                fallback["normalize_error"] = "llm_no_json"
                return fallback
            draw_prompt = self._clean_prompt_note_text(
                data.get("draw_prompt") or data.get("prompt") or data.get("template_text") or ""
            )
            if not draw_prompt:
                fallback["normalize_error"] = "llm_empty_draw_prompt"
                return fallback
            max_len = int(getattr(self, "prompt_note_max_prompt_length", 6000) or 6000)
            if len(draw_prompt) > max_len:
                draw_prompt = draw_prompt[:max_len].rstrip()
            summary = self._clean_prompt_note_text(data.get("summary") or data.get("description") or "")
            summary = self._prompt_note_pending_preview(summary or draw_prompt, 90)
            aliases = self._prompt_note_dedupe_texts([title] + self._prompt_note_dedupe_texts(data.get("aliases"), limit=8), limit=8)
            trigger_words = self._prompt_note_dedupe_texts([title] + self._prompt_note_dedupe_texts(data.get("trigger_words"), limit=8), limit=8)
            await self._trace_test_mode(
                event,
                "prompt_note_template_normalized",
                payload={"title": title, "llm": True, "draw_prompt_len": len(draw_prompt)},
            )
            return {
                "template_version": 1,
                "draw_prompt": draw_prompt,
                "template_text": draw_prompt,
                "summary": summary,
                "aliases": aliases or self._prompt_note_dedupe_texts([title], limit=4),
                "trigger_words": trigger_words or self._prompt_note_dedupe_texts([title], limit=4),
                "llm_normalized": True,
                "normalize_error": "",
            }
        except Exception as e:
            reason = self._short_user_error(e, 120)
            logger.warning(f"[ComfyUI] 酒狐记下来模板整理失败，使用原文保存: {reason}")
            fallback["normalize_error"] = reason
            return fallback
    def _prompt_notebook_record_draw_prompt(self, record: dict) -> str:
        if not isinstance(record, dict):
            return ""
        template = record.get("template") if isinstance(record.get("template"), dict) else {}
        return self._clean_prompt_note_text(
            record.get("draw_prompt")
            or record.get("template_text")
            or template.get("draw_prompt")
            or template.get("template_text")
            or record.get("prompt")
            or record.get("raw_prompt")
        )
    def _prompt_notebook_record_names(self, record: dict) -> list:
        if not isinstance(record, dict):
            return []
        template = record.get("template") if isinstance(record.get("template"), dict) else {}
        values = [
            record.get("title"),
            record.get("key"),
        ]
        for field in ("aliases", "trigger_words", "tags"):
            values.extend(self._prompt_note_dedupe_texts(record.get(field), limit=12))
            values.extend(self._prompt_note_dedupe_texts(template.get(field), limit=12))
        return self._prompt_note_dedupe_texts(values, limit=24)
    def _prompt_notebook_records_for_event(self, event: AstrMessageEvent) -> list:
        data = self._load_json_file_safe(self.saved_prompt_store_path, default=self._prompt_note_store_default())
        entries = data.get("entries") if isinstance(data, dict) else {}
        if not isinstance(entries, dict):
            return []
        scope = self._prompt_note_scope_meta(event)
        owner_id = str(event.get_sender_id() or "")
        records = []
        for item_id, item in entries.items():
            if not isinstance(item, dict):
                continue
            if str(item.get("owner_user_id") or "") != owner_id:
                continue
            scope_key = str(item.get("scope_key") or "")
            if not scope_key:
                group_id = str(item.get("group_id") or "")
                scope_key = f"group:{group_id}" if group_id else "private"
            if scope_key != str(scope.get("scope_key") or ""):
                continue
            record = dict(item)
            record.setdefault("id", str(item_id))
            records.append(record)
        records.sort(
            key=lambda record: str(record.get("last_used_at") or record.get("updated_at") or record.get("created_at") or ""),
            reverse=True,
        )
        return records
    def _format_prompt_notebook_list(self, event: AstrMessageEvent, records: list, limit: int = 12) -> str:
        scope = self._prompt_note_scope_meta(event)
        scope_name = "本群" if scope.get("scope_type") == "group" else "私聊"
        if not records:
            return (
                f"📒 酒狐本本（{scope_name}）还是空的。\n"
                "发送：酒狐记下来 <提示词>\n"
                "或引用一段文字后发送：酒狐记下来"
            )
        lines = [
            f"📒 酒狐本本（{scope_name}）",
            "━━━━━━━━━━━━━━━━━━",
            "酒狐看本本 <标题/触发词>  会直接把模板拿去画",
        ]
        for index, record in enumerate(records[:limit], 1):
            title = str(record.get("title") or "未命名").strip()
            draw_prompt = self._prompt_notebook_record_draw_prompt(record)
            preview = self._prompt_note_pending_preview(draw_prompt, 52)
            names = [name for name in self._prompt_notebook_record_names(record) if name and name != title and name != str(record.get("key") or "")]
            alias_text = f"｜触发：{'、'.join(names[:2])}" if names else ""
            use_count = int(record.get("use_count") or 0)
            lines.append(f"{index}. {title}{alias_text}｜用过 {use_count} 次")
            if preview:
                lines.append(f"   {preview}")
        if len(records) > limit:
            lines.append(f"还有 {len(records) - limit} 条，输入更具体的标题/触发词来画。")
        return "\n".join(lines)
    def _resolve_prompt_notebook_record(self, records: list, query: str) -> tuple:
        query = self._clean_prompt_note_text(query)
        query = re.sub(r"\s+", " ", query).strip()
        if not query:
            return None, "", []
        query_lower = query.lower()
        query_key = self._prompt_note_key_from_title(query)

        def unique(items):
            result = []
            seen = set()
            for record, extra in items:
                record_id = str(record.get("id") or "")
                if record_id in seen:
                    continue
                seen.add(record_id)
                result.append((record, extra))
            return result

        exact = []
        for record in records:
            for name in self._prompt_notebook_record_names(record):
                name_key = self._prompt_note_key_from_title(name)
                if query_lower == str(name or "").lower() or (query_key and query_key == name_key):
                    exact.append((record, ""))
                    break
        exact = unique(exact)
        if len(exact) == 1:
            return exact[0][0], exact[0][1], []
        if len(exact) > 1:
            return None, "", [item[0] for item in exact]

        prefix = []
        for record in records:
            best = None
            for name in sorted(self._prompt_notebook_record_names(record), key=len, reverse=True):
                name = str(name or "").strip()
                if len(name) < 1:
                    continue
                if query_lower.startswith(name.lower()):
                    extra = query[len(name):].strip()
                    extra = re.sub(r"^[\s:：,，;；+＋/|-]+", "", extra).strip()
                    candidate = (len(name), record, extra)
                    if not best or candidate[0] > best[0]:
                        best = candidate
            if best:
                prefix.append((best[0], best[1], best[2]))
        if prefix:
            prefix.sort(key=lambda item: item[0], reverse=True)
            max_len = prefix[0][0]
            best_records = unique([(record, extra) for length, record, extra in prefix if length == max_len])
            if len(best_records) == 1:
                return best_records[0][0], best_records[0][1], []
            return None, "", [item[0] for item in best_records]
        return None, "", []
    def _format_prompt_notebook_ambiguous(self, records: list) -> str:
        lines = ["找到多个相近的本本，请把标题/触发词说完整一点："]
        for record in records[:6]:
            title = str(record.get("title") or "未命名").strip()
            names = [name for name in self._prompt_notebook_record_names(record) if name and name != title and name != str(record.get("key") or "")]
            suffix = f"（也可叫：{'、'.join(names[:2])}）" if names else ""
            lines.append(f"- {title}{suffix}")
        return "\n".join(lines)
    def _format_prompt_notebook_not_found(self, event: AstrMessageEvent, query: str, records: list) -> str:
        if not records:
            return self._format_prompt_notebook_list(event, records)
        titles = "、".join(str(record.get("title") or "未命名") for record in records[:8])
        return (
            f"没找到「{self._prompt_note_pending_preview(query, 40)}」这个本本。\n"
            f"你在当前会话里有：{titles}\n"
            "发送：酒狐看本本  可以查看完整列表。"
        )
    def _merge_prompt_notebook_extra(self, draw_prompt: str, extra: str) -> str:
        draw_prompt = self._clean_prompt_note_text(draw_prompt)
        extra = self._clean_prompt_note_text(extra)
        if not extra:
            return draw_prompt
        if not draw_prompt:
            return extra
        if draw_prompt.endswith((",", "，", ";", "；", "\n")):
            return f"{draw_prompt} {extra}".strip()
        return f"{draw_prompt}, {extra}".strip()
    async def _touch_prompt_notebook_record(self, record_id: str, event: AstrMessageEvent) -> None:
        record_id = str(record_id or "").strip()
        if not record_id:
            return
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        async with self._saved_prompt_lock:
            data = self._load_json_file_safe(self.saved_prompt_store_path, default=self._prompt_note_store_default())
            entries = data.get("entries") if isinstance(data, dict) else {}
            record = entries.get(record_id) if isinstance(entries, dict) else None
            if not isinstance(record, dict):
                return
            record["use_count"] = int(record.get("use_count") or 0) + 1
            record["last_used_at"] = now
            record["last_used_by"] = str(event.get_sender_id() or "")
            data["updated_at"] = now
            self._save_json_file_safe(self.saved_prompt_store_path, data)
    async def _handle_prompt_notebook_command(self, event: AstrMessageEvent, raw: str):
        if not getattr(self, "prompt_note_enabled", True):
            yield event.plain_result("❌ 提示词本本功能未开启，请在 WebUI 的 prompt_note_settings.enable 打开")
            return
        query, _head = self._prompt_notebook_command_body(raw)
        query = self._clean_prompt_note_text(query)
        records = self._prompt_notebook_records_for_event(event)
        if not query:
            yield event.plain_result(self._format_prompt_notebook_list(event, records))
            return
        record, extra, ambiguous = self._resolve_prompt_notebook_record(records, query)
        if ambiguous:
            yield event.plain_result(self._format_prompt_notebook_ambiguous(ambiguous))
            return
        if not record:
            yield event.plain_result(self._format_prompt_notebook_not_found(event, query, records))
            return
        draw_prompt = self._prompt_notebook_record_draw_prompt(record)
        if not self._looks_like_prompt_note_text(draw_prompt):
            yield event.plain_result("这个本本里的提示词为空或不可用，可以重新用「酒狐记下来」覆盖同名标题。")
            return
        prompt = self._merge_prompt_notebook_extra(draw_prompt, extra)
        await self._touch_prompt_notebook_record(str(record.get("id") or ""), event)
        await self._trace_test_mode(
            event,
            "prompt_notebook_draw",
            payload={
                "record_id": str(record.get("id") or ""),
                "title": str(record.get("title") or ""),
                "extra": extra,
                "prompt_len": len(prompt),
            },
        )
        logger.info(f"[ComfyUI] 酒狐看本本命中: title={record.get('title')} prompt_len={len(prompt)}")
        async for result in self._handle_paint_prompt(event, prompt, direct_send=False):
            yield result
    async def _save_prompt_note_record(self, event: AstrMessageEvent, pending: dict, title: str) -> tuple:
        title = self._prompt_note_title_from_text(title)
        key = self._prompt_note_key_from_title(title)
        raw_prompt = self._clean_prompt_note_text(pending.get("prompt"))
        template = await self._normalize_prompt_note_template_with_llm(event, title, raw_prompt)
        draw_prompt = self._clean_prompt_note_text(template.get("draw_prompt") or raw_prompt)
        aliases = self._prompt_note_dedupe_texts([title] + self._prompt_note_dedupe_texts(template.get("aliases"), limit=8), limit=10)
        trigger_words = self._prompt_note_dedupe_texts([title] + self._prompt_note_dedupe_texts(template.get("trigger_words"), limit=8), limit=10)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        async with self._saved_prompt_lock:
            data = self._load_json_file_safe(self.saved_prompt_store_path, default=self._prompt_note_store_default())
            if not isinstance(data, dict):
                data = self._prompt_note_store_default()
            entries = data.get("entries")
            if not isinstance(entries, dict):
                entries = {}
                data["entries"] = entries

            record_id = ""
            updated = False
            for item_id, record in entries.items():
                if self._prompt_note_entry_matches(record, pending, key):
                    record_id = str(item_id)
                    updated = True
                    break
            if not record_id:
                record_id = uuid.uuid4().hex

            old = entries.get(record_id) if isinstance(entries.get(record_id), dict) else {}
            record = {
                "id": record_id,
                "version": 2,
                "type": "prompt_template",
                "title": title,
                "key": key,
                "prompt": raw_prompt,
                "raw_prompt": raw_prompt,
                "draw_prompt": draw_prompt,
                "template_text": draw_prompt,
                "summary": self._clean_prompt_note_text(template.get("summary") or ""),
                "aliases": aliases,
                "trigger_words": trigger_words,
                "template": {
                    "version": int(template.get("template_version") or 1),
                    "text": draw_prompt,
                    "draw_prompt": draw_prompt,
                    "summary": self._clean_prompt_note_text(template.get("summary") or ""),
                    "aliases": aliases,
                    "trigger_words": trigger_words,
                    "llm_normalized": bool(template.get("llm_normalized")),
                    "normalize_error": str(template.get("normalize_error") or ""),
                },
                "owner_user_id": str(pending.get("sender_id") or event.get_sender_id() or ""),
                "owner_name": str(pending.get("sender_name") or self._sender_display_name(event) or ""),
                "scope_type": str(pending.get("scope_type") or "private"),
                "scope_key": str(pending.get("scope_key") or "private"),
                "group_id": str(pending.get("group_id") or ""),
                "source": {
                    "mode": str(pending.get("source_mode") or ""),
                    "command_text": str(pending.get("command_text") or ""),
                    "message_id": str(pending.get("message_id") or ""),
                },
                "tags": self._prompt_note_dedupe_texts([title] + trigger_words, limit=12),
                "created_at": old.get("created_at") or now,
                "updated_at": now,
                "revision": int(old.get("revision") or 0) + 1,
                "use_count": int(old.get("use_count") or 0),
                "last_used_at": old.get("last_used_at") or "",
            }
            entries[record_id] = record

            max_entries = int(getattr(self, "prompt_note_max_entries_per_user", 200) or 200)
            owned = [
                (item_id, item)
                for item_id, item in entries.items()
                if isinstance(item, dict)
                and str(item.get("owner_user_id") or "") == record["owner_user_id"]
                and str(item.get("scope_key") or "") == record["scope_key"]
            ]
            if len(owned) > max_entries:
                owned.sort(key=lambda pair: str((pair[1] or {}).get("updated_at") or ""))
                for item_id, _item in owned[: len(owned) - max_entries]:
                    if item_id != record_id:
                        entries.pop(item_id, None)

            data["version"] = PLUGIN_VERSION
            data["updated_at"] = now
            self._save_json_file_safe(self.saved_prompt_store_path, data)
            return record, updated
    async def _handle_prompt_note_pending_title(self, event: AstrMessageEvent, raw_candidates: list) -> tuple:
        self._purge_expired_prompt_note_pending()
        session_key = self._prompt_note_session_key(event)
        pending = (getattr(self, "_saved_prompt_pending", {}) or {}).get(session_key)
        if not pending:
            return False, None
        text = next((str(item or "").strip() for item in raw_candidates or [] if str(item or "").strip()), "")
        if not text:
            return True, event.plain_result("请发一个名字/标签，例如：标题：黄昏街景。发送“取消”放弃。")
        if self._prompt_note_cancel_text(text):
            self._saved_prompt_pending.pop(session_key, None)
            return True, event.plain_result("已取消，这条提示词没有保存。")
        title = self._prompt_note_title_from_text(text)
        if not title:
            return True, event.plain_result("名字不能为空。格式：标题：你的名字；发送“取消”放弃。")
        record, updated = await self._save_prompt_note_record(event, pending, title)
        self._saved_prompt_pending.pop(session_key, None)
        action = "已更新" if updated else "已记下来"
        template = record.get("template") if isinstance(record.get("template"), dict) else {}
        mode = "已整理成画图模板" if template.get("llm_normalized") else "已保存为原文模板"
        return True, event.plain_result(
            f"{action}：{record.get('title')}\n"
            f"{mode}，已按用户和会话分类保存。\n"
            f"使用：酒狐看本本 {record.get('title')}"
        )
