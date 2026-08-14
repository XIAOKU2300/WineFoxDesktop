import asyncio
import ipaddress
import json
import os
import re
import secrets
import socket
import struct
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp import web

try:
    import folder_paths
    from server import PromptServer
except Exception:
    folder_paths = None
    PromptServer = None


BRIDGE_VERSION = "1.4.2"
CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_ALLOWED_HOSTS = [
    "civitai.com",
    "civitai.red",
    "civitaired.com",
    "huggingface.co",
    "hf-mirror.com",
    "github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "release-assets.githubusercontent.com",
]
ALLOWED_SUFFIXES = {".safetensors"}
SAFE_HEADER_NAMES = {"authorization", "cookie", "referer", "user-agent"}
MODEL_TARGET_ALIASES = {
    "loras": ("loras", "lora"),
    "checkpoints": ("checkpoints", "checkpoint"),
    "controlnet": ("controlnet", "control_net"),
    "vae": ("vae",),
    "clip": ("clip",),
    "unet": ("unet",),
    "upscale_models": ("upscale_models", "upscale_model"),
    "embeddings": ("embeddings", "embedding"),
    "yolo": ("yolo",),
    "ultralytics_bbox": ("ultralytics/bbox",),
    "ultralytics_segm": ("ultralytics/segm",),
}
TARGET_TYPE_ALIASES = {
    "lora": "loras",
    "loras": "loras",
    "checkpoint": "checkpoints",
    "checkpoints": "checkpoints",
    "ckpt": "checkpoints",
    "controlnet": "controlnet",
    "control_net": "controlnet",
    "vae": "vae",
    "clip": "clip",
    "unet": "unet",
    "upscale": "upscale_models",
    "upscale_model": "upscale_models",
    "upscale_models": "upscale_models",
    "embedding": "embeddings",
    "embeddings": "embeddings",
    "yolo": "yolo",
    "ultralytics_bbox": "ultralytics_bbox",
    "ultralytics-bbox": "ultralytics_bbox",
    "yolo_bbox": "ultralytics_bbox",
    "bbox": "ultralytics_bbox",
    "foot_detector": "ultralytics_bbox",
    "ultralytics_segm": "ultralytics_segm",
    "ultralytics-segm": "ultralytics_segm",
    "yolo_segm": "ultralytics_segm",
    "segm": "ultralytics_segm",
    "person_detector": "ultralytics_segm",
    "controlnet_aux": "controlnet_aux_ckpts",
    "controlnet_aux_ckpts": "controlnet_aux_ckpts",
    "custom_nodes_ckpts": "controlnet_aux_ckpts",
}
CUSTOM_TARGET_RELATIVE_ROOTS = {
    "controlnet_aux_ckpts": ("custom_nodes", "comfyui_controlnet_aux", "ckpts"),
}
TASKS = {}
YOLO_MODEL_CACHE = {}
_DOWNLOAD_SEMAPHORE = None
_DOWNLOAD_SEMAPHORE_SIZE = 0
_PROXY_CIRCUITS = {}


def _safe_filename(value: str) -> str:
    name = str(value or "").strip() or f"astrbot_lora_{int(time.time())}.safetensors"
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if not name:
        name = f"astrbot_lora_{int(time.time())}.safetensors"
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        name = f"{Path(name).stem or 'astrbot_lora'}.safetensors"
    return name[:180]


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif", ".heic", ".avif"}


def _safe_image_filename(value: str) -> str:
    name = str(value or "").strip()
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    suffix = Path(name).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        name = f"{Path(name).stem or 'subject_audit'}.png"
    if not Path(name).stem:
        name = f"subject_audit{Path(name).suffix or '.png'}"
    return name[:180]


