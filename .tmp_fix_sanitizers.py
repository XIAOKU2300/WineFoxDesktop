# -*- coding: utf-8 -*-
"""一次性补丁：错误脱敏正则误伤 URL + aiohttp 错误中文化。用完即删。"""
import io

# ---------- 1) 路径正则加负向后行（盘符前是字母/数字则不是盘符，如 http:// 的 p:/） ----------

def patch(path, old, new):
    with io.open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert old in text, f"{path}: pattern not found: {old[:60]}"
    text = text.replace(old, new, 1)
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    print("patched", path)

# lora_download._safe_error（BUG 主现场：无 URL 先行脱敏，http:// 被 p:/ 吃掉）
patch(
    "desktop_runtime/lora_download.py",
    'text = re.sub(r"(?i)(?:[a-z]:[\\\\/]|\\\\\\\\)[^\\r\\n,;]+", "[LOCAL_PATH]", text)',
    'text = re.sub(r"(?i)(?<![a-z0-9])[a-z]:[\\\\/][^\\r\\n,;]+|\\\\\\\\[^\\r\\n,;]+", "[LOCAL_PATH]", text)',
)

# lora_search._safe_error（URL 先行脱敏，此处加保险）
patch(
    "desktop_runtime/lora_search.py",
    'text = re.sub(r"(?i)(?:[a-z]:[\\\\/]|\\\\\\\\)[^\\r\\n,;]+", "[LOCAL_PATH]", text)',
    'text = re.sub(r"(?i)(?<![a-z0-9])[a-z]:[\\\\/][^\\r\\n,;]+|\\\\\\\\[^\\r\\n,;]+", "[LOCAL_PATH]", text)',
)

# lora_llm_match._safe_error
patch(
    "desktop_runtime/lora_llm_match.py",
    'text = re.sub(r"(?i)(?:[a-z]:[\\\\/]|\\\\\\\\)[^\\r\\n,;]+", "[LOCAL_PATH]", text)',
    'text = re.sub(r"(?i)(?<![a-z0-9])[a-z]:[\\\\/][^\\r\\n,;]+|\\\\\\\\[^\\r\\n,;]+", "[LOCAL_PATH]", text)',
)

# civitai_api._safe_error（同保险）
patch(
    "civitai_api.py",
    'text = re.sub(r"(?i)(?:[a-z]:[\\\\/]|\\\\\\\\)[^\\r\\n,;]+", "[LOCAL_PATH]", text)',
    'text = re.sub(r"(?i)(?<![a-z0-9])[a-z]:[\\\\/][^\\r\\n,;]+|\\\\\\\\[^\\r\\n,;]+", "[LOCAL_PATH]", text)',
)

# setup_executor._sanitize_error_text
patch(
    "desktop_runtime/setup_executor.py",
    'pattern = r"(?:[A-Za-z]:[\\\\/]|\\\\\\\\)[^\\r\\n,;\\"\\']*"',
    'pattern = r"(?<![A-Za-z0-9])[A-Za-z]:[\\\\/][^\\r\\n,;\\"\\']*|\\\\\\\\[^\\r\\n,;\\"\\']*"',
)

# engine._redact_diagnostic_text
patch(
    "desktop_runtime/engine.py",
    'r"(?i)(?:\\"[a-z]:[\\\\/][^\\"\\r\\n]+\\"|(?:[a-z]:[\\\\/]|\\\\\\\\)[^\\s,;\\"<>|?*]+)"',
    'r"(?i)(?:\\"[a-z]:[\\\\/][^\\"\\r\\n]+\\"|(?<![a-z0-9])[a-z]:[\\\\/][^\\s,;\\"<>|?*]+|\\\\\\\\[^\\s,;\\"<>|?*]+)"',
)

print("ALL REGEX PATCHED")
