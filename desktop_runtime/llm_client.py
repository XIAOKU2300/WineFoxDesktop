"""Minimal OpenAI-compatible text client used by the desktop prompt pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

import aiohttp


@dataclass(frozen=True)
class ResolvedAIProvider:
    """Non-sensitive endpoint selection plus the exact SecretStore entry to use."""

    enabled: bool
    base_url: str
    model: str
    timeout_seconds: float
    secret_name: str
    source: str


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
    ) -> dict[str, Any]:
        model_name = str(model or "").strip()
        if not model_name:
            raise ValueError("未配置 AI 模型")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds, connect=min(15.0, self.timeout_seconds))
        payload = {
            "model": model_name,
            "messages": messages,
            "max_tokens": max(128, min(int(max_tokens), 1200)),
            "temperature": max(0.0, min(float(temperature), 2.0)),
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.post(self.endpoint, json=payload) as response:
                raw = await response.content.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise RuntimeError("AI 提供商响应过大")
                if response.status >= 400:
                    raise RuntimeError(f"AI 提供商请求失败（HTTP {response.status}）")
                try:
                    data = json.loads(raw.decode(response.charset or "utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise RuntimeError("AI 提供商返回了无效 JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError("AI 提供商返回格式无效")
        return data