def _verify_safetensors_file(path: Path) -> None:
    """校验 safetensors 文件头：前 8 字节 little-endian 头长度 + 可解析的 JSON 头。

    不解析张量数据，只确认头部结构合法，防止空文件/非 safetensors 文件落地。
    """
    size = path.stat().st_size
    if size < 16:
        raise RuntimeError("safetensors 文件过小，文件头无效")
    with path.open("rb") as fp:
        header_len = struct.unpack("<Q", fp.read(8))[0]
    if header_len < 2 or header_len > 128 * 1024 * 1024 or 8 + header_len > size:
        raise RuntimeError("safetensors 文件头长度异常")
    with path.open("rb") as fp:
        fp.seek(8)
        header_bytes = fp.read(header_len)
    try:
        header = json.loads(header_bytes.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("safetensors 文件头不是合法 JSON") from None
    if not isinstance(header, dict):
        raise RuntimeError("safetensors 文件头结构无效")
    data_len = size - 8 - header_len
    max_end = 0
    tensor_count = 0
    for key, info in header.items():
        if key == "__metadata__":
            continue
        if not isinstance(info, dict):
            raise RuntimeError(f"safetensors tensor {key!r} 元数据无效")
        offsets = info.get("data_offsets")
        if not isinstance(offsets, list) or len(offsets) != 2:
            raise RuntimeError(f"safetensors tensor {key!r} 缺少 data_offsets")
        start, end = offsets
        if not (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
        ):
            raise RuntimeError(f"safetensors tensor {key!r} 偏移不是整数")
        if start < 0 or end < start or end > data_len:
            raise RuntimeError(f"safetensors tensor {key!r} 数据偏移越界")
        tensor_count += 1
        max_end = max(max_end, end)
    if tensor_count < 1:
        raise RuntimeError("safetensors 文件不含任何 tensor")
    if max_end != data_len:
        raise RuntimeError("safetensors 数据区末尾不匹配")


def _load_config() -> dict:
    default = {
        "token": secrets.token_urlsafe(32),
        "allowed_hosts": list(DEFAULT_ALLOWED_HOSTS),
        "chunk_size_mb": 1,
        "connect_timeout": 30,
        "sock_read_timeout": 120,
        "fetch_timeout": 45,
        "max_concurrent_downloads": 1,
        "proxy_cooldown": 60,
        "proxy": "",
        "allow_local_requests_without_token": True,
    }
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                merged = dict(default)
                merged.update(data)
                return merged
        except Exception:
            pass
    try:
        CONFIG_PATH.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return default


CONFIG = _load_config()


def _json_error(message: str, status: int = 400):
    return web.json_response({"ok": False, "error": str(message or "error")}, status=status)


def _download_semaphore():
    global _DOWNLOAD_SEMAPHORE, _DOWNLOAD_SEMAPHORE_SIZE
    size = max(int(CONFIG.get("max_concurrent_downloads") or 1), 1)
    if _DOWNLOAD_SEMAPHORE is None or _DOWNLOAD_SEMAPHORE_SIZE != size:
        _DOWNLOAD_SEMAPHORE = asyncio.Semaphore(size)
        _DOWNLOAD_SEMAPHORE_SIZE = size
    return _DOWNLOAD_SEMAPHORE


def _request_token(request, payload: dict = None) -> str:
    # Credentials must not enter URLs, request bodies, access logs, or task state.
    return str(request.headers.get("X-AstrBot-Bridge-Token") or "").strip()


def _request_remote_host(request) -> str:
    remote = str(getattr(request, "remote", "") or "").strip()
    if not remote:
        try:
            peer = request.transport.get_extra_info("peername") if request.transport else None
            if isinstance(peer, (tuple, list)) and peer:
                remote = str(peer[0] or "").strip()
        except Exception:
            remote = ""
    return remote.strip("[]")


def _request_is_local_or_private(request) -> bool:
    if not bool(CONFIG.get("allow_local_requests_without_token", True)):
        return False
    host = _request_remote_host(request).split("%", 1)[0]
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except Exception:
        return False
    return bool(ip.is_loopback)


def _check_auth(request, payload: dict = None) -> bool:
    expected = str(CONFIG.get("token") or "").strip()
    if not expected:
        return True
    if _request_is_local_or_private(request):
        return True
    provided = _request_token(request, payload=payload)
    return bool(provided and secrets.compare_digest(provided, expected))


def _comfy_root() -> Path:
    try:
        return Path(__file__).resolve().parents[2]
    except Exception:
        return Path.cwd()


def _models_root() -> Path:
    if folder_paths is not None:
        try:
            value = str(getattr(folder_paths, "models_dir", "") or "").strip()
            if value:
                return Path(value).expanduser()
        except Exception:
            pass
    return _comfy_root() / "models"


def _lora_dir() -> Path:
    folders = []
    if folder_paths is not None:
        try:
            folders = list(folder_paths.get_folder_paths("loras") or [])
        except Exception:
            folders = []
        if not folders:
            try:
                folders = [str(Path(folder_paths.models_dir) / "loras")]
            except Exception:
                folders = []
    if not folders:
        folders = [str(Path(__file__).resolve().parents[2] / "models" / "loras")]
    target = Path(str(folders[0])).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _model_target_dir(target_type: str) -> Path:
    target_type = TARGET_TYPE_ALIASES.get(str(target_type or "").strip().lower(), target_type)
    folder_names = MODEL_TARGET_ALIASES.get(target_type)
    if not folder_names:
        raise RuntimeError(f"unsupported download target type: {target_type}")
    if target_type == "loras":
        return _lora_dir()
    folders = []
    if folder_paths is not None:
        for name in folder_names:
            try:
                folders.extend(str(x) for x in (folder_paths.get_folder_paths(name) or []) if str(x or "").strip())
            except Exception:
                continue
    if not folders:
        folders = [str(_models_root() / folder_names[0])]
    target = Path(str(folders[0])).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _custom_target_dir(target_type: str) -> Path:
    parts = CUSTOM_TARGET_RELATIVE_ROOTS.get(target_type)
    if not parts:
        raise RuntimeError(f"unsupported download target type: {target_type}")
    target = _comfy_root().joinpath(*parts)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _safe_relative_path(value: str) -> Path:
    text = str(value or "").strip().replace("\\", "/").strip("/")
    if not text:
        return Path()
    parts = [part for part in text.split("/") if part]
    if any(part in {".", ".."} or ":" in part or "\x00" in part for part in parts):
        raise RuntimeError("unsafe target subdir")
    if len(parts) > 12 or sum(len(part) for part in parts) > 240:
        raise RuntimeError("target subdir is too long")
    return Path(*parts)


def _resolve_download_target(payload: dict, filename: str) -> tuple:
    raw_target = (
        payload.get("target_type")
        or payload.get("target_folder")
        or payload.get("folder")
        or "loras"
    )
    target_type = TARGET_TYPE_ALIASES.get(str(raw_target or "").strip().lower(), str(raw_target or "").strip().lower())
    if target_type in MODEL_TARGET_ALIASES:
        root = _model_target_dir(target_type)
    elif target_type in CUSTOM_TARGET_RELATIVE_ROOTS:
        root = _custom_target_dir(target_type)
    else:
        raise RuntimeError(f"unsupported download target type: {raw_target}")
    root = root.resolve()
    subdir = _safe_relative_path(payload.get("target_subdir") or payload.get("subdir") or "")
    target_dir = (root / subdir).resolve()
    try:
        target_dir.relative_to(root)
    except Exception:
        raise RuntimeError("unsafe target path")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = (target_dir / filename).resolve()
    try:
        target.relative_to(root)
    except Exception:
        raise RuntimeError("unsafe target path")
    return target, root, target_type, str(subdir).replace("\\", "/")


def _host_allowed(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.hostname or "").strip().lower()
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return False
    allowed = CONFIG.get("allowed_hosts") or DEFAULT_ALLOWED_HOSTS
    for item in allowed:
        item = str(item or "").strip().lower()
        if item and (host == item or host.endswith(f".{item}")):
            return True
    return False


def _host_resolves_public(url: str, *, require_allowlisted: bool = True) -> bool:
    if require_allowlisted and not _host_allowed(url):
        return False
    try:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.hostname or "").strip().lower()
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not address.is_global:
            return False
    return True


