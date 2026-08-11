"""WineFox Desktop production runtime integration."""

# 惰性导出 DesktopRuntime：runtime 依赖 desktop_backend，而 desktop_backend.app
# 又会引用 desktop_runtime.setup_errors；若在此处 eager 导入 .runtime 会造成
# 包初始化循环（desktop_backend/__init__ -> app -> setup_errors -> 本包 -> .runtime
# -> desktop_backend.server -> app 尚未就绪）。
def __getattr__(name: str):
    if name == "DesktopRuntime":
        from .runtime import DesktopRuntime

        return DesktopRuntime
    raise AttributeError(name)


__all__ = ["DesktopRuntime"]
