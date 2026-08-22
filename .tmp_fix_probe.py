# -*- coding: utf-8 -*-
"""补丁3：预览图/缩略图补拉路径接入死代理探测回退。用完即删。"""
import io


def patch(path, old, new):
    with io.open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert old in text, f"{path}: NOT FOUND: {old[:70]}"
    text = text.replace(old, new, 1)
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    print("patched", path)


# ---- 1) LoraPreviewService：构造参数 + fetch 前探测 ----
patch(
    "desktop_runtime/lora_preview.py",
    """    def __init__(
        self,
        *,
        data_root: Path,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        registry_ttl: float = 120.0,
        fetch_timeout: float = 20.0,
        proxy: str | Callable[[], str] = "",
    ) -> None:""",
    """    def __init__(
        self,
        *,
        data_root: Path,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        registry_ttl: float = 120.0,
        fetch_timeout: float = 20.0,
        proxy: str | Callable[[], str] = "",
        proxy_probe: Callable[[], object] | None = None,
    ) -> None:""",
)

patch(
    "desktop_runtime/lora_preview.py",
    "        self._proxy = proxy",
    "        self._proxy = proxy\n        self._proxy_probe = proxy_probe",
)

patch(
    "desktop_runtime/lora_preview.py",
    """        proxy = self._current_proxy()""",
    """        # 先探测显式代理可达性：死代理自动回退系统代理，避免预览图
        # 反复撞一个拒绝连接的端口（用户侧表现为"缩略图全挂"）。
        if self._proxy_probe is not None:
            try:
                await self._proxy_probe()
            except Exception:
                pass
        proxy = self._current_proxy()""",
)

# ---- 2) engine：给 preview 注入探测回调 ----
patch(
    "desktop_runtime/engine.py",
    """            proxy=lambda: net_env.effective_proxy(
                str((self.config.snapshot().get("lora_download") or {}).get("proxy") or "")
            )[0],
        )""",
    """            proxy=lambda: net_env.effective_proxy(
                str((self.config.snapshot().get("lora_download") or {}).get("proxy") or "")
            )[0],
            proxy_probe=lambda: net_env.ensure_probed(
                str((self.config.snapshot().get("lora_download") or {}).get("proxy") or "")
            ),
        )""",
)

# ---- 3) 缩略图补拉：effective_proxy 前 ensure ----
patch(
    "desktop_runtime/lora_library.py",
    """            from . import net_env

            settings = self._settings()
            proxy, _src = net_env.effective_proxy(str(settings.get("proxy") or ""))""",
    """            from . import net_env

            settings = self._settings()
            await net_env.ensure_probed(str(settings.get("proxy") or ""))
            proxy, _src = net_env.effective_proxy(str(settings.get("proxy") or ""))""",
)

print("STEP3 DONE")