def _redirect_trace_configs(*, require_allowlisted: bool = False) -> list:
    trace = aiohttp.TraceConfig()

    async def validate_redirect(_session, _context, params):
        location = str(params.response.headers.get("Location") or "").strip()
        target = urljoin(str(params.url), location)
        if not _host_resolves_public(
            target, require_allowlisted=require_allowlisted
        ):
            raise RuntimeError("download redirect host is not allowed")

    trace.on_request_redirect.append(validate_redirect)
    return [trace]


def _safe_proxy(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        value = f"http://{value}"
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    return ""


def _safe_int(value, default: int, minimum: int = 0, maximum: int = None) -> int:
    try:
        number = int(float(value))
    except Exception:
        number = int(default)
    number = max(number, int(minimum))
    if maximum is not None:
        number = min(number, int(maximum))
    return number


def _proxy_key(proxy: str) -> str:
    return _safe_proxy(proxy)


def _proxy_circuit_message(proxy: str) -> str:
    key = _proxy_key(proxy)
    if not key:
        return ""
    state = _PROXY_CIRCUITS.get(key)
    if not isinstance(state, dict):
        return ""
    blocked_until = float(state.get("blocked_until") or 0)
    if blocked_until <= time.time():
        _PROXY_CIRCUITS.pop(key, None)
        return ""
    remain = max(1, int(blocked_until - time.time()))
    return f"proxy {key} is temporarily blocked for about {remain}s: {state.get('reason') or 'network failure'}"


def _check_proxy_circuit(proxy: str):
    message = _proxy_circuit_message(proxy)
    if message:
        raise RuntimeError(message)


def _mark_proxy_failure(proxy: str, exc: Exception):
    key = _proxy_key(proxy)
    if not key:
        return
    cooldown = max(int(CONFIG.get("proxy_cooldown") or 60), 5)
    _PROXY_CIRCUITS[key] = {
        "blocked_until": time.time() + cooldown,
        "reason": str(exc),
        "updated_at": time.time(),
    }


def _mark_proxy_success(proxy: str):
    key = _proxy_key(proxy)
    if key:
        _PROXY_CIRCUITS.pop(key, None)


def _looks_like_proxy_failure(exc: Exception) -> bool:
    return isinstance(exc, (aiohttp.ClientError, TimeoutError, OSError))


def _download_headers(payload: dict) -> dict:
    raw = payload.get("headers") if isinstance(payload, dict) else {}
    if not isinstance(raw, dict):
        return {}
    headers = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if name.lower() in SAFE_HEADER_NAMES and value:
            headers[name] = str(value)
    if "User-Agent" not in headers and "user-agent" not in {x.lower() for x in headers}:
        headers["User-Agent"] = "AstrBot-ComfyUI-LoRA-Bridge/1.0"
    return headers


def _safe_response_headers(headers) -> dict:
    names = {"content-type", "content-length", "retry-after", "x-ratelimit-remaining", "x-ratelimit-reset"}
    result = {}
    for key, value in (headers or {}).items():
        if str(key or "").lower() in names:
            result[str(key)] = str(value)
    return result


def _task_snapshot(task_id: str) -> dict:
    task = TASKS.get(str(task_id or ""))
    if not isinstance(task, dict):
        return {}
    public = {
        "ok": True,
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "filename": task.get("filename"),
        "target_type": task.get("target_type") or "loras",
        "target_subdir": task.get("target_subdir") or "",
        "done": int(task.get("done") or 0),
        "total": int(task.get("total") or 0),
        "error": task.get("error") or "",
        "updated_at": task.get("updated_at") or time.time(),
    }
    if task.get("status") == "done":
        public["path"] = task.get("path") or ""
    return public


def _cleanup_tasks():
    now = time.time()
    for task_id, task in list(TASKS.items()):
        if not isinstance(task, dict):
            TASKS.pop(task_id, None)
            continue
        if task.get("status") in {"queued", "running"}:
            continue
        if now - float(task.get("updated_at") or now) > 3600:
            TASKS.pop(task_id, None)


async def _download_worker(task_id: str, url: str, target: Path, headers: dict, proxy: str = ""):
    task = TASKS[task_id]
    tmp_path = target.with_suffix(target.suffix + ".part")
    try:
        async with _download_semaphore():
            task.update({"status": "running", "updated_at": time.time()})
            timeout = aiohttp.ClientTimeout(
                total=None,
                connect=max(int(CONFIG.get("connect_timeout") or 30), 3),
                sock_read=max(int(CONFIG.get("sock_read_timeout") or 120), 30),
            )
            chunk_size = max(int(CONFIG.get("chunk_size_mb") or 1), 1) * 1024 * 1024
            proxy = _safe_proxy(proxy) or _safe_proxy(CONFIG.get("proxy")) or None
            _check_proxy_circuit(proxy)
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
                trace_configs=_redirect_trace_configs(),
            ) as session:
                async with session.get(url, allow_redirects=True, proxy=proxy) as resp:
                    _mark_proxy_success(proxy)
                    if resp.status >= 400:
                        text = await resp.text(errors="replace")
                        raise RuntimeError(f"download HTTP {resp.status}: {text[:200]}")
                    total = int(resp.headers.get("Content-Length") or 0)
                    task.update({"total": total, "updated_at": time.time()})
                    done = 0
                    with tmp_path.open("wb") as fp:
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            if not chunk:
                                continue
                            fp.write(chunk)
                            done += len(chunk)
                            task.update({"done": done, "updated_at": time.time()})
            if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
                raise RuntimeError("download produced an empty file")
            _verify_safetensors_file(tmp_path)
            tmp_path.replace(target)
            try:
                os.utime(target, None)
                os.utime(target.parent, None)
            except Exception:
                pass
            task.update({
                "status": "done",
                "done": target.stat().st_size,
                "path": str(target),
                "updated_at": time.time(),
            })
    except Exception as exc:
        proxy = _safe_proxy(proxy) or _safe_proxy(CONFIG.get("proxy")) or None
        if proxy and _looks_like_proxy_failure(exc):
            _mark_proxy_failure(proxy, exc)
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        task.update({"status": "error", "error": str(exc), "updated_at": time.time()})


