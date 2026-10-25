"""Minimal OpenAI-compatible text client used by the desktop prompt pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import aiohttp


class AIProviderError(RuntimeError):
    """A public diagnostic that never includes response bodies or credentials."""
    def __init__(self, message: str):
        self.safe_message = message
        super().__init__(message)


async def _read_response(response: aiohttp.ClientResponse, limit: int = 2 * 1024 * 1024) -> bytes:
    # StreamReader.read(n) may return just the currently available first packet.
    # A chat response often arrives in multiple chunks even with stream=false.
    chunks, size = [], 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > limit:
            raise AIProviderError("AI 提供商响应过大")
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass(frozen=True)
class ResolvedAIProvider:
    """Non-sensitive endpoint selection plus the exact SecretStore entry to use."""

    enabled: bool
    base_url: str
    model: str
    timeout_seconds: float
    secret_name: str
    source: str


def resolve_system_injection(settings: Mapping[str, Any]) -> str:
    """从设置解析头部注入文本：开关关闭或内容为空时返回空串（= 不注入）。"""
    ui = settings.get("ui")
    ui = ui if isinstance(ui, Mapping) else {}
    if not ui.get("prompt_injection_enabled"):
        return ""
    return str(ui.get("prompt_injection") or "").strip()


# 全局头部注入来源：每次 LLM 请求时实时读取（而非构造客户端时快照），
# 这样 LoRA 匹配/搜索等缓存客户端的场景下，用户改设置也能立即生效。
_system_injection_provider: Callable[[], str] | None = None


def set_system_injection_provider(provider: Callable[[], str] | None) -> None:
    """注册/清除全局注入来源。组合根（DesktopRuntime）启动时设置一次。"""
    global _system_injection_provider
    _system_injection_provider = provider


def _apply_system_injection(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把注入文本放到消息列表头部，保证它是 AI 第一个读到的内容。

    已有 system 消息 -> 拼在其内容最前面；没有 -> 在头部插入一条 system 消息。
    读取来源失败或文本为空时原样返回，绝不影响正常请求。
    """
    provider = _system_injection_provider
    if provider is None:
        return messages
    try:
        injection = str(provider() or "").strip()
    except Exception:  # noqa: BLE001 - 注入是增强，不能拖垮请求
        return messages
    if not injection:
        return messages
    result = [dict(message) for message in messages]
    if result and isinstance(result[0].get("content"), str) and result[0].get("role") == "system":
        result[0]["content"] = f"{injection}\n\n{result[0]['content']}"
    else:
        result.insert(0, {"role": "system", "content": injection})
    return result


def resolve_provider(settings: Mapping[str, Any], purpose: str = "text") -> ResolvedAIProvider:
    """Resolve text or vision settings without ever mixing independent credentials.

    Vision may explicitly follow the text endpoint. In that mode sharing the text
    secret is intentional; otherwise the resolver always selects ``vision_api_key``.
    """

    if purpose not in {"text", "vision"}:
        raise ValueError("purpose must be 'text' or 'vision'")
    text = settings.get("ai_provider")
    text = text if isinstance(text, Mapping) else {}
    if purpose == "text":
        selected = text
        source = "ai_provider"
        secret_name = "ai_api_key"
        enabled = bool(selected.get("enabled"))
        timeout = selected.get("timeout_seconds", 60)
    else:
        vision = settings.get("vision_provider")
        vision = vision if isinstance(vision, Mapping) else {}
        follows_text = bool(vision.get("follow_text_provider", True))
        selected = text if follows_text else vision
        source = "ai_provider" if follows_text else "vision_provider"
        secret_name = "ai_api_key" if follows_text else "vision_api_key"
        enabled = bool(vision.get("enabled"))
        # Image requests keep their own timeout even when endpoint credentials follow text.
        timeout = vision.get("timeout_seconds", 90)
    try:
        timeout_seconds = max(5.0, min(float(timeout), 180.0))
    except (TypeError, ValueError):
        timeout_seconds = 90.0 if purpose == "vision" else 60.0
    return ResolvedAIProvider(
        enabled=enabled,
        base_url=str(selected.get("base_url") or "").strip(),
        model=str(selected.get("model") or "").strip(),
        timeout_seconds=timeout_seconds,
        secret_name=secret_name,
        source=source,
    )


