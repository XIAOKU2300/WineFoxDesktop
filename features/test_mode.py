from .common import *


class _ComfyTestModeLogHandler(logging.Handler):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def emit(self, record):
        try:
            plugin = self.plugin
            if getattr(plugin, "_test_mode_sending", False):
                return
            message = self.format(record)
            if not plugin._should_forward_test_mode_log(message):
                return
            plugin._schedule_test_mode_log(getattr(record, "levelname", "INFO"), message)
        except Exception:
            pass


class TestModeFeature(PluginFeature):
    def _load_test_mode_state(self):
        data = self._load_json_file_safe(getattr(self, "test_mode_state_path", ""), default={})
        raw_groups = []
        if isinstance(data, dict):
            raw_groups = data.get("groups") or data.get("group_ids") or []
        elif isinstance(data, (list, tuple, set, str)):
            raw_groups = data
        self.test_mode_group_ids = self._normalize_group_id_set(raw_groups)
    def _save_test_mode_state(self) -> bool:
        payload = {
            "version": PLUGIN_VERSION,
            "groups": sorted(getattr(self, "test_mode_group_ids", set()) or []),
            "updated_at": int(time.time()),
        }
        return self._save_json_file_safe(getattr(self, "test_mode_state_path", ""), payload)
    def _install_test_mode_log_handler(self):
        if getattr(self, "_test_mode_log_handler", None):
            return
        handler = _ComfyTestModeLogHandler(self)
        self._test_mode_log_handler = handler
        for logger_name in ("", "astrbot", "astrbot.api"):
            try:
                target = logging.getLogger(logger_name)
                if handler not in target.handlers:
                    target.addHandler(handler)
            except Exception:
                pass
    def _uninstall_test_mode_log_handler(self):
        handler = getattr(self, "_test_mode_log_handler", None)
        if not handler:
            return
        for logger_name in ("", "astrbot", "astrbot.api"):
            try:
                target = logging.getLogger(logger_name)
                if handler in target.handlers:
                    target.removeHandler(handler)
            except Exception:
                pass
        self._test_mode_log_handler = None
    def _should_forward_test_mode_log(self, message: str) -> bool:
        if not getattr(self, "test_mode_group_ids", set()):
            return False
        text = str(message or "")
        if not text or "[JH TestMode]" in text or "[test-mode]" in text:
            return False
        markers = ("[ComfyUI]", "[GPT Magic]", "[Probe]", "[探针]", "[鎺㈤拡]")
        return any(marker in text for marker in markers)
    def _redact_test_mode_text(self, text: str) -> str:
        result = str(text or "")
        secret_values = [
            getattr(self, "prompt_translate_api_key", ""),
            getattr(self, "utility_llm_api_key", ""),
            getattr(self, "img2img_vision_api_key", ""),
            getattr(self, "civitai_api_token", ""),
            getattr(self, "huggingface_api_token", ""),
            getattr(self, "yodayo_cookie", ""),
            getattr(self, "self_update_access_token", ""),
            self._self_update_token() if hasattr(self, "_self_update_token") else "",
        ]
        for value in secret_values:
            value = str(value or "").strip()
            if len(value) >= 6:
                result = result.replace(value, "***")
        result = re.sub(r"(https?://)([^/@\s]+)@([^/\s]+)", r"\1***@\3", result)
        result = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|token|authorization|secret|password|cookie)\s*[:=]\s*)[^\s,;]+", r"\1***", result)
        result = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}", r"\1***", result)
        result = re.sub(r"(?<![A-Za-z0-9_])(?:github_pat_|ghp_|gho_|ghu_|ghs_|ghr_|sk-|hf_)[A-Za-z0-9_\-]{12,}", "***", result)
        return result
    def _chunk_test_mode_text(self, text: str, limit: int = 1500) -> list:
        text = self._redact_test_mode_text(text)
        limit = max(int(limit or 1500), 300)
        chunks = []
        while len(text) > limit:
            cut = text.rfind("\n", 0, limit)
            if cut < max(200, limit // 3):
                cut = limit
            chunks.append(text[:cut].rstrip())
            text = text[cut:].lstrip()
        if text:
            chunks.append(text)
        return chunks or [""]
    def _format_test_mode_payload(self, payload) -> str:
        if payload is None:
            return ""
        try:
            if isinstance(payload, (dict, list, tuple, set)):
                text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            else:
                text = str(payload)
        except Exception:
            text = repr(payload)
        return self._redact_test_mode_text(text)
    def _remember_test_mode_event(self, event: AstrMessageEvent):
        gid = self._get_group_id(event)
        if not gid or gid not in getattr(self, "test_mode_group_ids", set()):
            return ""
        bot = getattr(event, "bot", None)
        if bot:
            self._test_mode_bot_by_group[gid] = bot
        self._test_mode_event_by_group[gid] = event
        return gid
    def _is_test_mode_enabled_for_event(self, event: AstrMessageEvent) -> bool:
        gid = self._remember_test_mode_event(event)
        return bool(gid)
    async def _send_test_mode_group_message(self, group_id: str, text: str, bot=None, event: AstrMessageEvent = None):
        group_id = self._normalize_group_id(group_id)
        if not group_id:
            return False
        bot = bot or getattr(event, "bot", None) or getattr(self, "_test_mode_bot_by_group", {}).get(group_id)
        event = event or getattr(self, "_test_mode_event_by_group", {}).get(group_id)
        sent = False
        for chunk in self._chunk_test_mode_text(text):
            if not chunk:
                continue
            try:
                self._test_mode_sending = True
                if bot:
                    payload_gid = int(group_id) if str(group_id).isdigit() else group_id
                    await self._call_onebot_action(
                        bot,
                        "send_group_msg",
                        group_id=payload_gid,
                        message=[{"type": "text", "data": {"text": chunk}}],
                    )
                    sent = True
                elif event:
                    await event.send(event.plain_result(chunk))
                    sent = True
            except Exception:
                pass
            finally:
                self._test_mode_sending = False
            await asyncio.sleep(0.12)
        return sent
    async def _trace_test_mode(self, event: AstrMessageEvent, stage: str, detail: str = "", payload=None):
        if not self._is_test_mode_enabled_for_event(event):
            return
        gid = self._get_group_id(event)
        lines = [f"[JH TestMode] {stage}"]
        detail = str(detail or "").strip()
        if detail:
            lines.append(detail)
        payload_text = self._format_test_mode_payload(payload)
        if payload_text:
            lines.append(payload_text)
        await self._send_test_mode_group_message(gid, "\n".join(lines), event=event)
    def _schedule_test_mode_log(self, level: str, message: str):
        groups = list(getattr(self, "test_mode_group_ids", set()) or [])
        if not groups:
            return
        message = self._redact_test_mode_text(message)
        now = time.time()
        fingerprint = (str(level or "INFO"), message[:500])
        dedupe = getattr(self, "_test_mode_log_dedupe", None)
        if not isinstance(dedupe, dict):
            dedupe = {}
            self._test_mode_log_dedupe = dedupe
        last = dedupe.get(fingerprint, 0)
        if now - last < 0.8:
            return
        dedupe[fingerprint] = now
        if len(dedupe) > 500:
            cutoff = now - 60
            for key, ts in list(dedupe.items()):
                if ts < cutoff:
                    dedupe.pop(key, None)
        try:
            loop = asyncio.get_running_loop()
        except Exception:
            return
        text = f"[JH TestMode][LOG:{str(level or 'INFO').upper()}]\n{message}"
        for gid in groups:
            bot = getattr(self, "_test_mode_bot_by_group", {}).get(gid)
            if not bot:
                continue
            task = loop.create_task(self._send_test_mode_group_message(gid, text, bot=bot))
            self._test_mode_log_tasks.add(task)
            task.add_done_callback(lambda t, tasks=self._test_mode_log_tasks: tasks.discard(t))
    def _parse_test_mode_action(self, rest: str) -> str:
        compact = re.sub(r"\s+", "", str(rest or "")).lower()
        if compact in {"on", "open", "enable", "enabled", "start", "1", "true", "开", "开启", "打开", "启用"}:
            return "enable"
        if compact in {"off", "close", "disable", "disabled", "stop", "0", "false", "关", "关闭", "停用"}:
            return "disable"
        if compact in {"", "status", "state", "check", "show", "状态", "查看", "看看"}:
            return "status"
        return "help"
    def _parse_plain_test_mode_command(self, raw: str):
        clean = self._plain_trigger_text(raw)
        if not clean:
            return None
        if clean.startswith("/"):
            clean = clean[1:].strip()
        heads = ("酒狐测试模式", "酒狐调试模式", "酒狐debug", "酒狐 debug")
        lower = clean.lower()
        for head in heads:
            if lower.startswith(head.lower()):
                return clean[len(head):].strip()
        return None
    def _is_test_mode_trigger(self, raw: str, event: AstrMessageEvent = None) -> bool:
        return self._parse_plain_test_mode_command(raw) is not None
    async def _handle_test_mode_command(self, event: AstrMessageEvent, rest: str = ""):
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("权限不足：仅管理员可切换测试模式")
            return
        if not self._is_group_message(event):
            yield event.plain_result("测试模式只能在群聊里开启，会把流程日志发回当前群。")
            return
        gid = self._get_group_id(event)
        if not gid:
            yield event.plain_result("无法识别当前群号，不能开启测试模式。")
            return
        action = self._parse_test_mode_action(rest)
        if action == "enable":
            self.test_mode_group_ids.add(gid)
            self._remember_test_mode_event(event)
            self._install_test_mode_log_handler()
            self._save_test_mode_state()
            yield event.plain_result(
                f"JH 测试模式已开启\n"
                f"群号：{gid}\n"
                f"版本：{PLUGIN_VERSION}\n"
                "之后本群会收到绘图、LoRA 搜索、更新流程和插件关键日志。\n"
                "注意：AstrBot 核心白名单仍需先放行本群，否则插件收不到消息。"
            )
            return
        if action == "disable":
            self.test_mode_group_ids.discard(gid)
            self._test_mode_bot_by_group.pop(gid, None)
            self._test_mode_event_by_group.pop(gid, None)
            self._save_test_mode_state()
            yield event.plain_result(f"JH 测试模式已关闭\n群号：{gid}")
            return
        if action == "status":
            enabled = gid in self.test_mode_group_ids
            groups = ", ".join(sorted(self.test_mode_group_ids)) or "无"
            yield event.plain_result(
                f"JH 测试模式状态\n"
                f"当前群：{gid} / {'已开启' if enabled else '未开启'}\n"
                f"已开启群：{groups}\n"
                f"版本：{PLUGIN_VERSION}\n"
                "命令：jh测试模式 开 / jh测试模式 关 / jh测试模式 状态"
            )
            return
        yield event.plain_result("用法：jh测试模式 开 / 关 / 状态，也可用 jh debug on/off/status")