async def _fetch_url(payload: dict) -> dict:
    url = str(payload.get("url") or "").strip()
    if not _host_resolves_public(url):
        raise RuntimeError("fetch host is not allowed")
    method = str(payload.get("method") or "GET").upper()
    if method not in {"GET", "POST"}:
        raise RuntimeError("method must be GET or POST")
    proxy = _safe_proxy(payload.get("proxy")) or _safe_proxy(CONFIG.get("proxy")) or None
    config_fetch_timeout = max(_safe_int(CONFIG.get("fetch_timeout"), 45, minimum=10), 10)
    request_fetch_timeout = _safe_int(
        payload.get("timeout") or payload.get("fetch_timeout") or config_fetch_timeout,
        config_fetch_timeout,
        minimum=10,
        maximum=600,
    )
    timeout = aiohttp.ClientTimeout(
        total=request_fetch_timeout,
        connect=max(_safe_int(CONFIG.get("connect_timeout"), 30, minimum=3), 3),
        sock_read=request_fetch_timeout,
    )
    headers = _download_headers(payload)
    max_bytes = max(int(payload.get("max_bytes") or 4 * 1024 * 1024), 64 * 1024)
    chunks = []
    size = 0
    try:
        _check_proxy_circuit(proxy)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            trace_configs=_redirect_trace_configs(require_allowlisted=True),
        ) as session:
            request_kwargs = {"allow_redirects": True, "proxy": proxy}
            if method == "POST":
                request_kwargs["json"] = payload.get("json") if isinstance(payload.get("json"), dict) else {}
                request = session.post
            else:
                request = session.get
            async with request(url, **request_kwargs) as resp:
                _mark_proxy_success(proxy)
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size <= max_bytes:
                        chunks.append(chunk)
                    else:
                        remain = max_bytes - (size - len(chunk))
                        if remain > 0:
                            chunks.append(chunk[:remain])
                        break
                raw = b"".join(chunks)
                text = raw.decode(resp.charset or "utf-8", errors="replace")
                return {
                    "ok": True,
                    "status": int(resp.status),
                    "headers": _safe_response_headers(resp.headers),
                    "text": text,
                    "truncated": size > max_bytes,
                    "bytes": min(size, max_bytes),
                }
    except Exception as exc:
        if proxy and _looks_like_proxy_failure(exc):
            _mark_proxy_failure(proxy, exc)
        target = urlparse(url).netloc or url
        proxy_text = proxy or "direct"
        raise RuntimeError(f"fetch {target} via {proxy_text} failed: {exc}") from exc