class OpenAICompatibleLLMClient:
    def __init__(self, base_url: str, api_key: str = "", *, timeout_seconds: float = 60.0) -> None:
        base = str(base_url or "").strip().rstrip("/")
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("AI Base URL 必须是有效的 HTTP 或 HTTPS 地址")
        self.endpoint = self._completion_endpoint(base, parsed.path)
        self.api_key = str(api_key or "").strip()
        self.timeout_seconds = max(5.0, min(float(timeout_seconds), 180.0))

    @staticmethod
    def _completion_endpoint(base: str, path: str) -> str:
        lowered = path.rstrip("/").lower()
        if lowered.endswith("/chat/completions"):
            return base
        if lowered.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 450,
        temperature: float = 0.1,
        inject_system: bool = True,
    ) -> dict[str, Any]:
        model_name = str(model or "").strip()
        if not model_name:
            raise ValueError("未配置 AI 模型")
        # 头部注入：所有内容生成类请求统一在 complete() 咽喉点注入，
        # 用户自定义文本成为 AI 第一个读到的指令；诊断类调用传 inject_system=False 绕过。
        if inject_system:
            messages = _apply_system_injection(messages)
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds, connect=min(15.0, self.timeout_seconds))
        payload = {
            "model": model_name,
            "messages": messages,
            "max_tokens": max(128, min(int(max_tokens), 2000)),
            "temperature": max(0.0, min(float(temperature), 2.0)),
            "stream": False,
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.post(self.endpoint, json=payload) as response:
                raw = await _read_response(response)
                if response.status >= 400:
                    raise AIProviderError(f"AI 提供商请求失败（HTTP {response.status}）")
                try:
                    data = json.loads(raw.decode(response.charset or "utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise AIProviderError("AI 提供商返回了无效 JSON") from exc
        if not isinstance(data, dict):
            raise AIProviderError("AI 提供商返回格式无效")
        if data.get("error"):
            raise AIProviderError("AI 提供商返回了错误响应，请在设置中测试当前模型")
        return data

    async def list_models(self) -> list[str]:
        """拉取 OpenAI 兼容的 /models 列表（用于设置页选择模型）。"""
        models_url = self.endpoint
        for suffix in ("/chat/completions",):
            if models_url.lower().endswith(suffix):
                models_url = models_url[: -len(suffix)] + "/models"
                break
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds, connect=min(15.0, self.timeout_seconds))
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(models_url) as response:
                raw = await _read_response(response)
                if response.status >= 400:
                    raise AIProviderError(f"获取模型列表失败（HTTP {response.status}）")
                try:
                    data = json.loads(raw.decode(response.charset or "utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise AIProviderError("AI 提供商返回了无效 JSON") from exc
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = data if isinstance(data, list) else []
        models: list[str] = []
        seen: set[str] = set()
        for item in items:
            model_id = str(item.get("id") or "").strip() if isinstance(item, dict) else str(item or "").strip()
            if model_id and model_id not in seen:
                seen.add(model_id)
                models.append(model_id)
        return sorted(models)

    async def test_completion(self, model: str) -> str:
        """最小 chat 请求验证连通性与模型可用性，返回模型回复文本。

        诊断用途：绕过头部注入，保持 "Reply with exactly: OK" 语义纯净。
        """
        data = await self.complete(
            [{"role": "user", "content": "Reply with exactly: OK"}],
            model=model,
            max_tokens=128,
            temperature=0.0,
            inject_system=False,
        )
        choices = data.get("choices") if isinstance(data, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
            content = str(reasoning or "") if isinstance(reasoning, str) else ""
        return content.strip()[:200]