def _subject_model_path(target_type: str, model_name: str) -> Path:
    name = _safe_filename(model_name)
    if not name:
        raise RuntimeError(f"empty {target_type} model name")
    return (_model_target_dir(target_type) / name).resolve()


def _subject_audit_preflight(payload: dict) -> tuple:
    missing = []
    paths = {}
    try:
        import ultralytics  # noqa: F401
    except Exception:
        missing.append("Python package: ultralytics")

    skip_subject_models = str(payload.get("skip_subject_models") or "").lower() in {"1", "true", "yes", "on"}
    model_specs = []
    if not skip_subject_models:
        model_specs.extend([
            ("face_model", "ultralytics_bbox", "face_yolov8m.pt"),
            ("person_model", "ultralytics_segm", "person_yolov8s-seg.pt"),
        ])
    model_specs.append(("foot_model", "ultralytics_bbox", ""))
    for key, target_type, default_name in model_specs:
        model_name = str(payload.get(key) or default_name).strip()
        if not model_name:
            continue
        try:
            path = _subject_model_path(target_type, model_name)
            paths[key] = path
            if not path.exists() or path.stat().st_size <= 0:
                missing.append(f"models/{MODEL_TARGET_ALIASES[target_type][0]}/{path.name}")
        except Exception as exc:
            missing.append(f"{key}: {exc}")
    return missing, paths


def _load_yolo_model(path: Path):
    key = str(path.resolve())
    model = YOLO_MODEL_CACHE.get(key)
    if model is not None:
        return model
    from ultralytics import YOLO
    model = YOLO(key)
    YOLO_MODEL_CACHE[key] = model
    return model


def _run_yolo_count(model_path: Path, image_path: Path, threshold: float) -> tuple:
    threshold = min(max(float(threshold or 0.35), 0.01), 1.0)
    model = _load_yolo_model(model_path)
    results = model(str(image_path), verbose=False, conf=threshold)
    boxes = []
    for result in results or []:
        raw_boxes = getattr(result, "boxes", None)
        if raw_boxes is None:
            continue
        xyxy_values = getattr(raw_boxes, "xyxy", None)
        conf_values = getattr(raw_boxes, "conf", None)
        cls_values = getattr(raw_boxes, "cls", None)
        xyxy_list = xyxy_values.detach().cpu().tolist() if xyxy_values is not None else []
        conf_list = conf_values.detach().cpu().tolist() if conf_values is not None else []
        cls_list = cls_values.detach().cpu().tolist() if cls_values is not None else []
        for index, box in enumerate(xyxy_list):
            conf = float(conf_list[index]) if index < len(conf_list) else 0.0
            if conf < threshold:
                continue
            boxes.append({
                "bbox": [round(float(x), 2) for x in box[:4]],
                "confidence": round(conf, 4),
                "class": int(float(cls_list[index])) if index < len(cls_list) else 0,
            })
    return len(boxes), boxes[:12]


async def _subject_audit_payload(request) -> tuple:
    if str(request.content_type or "").lower().startswith("multipart/"):
        reader = await request.multipart()
        payload = {}
        image_bytes = b""
        image_name = "subject_audit.png"
        async for part in reader:
            if part.name == "image":
                image_name = _safe_image_filename(part.filename or image_name)
                image_bytes = await part.read(decode=False)
            else:
                payload[str(part.name or "")] = (await part.text()).strip()
        return payload, image_bytes, image_name
    payload = await request.json()
    return payload if isinstance(payload, dict) else {}, b"", "subject_audit.png"


async def _run_subject_audit_request(request):
    payload, image_bytes, image_name = await _subject_audit_payload(request)
    missing, paths = _subject_audit_preflight(payload)
    base = {
        "ok": not missing,
        "subject_audit_supported": True,
        "ready": not missing,
        "missing": missing,
        "face_model": Path(str(payload.get("face_model") or "face_yolov8m.pt")).name,
        "person_model": Path(str(payload.get("person_model") or "person_yolov8s-seg.pt")).name,
    }
    if payload.get("foot_model"):
        base["foot_model"] = Path(str(payload.get("foot_model") or "")).name
    if missing:
        base["error"] = "Illustrious single-subject audit dependency missing: " + ", ".join(missing)
        return base
    if str(payload.get("preflight_only") or "").lower() in {"1", "true", "yes", "on"}:
        return base
    if not image_bytes:
        return {**base, "ok": False, "error": "subject audit image is required"}

    temp_path = Path(tempfile.gettempdir()) / f"astrbot_subject_audit_{uuid.uuid4().hex}_{image_name}"
    try:
        temp_path.write_bytes(image_bytes)
        face_count, face_boxes = _run_yolo_count(
            paths["face_model"],
            temp_path,
            float(payload.get("face_threshold") or 0.35),
        ) if "face_model" in paths else (0, [])
        person_count, person_boxes = _run_yolo_count(
            paths["person_model"],
            temp_path,
            float(payload.get("person_threshold") or 0.35),
        ) if "person_model" in paths else (0, [])
        foot_count, foot_boxes = (0, [])
        if "foot_model" in paths:
            foot_count, foot_boxes = _run_yolo_count(
                paths["foot_model"],
                temp_path,
                float(payload.get("foot_threshold") or 0.35),
            )
        rejected = bool(face_count > 1 or person_count > 1)
        result = {
            **base,
            "face_count": face_count,
            "person_count": person_count,
            "foot_count": foot_count,
            "face_boxes": face_boxes,
            "person_boxes": person_boxes,
            "foot_boxes": foot_boxes,
            "rejected": rejected,
            "reject_reason": ", ".join(
                item for item in [
                    f"face_count={face_count}" if face_count > 1 else "",
                    f"person_count={person_count}" if person_count > 1 else "",
                ] if item
            ),
        }
        return result
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


if PromptServer is not None:
    routes = PromptServer.instance.routes

    @routes.get("/astrbot/lora/status")
    async def astrbot_lora_bridge_status(request):
        auth_required = bool(str(CONFIG.get("token") or "").strip())
        local_auth_bypass = _request_is_local_or_private(request)
        if auth_required and not _check_auth(request):
            return web.json_response({
                "ok": True,
                "version": BRIDGE_VERSION,
                "auth_required": True,
                "authenticated": False,
                "local_auth_bypass": local_auth_bypass,
                "allow_local_requests_without_token": bool(CONFIG.get("allow_local_requests_without_token", True)),
            })
        lora_dir = _lora_dir()
        return web.json_response({
            "ok": True,
            "version": BRIDGE_VERSION,
            "auth_required": auth_required,
            "authenticated": True,
            "local_auth_bypass": local_auth_bypass,
            "allow_local_requests_without_token": bool(CONFIG.get("allow_local_requests_without_token", True)),
            "lora_dir": str(lora_dir),
            "allowed_hosts": CONFIG.get("allowed_hosts") or DEFAULT_ALLOWED_HOSTS,
            "proxy_configured": bool(str(CONFIG.get("proxy") or "").strip()),
            "request_proxy_supported": True,
            "request_fetch_supported": True,
            "request_resource_download_supported": True,
            "subject_audit_supported": True,
            "subject_audit_endpoint": "/astrbot/subject-audit",
            "download_targets": sorted(set(TARGET_TYPE_ALIASES.values())),
            "max_concurrent_downloads": max(int(CONFIG.get("max_concurrent_downloads") or 1), 1),
            "task_counts": {
                "queued": sum(1 for task in TASKS.values() if isinstance(task, dict) and task.get("status") == "queued"),
                "running": sum(1 for task in TASKS.values() if isinstance(task, dict) and task.get("status") == "running"),
                "done": sum(1 for task in TASKS.values() if isinstance(task, dict) and task.get("status") == "done"),
                "error": sum(1 for task in TASKS.values() if isinstance(task, dict) and task.get("status") == "error"),
            },
        })

    @routes.post("/astrbot/lora/fetch")
    async def astrbot_lora_bridge_fetch(request):
        try:
            payload = await request.json()
        except Exception:
            return _json_error("request body must be JSON", status=400)
        if not _check_auth(request, payload=payload):
            return _json_error("invalid AstrBot bridge token", status=401)
        try:
            return web.json_response(await _fetch_url(payload))
        except Exception as exc:
            return _json_error(str(exc), status=502)

    @routes.post("/astrbot/subject-audit")
    async def astrbot_subject_audit(request):
        try:
            if not _check_auth(request):
                return _json_error("invalid AstrBot bridge token", status=401)
            data = await _run_subject_audit_request(request)
            return web.json_response(data)
        except Exception as exc:
            return _json_error(str(exc), status=500)

    @routes.post("/astrbot/lora/download")
    async def astrbot_lora_bridge_download(request):
        _cleanup_tasks()
        try:
            payload = await request.json()
        except Exception:
            return _json_error("request body must be JSON", status=400)
        if not _check_auth(request, payload=payload):
            return _json_error("invalid AstrBot bridge token", status=401)
        url = str(payload.get("url") or "").strip()
        if not _host_resolves_public(url):
            return _json_error("download host is not allowed", status=400)
        proxy = str(payload.get("proxy") or "").strip()
        if proxy and not _safe_proxy(proxy):
            return _json_error("proxy must be an http(s) URL or host:port", status=400)
        try:
            filename = _safe_filename(payload.get("filename") or Path(urlparse(url).path).name)
            target, root, target_type, target_subdir = _resolve_download_target(payload, filename)
        except Exception as exc:
            return _json_error(str(exc), status=400)
        task_id = uuid.uuid4().hex
        TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "filename": filename,
            "target_type": target_type,
            "target_subdir": target_subdir,
            "done": 0,
            "total": 0,
            "error": "",
            "path": str(target),
            "updated_at": time.time(),
        }
        if target.exists() and target.stat().st_size > 0:
            try:
                _verify_safetensors_file(target)
            except Exception:
                return _json_error(
                    "existing target is not a valid safetensors file",
                    status=409,
                )
            TASKS[task_id].update({
                "status": "done",
                "done": target.stat().st_size,
                "total": target.stat().st_size,
                "updated_at": time.time(),
            })
            return web.json_response(_task_snapshot(task_id))
        asyncio.create_task(_download_worker(task_id, url, target, _download_headers(payload), proxy=proxy))
        return web.json_response(_task_snapshot(task_id))

    @routes.get("/astrbot/lora/task/{task_id}")
    async def astrbot_lora_bridge_task(request):
        _cleanup_tasks()
        if not _check_auth(request):
            return _json_error("invalid AstrBot bridge token", status=401)
        task_id = request.match_info.get("task_id", "")
        task = _task_snapshot(task_id)
        if not task:
            return _json_error("task not found", status=404)
        return web.json_response(task)


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
