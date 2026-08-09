import asyncio
import ast
import copy
import json
import mimetypes
import os
import random
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from astrbot.api import logger


DEFAULT_CONNECT_TIMEOUT = 20
DEFAULT_READ_TIMEOUT = 300
DEFAULT_LORA_CACHE_FILE = "lora_cache.json"
DEFAULT_POSE_CONTROLNET_NAME = "illustriousXL_Openpose_v10.safetensors"
DEFAULT_ILLUSTRIOUS_FOOT_DETECTOR_MODEL = "foot-yolov8l.pt"
IGNORED_LORA_PREFIXES = ("intrinsic_lora_",)
IGNORED_LORA_MARKERS = (
    "controlnet", "control_net", "openpose", "depth", "sdpose", "sam2", "sam3",
    "segment", "segm", "bbox", "yolo", "detection", "preprocessor", "lllite",
    "t2i_adapter", "adapter",
)
DEFAULT_CHARACTER_REPLACE_WORKFLOW = "character_replace_anima_reference_api.json"
LEGACY_LLLITE_CHARACTER_REPLACE_WORKFLOW = "character_replace_anima_lllite_api.json"
LEGACY_UNION_CHARACTER_REPLACE_WORKFLOW = "character_replace_union_control_api.json"


def _normalize_server_address(server_address: str) -> str:
    """规范化 ComfyUI 地址，允许填写 127.0.0.1:8188 或完整 URL。"""
    server_address = (server_address or "").strip()
    if not server_address:
        return server_address
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", server_address):
        server_address = f"http://{server_address}"
    return server_address.rstrip("/")


def _safe_float(value, default: float) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value, default: int) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def _safe_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return bool(default)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    return bool(default)


def _is_ignored_lora_name(name: str) -> bool:
    filename = Path(str(name or "").strip()).name.lower()
    return (
        any(filename.startswith(prefix) for prefix in IGNORED_LORA_PREFIXES)
        or any(marker in filename for marker in IGNORED_LORA_MARKERS)
    )


class ComfyUI:
    def __init__(self, config: dict, data_dir: Path = None) -> None:
        """
        ComfyUI API 客户端。

        重点：
        - 工作流仍然只使用一个 API JSON；
        - LoRA 通过运行时覆盖工作流里的 LoRA Loader 节点来切换；
        - 支持从 ComfyUI /object_info 尝试获取 LoRA 列表并缓存给 WebUI 下拉框使用。
        """
        self.config = config or {}
        self.server_address = _normalize_server_address(self.config.get("server_address", "127.0.0.1:8188"))
        self.url = self.server_address

        if data_dir is not None:
            self.data_dir = Path(data_dir)
        else:
            self.data_dir = Path(os.path.dirname(os.path.abspath(__file__)))
            logger.warning("[ComfyUI API] 未传入 data_dir，使用插件目录（更新后可能丢失数据）")

        self.workflow_dir = self.data_dir / "workflow"
        self.lora_cache_path = self.data_dir / DEFAULT_LORA_CACHE_FILE
        self.runtime_wf_filename = None
        self.runtime_fallback_wf_filename = None
        self.runtime_input_id = None
        self.runtime_neg_node_id = None
        self.runtime_output_id = None
        self.runtime_neg_prompt = None
        self.runtime_lora_node_id = None
        self.runtime_lora_override = None  # None=跟随 WebUI 配置；""=命令临时关闭；其他=命令临时指定
        self.runtime_multi_loras = None
        self.runtime_multi_lora_mode = None
        self.runtime_checkpoint_name = None
        self.runtime_generation_preset = None
        self.runtime_lora_strength_cap = None
        self.runtime_strength_model = None
        self.runtime_strength_clip = None
        self.runtime_seed_override = None
        self.runtime_model_family = None
        self.runtime_model_family_label = None
        self.runtime_lora_family_guard = None
        self._object_info_cache = (0.0, {})
        self.server_ssl_verify = False

        self._lora_cache = []
        self._load_runtime_config()
        self._load_lora_cache_from_disk()

        logger.info(
            f"[ComfyUI API] 已加载 | 工作流目录: {self.workflow_dir} | "
            f"当前工作流: {self.wf_filename} | LoRA: {self.current_lora or '未指定'}"
        )

    # ------------------------------------------------------------------
    # 配置与缓存
    # ------------------------------------------------------------------
    def _load_runtime_config(self):
        """从 AstrBot 配置读取当前运行参数。每次生成前会刷新一次，便于 WebUI 修改后生效。"""
        self.server_address = _normalize_server_address(self.config.get("server_address", self.server_address))
        self.url = self.server_address
        self.server_ssl_verify = _safe_bool(self.config.get("server_ssl_verify", False), False)

        sub_conf = self.config.get("sub_config", {}) or {}
        self.steps = _safe_int(sub_conf.get("steps", 20), 20)
        self.width = _safe_int(sub_conf.get("width", 768), 768)
        self.height = _safe_int(sub_conf.get("height", 1024), 1024)
        self.fixed_seed = max(_safe_int(sub_conf.get("seed", 0), 0), 0)
        self.fixed_seed_enabled = _safe_bool(sub_conf.get("fixed_seed_enabled", self.fixed_seed > 0), self.fixed_seed > 0)
        self.neg_prompt = str(sub_conf.get("negative_prompt", "") or "")
        if self.runtime_neg_prompt is not None:
            self.neg_prompt = str(self.runtime_neg_prompt or "")

        wf_conf = self.config.get("workflow_settings", {}) or {}
        self.wf_filename = wf_conf.get("json_file", "workflow_api.json") or "workflow_api.json"
        self.input_id = str(wf_conf.get("input_node_id", "6") or "6")
        self.neg_node_id = str(wf_conf.get("neg_node_id", "") or "")
        self.output_id = str(wf_conf.get("output_node_id", "") or "")
        if self.runtime_wf_filename is not None:
            self.wf_filename = self.runtime_wf_filename
        if self.runtime_input_id is not None:
            self.input_id = self.runtime_input_id
        if self.runtime_neg_node_id is not None:
            self.neg_node_id = self.runtime_neg_node_id
        if self.runtime_output_id is not None:
            self.output_id = self.runtime_output_id
        self.workflow_path = self.workflow_dir / self.wf_filename

        lora_conf = self.config.get("lora_settings", {}) or {}
        self.lora_enabled = bool(lora_conf.get("enable_lora", True))
        self.config_lora_name = str(lora_conf.get("default_lora", "") or "").strip()
        self.lora_node_id = str(lora_conf.get("lora_node_id", "") or "").strip()
        if self.runtime_lora_node_id is not None:
            self.lora_node_id = str(self.runtime_lora_node_id or "").strip()
        self.strength_model = _safe_float(lora_conf.get("strength_model", 1.0), 1.0)
        self.strength_clip = _safe_float(lora_conf.get("strength_clip", 1.0), 1.0)
        self.style_lora_enabled = bool(lora_conf.get("enable_style_lora", True))
        self.config_style_lora_name = str(lora_conf.get("default_style_lora", "") or "").strip()
        self.style_lora_node_id = str(lora_conf.get("style_lora_node_id", "") or "").strip()
        self.style_strength_model = _safe_float(lora_conf.get("style_strength_model", 0.25), 0.25)
        self.style_strength_clip = _safe_float(lora_conf.get("style_strength_clip", 0.25), 0.25)
        self.allow_unmarked_style_lora = bool(lora_conf.get("allow_unmarked_style_lora", False))
        self.apply_all_lora_nodes = bool(lora_conf.get("apply_all_lora_nodes", False))
        self.auto_refresh_lora_list = bool(lora_conf.get("auto_refresh_lora_list", True))
        self.multi_lora_debug_masks = _safe_bool(lora_conf.get("multi_lora_debug_masks", False), False)
        self.multi_lora_debug_workflow = _safe_bool(
            lora_conf.get("multi_lora_debug_workflow", self.multi_lora_debug_masks),
            self.multi_lora_debug_masks,
        )
        self.poll_interval = max(_safe_float(lora_conf.get("poll_interval", 1.0), 1.0), 0.2)
        self.poll_attempts = max(_safe_int(lora_conf.get("poll_attempts", 300), 300), 1)
        self.comfyui_connect_timeout = max(_safe_float(lora_conf.get("comfyui_connect_timeout", DEFAULT_CONNECT_TIMEOUT), DEFAULT_CONNECT_TIMEOUT), 3.0)
        self.comfyui_read_timeout = max(_safe_float(lora_conf.get("comfyui_read_timeout", DEFAULT_READ_TIMEOUT), DEFAULT_READ_TIMEOUT), 30.0)
        self.history_request_timeout = max(_safe_float(lora_conf.get("history_request_timeout", 30), 30), 8.0)
        self.poll_max_interval = max(_safe_float(lora_conf.get("poll_max_interval", 3.0), 3.0), self.poll_interval)
        self.queue_request_timeout = max(_safe_float(lora_conf.get("queue_request_timeout", 8), 8), 3.0)
        self.queue_missing_grace_seconds = max(_safe_float(lora_conf.get("queue_missing_grace_seconds", 45), 45), 10.0)
        self.queue_missing_confirmations = max(_safe_int(lora_conf.get("queue_missing_confirmations", 3), 3), 1)
        self.view_download_retries = max(_safe_int(lora_conf.get("view_download_retries", 3), 3), 1)

        img2img_conf = self.config.get("img2img_settings", {}) or {}
        self.img2img_enabled = bool(img2img_conf.get("enable", True))
        self.img2img_denoise = min(max(_safe_float(img2img_conf.get("denoise_strength", 0.65), 0.65), 0.01), 1.0)
        self.img2img_auto_use_character_replace_workflow = bool(img2img_conf.get("auto_use_character_replace_workflow", True))
        replace_workflow = str(
            img2img_conf.get("character_replace_workflow", DEFAULT_CHARACTER_REPLACE_WORKFLOW)
            or DEFAULT_CHARACTER_REPLACE_WORKFLOW
        ).strip()
        if (
            replace_workflow in {LEGACY_UNION_CHARACTER_REPLACE_WORKFLOW, LEGACY_LLLITE_CHARACTER_REPLACE_WORKFLOW}
            and (self.workflow_dir / DEFAULT_CHARACTER_REPLACE_WORKFLOW).exists()
        ):
            replace_workflow = DEFAULT_CHARACTER_REPLACE_WORKFLOW
            logger.info("[ComfyUI] 已将旧版角色替换工作流自动切换为 Anima 官方参考图生图工作流")
        self.img2img_character_replace_workflow = replace_workflow
        self.img2img_character_replace_input_id = str(img2img_conf.get("character_replace_input_node_id", "11") or "11").strip()
        self.img2img_character_replace_neg_id = str(img2img_conf.get("character_replace_neg_node_id", "12") or "12").strip()
        replace_output_default = "404" if replace_workflow in {DEFAULT_CHARACTER_REPLACE_WORKFLOW, LEGACY_LLLITE_CHARACTER_REPLACE_WORKFLOW} else "66"
        replace_output_id = str(
            img2img_conf.get("character_replace_output_node_id", replace_output_default)
            or replace_output_default
        ).strip()
        if replace_workflow in {DEFAULT_CHARACTER_REPLACE_WORKFLOW, LEGACY_LLLITE_CHARACTER_REPLACE_WORKFLOW} and replace_output_id == "66":
            replace_output_id = "404"
        self.img2img_character_replace_output_id = replace_output_id
        self.img2img_character_replace_lora_id = str(img2img_conf.get("character_replace_lora_node_id", "99") or "99").strip()
        self.img2img_negative_prompt = str(
            img2img_conf.get("negative_prompt", "")
            or (
                "unchanged source character, original character identity, partial character replacement, "
                "only eyes changed, face-only swap, identity mixing, wrong hair, wrong outfit, source outfit kept, "
                "extreme close-up, body-part crop, cropped head, cropped face, cropped torso, cropped legs, out of frame"
            )
        ).strip()
        self.img2img_character_replace_control_strength = min(
            max(_safe_float(img2img_conf.get("character_replace_control_strength", 1.0), 1.0), 0.0),
            2.0,
        )
        self.img2img_character_replace_control_end = min(
            max(_safe_float(img2img_conf.get("character_replace_control_end_percent", 1.0), 1.0), 0.0),
            1.0,
        )
        self.img2img_character_replace_preprocessor = str(
            img2img_conf.get("character_replace_preprocessor", "DepthAnythingV2Preprocessor")
            or "DepthAnythingV2Preprocessor"
        ).strip()
        self.img2img_character_replace_preprocessor_resolution = max(
            _safe_int(img2img_conf.get("character_replace_preprocessor_resolution", 1024), 1024),
            256,
        )
        self.img2img_character_replace_scale_to_length = max(
            _safe_int(img2img_conf.get("character_replace_scale_to_length", 1536), 1536),
            512,
        )
        self.img2img_upload_subfolder = str(img2img_conf.get("upload_subfolder", "astrbot_img2img") or "").strip()

        if self.runtime_lora_override is None:
            self.current_lora = self.config_lora_name
        else:
            self.current_lora = self.runtime_lora_override
        if self.current_lora and not self._lora_allowed_by_runtime_family(self.current_lora):
            family_label = str(self.runtime_model_family_label or self.runtime_model_family or "当前模型族")
            if self.runtime_lora_override is None:
                logger.warning(f"[ComfyUI] WebUI 默认 LoRA 与 {family_label} 不兼容，本次已按未启用处理：{self.current_lora}")
            else:
                logger.warning(f"[ComfyUI] 命令临时 LoRA 与 {family_label} 不兼容，本次已按未启用处理：{self.current_lora}")
            self.current_lora = ""

        if self.runtime_strength_model is not None:
            self.strength_model = self.runtime_strength_model
        if self.runtime_strength_clip is not None:
            self.strength_clip = self.runtime_strength_clip

        self.lora_strength_cap_note = ""
        cap = self.runtime_lora_strength_cap if isinstance(self.runtime_lora_strength_cap, dict) else {}
        if self.current_lora and cap:
            before_model = self.strength_model
            before_clip = self.strength_clip
            changed = False
            max_model = _safe_float(cap.get("model"), 0)
            max_clip = _safe_float(cap.get("clip"), 0)
            if max_model > 0 and self.strength_model > max_model:
                self.strength_model = max_model
                changed = True
            if max_clip > 0 and self.strength_clip > max_clip:
                self.strength_clip = max_clip
                changed = True
            if changed:
                reason = str(cap.get("reason") or "当前画风已限制角色 LoRA 强度").strip()
                self.lora_strength_cap_note = (
                    f"{reason}：model {before_model:g}->{self.strength_model:g}, "
                    f"clip {before_clip:g}->{self.strength_clip:g}"
                )

    def _client_session(self, timeout: aiohttp.ClientTimeout, **kwargs):
        session_kwargs = dict(kwargs)
        session_kwargs["timeout"] = timeout
        if not self.server_ssl_verify:
            session_kwargs["connector"] = aiohttp.TCPConnector(ssl=False)
        return aiohttp.ClientSession(**session_kwargs)

    def _load_lora_cache_from_disk(self):
        try:
            if not self.lora_cache_path.exists():
                self._lora_cache = []
                return
            data = json.loads(self.lora_cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                items = data.get("loras", [])
            else:
                items = data
            self._lora_cache = sorted({str(x).strip() for x in items if str(x).strip()})
        except Exception as e:
            logger.warning(f"[ComfyUI] 读取 LoRA 缓存失败: {e}")
            self._lora_cache = []

    def _save_lora_cache_to_disk(self, loras):
        try:
            clean_loras = [
                str(x).strip()
                for x in (loras or [])
                if str(x).strip() and not _is_ignored_lora_name(str(x))
            ]
            payload = {
                "updated_at": int(time.time()),
                "server": self.url,
                "loras": sorted(set(clean_loras)),
            }
            self.lora_cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._lora_cache = payload["loras"]
        except Exception as e:
            logger.warning(f"[ComfyUI] 写入 LoRA 缓存失败: {e}")

    def get_lora_cache(self):
        if not self._lora_cache:
            self._load_lora_cache_from_disk()
        return list(self._lora_cache)

    def set_lora(self, lora_name: str = None, strength_model=None, strength_clip=None):
        """
        命令动态切换 LoRA。
        lora_name=None 表示清除命令覆盖，重新跟随 WebUI；
        lora_name="" 表示命令临时关闭 LoRA 注入。
        """
        if lora_name is None:
            self.runtime_lora_override = None
        else:
            self.runtime_lora_override = str(lora_name).strip()

        if strength_model is not None:
            self.runtime_strength_model = _safe_float(strength_model, self.strength_model)
        if strength_clip is not None:
            self.runtime_strength_clip = _safe_float(strength_clip, self.strength_clip)

        self._load_runtime_config()
        return self.current_lora, self.strength_model, self.strength_clip

    def set_multi_loras(self, loras=None):
        entries = []
        for item in (loras or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("lora_name") or item.get("lora") or item.get("name") or "").strip()
            char_prompt = str(item.get("char_prompt") or "").strip()
            if name and _is_ignored_lora_name(name):
                name = ""
            if not name and not char_prompt:
                continue
            entries.append({
                "lora_name": name,
                "strength_model": _safe_float(item.get("strength_model"), self.strength_model),
                "strength_clip": _safe_float(item.get("strength_clip"), self.strength_clip),
                "character": str(item.get("character") or "").strip(),
                "char_prompt": char_prompt,
                "role_identity_prompt": str(item.get("role_identity_prompt") or char_prompt).strip(),
                "role_aux_prompt": str(item.get("role_aux_prompt") or item.get("composition_prompt") or "").strip(),
                "global_prompt": str(item.get("global_prompt") or "").strip(),
                "interaction_prompt": str(item.get("interaction_prompt") or "").strip(),
                "contamination_negative_prompt": str(item.get("contamination_negative_prompt") or "").strip(),
                "prompt_only": bool(item.get("prompt_only") or not name),
                "interaction_intent": bool(item.get("interaction_intent")),
            })
        self.runtime_multi_loras = entries or None
        return self.runtime_multi_loras or []

    def set_multi_lora_mode(self, mode: str = None):
        self.runtime_multi_lora_mode = self._normalize_multi_lora_mode(mode)
        return self.runtime_multi_lora_mode

    def set_seed_override(self, seed=None):
        if seed is None:
            self.runtime_seed_override = None
            return self.runtime_seed_override
        text = str(seed).strip()
        if not re.fullmatch(r"\+?\d+", text):
            self.runtime_seed_override = None
            return self.runtime_seed_override
        self.runtime_seed_override = max(_safe_int(text, 0), 0)
        return self.runtime_seed_override

    def _normalize_multi_lora_mode(self, mode: str = None) -> str:
        value = str(mode or getattr(self, "runtime_multi_lora_mode", None) or "auto").strip().lower()
        aliases = {
            "regional": "regional_lora",
            "region": "regional_lora",
            "hook": "regional_lora",
            "hooks": "regional_lora",
            "staged": "staged_multi_lora",
            "stage": "staged_multi_lora",
            "staged_inpaint": "staged_multi_lora",
            "sequential": "staged_multi_lora",
            "safe": "safe_chain",
            "chain": "safe_chain",
            "dd": "densediffusion",
            "dense": "densediffusion",
            "densediffusion": "densediffusion",
            "dense_diffusion": "densediffusion",
            "off": "disabled",
            "none": "disabled",
            "false": "disabled",
        }
        value = aliases.get(value, value)
        if value not in {"auto", "staged_multi_lora", "regional_lora", "safe_chain", "densediffusion", "disabled"}:
            return "auto"
        return value

    def set_negative_prompt(self, negative_prompt: str = None):
        """命令动态切换默认负面提示词。None 表示恢复跟随 WebUI 配置。"""
        if negative_prompt is None:
            self.runtime_neg_prompt = None
        else:
            self.runtime_neg_prompt = str(negative_prompt)
        self._load_runtime_config()
        return self.neg_prompt

    def reload_config(self, filename: str, input_id: str = None, output_id: str = None, neg_node_id: str = None):
        """动态切换工作流，无需重启。命令切换会作为运行时覆盖保留到插件重载前。"""
        self.runtime_wf_filename = filename
        self.wf_filename = filename

        if input_id:
            self.runtime_input_id = str(input_id)
            self.input_id = str(input_id)
        else:
            self.runtime_input_id = self.input_id
        if output_id:
            self.runtime_output_id = str(output_id)
            self.output_id = str(output_id)
        else:
            self.runtime_output_id = self.output_id
        if neg_node_id:
            self.runtime_neg_node_id = str(neg_node_id)
            self.neg_node_id = str(neg_node_id)
        else:
            self.runtime_neg_node_id = self.neg_node_id

        self.workflow_path = self.workflow_dir / filename
        exists = self.workflow_path.exists()
        status = "存在" if exists else "不存在(请检查文件名)"
        logger.info(
            f"[ComfyUI] 切换工作流 -> {filename} [{status}] | "
            f"Input:{self.input_id} | Neg:{self.neg_node_id} | Output:{self.output_id or '自动'}"
        )
        return exists, (
            f"已切换至 {filename}，文件{status}。\n"
            f"当前节点设置: Positive={self.input_id}, Negative={self.neg_node_id}, Output={self.output_id or '自动'}"
        )

    # ------------------------------------------------------------------
    # LoRA 列表获取与工作流注入
    # ------------------------------------------------------------------
    async def fetch_lora_list(self, force: bool = False, allow_cache_fallback: bool = True):
        """通过 ComfyUI API 获取 LoRA 列表，可按调用场景决定是否允许回退缓存。"""
        if self._lora_cache and not force:
            return list(self._lora_cache), None

        self._load_runtime_config()
        timeout = aiohttp.ClientTimeout(total=DEFAULT_CONNECT_TIMEOUT)
        object_warn = ""
        try:
            async with self._client_session(timeout) as session:
                async with session.get(f"{self.url}/object_info") as resp:
                    if resp.status != 200:
                        object_warn = f"ComfyUI /object_info 返回 {resp.status}"
                        model_loras = await self._fetch_loras_from_models_endpoint(session)
                        if model_loras:
                            self._save_lora_cache_to_disk(model_loras)
                            logger.info(f"[ComfyUI] 已从 /models/loras 获取 LoRA 列表: {len(model_loras)} 个")
                            return list(self._lora_cache), object_warn
                        if not allow_cache_fallback:
                            return [], f"{object_warn}，未使用缓存"
                        cached = self.get_lora_cache()
                        return cached, f"{object_warn}，已使用缓存 {len(cached)} 个"
                    object_info = await resp.json()
                model_loras = await self._fetch_loras_from_models_endpoint(session)
        except Exception as e:
            object_warn = f"获取 LoRA 列表失败：{e}"
            if not allow_cache_fallback:
                return [], f"{object_warn}，未使用缓存"
            cached = self.get_lora_cache()
            return cached, f"{object_warn}，已使用缓存 {len(cached)} 个"

        loras = self._extract_lora_names_from_object_info(object_info)
        if model_loras:
            merged = list(dict.fromkeys([str(x).strip() for x in (loras + model_loras) if str(x).strip()]))
            loras = merged
        self._save_lora_cache_to_disk(loras)
        logger.info(f"[ComfyUI] 已从 API 获取 LoRA 列表: {len(loras)} 个")
        if not loras:
            return [], "ComfyUI 当前未返回 LoRA 选项，已清空插件 LoRA 缓存"
        return list(self._lora_cache), object_warn or None

    async def _fetch_loras_from_models_endpoint(self, session: aiohttp.ClientSession):
        """ComfyUI /models/loras sometimes sees files before all node object_info is refreshed."""
        try:
            async with session.get(f"{self.url}/models/loras") as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [
            str(item).strip()
            for item in data
            if str(item).strip().lower().endswith((".safetensors", ".ckpt", ".pt", ".bin"))
            and not _is_ignored_lora_name(str(item).strip())
        ]

    def _extract_lora_names_from_object_info(self, object_info: dict):
        names = set()
        if not isinstance(object_info, dict):
            return []

        def collect(value):
            if isinstance(value, str):
                if (
                    value.strip().lower().endswith((".safetensors", ".ckpt", ".pt", ".bin"))
                    and not _is_ignored_lora_name(value)
                ):
                    names.add(value.strip())
            elif isinstance(value, list):
                # ComfyUI 常见格式：[["a.safetensors", "b.safetensors"], {...}]
                if value and isinstance(value[0], list):
                    for item in value[0]:
                        collect(item)
                else:
                    for item in value:
                        collect(item)
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item)

        for class_name, class_info in object_info.items():
            if not isinstance(class_info, dict):
                continue
            input_info = class_info.get("input", {}) or {}
            required = input_info.get("required", {}) or {}
            optional = input_info.get("optional", {}) or {}
            all_inputs = {}
            all_inputs.update(required if isinstance(required, dict) else {})
            all_inputs.update(optional if isinstance(optional, dict) else {})

            class_name_lower = str(class_name).lower()
            for input_name, spec in all_inputs.items():
                input_name_lower = str(input_name).lower()
                if "lora" in input_name_lower or ("lora" in class_name_lower and input_name_lower in {"name", "file"}):
                    collect(spec)

        return sorted(names)

    def _find_lora_nodes(self, workflow: dict):
        nodes = []
        for nid, node_data in workflow.items():
            if not isinstance(node_data, dict):
                continue
            inputs = node_data.get("inputs", {}) or {}
            class_type = str(node_data.get("class_type", ""))
            if "lora_name" in inputs or "lora" in class_type.lower():
                nodes.append((str(nid), node_data))
        return nodes

    def _pick_lora_placeholder(self, valid_loras=None):
        candidates = []
        if valid_loras:
            candidates.extend(valid_loras)
        candidates.extend(self.get_lora_cache())

        fallback = ""
        seen = set()
        for item in candidates:
            name = str(item or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if not name.lower().endswith((".safetensors", ".ckpt", ".pt", ".bin")) or _is_ignored_lora_name(name):
                continue
            if not fallback:
                fallback = name
            if self._lora_allowed_by_runtime_family(name, allow_unknown=False):
                return name
        return fallback

    def _is_style_lora_node(self, node_id: str, node_data: dict) -> bool:
        if not isinstance(node_data, dict):
            return False
        inputs = node_data.get("inputs", {}) or {}
        if "lora_name" not in inputs and "lora" not in inputs and "name" not in inputs:
            return False
        if str(node_id) == str(self.lora_node_id or ""):
            return False
        title = str((node_data.get("_meta") or {}).get("title") or "").lower()
        class_type = str(node_data.get("class_type") or "").lower()
        marker = f"{title} {class_type}"
        return any(word in marker for word in ("style", "quality", "aesthetic", "画风", "风格", "质量"))

    def _is_utility_lora_node(self, node_id: str, node_data: dict) -> bool:
        if not isinstance(node_data, dict):
            return False
        inputs = node_data.get("inputs", {}) or {}
        title = str((node_data.get("_meta") or {}).get("title") or "").lower()
        class_type = str(node_data.get("class_type") or "").lower()
        name = str(inputs.get("lora_name") or inputs.get("lora") or inputs.get("name") or "").lower()
        marker = f"{title} {class_type} {name}"
        utility_markers = (
            "controlnet", "control_net", "union", "diffsynth", "lllite",
            "adapter", "t2i", "pose", "openpose", "depth", "sam", "segment",
            "bbox", "yolo", "preprocessor", "intrinsic",
        )
        return any(word in marker for word in utility_markers)

    def _find_character_lora_nodes(self, workflow: dict):
        if self.lora_node_id and isinstance(workflow.get(self.lora_node_id), dict):
            return [(self.lora_node_id, workflow[self.lora_node_id])]
        candidates = [
            (nid, node_data)
            for nid, node_data in self._find_lora_nodes(workflow)
            if not self._is_style_lora_node(nid, node_data)
            and not self._is_utility_lora_node(nid, node_data)
        ]
        if not candidates:
            return []
        character_nodes = [
            (nid, node_data)
            for nid, node_data in candidates
            if "character" in str((node_data.get("_meta") or {}).get("title") or "").lower()
            or "角色" in str((node_data.get("_meta") or {}).get("title") or "")
        ]
        return character_nodes or candidates

    def _find_style_lora_nodes(self, workflow: dict):
        if self.style_lora_node_id and str(self.style_lora_node_id) == str(self.lora_node_id or ""):
            logger.warning("[ComfyUI] 画风 LoRA 节点 ID 与角色 LoRA 节点 ID 相同，已忽略画风节点配置")
            return []
        if self.style_lora_node_id and isinstance(workflow.get(self.style_lora_node_id), dict):
            if self._is_utility_lora_node(self.style_lora_node_id, workflow[self.style_lora_node_id]):
                logger.warning("[ComfyUI] 画风 LoRA 节点 ID 指向工具/控制类 LoRA 节点，已忽略画风节点配置")
                return []
            return [(self.style_lora_node_id, workflow[self.style_lora_node_id])]
        return [
            (nid, node_data)
            for nid, node_data in self._find_lora_nodes(workflow)
            if self._is_style_lora_node(nid, node_data)
            and not self._is_utility_lora_node(nid, node_data)
        ]

    def _replace_exact_references(self, value, replacements: dict):
        if isinstance(value, list):
            key = None
            if len(value) == 2:
                key = (str(value[0]), str(value[1]))
            if key in replacements:
                return copy.deepcopy(replacements[key]), 1
            changed = 0
            new_value = []
            for item in value:
                replaced, count = self._replace_exact_references(item, replacements)
                new_value.append(replaced)
                changed += count
            return (new_value, changed) if changed else (value, 0)

        if isinstance(value, dict):
            changed = 0
            new_value = {}
            for key, item in value.items():
                replaced, count = self._replace_exact_references(item, replacements)
                new_value[key] = replaced
                changed += count
            return (new_value, changed) if changed else (value, 0)

        return value, 0

    def _replace_workflow_references(self, workflow: dict, replacements: dict, skip_node_ids=None) -> int:
        skip = {str(x) for x in (skip_node_ids or [])}
        rewired = 0
        for nid, node_data in workflow.items():
            if str(nid) in skip or not isinstance(node_data, dict):
                continue
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            for key, value in list(inputs.items()):
                replaced, count = self._replace_exact_references(value, replacements)
                if count:
                    inputs[key] = replaced
                    rewired += count
        return rewired

    def _find_primary_generation_refs(self, workflow: dict):
        model_ref = None
        clip_ref = None
        for node_data in workflow.values():
            if not isinstance(node_data, dict):
                continue
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            class_type = str(node_data.get("class_type") or "")
            if model_ref is None and ("Sampler" in class_type or class_type in {"KSampler", "KSamplerAdvanced"}):
                ref = inputs.get("model")
                if isinstance(ref, list) and len(ref) == 2:
                    model_ref = [str(ref[0]), int(ref[1]) if isinstance(ref[1], int) else ref[1]]
            if clip_ref is None and class_type == "CLIPTextEncode":
                ref = inputs.get("clip")
                if isinstance(ref, list) and len(ref) == 2:
                    clip_ref = [str(ref[0]), int(ref[1]) if isinstance(ref[1], int) else ref[1]]
            if model_ref is not None and clip_ref is not None:
                break
        return model_ref, clip_ref

    def _replace_lora_references(self, value, node_id: str, model_source, clip_source):
        if isinstance(value, list):
            if len(value) == 2 and str(value[0]) == node_id:
                output_idx = str(value[1])
                if output_idx == "0" and model_source is not None:
                    return copy.deepcopy(model_source), 1
                if output_idx == "1" and clip_source is not None:
                    return copy.deepcopy(clip_source), 1

            changed = 0
            new_value = []
            for item in value:
                replaced, count = self._replace_lora_references(item, node_id, model_source, clip_source)
                new_value.append(replaced)
                changed += count
            return (new_value, changed) if changed else (value, 0)

        if isinstance(value, dict):
            changed = 0
            new_value = {}
            for key, item in value.items():
                replaced, count = self._replace_lora_references(item, node_id, model_source, clip_source)
                new_value[key] = replaced
                changed += count
            return (new_value, changed) if changed else (value, 0)

        return value, 0

    def _bypass_lora_node(self, workflow: dict, node_id: str, node_inputs: dict):
        """把下游对 LoRA Loader 输出的引用改回原始 model/clip 输入。"""
        model_source = node_inputs.get("model")
        clip_source = node_inputs.get("clip")
        if model_source is None and clip_source is None:
            return 0

        rewired = 0
        for nid, node_data in workflow.items():
            if str(nid) == str(node_id) or not isinstance(node_data, dict):
                continue
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            for key, value in list(inputs.items()):
                replaced, count = self._replace_lora_references(value, str(node_id), model_source, clip_source)
                if count:
                    inputs[key] = replaced
                    rewired += count
        return rewired

    def _extract_validation_lora_names(self, error_text: str):
        try:
            data = json.loads(error_text)
        except Exception:
            data = {}

        haystacks = [error_text]
        if isinstance(data, dict):
            node_errors = data.get("node_errors", {}) or {}
            for node_error in node_errors.values():
                for err in (node_error or {}).get("errors", []) or []:
                    haystacks.append(str(err.get("details", "")))

        names = []
        fallback_names = []
        for text in haystacks:
            for match in re.finditer(r"not in\s*(\[[^\]]*\])", text):
                try:
                    values = ast.literal_eval(match.group(1))
                    if isinstance(values, list):
                        names.extend(str(x).strip() for x in values if str(x).strip())
                except Exception:
                    pass
            fallback_names.extend(re.findall(r"['\"]([^'\"]+\.(?:safetensors|ckpt|pt|bin))['\"]", text, flags=re.I))

        if names:
            return sorted({name for name in names if name})
        return sorted({name for name in fallback_names if name})

    def _extract_validation_lora_node_ids(self, error_text: str):
        try:
            data = json.loads(error_text)
        except Exception:
            return []
        node_errors = data.get("node_errors", {}) if isinstance(data, dict) else {}
        if not isinstance(node_errors, dict):
            return []
        return [str(node_id) for node_id in node_errors]

    def _neutralize_lora_nodes(self, workflow: dict, meta: dict, reason: str, valid_loras=None, target_node_ids=None):
        """把 LoRA 节点强度归零并尽量旁路，避免残留的坏 lora_name 被 ComfyUI 校验拦截。"""
        lora_nodes = self._find_lora_nodes(workflow)
        if target_node_ids is not None:
            wanted = {str(x) for x in target_node_ids}
            lora_nodes = [(nid, node) for nid, node in lora_nodes if nid in wanted]
        elif self.lora_node_id:
            lora_nodes = [(nid, node) for nid, node in lora_nodes if nid == self.lora_node_id]

        placeholder = self._pick_lora_placeholder(valid_loras)
        bypass_count = 0
        for nid, node_data in lora_nodes:
            inputs = node_data.setdefault("inputs", {})
            bypass_count += self._bypass_lora_node(workflow, nid, inputs)
            if placeholder:
                if "lora_name" in inputs:
                    inputs["lora_name"] = placeholder
                elif "lora" in inputs:
                    inputs["lora"] = placeholder
                elif "name" in inputs:
                    inputs["name"] = placeholder
            for key in ("strength_model", "strength_clip", "strength", "model_strength", "clip_strength"):
                if key in inputs:
                    inputs[key] = 0
            if str(nid) not in meta["lora_node_ids"]:
                meta["lora_node_ids"].append(str(nid))
        meta["lora_name"] = ""
        meta["strength_model"] = 0
        meta["strength_clip"] = 0
        meta["lora_warning"] = reason
        if bypass_count:
            meta["lora_warning"] += f"；已旁路 {bypass_count} 个 LoRA 输出引用"
        if placeholder and lora_nodes:
            meta["lora_warning"] += f"；仅用 {placeholder} 作为校验占位，LoRA 强度为 0，实际未应用该 LoRA"
        elif lora_nodes and not bypass_count:
            meta["lora_warning"] += "；未获取到可用 LoRA 占位，请刷新 ComfyUI LoRA 列表"
        logger.warning(f"[ComfyUI] {meta['lora_warning']}")
        return meta

    def _normalize_lora_lookup_key(self, value: str) -> str:
        value = Path(str(value or "").strip()).name
        value = re.sub(r"\.(safetensors|ckpt|pt|pth|bin)$", "", value, flags=re.I)
        return value.lower()

    def _runtime_guard_lora_keys(self, *fields) -> set:
        guard = getattr(self, "runtime_lora_family_guard", None)
        if not isinstance(guard, dict):
            return set()
        keys = set()
        for field in fields:
            for item in guard.get(field) or []:
                key = self._normalize_lora_lookup_key(item)
                if key:
                    keys.add(key)
        return keys

    def _runtime_guard_has_lora_key(self, name: str, *fields) -> bool:
        key = self._normalize_lora_lookup_key(name)
        return bool(key and key in self._runtime_guard_lora_keys(*fields))

    def _lora_allowed_by_runtime_family(self, name: str, allow_unknown: bool = False) -> bool:
        guard = getattr(self, "runtime_lora_family_guard", None)
        if not isinstance(guard, dict):
            return True
        raw = str(name or "").strip()
        if not raw:
            return True
        family = str(guard.get("family") or "").strip().lower()
        if not family:
            return True
        key = self._normalize_lora_lookup_key(raw)
        if key and key in self._runtime_guard_lora_keys("profile_keys", "trusted_lora_keys"):
            return True

        text = raw.replace("\\", "/").lower()
        if family == "illustrious":
            if any(marker in text for marker in ("anima", "pony", "flux", "sd15", "sd_15", "sd-15")):
                return False
            return allow_unknown or "illustrious" in text or "illu" in text
        if family == "anima":
            if any(marker in text for marker in ("illustrious", "pony", "flux")):
                return False
            return allow_unknown or "anima" in text
        return True

    def _lora_bound_to_profile(self, name: str) -> bool:
        guard = getattr(self, "runtime_lora_family_guard", None)
        if not isinstance(guard, dict):
            return False
        key = self._normalize_lora_lookup_key(name)
        if not key:
            return False
        profile_keys = {
            self._normalize_lora_lookup_key(item)
            for item in (guard.get("style_excluded_keys") or guard.get("profile_keys") or [])
            if self._normalize_lora_lookup_key(item)
        }
        return key in profile_keys

    def _looks_like_style_lora_name(self, name: str) -> bool:
        text = str(name or "").replace("\\", "/").lower()
        text = re.sub(r"[_\-.]+", " ", text)
        markers = (
            "style", "artstyle", "artist", "aesthetic", "quality", "detail",
            "detailed", "texture", "lighting", "render", "lineart", "outline",
            "color", "colour", "shade", "shading", "enhance", "enhancer",
            "masterpiece", "cinematic", "realistic", "realism", "illustration",
            "pvc", "figure", "figurine", "plastic", "resin", "3d", "3dcg", "cgi",
            "画风", "风格", "美术", "质感", "质量", "细节", "线稿", "上色", "光影",
        )
        return any(marker in text for marker in markers)

    def _lora_strength_is_active(self, inputs: dict) -> bool:
        if not isinstance(inputs, dict):
            return False
        keys = ("strength_model", "strength_clip", "strength", "model_strength", "clip_strength")
        seen_strength = False
        for key in keys:
            if key not in inputs:
                continue
            seen_strength = True
            try:
                if abs(float(inputs.get(key) or 0)) > 0.0001:
                    return True
            except Exception:
                return True
        return not seen_strength

    def _neutralize_specific_lora_nodes(self, workflow: dict, nodes: list, valid_loras=None) -> tuple:
        placeholder = self._pick_lora_placeholder(valid_loras)
        node_ids = []
        bypass_count = 0
        for nid, node_data in nodes or []:
            if not isinstance(node_data, dict):
                continue
            inputs = node_data.setdefault("inputs", {})
            bypass_count += self._bypass_lora_node(workflow, nid, inputs)
            if placeholder:
                if "lora_name" in inputs:
                    inputs["lora_name"] = placeholder
                elif "lora" in inputs:
                    inputs["lora"] = placeholder
                elif "name" in inputs:
                    inputs["name"] = placeholder
            for key in ("strength_model", "strength_clip", "strength", "model_strength", "clip_strength"):
                if key in inputs:
                    inputs[key] = 0
            node_ids.append(str(nid))
        return node_ids, bypass_count

    def _neutralize_incompatible_workflow_lora_defaults(self, workflow: dict):
        meta = {
            "family_guard_node_ids": [],
            "family_guard_warning": "",
        }
        guard = getattr(self, "runtime_lora_family_guard", None)
        if not isinstance(guard, dict):
            return meta
        bad_nodes = []
        bad_names = []
        for nid, node_data in self._find_lora_nodes(workflow):
            inputs = node_data.get("inputs", {}) if isinstance(node_data, dict) else {}
            name = str(inputs.get("lora_name") or inputs.get("lora") or inputs.get("name") or "").strip()
            if not name or not self._lora_strength_is_active(inputs):
                continue
            if self._runtime_guard_has_lora_key(name, "trusted_workflow_lora_keys"):
                continue
            if self._lora_allowed_by_runtime_family(name, allow_unknown=False):
                continue
            bad_nodes.append((nid, node_data))
            bad_names.append(name)
        if not bad_nodes:
            return meta
        node_ids, bypass_count = self._neutralize_specific_lora_nodes(workflow, bad_nodes, valid_loras=self.get_lora_cache())
        family_label = str(getattr(self, "runtime_model_family_label", "") or getattr(self, "runtime_model_family", "") or "当前模型族")
        shown = "、".join(dict.fromkeys(bad_names))
        warning = f"检测到与 {family_label} 不兼容的工作流 LoRA，已中和：{shown}"
        if bypass_count:
            warning += f"；已旁路 {bypass_count} 个输出引用"
        meta["family_guard_node_ids"] = node_ids
        meta["family_guard_warning"] = warning
        logger.warning(f"[ComfyUI] {warning}")
        return meta

    async def _apply_lora_override(self, workflow: dict, session: aiohttp.ClientSession = None):
        meta = {
            "lora_enabled": self.lora_enabled,
            "lora_name": self.current_lora or "",
            "lora_node_ids": [],
            "lora_warning": "",
            "strength_model": self.strength_model,
            "strength_clip": self.strength_clip,
            "lora_strength_cap_note": getattr(self, "lora_strength_cap_note", ""),
        }
        lora_nodes = self._find_character_lora_nodes(workflow)
        target_node_ids = [nid for nid, _ in lora_nodes]
        multi_lora_entries = self._runtime_multi_lora_entries()

        if not self.lora_enabled:
            return self._neutralize_lora_nodes(workflow, meta, "LoRA 注入已在 WebUI 关闭", target_node_ids=target_node_ids)
        if multi_lora_entries:
            return await self._apply_multi_lora_override(
                workflow,
                meta,
                multi_lora_entries,
                lora_nodes,
                target_node_ids,
                session=session,
            )
        if not self.current_lora:
            return self._neutralize_lora_nodes(workflow, meta, "本次未使用 LoRA，已跳过自动 LoRA 注入", target_node_ids=target_node_ids)

        cached_loras = self.get_lora_cache()
        if cached_loras and self.current_lora not in cached_loras:
            return self._neutralize_lora_nodes(workflow, meta, f"LoRA 不在 ComfyUI 可用列表，已跳过注入：{self.current_lora}", target_node_ids=target_node_ids)
        if not self._lora_allowed_by_runtime_family(self.current_lora):
            family_label = str(getattr(self, "runtime_model_family_label", "") or getattr(self, "runtime_model_family", "") or "当前模型族")
            return self._neutralize_lora_nodes(
                workflow,
                meta,
                f"LoRA 与 {family_label} 不兼容，已跳过注入：{self.current_lora}",
                valid_loras=cached_loras,
                target_node_ids=target_node_ids,
            )

        if not lora_nodes:
            meta["lora_warning"] = (
                f"未在工作流中找到 LoRA 节点"
                f"{f' {self.lora_node_id}' if self.lora_node_id else ''}，无法注入 {self.current_lora}"
            )
            logger.warning(f"[ComfyUI] {meta['lora_warning']}")
            return meta

        targets = lora_nodes if self.apply_all_lora_nodes else lora_nodes[:1]
        for nid, node_data in targets:
            inputs = node_data.setdefault("inputs", {})
            if "lora_name" in inputs:
                inputs["lora_name"] = self.current_lora
            elif "lora" in inputs:
                inputs["lora"] = self.current_lora
            elif "name" in inputs:
                inputs["name"] = self.current_lora
            else:
                inputs["lora_name"] = self.current_lora

            if "strength_model" in inputs:
                inputs["strength_model"] = self.strength_model
            if "strength_clip" in inputs:
                inputs["strength_clip"] = self.strength_clip
            if "strength" in inputs:
                inputs["strength"] = self.strength_model
            if "model_strength" in inputs:
                inputs["model_strength"] = self.strength_model
            if "clip_strength" in inputs:
                inputs["clip_strength"] = self.strength_clip

            meta["lora_node_ids"].append(nid)

        logger.info(
            f"[ComfyUI] ✓ LoRA 注入: {self.current_lora} | "
            f"节点: {', '.join(meta['lora_node_ids'])} | "
            f"model={self.strength_model}, clip={self.strength_clip}"
        )
        return meta

    def _neutralize_style_lora_nodes(self, workflow: dict, meta: dict, reason: str, valid_loras=None, nodes=None):
        style_nodes = nodes if nodes is not None else self._find_style_lora_nodes(workflow)
        placeholder = self._pick_lora_placeholder(valid_loras)
        bypass_count = 0
        for nid, node_data in style_nodes:
            inputs = node_data.setdefault("inputs", {})
            bypass_count += self._bypass_lora_node(workflow, nid, inputs)
            if placeholder:
                if "lora_name" in inputs:
                    inputs["lora_name"] = placeholder
                elif "lora" in inputs:
                    inputs["lora"] = placeholder
                elif "name" in inputs:
                    inputs["name"] = placeholder
            for key in ("strength_model", "strength_clip", "strength", "model_strength", "clip_strength"):
                if key in inputs:
                    inputs[key] = 0
            if str(nid) not in meta["style_lora_node_ids"]:
                meta["style_lora_node_ids"].append(str(nid))
        meta["style_lora_name"] = ""
        meta["style_strength_model"] = 0
        meta["style_strength_clip"] = 0
        meta["style_lora_warning"] = reason
        if bypass_count:
            meta["style_lora_warning"] += f"；已旁路 {bypass_count} 个画风 LoRA 输出引用"
        if placeholder and style_nodes:
            meta["style_lora_warning"] += f"；已用 {placeholder} 占位并把画风 LoRA 强度设为 0"
        logger.warning(f"[ComfyUI] {meta['style_lora_warning']}")
        return meta

    def _set_lora_node_inputs(self, node_data: dict, lora_name: str, strength_model: float, strength_clip: float):
        inputs = node_data.setdefault("inputs", {})
        if "lora_name" in inputs:
            inputs["lora_name"] = lora_name
        elif "lora" in inputs:
            inputs["lora"] = lora_name
        elif "name" in inputs:
            inputs["name"] = lora_name
        else:
            inputs["lora_name"] = lora_name
        if "strength_model" in inputs:
            inputs["strength_model"] = strength_model
        if "strength_clip" in inputs:
            inputs["strength_clip"] = strength_clip
        if "strength" in inputs:
            inputs["strength"] = strength_model
        if "model_strength" in inputs:
            inputs["model_strength"] = strength_model
        if "clip_strength" in inputs:
            inputs["clip_strength"] = strength_clip

    def _runtime_multi_lora_entries(self) -> list:
        entries = []
        for item in (getattr(self, "runtime_multi_loras", None) or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("lora_name") or item.get("lora") or item.get("name") or "").strip()
            char_prompt = str(item.get("char_prompt") or "").strip()
            if name and _is_ignored_lora_name(name):
                name = ""
            if not name and not char_prompt:
                continue
            entries.append({
                "lora_name": name,
                "strength_model": _safe_float(item.get("strength_model"), self.strength_model),
                "strength_clip": _safe_float(item.get("strength_clip"), self.strength_clip),
                "character": str(item.get("character") or "").strip(),
                "char_prompt": char_prompt,
                "role_identity_prompt": str(item.get("role_identity_prompt") or char_prompt).strip(),
                "role_aux_prompt": str(item.get("role_aux_prompt") or item.get("composition_prompt") or "").strip(),
                "global_prompt": str(item.get("global_prompt") or "").strip(),
                "interaction_prompt": str(item.get("interaction_prompt") or "").strip(),
                "contamination_negative_prompt": str(item.get("contamination_negative_prompt") or "").strip(),
                "prompt_only": bool(item.get("prompt_only") or not name),
                "interaction_intent": bool(item.get("interaction_intent")),
            })
        return entries

    def _find_nodes_by_class_type(self, workflow: dict, class_type: str) -> list:
        result = []
        for nid, node_data in (workflow or {}).items():
            if isinstance(node_data, dict) and str(node_data.get("class_type") or "") == str(class_type):
                result.append((str(nid), node_data))
        result.sort(key=lambda item: int(item[0]) if str(item[0]).isdigit() else 999999)
        return result

    def _multi_lora_hook_prompt_node_ids(self, workflow: dict) -> list:
        preferred = []
        for nid in ("33", "34", "35"):
            node = workflow.get(nid) if isinstance(workflow, dict) else None
            if isinstance(node, dict) and str(node.get("class_type") or "") == "CLIPTextEncode":
                preferred.append(str(nid))
        if len(preferred) >= 2:
            return preferred

        seen = set(preferred)
        for nid, node_data in self._find_nodes_by_class_type(workflow, "CLIPTextEncode"):
            if nid in seen:
                continue
            title = str((node_data.get("_meta") or {}).get("title") or "").lower()
            if "character" not in title:
                continue
            preferred.append(nid)
            seen.add(nid)
        return preferred

    def _set_sampler_positive_ref(self, workflow: dict, positive_ref: list) -> int:
        changed = 0
        for node_data in (workflow or {}).values():
            if not isinstance(node_data, dict):
                continue
            class_type = str(node_data.get("class_type") or "")
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict) or "positive" not in inputs:
                continue
            if "Sampler" not in class_type and class_type not in {"KSampler", "KSamplerAdvanced"}:
                continue
            inputs["positive"] = copy.deepcopy(positive_ref)
            changed += 1
        return changed

    def _set_sampler_conditioning_refs(self, workflow: dict, positive_ref: list, negative_ref: list = None) -> int:
        changed = 0
        for node_data in (workflow or {}).values():
            if not isinstance(node_data, dict):
                continue
            class_type = str(node_data.get("class_type") or "")
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict) or "positive" not in inputs:
                continue
            if "Sampler" not in class_type and class_type not in {"KSampler", "KSamplerAdvanced"}:
                continue
            inputs["positive"] = copy.deepcopy(positive_ref)
            if negative_ref is not None and "negative" in inputs:
                inputs["negative"] = copy.deepcopy(negative_ref)
            changed += 1
        return changed

    def _sampler_node_items(self, workflow: dict) -> list:
        samplers = []
        for node_id, node_data in (workflow or {}).items():
            if not isinstance(node_data, dict):
                continue
            class_type = str(node_data.get("class_type") or "")
            if "Sampler" not in class_type and class_type not in {"KSampler", "KSamplerAdvanced"}:
                continue
            inputs = node_data.get("inputs", {})
            if isinstance(inputs, dict) and "positive" in inputs:
                samplers.append((str(node_id), node_data))
        samplers.sort(key=lambda item: int(item[0]) if str(item[0]).isdigit() else 999999)
        return samplers

    def _densediffusion_base_sampler_node_ids(self, workflow: dict) -> set:
        base_sampler_ids = set()
        for node_id, node_data in self._sampler_node_items(workflow):
            inputs = node_data.get("inputs", {}) if isinstance(node_data, dict) else {}
            latent_ref = inputs.get("latent_image") if isinstance(inputs, dict) else None
            latent_node = workflow.get(str(latent_ref[0])) if isinstance(latent_ref, list) and latent_ref else None
            if isinstance(latent_node, dict) and str(latent_node.get("class_type") or "") == "EmptyLatentImage":
                base_sampler_ids.add(str(node_id))
        if base_sampler_ids:
            return base_sampler_ids
        samplers = self._sampler_node_items(workflow)
        return {samplers[0][0]} if samplers else set()

    def _repeat_conditioning_with_concat(self, workflow: dict, conditioning_ref: list, count: int, title: str) -> list:
        if not isinstance(conditioning_ref, list) or count <= 1:
            return copy.deepcopy(conditioning_ref)
        current_ref = copy.deepcopy(conditioning_ref)
        for idx in range(2, int(count) + 1):
            concat_id = self._next_node_id(workflow)
            workflow[concat_id] = {
                "inputs": {
                    "conditioning_to": current_ref,
                    "conditioning_from": copy.deepcopy(conditioning_ref),
                },
                "class_type": "ConditioningConcat",
                "_meta": {"title": f"{title} x{idx}"},
            }
            current_ref = [str(concat_id), 0]
        return current_ref

    def _get_workflow_latent_size(self, workflow: dict) -> tuple:
        for node_data in (workflow or {}).values():
            if not isinstance(node_data, dict):
                continue
            if str(node_data.get("class_type") or "") != "EmptyLatentImage":
                continue
            inputs = node_data.get("inputs", {}) or {}
            return (
                _safe_int(inputs.get("width"), 960),
                _safe_int(inputs.get("height"), 1344),
            )
        return 960, 1344

    def _multi_lora_debug_dir(self) -> Path:
        return self.data_dir / "debug" / "multi_lora_masks"

    def _save_multi_lora_debug_workflow(self, workflow: dict, meta: dict, label: str = "regional") -> str:
        if not self.multi_lora_debug_workflow:
            return ""
        try:
            debug_dir = self._multi_lora_debug_dir()
            debug_dir.mkdir(parents=True, exist_ok=True)
            safe_label = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(label or "regional")).strip("_") or "regional"
            path = debug_dir / f"{int(time.time())}_{safe_label}_{uuid.uuid4().hex[:8]}_workflow.json"
            path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
            if isinstance(meta, dict):
                meta["multi_lora_debug_workflow_path"] = str(path)
            logger.info(f"[ComfyUI] Multi-LoRA debug workflow dumped: {path}")
            return str(path)
        except Exception as exc:
            logger.warning(f"[ComfyUI] Multi-LoRA debug workflow dump failed: {exc}")
            return ""

    def _clip_hook_chain_ids(self, workflow: dict, clip_ref) -> list:
        hooks = []
        visited = set()
        ref = clip_ref
        while isinstance(ref, list) and len(ref) == 2:
            node_id = str(ref[0])
            if node_id in visited:
                break
            visited.add(node_id)
            node = workflow.get(node_id) if isinstance(workflow, dict) else None
            if not isinstance(node, dict) or str(node.get("class_type") or "") != "SetClipHooks":
                break
            inputs = node.get("inputs", {}) or {}
            hook_ref = inputs.get("hooks")
            if isinstance(hook_ref, list) and len(hook_ref) == 2:
                hooks.append(str(hook_ref[0]))
            ref = inputs.get("clip")
        return hooks

    def _find_base_clip_ref_for_hooks(self, workflow: dict):
        candidate_ids = [self.input_id, self.neg_node_id, "11", "12"]
        seen = set()
        for node_id in candidate_ids:
            node_id = str(node_id or "").strip()
            if not node_id or node_id in seen:
                continue
            seen.add(node_id)
            node = workflow.get(node_id) if isinstance(workflow, dict) else None
            if not isinstance(node, dict) or str(node.get("class_type") or "") != "CLIPTextEncode":
                continue
            ref = (node.get("inputs", {}) or {}).get("clip")
            if isinstance(ref, list) and len(ref) == 2 and self._node_class_type(workflow, str(ref[0])) != "SetClipHooks":
                return copy.deepcopy(ref)
        for nid, node in self._find_nodes_by_class_type(workflow, "CLIPSetLastLayer"):
            return [str(nid), 0]
        for nid, node in self._find_nodes_by_class_type(workflow, "CheckpointLoaderSimple"):
            return [str(nid), 1]
        return None

    async def _upload_split_masks(
        self,
        session: aiohttp.ClientSession,
        width: int,
        height: int,
        count: int,
        overlap_ratio: float = None,
        blur_radius: float = 0.0,
        binary: bool = False,
        debug_label: str = "",
    ) -> list:
        if session is None or count < 2:
            return []
        try:
            import io
            from PIL import Image as PILImage
            from PIL import ImageFilter
        except Exception as exc:
            logger.warning(f"[ComfyUI] Multi-LoRA split masks unavailable: {exc}")
            return []

        width = max(64, _safe_int(width, 960))
        height = max(64, _safe_int(height, 1344))
        count = max(2, min(3, _safe_int(count, 2)))
        subfolder = str(self.img2img_upload_subfolder or "astrbot_runtime").strip().strip("/")
        uploaded = [None] * count
        rows = []

        for idx in range(count):
            band_start = int(width * idx / count)
            band_end = int(width * (idx + 1) / count)
            if binary:
                effective_start = band_start
                effective_end = band_end
            else:
                if overlap_ratio is None:
                    overlap = max(int(width * 0.08 / count), 8)
                else:
                    overlap = max(int(width * max(0.0, min(float(overlap_ratio), 0.8)) / count), 8)
                effective_start = max(0, band_start - overlap)
                effective_end = min(width, band_end + overlap)

            row = bytearray(width)
            for x in range(effective_start, effective_end):
                if binary:
                    alpha = 255
                elif x < band_start:
                    alpha = int(255 * (x - effective_start) / max(1, band_start - effective_start))
                elif x >= band_end:
                    alpha = int(255 * (effective_end - x) / max(1, effective_end - band_end))
                else:
                    alpha = 255
                row[x] = max(0, min(255, alpha))
            rows.append(row)

            image = PILImage.frombytes("L", (width, height), bytes(row) * height)
            if not binary:
                blur = max(12.0, _safe_float(blur_radius, 12.0))
                image = image.filter(ImageFilter.GaussianBlur(radius=min(blur, 96.0)))
            debug_path = ""
            if self.multi_lora_debug_masks:
                try:
                    debug_dir = self._multi_lora_debug_dir()
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    safe_label = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(debug_label or "split")).strip("_") or "split"
                    debug_path_obj = debug_dir / f"{int(time.time())}_{safe_label}_{idx + 1}of{count}_{uuid.uuid4().hex[:8]}.png"
                    image.save(debug_path_obj, format="PNG")
                    debug_path = str(debug_path_obj)
                except Exception as exc:
                    logger.warning(f"[ComfyUI] Multi-LoRA debug mask save failed: {exc}")
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            buf.seek(0)

            filename = f"astrbot_split_mask_{uuid.uuid4().hex}_{idx + 1}of{count}.png"
            data = {}
            uploaded_ok = False
            for attempt in range(2):
                buf.seek(0)
                form = aiohttp.FormData()
                form.add_field("image", buf, filename=filename, content_type="image/png")
                form.add_field("type", "input")
                form.add_field("overwrite", "true")
                if subfolder:
                    form.add_field("subfolder", subfolder)
                try:
                    async with session.post(f"{self.url}/upload/image", data=form) as resp:
                        text = await resp.text()
                        if resp.status >= 400:
                            logger.warning(f"[ComfyUI] Multi-LoRA split mask upload failed: {resp.status} {text[:160]}")
                            if attempt == 0:
                                await asyncio.sleep(0.35)
                                continue
                            break
                        try:
                            data = json.loads(text) if text else {}
                        except Exception:
                            data = {}
                        uploaded_ok = True
                        break
                except Exception as exc:
                    logger.warning(f"[ComfyUI] Multi-LoRA split mask upload exception: {exc}")
                    if attempt == 0:
                        await asyncio.sleep(0.35)
                        continue
                    break
            if not uploaded_ok:
                continue

            name = str(data.get("name") or filename).strip()
            returned_subfolder = str(data.get("subfolder") or subfolder or "").strip().strip("/")
            image_value = f"{returned_subfolder}/{name}" if returned_subfolder else name
            uploaded[idx] = {
                "name": name,
                "subfolder": returned_subfolder,
                "image_value": image_value,
                "binary": bool(binary),
                "debug_path": debug_path,
            }

        if binary and rows:
            union_cols = 0
            overlap_cols = 0
            for x in range(width):
                hits = sum(1 for row in rows if row[x] >= 128)
                if hits:
                    union_cols += 1
                if hits > 1:
                    overlap_cols += 1
            coverage_ratio = union_cols / max(1, width)
            overlap_pixels = overlap_cols * height
            validation = {
                "binary": True,
                "width": width,
                "height": height,
                "count": count,
                "coverage_ratio": round(coverage_ratio, 6),
                "overlap_pixels": int(overlap_pixels),
            }
            for item in uploaded:
                if isinstance(item, dict):
                    item["validation"] = validation
            if overlap_pixels or coverage_ratio < 0.999:
                logger.warning(
                    f"[ComfyUI] Multi-LoRA binary mask validation warning: "
                    f"coverage={coverage_ratio:.4f} overlap_pixels={overlap_pixels}"
                )
            else:
                logger.info(
                    f"[ComfyUI] Multi-LoRA binary masks validated: "
                    f"count={count} size={width}x{height} coverage={coverage_ratio:.4f}"
                )

        return uploaded

    def _attach_multi_lora_split_masks(self, workflow: dict, combine_nodes: list, mask_infos: list) -> list:
        if not combine_nodes or not mask_infos:
            return []
        attached = []
        for idx, (combine_nid, combine_node) in enumerate(combine_nodes[:len(mask_infos)]):
            mask_info = mask_infos[idx] if idx < len(mask_infos) else None
            if not mask_info:
                continue
            load_id = self._next_node_id(workflow)
            workflow[load_id] = {
                "inputs": {"image": mask_info["image_value"]},
                "class_type": "LoadImage",
                "_meta": {"title": f"AstrBot Multi-LoRA Split Mask {idx + 1}"},
            }
            mask_id = self._next_node_id(workflow)
            workflow[mask_id] = {
                "inputs": {"image": [load_id, 0], "channel": "red"},
                "class_type": "ImageToMask",
                "_meta": {"title": f"AstrBot Multi-LoRA Mask Convert {idx + 1}"},
            }
            inputs = combine_node.setdefault("inputs", {})
            inputs["mask"] = [mask_id, 0]
            inputs["set_cond_area"] = "default"
            if "strength" in inputs:
                inputs["strength"] = 1.0
            attached.append({
                "combine_node": str(combine_nid),
                "load_node": str(load_id),
                "mask_node": str(mask_id),
                "image": mask_info["image_value"],
            })
        return attached

    def _multi_lora_regional_prompt_node_ids(self, workflow: dict) -> list:
        return self._multi_lora_hook_prompt_node_ids(workflow)

    def _multi_lora_regional_combine_node(self, workflow: dict, count: int):
        preferred_type = "ConditioningSetMaskAndCombine3" if count >= 3 else "ConditioningSetMaskAndCombine"
        nodes = self._find_nodes_by_class_type(workflow, preferred_type)
        if nodes:
            return nodes[0]
        if count >= 3:
            nodes = self._find_nodes_by_class_type(workflow, "ConditioningSetMaskAndCombine")
            if nodes:
                return nodes[0]
        return None

    def _multi_lora_global_region_output_node(self, workflow: dict, count: int):
        preferred_id = "39" if count >= 3 else "38"
        node = workflow.get(preferred_id) if isinstance(workflow, dict) else None
        if isinstance(node, dict) and str(node.get("class_type") or "") == "ConditioningCombine":
            return preferred_id, node
        title_marker = "three regional" if count >= 3 else "two regional"
        for nid, node_data in self._find_nodes_by_class_type(workflow, "ConditioningCombine"):
            title = str((node_data.get("_meta") or {}).get("title") or "").lower()
            if title_marker in title:
                return nid, node_data
        return None

    def _multi_lora_prompt_has_close_interaction(self, text: str) -> bool:
        return bool(re.search(
            r"(相互|互相|抱|拥抱|搂|贴贴|牵手|亲吻|接吻|亲密|对视|依偎|"
            r"hug|hugging|embrac|cuddle|kiss|holding\s+hands|holding\s+each\s+other|"
            r"close\s+together|facing\s+each\s+other|intimate|affectionate)",
            str(text or ""),
            re.I,
        ))

    def _multi_region_entry_identity_prompt(self, item: dict) -> str:
        if not isinstance(item, dict):
            return ""
        return str(
            item.get("role_identity_prompt")
            or item.get("char_prompt")
            or item.get("character")
            or item.get("lora_name")
            or ""
        ).strip()

    def _dedupe_prompt_text(self, text: str) -> str:
        seen = set()
        parts = []
        for raw in re.split(r"[,，\n\r]+", str(text or "")):
            tag = raw.strip()
            if not tag:
                continue
            marker = re.sub(r"[\s_\-\.]+", "", tag.lower())
            if marker in seen:
                continue
            seen.add(marker)
            parts.append(tag)
        return ", ".join(parts)

    def _multi_region_entry_aux_prompt(self, item: dict, position: str = "", anchor: str = "", interaction_intent: bool = False, count: int = 2) -> str:
        if not isinstance(item, dict):
            return ""
        parts = [
            str(position or "").strip(),
            str(anchor or "").strip(),
            str(item.get("role_aux_prompt") or item.get("composition_prompt") or "").strip(),
            str(item.get("interaction_prompt") or "").strip(),
            "close to the other character" if interaction_intent and count == 2 else "",
            "shared interaction pose" if interaction_intent else "",
            "narrow personal region" if count >= 3 else "",
            "only this character in this region",
            "detailed face",
            "detailed hair",
        ]
        return self._dedupe_prompt_text(", ".join(part for part in parts if part))

    def _multi_region_entry_region_prompt(self, item: dict, position: str = "", anchor: str = "", interaction_intent: bool = False, count: int = 2) -> str:
        return self._dedupe_prompt_text(", ".join(part for part in [
            str(position or "").strip(),
            str(anchor or "").strip(),
            self._multi_region_entry_identity_prompt(item),
            self._multi_region_entry_aux_prompt(item, position="", anchor="", interaction_intent=interaction_intent, count=count),
        ] if part))

    def _attach_multi_lora_regional_masks(self, workflow: dict, combine_node: dict, mask_infos: list) -> list:
        if not isinstance(combine_node, dict) or not mask_infos:
            return []
        inputs = combine_node.setdefault("inputs", {})
        attached = []
        for idx, mask_info in enumerate(mask_infos, start=1):
            if not mask_info:
                continue
            key = f"mask_{idx}"
            if key not in inputs:
                continue
            load_id = self._next_node_id(workflow)
            workflow[load_id] = {
                "inputs": {"image": mask_info["image_value"]},
                "class_type": "LoadImage",
                "_meta": {"title": f"AstrBot Multi-LoRA Regional Mask {idx}"},
            }
            mask_id = self._next_node_id(workflow)
            workflow[mask_id] = {
                "inputs": {"image": [load_id, 0], "channel": "red"},
                "class_type": "ImageToMask",
                "_meta": {"title": f"AstrBot Multi-LoRA Regional Mask Convert {idx}"},
            }
            inputs[key] = [mask_id, 0]
            strength_key = f"mask_{idx}_strength"
            if strength_key in inputs:
                inputs[strength_key] = 1.0
            attached.append({
                "combine_node": "",
                "load_node": str(load_id),
                "mask_node": str(mask_id),
                "mask_input": key,
                "image": mask_info["image_value"],
            })
        if "set_cond_area" in inputs:
            inputs["set_cond_area"] = "default"
        return attached

    def _attach_img2img_latent_noise_mask(self, workflow: dict, mask_info: dict, meta: dict, options: dict = None, latent_ref_override=None) -> int:
        if not isinstance(workflow, dict) or not isinstance(mask_info, dict):
            return 0
        image_value = str(mask_info.get("image_value") or "").strip()
        if not image_value:
            return 0

        sampler_nodes = []
        for node_id, node_data in workflow.items():
            if not isinstance(node_data, dict):
                continue
            class_type = str(node_data.get("class_type") or "")
            if "Sampler" not in class_type and class_type not in {"KSampler", "KSamplerAdvanced"}:
                continue
            inputs = node_data.get("inputs", {})
            if isinstance(inputs, dict) and isinstance(inputs.get("latent_image"), list):
                sampler_nodes.append((str(node_id), node_data))
        if not sampler_nodes:
            meta["img2img_inpaint_mask_warning"] = "No sampler latent_image input found for staged inpaint mask"
            return 0

        latent_ref = latent_ref_override if isinstance(latent_ref_override, list) else sampler_nodes[0][1].get("inputs", {}).get("latent_image")
        if not isinstance(latent_ref, list):
            return 0

        load_id = self._next_node_id(workflow)
        workflow[load_id] = {
            "inputs": {"image": image_value},
            "class_type": "LoadImage",
            "_meta": {"title": "AstrBot Staged Inpaint Mask"},
        }
        mask_id = self._next_node_id(workflow)
        workflow[mask_id] = {
            "inputs": {"image": [load_id, 0], "channel": "red"},
            "class_type": "ImageToMask",
            "_meta": {"title": "AstrBot Staged Inpaint Mask Convert"},
        }
        noise_id = self._next_node_id(workflow)
        workflow[noise_id] = {
            "inputs": {"samples": latent_ref, "mask": [mask_id, 0]},
            "class_type": "SetLatentNoiseMask",
            "_meta": {"title": "AstrBot Staged Inpaint Noise Mask"},
        }
        for _node_id, node_data in sampler_nodes:
            node_data.setdefault("inputs", {})["latent_image"] = [noise_id, 0]

        opts = options or {}
        meta["img2img_inpaint_mask"] = {
            "image": image_value,
            "slot": _safe_int(opts.get("regional_mask_slot"), 0),
            "count": _safe_int(opts.get("regional_mask_count"), 0),
            "load_node": str(load_id),
            "mask_node": str(mask_id),
            "noise_mask_node": str(noise_id),
            "sampler_nodes": [node_id for node_id, _node in sampler_nodes],
        }
        return len(sampler_nodes)

    async def _apply_multi_lora_regional_conditioning(self, workflow: dict, meta: dict, entries: list, session: aiohttp.ClientSession = None) -> bool:
        count = min(len(entries or []), 3)
        if count < 2:
            return False
        prompt_node_ids = self._multi_lora_regional_prompt_node_ids(workflow)
        combine_pair = self._multi_lora_regional_combine_node(workflow, count)
        if len(prompt_node_ids) < count or not combine_pair:
            return False
        combine_id, combine_node = combine_pair
        output_pair = self._multi_lora_global_region_output_node(workflow, count)
        # Node 11 is already written by _inject_params with the fully processed
        # quality prefix/stabilizer prompt. Do not overwrite it with the earlier
        # multi-LoRA global prompt; character tags belong only in regional nodes.
        global_text = str((((workflow.get("11") or {}).get("inputs") or {}).get("text")) or "")
        interaction_intent = bool(any(item.get("interaction_intent") for item in entries[:count])) or self._multi_lora_prompt_has_close_interaction(global_text)

        position_names = ["left character", "right character"] if count == 2 else ["left character", "center character", "right character"]
        position_anchors = ["on the left side", "on the right side"] if count == 2 else ["on the left side", "in the center", "on the right side"]
        for idx, item in enumerate(entries[:count]):
            prompt_nid = str(prompt_node_ids[idx])
            prompt_node = workflow.get(prompt_nid)
            if not isinstance(prompt_node, dict):
                continue
            position = position_names[idx] if idx < len(position_names) else f"character {idx + 1}"
            anchor = position_anchors[idx] if idx < len(position_anchors) else position
            region_prompt = self._multi_region_entry_region_prompt(
                item,
                position=position,
                anchor=anchor,
                interaction_intent=interaction_intent,
                count=count,
            )
            prompt_node.setdefault("inputs", {})["text"] = region_prompt

        for prompt_nid in prompt_node_ids[count:]:
            prompt_node = workflow.get(str(prompt_nid))
            if isinstance(prompt_node, dict):
                prompt_node.setdefault("inputs", {})["text"] = ""

        mask_infos = await self._upload_split_masks(
            session,
            *self._get_workflow_latent_size(workflow),
            count,
            overlap_ratio=0.25 if interaction_intent else 0.10,
        )
        if len(mask_infos) < count or not all(mask_infos[:count]):
            meta["multi_lora_regional_warning"] = "Multi-LoRA regional masks upload failed; regional conditioning disabled"
            logger.warning(f"[ComfyUI] {meta['multi_lora_regional_warning']}")
            self._set_sampler_conditioning_refs(workflow, ["11", 0], ["12", 0])
            return False

        attached = self._attach_multi_lora_regional_masks(workflow, combine_node, mask_infos[:count])
        if len(attached) < count:
            meta["multi_lora_regional_warning"] = "Multi-LoRA regional masks incomplete; regional conditioning disabled"
            logger.warning(f"[ComfyUI] {meta['multi_lora_regional_warning']}")
            self._set_sampler_conditioning_refs(workflow, ["11", 0], ["12", 0])
            return False

        for item in attached:
            item["combine_node"] = str(combine_id)
        positive_ref = [str(combine_id), 0]
        if output_pair:
            output_id, _output_node = output_pair
            positive_ref = [str(output_id), 0]
        self._set_sampler_conditioning_refs(workflow, positive_ref, ["12", 0])
        meta["multi_lora_regional_conditioning"] = True
        meta["multi_lora_region_count"] = count
        meta["multi_lora_region_prompt_node_ids"] = [str(x) for x in prompt_node_ids[:count]]
        meta["multi_lora_region_combine_node"] = str(combine_id)
        meta["multi_lora_region_output_node"] = str(positive_ref[0])
        meta["multi_lora_interaction_intent"] = bool(interaction_intent)
        meta["multi_lora_region_masks"] = attached
        logger.info(
            f"[ComfyUI] Multi-LoRA regional conditioning enabled: "
            f"combine={combine_id} prompts={', '.join(meta['multi_lora_region_prompt_node_ids'])} masks={count}"
        )
        return True

    async def _apply_multi_lora_hooks(self, workflow: dict, meta: dict, entries: list, hook_nodes: list, char_prompt_node_ids: list, session: aiohttp.ClientSession = None) -> dict:
        cached_loras = self.get_lora_cache()
        applied = []
        skipped = []
        placeholder = self._pick_lora_placeholder(cached_loras)
        if not placeholder:
            for item in entries:
                candidate = str(item.get("lora_name") or "").strip()
                if candidate:
                    placeholder = candidate
                    break

        for _nid, hook_node in hook_nodes:
            inputs = hook_node.setdefault("inputs", {})
            if placeholder:
                inputs["lora_name"] = placeholder
            if "strength_model" in inputs:
                inputs["strength_model"] = 0
            if "strength_clip" in inputs:
                inputs["strength_clip"] = 0
        for prompt_nid in char_prompt_node_ids:
            prompt_node = workflow.get(str(prompt_nid))
            if isinstance(prompt_node, dict):
                prompt_node.setdefault("inputs", {})["text"] = ""

        set_clip_nodes = self._find_nodes_by_class_type(workflow, "SetClipHooks")
        base_clip_ref = self._find_base_clip_ref_for_hooks(workflow)
        max_slots = min(len(hook_nodes), len(char_prompt_node_ids), 3)
        if not base_clip_ref:
            meta["lora_warning"] = "Hooks multi-LoRA injection could not find a base CLIP reference"
            logger.warning(f"[ComfyUI] {meta['lora_warning']}")
            return meta
        global_text = str((((workflow.get("11") or {}).get("inputs") or {}).get("text")) or "")
        global_negative_text = str((((workflow.get("12") or {}).get("inputs") or {}).get("text")) or "")
        interaction_intent = (
            bool(any(item.get("interaction_intent") for item in entries[:max_slots]))
            or self._multi_lora_prompt_has_close_interaction(global_text)
        )
        position_names = ["left character", "right character"] if max_slots == 2 else ["left character", "center character", "right character"]
        position_anchors = ["on the left side", "on the right side"] if max_slots == 2 else ["on the left side", "in the center", "on the right side"]
        for item in entries:
            if len(applied) >= max_slots:
                skipped.append(f"{item.get('lora_name')}: no hook slot")
                continue
            name = str(item.get("lora_name") or "").strip()
            if not name:
                continue
            if cached_loras and name not in cached_loras:
                skipped.append(f"{name}: not in ComfyUI LoRA list")
                continue
            if not self._lora_allowed_by_runtime_family(name):
                skipped.append(f"{name}: incompatible model family")
                continue

            slot = len(applied)
            hook_nid, hook_node = hook_nodes[slot]
            hook_inputs = hook_node.setdefault("inputs", {})
            hook_inputs["lora_name"] = name
            hook_inputs["strength_model"] = item.get("strength_model", self.strength_model)
            hook_inputs["strength_clip"] = item.get("strength_clip", self.strength_clip)

            prompt_nid = str(char_prompt_node_ids[slot])
            prompt_node = workflow.get(prompt_nid)
            if not isinstance(prompt_node, dict):
                skipped.append(f"{name}: missing role prompt node")
                continue
            prompt_inputs = prompt_node.setdefault("inputs", {})
            prompt_clip_ref = prompt_inputs.get("clip")
            set_clip_nid = ""
            set_clip_node = None
            if isinstance(prompt_clip_ref, list) and len(prompt_clip_ref) == 2:
                candidate_id = str(prompt_clip_ref[0])
                candidate = workflow.get(candidate_id)
                if isinstance(candidate, dict) and str(candidate.get("class_type") or "") == "SetClipHooks":
                    set_clip_nid = candidate_id
                    set_clip_node = candidate
            if set_clip_node is None and slot < len(set_clip_nodes):
                set_clip_nid, set_clip_node = set_clip_nodes[slot]
            if set_clip_node is None:
                set_clip_nid = self._next_node_id(workflow)
                set_clip_node = {
                    "inputs": {},
                    "class_type": "SetClipHooks",
                    "_meta": {"title": f"AstrBot Runtime Role {slot + 1} CLIP Hook"},
                }
                workflow[set_clip_nid] = set_clip_node
            set_clip_inputs = set_clip_node.setdefault("inputs", {})
            set_clip_inputs["clip"] = copy.deepcopy(base_clip_ref)
            set_clip_inputs["hooks"] = [str(hook_nid), 0]
            set_clip_inputs["apply_to_conds"] = True
            set_clip_inputs["schedule_clip"] = False
            prompt_inputs["clip"] = [str(set_clip_nid), 0]

            if isinstance(prompt_node, dict):
                position = position_names[slot] if slot < len(position_names) else f"character {slot + 1}"
                anchor = position_anchors[slot] if slot < len(position_anchors) else position
                region_prompt = self._multi_region_entry_region_prompt(
                    item,
                    position=position,
                    anchor=anchor,
                    interaction_intent=interaction_intent,
                    count=max_slots,
                )
                prompt_inputs["text"] = region_prompt

            prompt_chain_hooks = self._clip_hook_chain_ids(workflow, prompt_inputs.get("clip"))
            if prompt_chain_hooks != [str(hook_nid)]:
                skipped.append(f"{name}: CLIP hook chain mismatch {prompt_chain_hooks or '-'}")
                continue

            applied.append({
                "lora_name": name,
                "hook_node": str(hook_nid),
                "clip_hook_node": str(set_clip_nid),
                "prompt_node": prompt_nid,
                "character": str(item.get("character") or "").strip(),
                "strength_model": _safe_float(item.get("strength_model"), self.strength_model),
                "strength_clip": _safe_float(item.get("strength_clip"), self.strength_clip),
            })

        if len(applied) < 2:
            reason = "Hooks multi-LoRA injection needs at least 2 available character LoRAs"
            if skipped:
                reason += "; " + "; ".join(skipped[:3])
            meta["lora_warning"] = reason
            logger.warning(f"[ComfyUI] {reason}")
            return meta

        pair_combine_nodes = self._find_nodes_by_class_type(workflow, "PairConditioningSetPropertiesAndCombine")
        combine_nodes = pair_combine_nodes or self._find_nodes_by_class_type(workflow, "ConditioningSetPropertiesAndCombine")
        active_combine_nodes = combine_nodes[:len(applied)]
        role_bindings = []
        if pair_combine_nodes and len(active_combine_nodes) >= len(applied):
            negative_prompt_node_ids = []
            for neg_nid in ("36", "37", "38"):
                neg_node = workflow.get(neg_nid)
                if isinstance(neg_node, dict) and str(neg_node.get("class_type") or "") == "CLIPTextEncode":
                    title = str((neg_node.get("_meta") or {}).get("title") or "").lower()
                    if "negative" in title:
                        negative_prompt_node_ids.append(str(neg_nid))
            seen_negative_ids = set(negative_prompt_node_ids)
            for neg_nid, neg_node in self._find_nodes_by_class_type(workflow, "CLIPTextEncode"):
                if neg_nid in seen_negative_ids:
                    continue
                title = str((neg_node.get("_meta") or {}).get("title") or "").lower()
                if "regional" in title and "negative" in title:
                    negative_prompt_node_ids.append(str(neg_nid))
                    seen_negative_ids.add(str(neg_nid))
            previous_positive = ["11", 0]
            previous_negative = ["12", 0]
            for slot, item in enumerate(applied):
                combine_nid, combine_node = active_combine_nodes[slot]
                if slot < len(negative_prompt_node_ids):
                    neg_id = str(negative_prompt_node_ids[slot])
                    neg_node = workflow.get(neg_id)
                else:
                    neg_id = self._next_node_id(workflow)
                    neg_node = {
                        "inputs": {},
                        "class_type": "CLIPTextEncode",
                        "_meta": {"title": f"AstrBot Regional Negative Prompt {slot + 1}"},
                    }
                    workflow[neg_id] = neg_node
                neg_inputs = neg_node.setdefault("inputs", {}) if isinstance(neg_node, dict) else {}
                neg_inputs["text"] = global_negative_text
                neg_inputs["clip"] = [item["clip_hook_node"], 0]
                negative_chain_hooks = self._clip_hook_chain_ids(workflow, [item["clip_hook_node"], 0])
                inputs = combine_node.setdefault("inputs", {})
                inputs["positive"] = copy.deepcopy(previous_positive)
                inputs["negative"] = copy.deepcopy(previous_negative)
                inputs["positive_NEW"] = [item["prompt_node"], 0]
                inputs["negative_NEW"] = [str(neg_id), 0]
                inputs["hooks"] = [item["hook_node"], 0]
                inputs["strength"] = 1.0
                inputs["set_cond_area"] = "default"
                previous_positive = [str(combine_nid), 0]
                previous_negative = [str(combine_nid), 1]
                item["negative_prompt_node"] = str(neg_id)
                item["combine_node"] = str(combine_nid)
                binding = {
                    "lora": item["lora_name"],
                    "hook_node": item["hook_node"],
                    "clip_hook_node": item["clip_hook_node"],
                    "prompt_node": item["prompt_node"],
                    "negative_prompt_node": str(neg_id),
                    "combine_node": str(combine_nid),
                    "prompt_chain_hooks": self._clip_hook_chain_ids(workflow, [item["clip_hook_node"], 0]),
                    "negative_chain_hooks": negative_chain_hooks,
                }
                role_bindings.append(binding)
            self._set_sampler_conditioning_refs(workflow, previous_positive, previous_negative)
        elif len(active_combine_nodes) >= len(applied):
            final_combine_id = active_combine_nodes[len(applied) - 1][0]
            self._set_sampler_positive_ref(workflow, [final_combine_id, 0])

        mask_warning = ""
        if len(active_combine_nodes) >= len(applied):
            width, height = self._get_workflow_latent_size(workflow)
            mask_infos = await self._upload_split_masks(
                session,
                width,
                height,
                len(applied),
                overlap_ratio=0.18 if interaction_intent else 0.08,
                blur_radius=16.0,
                binary=False,
                debug_label="regional_hooks",
            )
            if len(mask_infos) >= len(applied) and all(mask_infos[:len(applied)]):
                attached_masks = self._attach_multi_lora_split_masks(
                    workflow,
                    active_combine_nodes,
                    mask_infos[:len(applied)],
                )
                if len(attached_masks) == len(applied):
                    meta["multi_lora_masked"] = True
                    meta["multi_lora_mask_count"] = len(attached_masks)
                    meta["multi_lora_mask_nodes"] = attached_masks
                    validation = (mask_infos[0] or {}).get("validation") if isinstance(mask_infos[0], dict) else {}
                    if validation:
                        meta["multi_lora_mask_validation"] = validation
                    for idx, mask_item in enumerate(attached_masks):
                        if idx < len(role_bindings):
                            role_bindings[idx]["mask_node"] = str(mask_item.get("mask_node") or "")
                            role_bindings[idx]["mask_image"] = str(mask_item.get("image") or "")
                else:
                    mask_warning = "Multi-LoRA split masks were incomplete; regional Hook masking disabled"
            else:
                mask_warning = "Multi-LoRA split mask upload failed; regional Hook masking disabled"
        else:
            mask_warning = "Multi-LoRA workflow has too few combine nodes for split masks"

        if mask_warning:
            meta["multi_lora_regional_warning"] = mask_warning
            meta["multi_lora_hooks_failed"] = True
            meta["multi_lora_fallback_reason"] = mask_warning
            self._set_sampler_conditioning_refs(workflow, ["11", 0], ["12", 0])
            logger.warning(f"[ComfyUI] {mask_warning}; regional Hook conditioning not enabled")
            return meta

        names = [item["lora_name"] for item in applied]
        meta["multi_lora"] = True
        meta["multi_lora_hooks"] = True
        meta["multi_lora_mode_effective"] = "regional_lora"
        meta["multi_lora_count"] = len(applied)
        meta["lora_name"] = "、".join(names)
        meta["lora_names"] = names
        meta["lora_node_ids"] = [item["hook_node"] for item in applied]
        meta["lora_hook_prompt_node_ids"] = [item["prompt_node"] for item in applied]
        meta["lora_hook_negative_prompt_node_ids"] = [item.get("negative_prompt_node", "") for item in applied]
        meta["multi_lora_role_bindings"] = role_bindings
        meta["strength_model"] = ", ".join(f"{item['strength_model']:g}" for item in applied)
        meta["strength_clip"] = ", ".join(f"{item['strength_clip']:g}" for item in applied)
        if skipped:
            meta["lora_warning"] = "Skipped part of multi-LoRA hooks: " + "; ".join(skipped[:3])
        if mask_warning:
            meta["multi_lora_mask_warning"] = mask_warning
            logger.warning(f"[ComfyUI] {mask_warning}")
        logger.info(
            f"[ComfyUI] Multi-LoRA Hooks injected: {'、'.join(names)} | "
            f"hooks={', '.join(item['hook_node'] for item in applied)} | "
            f"prompts={', '.join(item['prompt_node'] for item in applied)} | "
            f"masked={bool(meta.get('multi_lora_masked'))}"
        )
        for binding in role_bindings:
            logger.info(
                "[ComfyUI] Multi-LoRA role binding | "
                f"lora={binding.get('lora')} hook={binding.get('hook_node')} "
                f"clip_hook={binding.get('clip_hook_node')} pos={binding.get('prompt_node')} "
                f"neg={binding.get('negative_prompt_node')} combine={binding.get('combine_node')} "
                f"mask={binding.get('mask_node') or '-'}"
            )
        return meta

    def _zero_multi_lora_hook_nodes(self, hook_nodes: list):
        for _hook_nid, hook_node in (hook_nodes or []):
            if not isinstance(hook_node, dict):
                continue
            inputs = hook_node.setdefault("inputs", {})
            if "strength_model" in inputs:
                inputs["strength_model"] = 0
            if "strength_clip" in inputs:
                inputs["strength_clip"] = 0

    def _restore_safe_chain_role_prompts(self, workflow: dict, meta: dict, entries: list) -> int:
        role_prompts = []
        seen = set()
        for item in entries or []:
            prompt = str(
                item.get("char_prompt")
                or item.get("character")
                or item.get("lora_name")
                or ""
            ).strip()
            marker = re.sub(r"\s+", " ", prompt.lower())
            if prompt and marker not in seen:
                seen.add(marker)
                role_prompts.append(prompt)
        if not role_prompts:
            return 0

        target_node_ids = []
        for node_id in (self.input_id, "11"):
            node_id = str(node_id or "").strip()
            if node_id and node_id not in target_node_ids:
                target_node_ids.append(node_id)
        target_keys = [
            "text", "opt_text", "string",
            "text_positive", "positive", "prompt", "wildcard_text",
        ]

        changed = 0
        for node_id in target_node_ids:
            node = workflow.get(node_id) if isinstance(workflow, dict) else None
            if not isinstance(node, dict):
                continue
            inputs = node.setdefault("inputs", {})
            if not isinstance(inputs, dict):
                continue
            key = next((candidate for candidate in target_keys if candidate in inputs), "text")
            before = str(inputs.get(key) or "").strip()
            after = before
            for role_prompt in role_prompts:
                after = self._append_prompt_text(after, role_prompt)
            if after != before:
                inputs[key] = after
                changed += 1
                if str(node_id) == str(self.input_id or ""):
                    meta["prompt"] = after

        if changed:
            meta["multi_lora_safe_chain_role_prompts_restored"] = True
            meta["multi_lora_safe_chain_role_prompts"] = role_prompts
            meta["multi_lora_safe_chain_prompt_nodes"] = target_node_ids
            logger.info(
                "[ComfyUI] Multi-LoRA Safe Chain role prompts restored into global prompt: "
                + " | ".join(role_prompts[:3])
            )
        return changed

    def _workflow_value_references_any_node(self, value, node_ids: set) -> bool:
        refs = {str(x) for x in (node_ids or []) if str(x)}
        if not refs:
            return False
        if isinstance(value, list):
            if len(value) == 2 and str(value[0]) in refs:
                return True
            return any(self._workflow_value_references_any_node(item, refs) for item in value)
        if isinstance(value, dict):
            return any(self._workflow_value_references_any_node(item, refs) for item in value.values())
        return False

    def _prune_multi_lora_legacy_regional_nodes(self, workflow: dict, meta: dict = None, reason: str = "") -> list:
        if not isinstance(workflow, dict):
            return []
        legacy_types = {"ConditioningSetMaskAndCombine", "ConditioningSetMaskAndCombine3"}
        remove_ids = {
            str(nid)
            for nid, node_data in workflow.items()
            if isinstance(node_data, dict) and str(node_data.get("class_type") or "") in legacy_types
        }
        changed = True
        while changed:
            changed = False
            for nid, node_data in list(workflow.items()):
                nid = str(nid)
                if nid in remove_ids or not isinstance(node_data, dict):
                    continue
                class_type = str(node_data.get("class_type") or "")
                title = str((node_data.get("_meta") or {}).get("title") or "").lower()
                if not self._workflow_value_references_any_node(node_data.get("inputs", {}), remove_ids):
                    continue
                if class_type == "ConditioningCombine" or "regional conditioning" in title:
                    remove_ids.add(nid)
                    changed = True

        removed = []
        for nid in sorted(remove_ids, key=lambda x: int(x) if str(x).isdigit() else 999999):
            if workflow.pop(str(nid), None) is not None:
                removed.append(str(nid))
        if removed:
            if isinstance(meta, dict):
                meta["multi_lora_pruned_legacy_regional_nodes"] = removed
            logger.info(
                f"[ComfyUI] Pruned unused legacy regional conditioning nodes for {reason or 'multi_lora'}: "
                + ", ".join(removed)
            )
        return removed

    async def _apply_multi_lora_densediffusion(
        self,
        workflow: dict,
        meta: dict,
        entries: list,
        lora_nodes: list,
        target_node_ids: list,
        hook_nodes: list = None,
        session: aiohttp.ClientSession = None,
    ) -> dict:
        """DenseDiffusion multi-LoRA: global LoRA chain plus spatial attention conditioning."""
        object_info = await self._fetch_object_info(session) if session else {}
        required_dd_nodes = {"DenseDiffusionAddCondNode", "DenseDiffusionApplyNode"}
        missing_dd_nodes = sorted(required_dd_nodes - set(str(key) for key in object_info.keys())) if object_info else []
        if missing_dd_nodes:
            meta["multi_lora_dd_unavailable"] = True
            meta["multi_lora_dd_missing_nodes"] = missing_dd_nodes
            raise RuntimeError(
                "DenseDiffusion 多人 LoRA 需要安装 ComfyUI_densediffusion："
                f"缺少节点 {', '.join(missing_dd_nodes)}。"
                "请安装/启用该 ComfyUI 自定义节点并重启 ComfyUI；"
                "插件不会自动回退到 Safe Chain，只有显式选择 safe_chain 才会使用 Safe Chain。"
            )

        self._zero_multi_lora_hook_nodes(hook_nodes or [])

        cached_loras = self.get_lora_cache()
        valid_entries = []
        skipped = []
        seen = set()
        for item in entries or []:
            name = str(item.get("lora_name") or "").strip()
            char_prompt = self._multi_region_entry_identity_prompt(item)
            key = name.lower() if name else re.sub(r"\s+", " ", char_prompt.lower())
            if not key or key in seen:
                continue
            seen.add(key)
            if len(valid_entries) >= 3:
                skipped.append(f"{name or char_prompt[:40]}: no DenseDiffusion role slot")
                continue
            runtime_item = dict(item)
            if name:
                if cached_loras and name not in cached_loras:
                    skipped.append(f"{name}: not in ComfyUI LoRA list; using prompt-only region")
                    runtime_item["lora_name"] = ""
                    runtime_item["prompt_only"] = True
                elif not self._lora_allowed_by_runtime_family(name):
                    skipped.append(f"{name}: incompatible model family; using prompt-only region")
                    runtime_item["lora_name"] = ""
                    runtime_item["prompt_only"] = True
            else:
                runtime_item["prompt_only"] = True
            if not self._multi_region_entry_identity_prompt(runtime_item):
                continue
            valid_entries.append(runtime_item)

        if len(valid_entries) < 2:
            reason = "DenseDiffusion needs >= 2 character regions"
            if skipped:
                reason += "; " + "; ".join(skipped[:3])
            meta["lora_warning"] = reason
            raise RuntimeError(
                f"{reason}。DenseDiffusion 模式不会自动回退到 Safe Chain；"
                "只有显式选择 safe_chain 才会使用 Safe Chain。"
            )

        lora_loader_nodes = [
            (str(nid), node)
            for nid, node in (lora_nodes or [])
            if isinstance(node, dict) and str(node.get("class_type") or "") == "LoraLoader"
        ]

        first_source_model_ref = None
        first_source_clip_ref = None
        inserted_ids = set()
        lora_entries = [item for item in valid_entries if str(item.get("lora_name") or "").strip()]
        if not lora_entries:
            if lora_loader_nodes:
                first_id, first_node = lora_loader_nodes[0]
                first_inputs = first_node.setdefault("inputs", {})
                if "strength_model" in first_inputs:
                    first_inputs["strength_model"] = 0
                if "strength_clip" in first_inputs:
                    first_inputs["strength_clip"] = 0
                chain_ids = []
                current_model_ref = [str(first_id), 0]
                current_clip_ref = [str(first_id), 1]
            else:
                model_ref, clip_ref = self._find_primary_generation_refs(workflow)
                if not model_ref or not clip_ref:
                    meta["lora_warning"] = "DenseDiffusion could not find model/clip references"
                    raise RuntimeError(
                        f"{meta['lora_warning']}。DenseDiffusion 模式不会自动回退到 Safe Chain；"
                        "只有显式选择 safe_chain 才会使用 Safe Chain。"
                    )
                chain_ids = []
                current_model_ref = copy.deepcopy(model_ref)
                current_clip_ref = copy.deepcopy(clip_ref)
        elif not lora_loader_nodes:
            model_ref, clip_ref = self._find_primary_generation_refs(workflow)
            if not model_ref or not clip_ref:
                meta["lora_warning"] = "DenseDiffusion could not find model/clip references"
                raise RuntimeError(
                    f"{meta['lora_warning']}。DenseDiffusion 模式不会自动回退到 Safe Chain；"
                    "只有显式选择 safe_chain 才会使用 Safe Chain。"
                )
            first_source_model_ref = copy.deepcopy(model_ref)
            first_source_clip_ref = copy.deepcopy(clip_ref)
            first_id = self._next_node_id(workflow)
            workflow[first_id] = {
                "inputs": {
                    "model": copy.deepcopy(first_source_model_ref),
                    "clip": copy.deepcopy(first_source_clip_ref),
                    "lora_name": lora_entries[0]["lora_name"],
                    "strength_model": lora_entries[0]["strength_model"],
                    "strength_clip": lora_entries[0]["strength_clip"],
                },
                "class_type": "LoraLoader",
                "_meta": {"title": "AstrBot DenseDiffusion LoRA 1"},
            }
            inserted_ids.add(str(first_id))
            chain_ids = [str(first_id)]
            current_model_ref = [str(first_id), 0]
            current_clip_ref = [str(first_id), 1]
        else:
            first_id, first_node = lora_loader_nodes[0]
            self._set_lora_node_inputs(
                first_node,
                lora_entries[0]["lora_name"],
                lora_entries[0]["strength_model"],
                lora_entries[0]["strength_clip"],
            )
            chain_ids = [str(first_id)]
            current_model_ref = [str(first_id), 0]
            current_clip_ref = [str(first_id), 1]

        for index, item in enumerate(lora_entries[1:], start=2):
            node_id = self._next_node_id(workflow)
            workflow[node_id] = {
                "inputs": {
                    "model": copy.deepcopy(current_model_ref),
                    "clip": copy.deepcopy(current_clip_ref),
                    "lora_name": item["lora_name"],
                    "strength_model": item["strength_model"],
                    "strength_clip": item["strength_clip"],
                },
                "class_type": "LoraLoader",
                "_meta": {"title": f"AstrBot DenseDiffusion LoRA {index}"},
            }
            inserted_ids.add(str(node_id))
            chain_ids.append(str(node_id))
            current_model_ref = [str(node_id), 0]
            current_clip_ref = [str(node_id), 1]

        replacements = {}
        if first_source_model_ref:
            replacements.update({
                (str(first_source_model_ref[0]), str(first_source_model_ref[1])): current_model_ref,
                (str(first_source_model_ref[0]), first_source_model_ref[1]): current_model_ref,
                (str(first_source_clip_ref[0]), str(first_source_clip_ref[1])): current_clip_ref,
                (str(first_source_clip_ref[0]), first_source_clip_ref[1]): current_clip_ref,
            })
        elif len(chain_ids) > 1:
            replacements.update({
                (str(chain_ids[0]), "0"): current_model_ref,
                (str(chain_ids[0]), "1"): current_clip_ref,
                (str(chain_ids[0]), 0): current_model_ref,
                (str(chain_ids[0]), 1): current_clip_ref,
            })
        if replacements:
            self._replace_workflow_references(
                workflow,
                replacements,
                skip_node_ids={str(chain_ids[0]), *inserted_ids},
            )
        lora_node_by_name = {
            str(item.get("lora_name") or ""): str(chain_ids[idx])
            for idx, item in enumerate(lora_entries[:len(chain_ids)])
            if str(item.get("lora_name") or "")
        }

        char_prompt_node_ids = self._multi_lora_regional_prompt_node_ids(workflow)
        global_prompt_id = str(self.input_id or "11")
        if global_prompt_id not in workflow and "11" in workflow:
            global_prompt_id = "11"
        global_text = str((((workflow.get(global_prompt_id) or {}).get("inputs") or {}).get("text")) or "")
        interaction_prompts = []
        for item in valid_entries:
            prompt = str(item.get("interaction_prompt") or item.get("global_interaction_prompt") or "").strip()
            if prompt and prompt not in interaction_prompts:
                interaction_prompts.append(prompt)
        if interaction_prompts:
            global_node = workflow.get(global_prompt_id)
            if isinstance(global_node, dict):
                inputs = global_node.setdefault("inputs", {})
                for key in ("text", "string", "prompt", "positive", "text_positive"):
                    if key in inputs:
                        updated = str(inputs.get(key) or "")
                        for prompt in interaction_prompts:
                            updated = self._append_prompt_text(updated, prompt)
                        inputs[key] = updated
                        global_text = updated
                        break
        interaction_intent = (
            bool(any(item.get("interaction_intent") for item in valid_entries))
            or self._multi_lora_prompt_has_close_interaction(global_text)
        )
        count = len(valid_entries)
        position_names = (
            ["left character", "right character"]
            if count == 2
            else ["left character", "center character", "right character"]
        )
        position_anchors = (
            ["on the left side", "on the right side"]
            if count == 2
            else ["on the left side", "in the center", "on the right side"]
        )
        supports_conditioning_concat = not object_info or "ConditioningConcat" in object_info

        while len(char_prompt_node_ids) < count:
            prompt_id = self._next_node_id(workflow)
            workflow[prompt_id] = {
                "inputs": {"text": "", "clip": copy.deepcopy(current_clip_ref)},
                "class_type": "CLIPTextEncode",
                "_meta": {"title": f"AstrBot DD Role Prompt {len(char_prompt_node_ids) + 1}"},
            }
            char_prompt_node_ids.append(str(prompt_id))

        role_conditioning_refs = []
        role_prompt_bindings = []
        for idx, item in enumerate(valid_entries[:min(len(char_prompt_node_ids), 3)]):
            prompt_nid = str(char_prompt_node_ids[idx])
            prompt_node = workflow.get(prompt_nid)
            if not isinstance(prompt_node, dict):
                continue
            position = position_names[idx] if idx < len(position_names) else f"character {idx + 1}"
            anchor = position_anchors[idx] if idx < len(position_anchors) else position
            char_prompt = self._multi_region_entry_identity_prompt(item)
            role_identity_prompt = char_prompt
            role_aux_prompt = self._multi_region_entry_aux_prompt(
                item,
                position=position,
                anchor=anchor,
                interaction_intent=interaction_intent,
                count=count,
            )
            prompt_inputs = prompt_node.setdefault("inputs", {})
            prompt_inputs["text"] = role_identity_prompt
            prompt_inputs["clip"] = copy.deepcopy(current_clip_ref)
            conditioning_ref = [prompt_nid, 0]
            aux_prompt_nid = ""
            concat_nid = ""
            if supports_conditioning_concat:
                aux_prompt_id = self._next_node_id(workflow)
                workflow[aux_prompt_id] = {
                    "inputs": {
                        "text": role_aux_prompt,
                        "clip": copy.deepcopy(current_clip_ref),
                    },
                    "class_type": "CLIPTextEncode",
                    "_meta": {"title": f"AstrBot DD Role Aux Prompt {idx + 1}"},
                }
                concat_id = self._next_node_id(workflow)
                workflow[concat_id] = {
                    "inputs": {
                        "conditioning_to": [prompt_nid, 0],
                        "conditioning_from": [str(aux_prompt_id), 0],
                    },
                    "class_type": "ConditioningConcat",
                    "_meta": {"title": f"AstrBot DD Role Split Conditioning {idx + 1}"},
                }
                aux_prompt_nid = str(aux_prompt_id)
                concat_nid = str(concat_id)
                conditioning_ref = [concat_nid, 0]
            role_conditioning_refs.append(conditioning_ref)
            role_prompt_bindings.append({
                "identity_prompt_node": prompt_nid,
                "aux_prompt_node": aux_prompt_nid,
                "concat_node": concat_nid,
                "conditioning_ref": conditioning_ref,
                "identity_prompt": role_identity_prompt,
                "aux_prompt": role_aux_prompt,
            })

        for prompt_nid in char_prompt_node_ids[count:]:
            prompt_node = workflow.get(str(prompt_nid))
            if isinstance(prompt_node, dict):
                prompt_node.setdefault("inputs", {})["text"] = ""

        dd_mask_overlap_ratio = 0.0
        global_dd_strength = 1.0

        width, height = self._get_workflow_latent_size(workflow)
        mask_infos = await self._upload_split_masks(
            session,
            width,
            height,
            count,
            overlap_ratio=dd_mask_overlap_ratio,
            blur_radius=0.0,
            binary=True,
            debug_label="densediffusion",
        )
        if len(mask_infos) < count or not all(mask_infos[:count]):
            meta["multi_lora_dd_mask_failed"] = True
            raise RuntimeError(
                "DenseDiffusion split mask 上传失败。DenseDiffusion 模式不会自动回退到 Safe Chain；"
                "只有显式选择 safe_chain 才会使用 Safe Chain。"
            )

        def _add_dd_mask_nodes(mask_info: dict, label: str):
            load_id = self._next_node_id(workflow)
            workflow[load_id] = {
                "inputs": {"image": mask_info["image_value"]},
                "class_type": "LoadImage",
                "_meta": {"title": f"AstrBot DD Mask {label}"},
            }
            mask_id = self._next_node_id(workflow)
            workflow[mask_id] = {
                "inputs": {"image": [load_id, 0], "channel": "red"},
                "class_type": "ImageToMask",
                "_meta": {"title": f"AstrBot DD Mask Convert {label}"},
            }
            return str(load_id), str(mask_id)

        full_mask_id = self._next_node_id(workflow)
        workflow[full_mask_id] = {
            "inputs": {"value": 1.0, "width": width, "height": height},
            "class_type": "SolidMask",
            "_meta": {"title": "AstrBot DD Full Mask"},
        }

        global_dd_id = self._next_node_id(workflow)
        workflow[global_dd_id] = {
            "inputs": {
                "model": copy.deepcopy(current_model_ref),
                "conditioning": [str(global_prompt_id), 0],
                "mask": [str(full_mask_id), 0],
                "strength": global_dd_strength,
            },
            "class_type": "DenseDiffusionAddCondNode",
            "_meta": {"title": "AstrBot DD Global Prompt"},
            "widgets_values": [1],
        }

        prev_dd_id = str(global_dd_id)
        dd_chain_ids = [str(global_dd_id)]
        dd_role_bindings = []
        for idx in range(count):
            load_id, mask_id = _add_dd_mask_nodes(mask_infos[idx], f"Role {idx + 1}")
            char_prompt_nid = (
                str(char_prompt_node_ids[idx])
                if idx < len(char_prompt_node_ids)
                else str(self.input_id or "11")
            )
            conditioning_ref = (
                copy.deepcopy(role_conditioning_refs[idx])
                if idx < len(role_conditioning_refs)
                else [char_prompt_nid, 0]
            )
            dd_id = self._next_node_id(workflow)
            workflow[dd_id] = {
                "inputs": {
                    "model": [prev_dd_id, 0],
                    "conditioning": conditioning_ref,
                    "mask": [mask_id, 0],
                    "strength": 1.0,
                },
                "class_type": "DenseDiffusionAddCondNode",
                "_meta": {"title": f"AstrBot DD Role {idx + 1}"},
                "widgets_values": [1],
            }
            prev_dd_id = str(dd_id)
            dd_chain_ids.append(str(dd_id))
            role_lora_name = str(valid_entries[idx].get("lora_name") or "")
            dd_role_bindings.append({
                "lora": role_lora_name,
                "lora_node": lora_node_by_name.get(role_lora_name, ""),
                "prompt_only": not bool(str(valid_entries[idx].get("lora_name") or "").strip()),
                "character": str(valid_entries[idx].get("character") or ""),
                "prompt_node": char_prompt_nid,
                "identity_prompt_node": (
                    role_prompt_bindings[idx].get("identity_prompt_node", "")
                    if idx < len(role_prompt_bindings)
                    else char_prompt_nid
                ),
                "identity_prompt": (
                    role_prompt_bindings[idx].get("identity_prompt", "")
                    if idx < len(role_prompt_bindings)
                    else self._multi_region_entry_identity_prompt(valid_entries[idx])
                ),
                "aux_prompt_node": (
                    role_prompt_bindings[idx].get("aux_prompt_node", "")
                    if idx < len(role_prompt_bindings)
                    else ""
                ),
                "aux_prompt": (
                    role_prompt_bindings[idx].get("aux_prompt", "")
                    if idx < len(role_prompt_bindings)
                    else self._multi_region_entry_aux_prompt(valid_entries[idx], interaction_intent=interaction_intent, count=count)
                ),
                "concat_node": (
                    role_prompt_bindings[idx].get("concat_node", "")
                    if idx < len(role_prompt_bindings)
                    else ""
                ),
                "conditioning_ref": conditioning_ref,
                "mask_load_node": load_id,
                "mask_node": mask_id,
                "dd_node": str(dd_id),
                "mask_image": str(mask_infos[idx].get("image_value") or ""),
            })

        apply_id = self._next_node_id(workflow)
        workflow[apply_id] = {
            "inputs": {"model": [prev_dd_id, 0]},
            "class_type": "DenseDiffusionApplyNode",
            "_meta": {"title": "AstrBot DD Apply"},
        }

        dd_anti_extra_negative = (
            "3girls, 4girls, extra person, third character, background character, "
            "partial extra body, crowd, group, solo, single character, only one character"
            if count == 2
            else "4girls, 5girls, extra person, extra character, background character, "
            "partial extra body, crowd, group, solo, single character, only one character"
        )
        dd_anti_sheet_negative = (
            "character sheet, reference sheet, multiple views, model sheet, "
            "turnaround sheet, color palette, color swatches, text labels, "
            "split panels, side by side comparison, design document, "
            "front and back view, orthographic view"
        )
        dd_compact_negative = (
            "worst quality, low quality, bad anatomy, bad hands, extra limbs, "
            "extra fingers, missing fingers, watermark, text, signature, "
            "character sheet, reference sheet, multiple views, model sheet, "
            "turnaround sheet, color swatches, cropped head, head out of frame, "
            "face out of frame, body part foreground, lower body close-up, "
            "legs-only composition, giant thigh, oversized leg, disembodied limb, "
            "floating body parts, detached arm, detached leg, fused bodies, "
            "merged torso, shared torso, fused clothing, jagged skin edge, "
            "skin edge artifacts, aliasing, jagged outline, skin fringe, "
            "noisy skin outline, broken skin outline, rough skin border, "
            "rough lineart on skin, stray lines on skin, outline artifacts, "
            "over-sharpened outline, edge halos, color fringing, chromatic aberration, shiny skin"
        )
        dd_compact_negative = self._append_prompt_text(dd_compact_negative, dd_anti_extra_negative)
        dd_compact_negative = self._append_prompt_text(dd_compact_negative, dd_anti_sheet_negative)
        dd_contamination_negative = ""
        for item in valid_entries:
            guard_negative = str(item.get("contamination_negative_prompt") or "").strip()
            if guard_negative:
                dd_contamination_negative = self._append_prompt_text(dd_contamination_negative, guard_negative)
        dd_contamination_negative = self._dedupe_prompt_text(dd_contamination_negative)
        if dd_contamination_negative:
            dd_compact_negative = self._append_prompt_text(dd_compact_negative, dd_contamination_negative)
        dd_negative_id = self._next_node_id(workflow)
        workflow[dd_negative_id] = {
            "inputs": {
                "text": dd_compact_negative,
                "clip": copy.deepcopy(current_clip_ref),
            },
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "AstrBot DD Compact Negative"},
        }
        dd_sampler_negative_ref = self._repeat_conditioning_with_concat(
            workflow,
            [str(dd_negative_id), 0],
            count if supports_conditioning_concat else 1,
            "AstrBot DD Repeated Negative",
        )

        sampler_count = 0
        dd_sampler_ids = self._densediffusion_base_sampler_node_ids(workflow)
        for node_id, node_data in self._sampler_node_items(workflow):
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            if str(node_id) not in dd_sampler_ids:
                continue
            if "model" in inputs:
                inputs["model"] = [str(apply_id), 0]
            if "positive" in inputs:
                inputs["positive"] = [str(apply_id), 1]
            if "negative" in inputs:
                inputs["negative"] = copy.deepcopy(dd_sampler_negative_ref)
            sampler_count += 1

        dd_extra_updated = False
        neg_updated = False
        neg_node_ids = []
        for candidate in (dd_negative_id, self.neg_node_id, "12"):
            candidate = str(candidate or "").strip()
            if candidate and candidate not in neg_node_ids:
                neg_node_ids.append(candidate)
        for neg_node_id in neg_node_ids:
            neg_node = workflow.get(neg_node_id)
            if not isinstance(neg_node, dict):
                continue
            neg_inputs = neg_node.setdefault("inputs", {})
            for key in ("text", "string", "negative", "text_negative", "prompt"):
                if key in neg_inputs:
                    existing = str(neg_inputs.get(key) or "")
                    if "third character" not in existing.lower() and "extra character" not in existing.lower():
                        existing = self._append_prompt_text(existing, dd_anti_extra_negative)
                        dd_extra_updated = True
                    if "side by side comparison" not in existing.lower():
                        existing = self._append_prompt_text(existing, dd_anti_sheet_negative)
                    neg_inputs[key] = existing
                    neg_updated = True
                    break
        if not neg_updated:
            for _neg_nid, neg_node in self._find_nodes_by_class_type(workflow, "CLIPTextEncode"):
                title = str((neg_node.get("_meta") or {}).get("title") or "").lower()
                if "negative" not in title:
                    continue
                neg_inputs = neg_node.setdefault("inputs", {})
                if "text" in neg_inputs:
                    existing = str(neg_inputs.get("text") or "")
                    if "third character" not in existing.lower() and "extra character" not in existing.lower():
                        existing = self._append_prompt_text(existing, dd_anti_extra_negative)
                        dd_extra_updated = True
                    if "side by side comparison" not in existing.lower():
                        existing = self._append_prompt_text(existing, dd_anti_sheet_negative)
                    neg_inputs["text"] = existing
                    neg_updated = True
                    break

        self._prune_multi_lora_legacy_regional_nodes(workflow, meta, reason="densediffusion")

        names = [
            str(item.get("lora_name") or item.get("character") or f"prompt-only role {idx + 1}").strip()
            for idx, item in enumerate(valid_entries)
        ]
        lora_names = [
            str(item.get("lora_name") or "").strip()
            for item in valid_entries
            if str(item.get("lora_name") or "").strip()
        ]
        meta["multi_lora"] = True
        meta["multi_lora_densediffusion"] = True
        meta["multi_lora_hooks"] = False
        meta["multi_lora_masked"] = True
        meta["multi_lora_mode_effective"] = "densediffusion"
        meta["multi_lora_count"] = count
        meta["lora_name"] = " + ".join(lora_names) if lora_names else "prompt-only regional characters"
        meta["lora_names"] = lora_names
        meta["multi_region_role_names"] = names
        meta["multi_region_prompt_only_count"] = sum(
            1 for item in valid_entries if not str(item.get("lora_name") or "").strip()
        )
        meta["lora_node_ids"] = chain_ids
        meta["multi_lora_dd_global_prompt_node"] = str(global_prompt_id)
        meta["multi_lora_dd_global_prompt"] = global_text
        meta["multi_lora_dd_global_strength"] = global_dd_strength
        meta["multi_lora_dd_mask_overlap_ratio"] = dd_mask_overlap_ratio
        meta["multi_lora_dd_chain"] = dd_chain_ids + [str(apply_id)]
        meta["multi_lora_dd_apply_node"] = str(apply_id)
        meta["multi_lora_dd_negative_node"] = str(dd_negative_id)
        meta["multi_lora_dd_negative_ref"] = copy.deepcopy(dd_sampler_negative_ref)
        meta["multi_lora_dd_sampler_ids"] = sorted(dd_sampler_ids, key=lambda item: int(item) if str(item).isdigit() else 999999)
        if dd_contamination_negative:
            meta["multi_lora_dd_contamination_negative"] = dd_contamination_negative
        meta["multi_lora_dd_sampler_count"] = sampler_count
        meta["multi_lora_dd_role_bindings"] = dd_role_bindings
        meta["multi_lora_dd_anti_sheet_negative"] = bool(neg_updated)
        meta["multi_lora_dd_anti_extra_negative"] = bool(dd_extra_updated or dd_anti_extra_negative in dd_compact_negative)
        meta["multi_lora_dd_role_prompt_mode"] = (
            "split_conditioning_concat"
            if supports_conditioning_concat and role_prompt_bindings
            else "identity_only"
        )
        meta["multi_lora_interaction_intent"] = bool(interaction_intent)
        meta["multi_lora_regional_conditioning"] = False
        meta["strength_model"] = ", ".join(f"{_safe_float(item.get('strength_model'), self.strength_model):g}" for item in valid_entries)
        meta["strength_clip"] = ", ".join(f"{_safe_float(item.get('strength_clip'), self.strength_clip):g}" for item in valid_entries)
        if skipped:
            meta["lora_warning"] = "Skipped part of DenseDiffusion multi-LoRA: " + "; ".join(skipped[:3])

        logger.info(
            f"[ComfyUI] Multi-LoRA DenseDiffusion injected: {' + '.join(names)} | "
            f"lora_chain={', '.join(chain_ids)} | dd_chain={', '.join(dd_chain_ids)} | "
            f"apply={apply_id} | samplers={sampler_count}"
        )
        for binding in dd_role_bindings:
            logger.info(
                "[ComfyUI] Multi-LoRA DenseDiffusion binding | "
                f"lora={binding.get('lora')} lora_node={binding.get('lora_node')} "
                f"identity_prompt={binding.get('identity_prompt_node')} "
                f"aux_prompt={binding.get('aux_prompt_node') or '-'} "
                f"concat={binding.get('concat_node') or '-'} dd={binding.get('dd_node')} "
                f"mask={binding.get('mask_node')}"
            )
        self._save_multi_lora_debug_workflow(workflow, meta, label="densediffusion")
        return meta

    async def _apply_multi_lora_safe_chain_override(
        self,
        workflow: dict,
        meta: dict,
        entries: list,
        lora_nodes: list,
        target_node_ids: list,
        hook_nodes: list = None,
    ):
        self._zero_multi_lora_hook_nodes(hook_nodes or [])
        lora_nodes = [
            (str(nid), node)
            for nid, node in (lora_nodes or [])
            if isinstance(node, dict) and str(node.get("class_type") or "") == "LoraLoader"
        ]
        cached_loras = self.get_lora_cache()
        valid_entries = []
        skipped = []
        seen = set()
        for item in entries:
            name = str(item.get("lora_name") or "").strip()
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            if cached_loras and name not in cached_loras:
                skipped.append(f"{name}: not in ComfyUI LoRA list")
                continue
            if not self._lora_allowed_by_runtime_family(name):
                skipped.append(f"{name}: incompatible model family")
                continue
            valid_entries.append(item)

        if not valid_entries:
            reason = "No available Multi-LoRA entries; skipped injection"
            if skipped:
                reason += ": " + "; ".join(skipped[:3])
            if lora_nodes:
                return self._neutralize_lora_nodes(
                    workflow,
                    meta,
                    reason,
                    valid_loras=cached_loras,
                    target_node_ids=target_node_ids,
                )
            meta["lora_warning"] = reason
            logger.warning(f"[ComfyUI] {reason}")
            return meta

        inserted_first = False
        first_source_model_ref = None
        first_source_clip_ref = None
        if not lora_nodes:
            model_ref, clip_ref = self._find_primary_generation_refs(workflow)
            if not model_ref or not clip_ref:
                meta["lora_warning"] = "Multi-LoRA Safe Chain could not find model/clip references"
                logger.warning(f"[ComfyUI] {meta['lora_warning']}")
                return meta
            first_source_model_ref = copy.deepcopy(model_ref)
            first_source_clip_ref = copy.deepcopy(clip_ref)
            first_id = self._next_node_id(workflow)
            workflow[first_id] = {
                "inputs": {
                    "model": copy.deepcopy(first_source_model_ref),
                    "clip": copy.deepcopy(first_source_clip_ref),
                    "lora_name": valid_entries[0]["lora_name"],
                    "strength_model": valid_entries[0]["strength_model"],
                    "strength_clip": valid_entries[0]["strength_clip"],
                },
                "class_type": "LoraLoader",
                "_meta": {"title": "AstrBot runtime character LoRA 1"},
            }
            lora_nodes = [(str(first_id), workflow[first_id])]
            inserted_first = True

        if not lora_nodes:
            meta["lora_warning"] = "Multi-LoRA workflow has no LoRA node and could not insert one"
            logger.warning(f"[ComfyUI] {meta['lora_warning']}")
            return meta

        first_id, first_node = lora_nodes[0]
        self._set_lora_node_inputs(
            first_node,
            valid_entries[0]["lora_name"],
            valid_entries[0]["strength_model"],
            valid_entries[0]["strength_clip"],
        )
        chain_ids = [str(first_id)]
        current_model_ref = [str(first_id), 0]
        current_clip_ref = [str(first_id), 1]
        inserted_ids = {str(first_id)} if inserted_first else set()
        for index, item in enumerate(valid_entries[1:], start=2):
            node_id = self._next_node_id(workflow)
            workflow[node_id] = {
                "inputs": {
                    "model": copy.deepcopy(current_model_ref),
                    "clip": copy.deepcopy(current_clip_ref),
                    "lora_name": item["lora_name"],
                    "strength_model": item["strength_model"],
                    "strength_clip": item["strength_clip"],
                },
                "class_type": "LoraLoader",
                "_meta": {"title": f"AstrBot runtime character LoRA {index}"},
            }
            inserted_ids.add(str(node_id))
            chain_ids.append(str(node_id))
            current_model_ref = [str(node_id), 0]
            current_clip_ref = [str(node_id), 1]

        replacements = {}
        if inserted_first:
            replacements.update({
                (str(first_source_model_ref[0]), str(first_source_model_ref[1])): current_model_ref,
                (str(first_source_model_ref[0]), first_source_model_ref[1]): current_model_ref,
                (str(first_source_clip_ref[0]), str(first_source_clip_ref[1])): current_clip_ref,
                (str(first_source_clip_ref[0]), first_source_clip_ref[1]): current_clip_ref,
            })
        elif len(chain_ids) > 1:
            replacements.update({
                (str(first_id), "0"): current_model_ref,
                (str(first_id), "1"): current_clip_ref,
                (str(first_id), 0): current_model_ref,
                (str(first_id), 1): current_clip_ref,
            })
        if replacements:
            self._replace_workflow_references(
                workflow,
                replacements,
                skip_node_ids={str(first_id), *inserted_ids},
            )

        meta["multi_lora_regional_conditioning"] = False
        meta["multi_lora_regional_warning"] = "Using explicit global Safe Chain"
        self._set_sampler_conditioning_refs(workflow, ["11", 0], ["12", 0])
        self._restore_safe_chain_role_prompts(workflow, meta, valid_entries)
        self._prune_multi_lora_legacy_regional_nodes(workflow, meta, reason="safe_chain")

        names = [item["lora_name"] for item in valid_entries]
        meta["multi_lora"] = True
        meta["multi_lora_hooks"] = False
        meta["multi_lora_count"] = len(valid_entries)
        meta["multi_lora_mode_effective"] = "safe_chain"
        meta["lora_name"] = " + ".join(names)
        meta["lora_names"] = names
        meta["lora_node_ids"] = chain_ids
        meta["strength_model"] = ", ".join(f"{item['strength_model']:g}" for item in valid_entries)
        meta["strength_clip"] = ", ".join(f"{item['strength_clip']:g}" for item in valid_entries)
        if skipped:
            meta["lora_warning"] = "Skipped part of multi-LoRA: " + "; ".join(skipped[:3])
        logger.info(
            f"[ComfyUI] Multi-LoRA Safe Chain injected: {' + '.join(names)} | "
            f"nodes: {', '.join(chain_ids)}"
        )
        return meta

    async def _apply_multi_lora_override_v2(
        self,
        workflow: dict,
        meta: dict,
        entries: list,
        lora_nodes: list,
        target_node_ids: list,
        session: aiohttp.ClientSession = None,
    ):
        mode = self._normalize_multi_lora_mode()
        meta["multi_lora_mode"] = mode
        if mode == "disabled":
            return self._neutralize_lora_nodes(
                workflow,
                meta,
                "Multi-LoRA disabled by config",
                target_node_ids=target_node_ids,
            )

        hook_nodes = self._find_nodes_by_class_type(workflow, "CreateHookLora")
        if mode in {"auto", "densediffusion"}:
            dd_meta = await self._apply_multi_lora_densediffusion(
                workflow,
                meta,
                entries,
                lora_nodes,
                target_node_ids,
                hook_nodes=hook_nodes,
                session=session,
            )
            if dd_meta.get("multi_lora_densediffusion"):
                return dd_meta
            failure_reason = (
                dd_meta.get("lora_warning")
                or ("mask upload failed" if dd_meta.get("multi_lora_dd_mask_failed") else "")
                or ("DenseDiffusion nodes not installed" if dd_meta.get("multi_lora_dd_unavailable") else "")
                or "DenseDiffusion injection did not complete"
            )
            raise RuntimeError(
                f"DenseDiffusion 多人 LoRA 注入未完成：{failure_reason}。"
                "不会自动回退到 Safe Chain；只有显式选择 safe_chain 才会使用 Safe Chain。"
            )

        char_prompt_node_ids = self._multi_lora_hook_prompt_node_ids(workflow)
        if mode == "regional_lora":
            if len(hook_nodes) >= 2 and len(char_prompt_node_ids) >= 2 and len(entries) >= 2:
                hook_meta = await self._apply_multi_lora_hooks(
                    workflow,
                    meta,
                    entries,
                    hook_nodes,
                    char_prompt_node_ids,
                    session=session,
                )
                if (
                    hook_meta.get("multi_lora_hooks")
                    and hook_meta.get("multi_lora_masked")
                    and _safe_int(hook_meta.get("multi_lora_count"), 0) >= 2
                ):
                    return hook_meta
                failure_reason = (
                    hook_meta.get("multi_lora_fallback_reason")
                    or hook_meta.get("lora_warning")
                    or "regional Hook injection did not complete"
                )
                raise RuntimeError(
                    f"Regional LoRA 多人注入未完成：{failure_reason}。"
                    "不会自动回退到 Safe Chain；只有显式选择 safe_chain 才会使用 Safe Chain。"
                )
            else:
                failure_reason = "regional workflow missing Hook LoRA nodes or character prompt nodes"
                raise RuntimeError(
                    f"Regional LoRA 多人注入未完成：{failure_reason}。"
                    "不会自动回退到 Safe Chain；只有显式选择 safe_chain 才会使用 Safe Chain。"
                )

        if mode == "safe_chain":
            return await self._apply_multi_lora_safe_chain_override(
                workflow,
                meta,
                entries,
                lora_nodes,
                target_node_ids,
                hook_nodes=hook_nodes,
            )

        raise RuntimeError(f"Unsupported Multi-LoRA mode: {mode}")

    async def _apply_multi_lora_override(self, workflow: dict, meta: dict, entries: list, lora_nodes: list, target_node_ids: list, session: aiohttp.ClientSession = None):
        return await self._apply_multi_lora_override_v2(
            workflow,
            meta,
            entries,
            lora_nodes,
            target_node_ids,
            session=session,
        )

        hook_nodes = self._find_nodes_by_class_type(workflow, "CreateHookLora")
        char_prompt_node_ids = self._multi_lora_hook_prompt_node_ids(workflow)
        if len(hook_nodes) >= 2 and len(char_prompt_node_ids) >= 2 and len(entries) >= 2:
            return await self._apply_multi_lora_hooks(workflow, meta, entries, hook_nodes, char_prompt_node_ids, session=session)

        cached_loras = self.get_lora_cache()
        valid_entries = []
        skipped = []
        seen = set()
        for item in entries:
            name = str(item.get("lora_name") or "").strip()
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            if cached_loras and name not in cached_loras:
                skipped.append(f"{name}: not in ComfyUI LoRA list")
                continue
            if not self._lora_allowed_by_runtime_family(name):
                skipped.append(f"{name}: incompatible model family")
                continue
            valid_entries.append(item)

        if not valid_entries:
            reason = "多人 LoRA 均不可用，已跳过注入"
            if skipped:
                reason += "：" + "；".join(skipped[:3])
            return self._neutralize_lora_nodes(workflow, meta, reason, valid_loras=cached_loras, target_node_ids=target_node_ids)

        if not lora_nodes:
            meta["lora_warning"] = "未在多人 LoRA 工作流中找到角色 LoRA 节点，无法注入多人 LoRA"
            logger.warning(f"[ComfyUI] {meta['lora_warning']}")
            return meta

        first_id, first_node = lora_nodes[0]
        self._set_lora_node_inputs(
            first_node,
            valid_entries[0]["lora_name"],
            valid_entries[0]["strength_model"],
            valid_entries[0]["strength_clip"],
        )
        chain_ids = [str(first_id)]
        current_model_ref = [str(first_id), 0]
        current_clip_ref = [str(first_id), 1]
        inserted_ids = set()
        for index, item in enumerate(valid_entries[1:], start=2):
            node_id = self._next_node_id(workflow)
            workflow[node_id] = {
                "inputs": {
                    "model": copy.deepcopy(current_model_ref),
                    "clip": copy.deepcopy(current_clip_ref),
                    "lora_name": item["lora_name"],
                    "strength_model": item["strength_model"],
                    "strength_clip": item["strength_clip"],
                },
                "class_type": "LoraLoader",
                "_meta": {"title": f"AstrBot runtime character LoRA {index}"},
            }
            inserted_ids.add(str(node_id))
            chain_ids.append(str(node_id))
            current_model_ref = [str(node_id), 0]
            current_clip_ref = [str(node_id), 1]

        if len(chain_ids) > 1:
            replacements = {
                (str(first_id), "0"): current_model_ref,
                (str(first_id), "1"): current_clip_ref,
                (str(first_id), 0): current_model_ref,
                (str(first_id), 1): current_clip_ref,
            }
            self._replace_workflow_references(
                workflow,
                replacements,
                skip_node_ids={str(first_id), *inserted_ids},
            )

        # Masked regional conditioning cannot localize chain-loaded LoRA weights:
        # the LoRA model/clip patches are global, while the mask only affects
        # text conditioning. In practice this creates split panels or extra
        # characters for close interactions. Keep the safer global chain path.
        meta["multi_lora_regional_conditioning"] = False
        meta["multi_lora_regional_warning"] = (
            "Regional conditioning disabled for chain-loaded multi-LoRA; using global Safe Chain"
        )
        self._set_sampler_conditioning_refs(workflow, ["11", 0], ["12", 0])

        names = [item["lora_name"] for item in valid_entries]
        meta["multi_lora"] = True
        meta["multi_lora_count"] = len(valid_entries)
        meta["lora_name"] = "、".join(names)
        meta["lora_names"] = names
        meta["lora_node_ids"] = chain_ids
        meta["strength_model"] = ", ".join(f"{item['strength_model']:g}" for item in valid_entries)
        meta["strength_clip"] = ", ".join(f"{item['strength_clip']:g}" for item in valid_entries)
        if skipped:
            meta["lora_warning"] = "已跳过部分多人 LoRA：" + "；".join(skipped[:3])
        logger.info(
            f"[ComfyUI] ✓ 多人 LoRA 链式注入: {'、'.join(names)} | "
            f"节点: {', '.join(chain_ids)}"
        )
        return meta

    def _insert_style_lora_node(self, workflow: dict, lora_name: str, strength_model: float, strength_clip: float):
        model_ref, clip_ref = self._find_primary_generation_refs(workflow)
        if not model_ref or not clip_ref:
            return "", 0
        node_id = self._next_node_id(workflow)
        workflow[node_id] = {
            "inputs": {
                "model": copy.deepcopy(model_ref),
                "clip": copy.deepcopy(clip_ref),
                "lora_name": lora_name,
                "strength_model": strength_model,
                "strength_clip": strength_clip,
            },
            "class_type": "LoraLoader",
            "_meta": {"title": "AstrBot runtime style LoRA"},
        }
        replacements = {
            (str(model_ref[0]), str(model_ref[1])): [node_id, 0],
            (str(clip_ref[0]), str(clip_ref[1])): [node_id, 1],
        }
        rewired = self._replace_workflow_references(workflow, replacements, skip_node_ids={node_id})
        return node_id, rewired

    def _workflow_default_style_lora_meta(self, style_nodes):
        active = []
        for nid, node_data in style_nodes:
            inputs = node_data.get("inputs", {}) if isinstance(node_data, dict) else {}
            if not isinstance(inputs, dict):
                continue
            name = str(inputs.get("lora_name") or inputs.get("lora") or inputs.get("name") or "").strip()
            strength = _safe_float(
                inputs.get("strength_model", inputs.get("strength", inputs.get("model_strength", 0))),
                0,
            )
            if name and abs(strength) > 0:
                active.append((str(nid), name))
        return active

    def _apply_style_lora_override(self, workflow: dict):
        meta = {
            "style_lora_enabled": self.style_lora_enabled,
            "style_lora_name": self.config_style_lora_name or "",
            "style_lora_node_ids": [],
            "style_lora_warning": "",
            "style_strength_model": self.style_strength_model,
            "style_strength_clip": self.style_strength_clip,
        }
        style_nodes = self._find_style_lora_nodes(workflow)

        if not self.style_lora_enabled:
            if style_nodes:
                return self._neutralize_style_lora_nodes(workflow, meta, "画风 LoRA 已在 WebUI 关闭", nodes=style_nodes)
            meta["style_lora_name"] = "已关闭"
            return meta

        style_lora = str(self.config_style_lora_name or "").strip()
        if not style_lora:
            defaults = self._workflow_default_style_lora_meta(style_nodes)
            if defaults:
                meta["style_lora_name"] = "工作流默认：" + "、".join(name for _, name in defaults)
                meta["style_lora_node_ids"] = [nid for nid, _ in defaults]
            else:
                meta["style_lora_name"] = ""
            return meta

        cached_loras = self.get_lora_cache()
        if self._lora_bound_to_profile(style_lora):
            return self._neutralize_style_lora_nodes(
                workflow,
                meta,
                f"画风 LoRA 指向了角色档案，已跳过：{style_lora}",
                valid_loras=cached_loras,
                nodes=style_nodes,
            )
        if not self.allow_unmarked_style_lora and not self._looks_like_style_lora_name(style_lora):
            return self._neutralize_style_lora_nodes(
                workflow,
                meta,
                f"画风 LoRA 文件名不像风格/质量 LoRA，已跳过：{style_lora}",
                valid_loras=cached_loras,
                nodes=style_nodes,
            )
        if cached_loras and style_lora not in cached_loras:
            return self._neutralize_style_lora_nodes(
                workflow,
                meta,
                f"画风 LoRA 不在 ComfyUI 可用列表，已跳过：{style_lora}",
                valid_loras=cached_loras,
                nodes=style_nodes,
            )
        if (
            not self._runtime_guard_has_lora_key(style_lora, "trusted_workflow_lora_keys")
            and not self._lora_allowed_by_runtime_family(style_lora, allow_unknown=self.allow_unmarked_style_lora)
        ):
            family_label = str(getattr(self, "runtime_model_family_label", "") or getattr(self, "runtime_model_family", "") or "当前模型族")
            return self._neutralize_style_lora_nodes(
                workflow,
                meta,
                f"画风 LoRA 与 {family_label} 不兼容或缺少模型族标记，已跳过：{style_lora}",
                valid_loras=cached_loras,
                nodes=style_nodes,
            )

        if style_nodes:
            targets = style_nodes[:1]
            for nid, node_data in targets:
                self._set_lora_node_inputs(node_data, style_lora, self.style_strength_model, self.style_strength_clip)
                meta["style_lora_node_ids"].append(str(nid))
        else:
            node_id, rewired = self._insert_style_lora_node(
                workflow,
                style_lora,
                self.style_strength_model,
                self.style_strength_clip,
            )
            if not node_id:
                meta["style_lora_warning"] = f"未找到可接入画风 LoRA 的 model/clip 链路，无法注入 {style_lora}"
                logger.warning(f"[ComfyUI] {meta['style_lora_warning']}")
                return meta
            meta["style_lora_node_ids"].append(node_id)
            meta["style_lora_dynamic_node"] = True
            meta["style_lora_rewired_refs"] = rewired

        logger.info(
            f"[ComfyUI] ✓ 画风 LoRA 注入: {style_lora} | "
            f"节点: {', '.join(meta['style_lora_node_ids'])} | "
            f"model={self.style_strength_model}, clip={self.style_strength_clip}"
        )
        return meta

    def _inject_pose_control_params(self, workflow: dict, upload_info: dict, meta: dict, options: dict = None):
        options = options or {}
        image_name = str((upload_info or {}).get("name") or "").strip()
        subfolder = str((upload_info or {}).get("subfolder") or "").strip().strip("/")
        if not image_name:
            raise RuntimeError("OpenPose 控制图上传失败：ComfyUI 未返回图片名")
        image_value = f"{subfolder}/{image_name}" if subfolder else image_name

        controlnet_name = str(options.get("controlnet_name") or DEFAULT_POSE_CONTROLNET_NAME).strip() or DEFAULT_POSE_CONTROLNET_NAME
        strength = min(max(_safe_float(options.get("strength"), 0.92), 0.0), 2.0)
        start_percent = min(max(_safe_float(options.get("start_percent"), 0.0), 0.0), 1.0)
        end_percent = min(max(_safe_float(options.get("end_percent"), 0.82), 0.0), 1.0)
        if end_percent < start_percent:
            end_percent = start_percent

        load_id = self._next_node_id(workflow)
        workflow[load_id] = {
            "inputs": {"image": image_value},
            "class_type": "LoadImage",
            "_meta": {"title": "AstrBot OpenPose Control Image"},
        }

        control_id = self._next_node_id(workflow)
        workflow[control_id] = {
            "inputs": {"control_net_name": controlnet_name},
            "class_type": "ControlNetLoader",
            "_meta": {"title": "AstrBot OpenPose ControlNet"},
        }

        apply_inputs = {
            "positive": [self.input_id, 0],
            "negative": [self.neg_node_id, 0] if self.neg_node_id else [self.input_id, 0],
            "control_net": [control_id, 0],
            "image": [load_id, 0],
            "strength": strength,
            "start_percent": start_percent,
            "end_percent": end_percent,
        }
        vae_source = self._find_vae_source(workflow)
        if vae_source:
            apply_inputs["vae"] = vae_source

        apply_id = self._next_node_id(workflow)
        workflow[apply_id] = {
            "inputs": apply_inputs,
            "class_type": "ControlNetApplyAdvanced",
            "_meta": {"title": "AstrBot OpenPose Apply"},
        }

        rewired = self._set_sampler_conditioning_refs(workflow, [apply_id, 0], [apply_id, 1])
        if not rewired:
            raise RuntimeError("OpenPose ControlNet 注入失败：未找到可重连的采样器")

        meta["pose_control"] = True
        meta["pose_control_image"] = image_value
        meta["pose_controlnet_name"] = controlnet_name
        meta["pose_control_strength"] = strength
        meta["pose_control_start_percent"] = start_percent
        meta["pose_control_end_percent"] = end_percent
        meta["pose_control_nodes"] = {
            "load_image": load_id,
            "controlnet_loader": control_id,
            "controlnet_apply": apply_id,
            "samplers_rewired": rewired,
        }
        logger.info(
            "[ComfyUI] WebUI OpenPose 姿态控制已注入: "
            f"image={image_value} controlnet={controlnet_name} strength={strength} end={end_percent} samplers={rewired}"
        )
        return meta

    # ------------------------------------------------------------------
    # 工作流注入
    # ------------------------------------------------------------------
    def _load_workflow(self):
        if not self.workflow_path.exists():
            raise FileNotFoundError(f"工作流文件不存在: {self.workflow_path}")
        with open(self.workflow_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _activate_character_replace_workflow(self):
        if not self.img2img_auto_use_character_replace_workflow:
            return False
        filename = str(self.img2img_character_replace_workflow or "").strip()
        if not filename:
            return False
        candidate = self.workflow_dir / filename
        if not candidate.exists():
            logger.warning(f"[ComfyUI] 角色替换专用工作流不存在，继续使用当前工作流: {candidate}")
            return False

        self.wf_filename = filename
        self.workflow_path = candidate
        self.input_id = self.img2img_character_replace_input_id or self.input_id
        self.neg_node_id = self.img2img_character_replace_neg_id or self.neg_node_id
        self.output_id = self.img2img_character_replace_output_id or self.output_id
        self.lora_node_id = self.img2img_character_replace_lora_id or self.lora_node_id
        logger.info(
            f"[ComfyUI] 已切换角色替换专用工作流: {filename} | "
            f"Positive={self.input_id} Negative={self.neg_node_id} Output={self.output_id} LoRA={self.lora_node_id}"
        )
        return True

    def _workflow_model_names(self, workflow: dict):
        names = []
        for node_data in workflow.values():
            if not isinstance(node_data, dict):
                continue
            inputs = node_data.get("inputs", {}) or {}
            class_type = str(node_data.get("class_type", "") or "")
            for key in ("ckpt_name", "unet_name", "model_name"):
                value = inputs.get(key)
                if value and class_type in {"CheckpointLoaderSimple", "UNETLoader", "UNETLoaderGGUF"}:
                    names.append(str(value))
        result = []
        seen = set()
        for name in names:
            marker = name.lower()
            if marker in seen:
                continue
            seen.add(marker)
            result.append(name)
        return result

    def _apply_checkpoint_override(self, workflow: dict):
        checkpoint = str(getattr(self, "runtime_checkpoint_name", "") or "").strip()
        if not checkpoint:
            return ""
        changed = 0
        for node_data in workflow.values():
            if not isinstance(node_data, dict):
                continue
            if str(node_data.get("class_type") or "") != "CheckpointLoaderSimple":
                continue
            inputs = node_data.setdefault("inputs", {})
            if not isinstance(inputs, dict) or "ckpt_name" not in inputs:
                continue
            if str(inputs.get("ckpt_name") or "") != checkpoint:
                inputs["ckpt_name"] = checkpoint
                changed += 1
        if changed:
            logger.info(f"[ComfyUI] Illustrious checkpoint override: {checkpoint} ({changed} node)")
        return checkpoint if changed else ""

    def _sampler_is_refine_pass(self, node_data: dict) -> bool:
        title = str((node_data.get("_meta") or {}).get("title") or "").lower()
        inputs = node_data.get("inputs", {}) if isinstance(node_data, dict) else {}
        if "refine" in title or "二采" in title:
            return True
        try:
            return "denoise" in inputs and float(inputs.get("denoise") or 0) < 0.999
        except Exception:
            return False

    def _apply_runtime_generation_preset(self, workflow: dict) -> dict:
        preset = getattr(self, "runtime_generation_preset", None)
        if not isinstance(preset, dict) or not preset:
            return {}
        width = _safe_int(preset.get("width"), 0)
        height = _safe_int(preset.get("height"), 0)
        steps = _safe_int(preset.get("steps"), 0)
        cfg = _safe_float(preset.get("cfg"), 0)
        refine_steps = _safe_int(preset.get("refine_steps"), 0)
        refine_cfg = _safe_float(preset.get("refine_cfg"), 0)
        refine_denoise = preset.get("refine_denoise", None)
        latent_upscale_by = _safe_float(preset.get("latent_upscale_by", preset.get("upscale_by", 0)), 0)
        sampler_name = str(preset.get("sampler_name") or "").strip()
        scheduler = str(preset.get("scheduler") or "").strip()
        latent_nodes = 0
        upscale_nodes = 0
        sampler_nodes = 0
        for _nid, node_data in workflow.items():
            if not isinstance(node_data, dict):
                continue
            class_type = str(node_data.get("class_type") or "")
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            if (
                width > 0 and height > 0
                and "width" in inputs and "height" in inputs
                and ("Latent" in class_type or class_type in {"EmptyLatentImage", "EmptySD3LatentImage"})
            ):
                inputs["width"] = width
                inputs["height"] = height
                latent_nodes += 1
            if latent_upscale_by > 0 and class_type == "LatentUpscaleBy" and "scale_by" in inputs:
                inputs["scale_by"] = latent_upscale_by
                upscale_nodes += 1
            if "Sampler" not in class_type and class_type not in {"KSampler", "KSamplerAdvanced"}:
                continue
            is_refine = self._sampler_is_refine_pass(node_data)
            node_steps = refine_steps if is_refine and refine_steps > 0 else steps
            node_cfg = refine_cfg if is_refine and refine_cfg > 0 else cfg
            if node_steps > 0 and "steps" in inputs:
                inputs["steps"] = node_steps
            if node_cfg > 0 and "cfg" in inputs:
                inputs["cfg"] = node_cfg
            if sampler_name and "sampler_name" in inputs:
                inputs["sampler_name"] = sampler_name
            if scheduler and "scheduler" in inputs:
                inputs["scheduler"] = scheduler
            if is_refine and refine_denoise is not None and "denoise" in inputs:
                inputs["denoise"] = min(max(_safe_float(refine_denoise, _safe_float(inputs.get("denoise"), 0.3)), 0.01), 1.0)
            sampler_nodes += 1
        meta = {
            "style_preset": str(preset.get("key") or ""),
            "style_preset_label": str(preset.get("label") or ""),
            "generation_sampler": sampler_name,
            "generation_scheduler": scheduler,
            "generation_steps": steps,
            "generation_cfg": cfg,
            "generation_width": width,
            "generation_height": height,
            "generation_latent_upscale_by": latent_upscale_by,
            "generation_preset_nodes": {"samplers": sampler_nodes, "latents": latent_nodes, "upscales": upscale_nodes},
        }
        detail = []
        if width > 0 and height > 0:
            detail.append(f"{width}x{height}")
        if sampler_name:
            detail.append(f"{sampler_name}/{scheduler or '-'}")
        if steps > 0:
            detail.append(f"steps={steps}")
        if cfg > 0:
            detail.append(f"cfg={cfg}")
        if latent_upscale_by > 0:
            detail.append(f"upscale={latent_upscale_by:g}")
        logger.info(
            f"[ComfyUI] 画风预设已应用: {meta['style_preset_label'] or meta['style_preset']}"
            + (f" | {' '.join(detail)}" if detail else "")
        )
        return meta

    def _infer_model_family_label(self, workflow: dict, model_names: list):
        if self.runtime_model_family_label:
            return str(self.runtime_model_family_label)
        haystack = " ".join([self.wf_filename] + [str(x) for x in (model_names or [])]).lower()
        if "illustrious" in haystack or "wai" in haystack:
            return "Illustrious"
        if "anima" in haystack:
            return "Anima"
        return "默认/自定义"

    def _format_history_error(self, history_item: dict):
        status = (history_item or {}).get("status", {}) or {}
        messages = status.get("messages", []) or []
        errors = []
        for item in messages:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            kind, payload = item[0], item[1]
            if kind != "execution_error" or not isinstance(payload, dict):
                continue
            node_id = str(payload.get("node_id") or "").strip()
            node_type = str(payload.get("node_type") or "").strip()
            exc_type = str(payload.get("exception_type") or "").strip()
            exc_msg = str(payload.get("exception_message") or payload.get("exception") or "").strip()
            where = " ".join([x for x in [f"节点{node_id}" if node_id else "", node_type] if x])
            detail = "：".join([x for x in [exc_type, exc_msg] if x])
            errors.append(" - ".join([x for x in [where, detail] if x]))
        if errors:
            return "；".join(errors)[:500]
        if status.get("status_str"):
            return str(status.get("status_str"))[:200]
        return ""

    async def _fetch_object_info(self, session: aiohttp.ClientSession) -> dict:
        cached_at, cached_data = getattr(self, "_object_info_cache", (0.0, {}))
        if isinstance(cached_data, dict) and cached_data and time.time() - float(cached_at or 0) < 60:
            return cached_data
        try:
            async with session.get(f"{self.url}/object_info") as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
        except Exception as exc:
            logger.debug(f"[ComfyUI] object_info workflow check failed: {exc}")
            return {}
        if isinstance(data, dict) and data:
            self._object_info_cache = (time.time(), data)
        return data if isinstance(data, dict) else {}

    def _object_info_combo_choices(self, object_info: dict, class_type: str, input_name: str) -> list:
        required = (((object_info or {}).get(class_type) or {}).get("input") or {}).get("required") or {}
        spec = required.get(input_name)
        if not isinstance(spec, list) or not spec:
            return []
        if isinstance(spec[0], list):
            return [str(item) for item in spec[0]]
        if (
            str(spec[0]).upper() == "COMBO"
            and len(spec) > 1
            and isinstance(spec[1], dict)
            and isinstance(spec[1].get("options"), list)
        ):
            return [str(item) for item in spec[1].get("options") or []]
        return []

    def _normalize_illustrious_foot_detector_model(self, detector_model: str = None) -> str:
        raw = str(detector_model or DEFAULT_ILLUSTRIOUS_FOOT_DETECTOR_MODEL).strip().replace("\\", "/")
        if not raw:
            raw = DEFAULT_ILLUSTRIOUS_FOOT_DETECTOR_MODEL
        if raw.startswith(("bbox/", "segm/")):
            return raw
        return "bbox/" + Path(raw).name

    def _format_illustrious_foot_detailer_preflight_error(self, missing: list, detector_model: str = None) -> str:
        detector_name = Path(str(detector_model or DEFAULT_ILLUSTRIOUS_FOOT_DETECTOR_MODEL)).name
        missing_text = "、".join(dict.fromkeys(str(item) for item in missing if item)) or "未知依赖"
        return (
            "Illustrious 单人图已启用足部 Detailer，但 ComfyUI 依赖不完整，已停止生成。\n"
            f"缺少：{missing_text}\n"
            "请在 ComfyUI 服务器端的 ComfyUI/custom_nodes 安装并重启：\n"
            "- ComfyUI-Impact-Pack: https://github.com/ltdrdata/ComfyUI-Impact-Pack\n"
            "- ComfyUI-Impact-Subpack: https://github.com/ltdrdata/ComfyUI-Impact-Subpack\n"
            f"并确认检测模型位于 ComfyUI 服务器端的 ComfyUI/models/ultralytics/bbox/{detector_name}。"
        )

    async def check_illustrious_foot_detailer_ready(self, detector_model: str = None) -> tuple:
        detector_value = self._normalize_illustrious_foot_detector_model(detector_model)
        detector_name = Path(detector_value).name
        meta = {
            "foot_detailer_detector_model": detector_name,
            "foot_detailer_detector_value": detector_value,
        }
        timeout = aiohttp.ClientTimeout(
            total=max(float(getattr(self, "comfyui_connect_timeout", DEFAULT_CONNECT_TIMEOUT) or DEFAULT_CONNECT_TIMEOUT) + 20.0, 30.0),
            connect=max(float(getattr(self, "comfyui_connect_timeout", DEFAULT_CONNECT_TIMEOUT) or DEFAULT_CONNECT_TIMEOUT), 3.0),
            sock_read=max(float(getattr(self, "history_request_timeout", 30) or 30), 20.0),
        )
        async with self._client_session(timeout) as session:
            object_info = await self._fetch_object_info(session)
        if not object_info:
            return False, self._format_illustrious_foot_detailer_preflight_error(["/object_info 不可用"], detector_name), meta

        available = {str(key) for key in object_info.keys()}
        required = {
            "UltralyticsDetectorProvider": "ComfyUI-Impact-Subpack / UltralyticsDetectorProvider",
            "ImpactSimpleDetectorSEGS": "ComfyUI-Impact-Pack / SimpleDetectorSEGS",
            "DetailerForEach": "ComfyUI-Impact-Pack / DetailerForEach",
            "CheckpointLoaderSimple": "ComfyUI 内置 CheckpointLoaderSimple",
            "CLIPTextEncode": "ComfyUI 内置 CLIPTextEncode",
            "LoadImage": "ComfyUI 内置 LoadImage",
            "SaveImage": "ComfyUI 内置 SaveImage",
        }
        missing = [label for class_type, label in required.items() if class_type not in available]
        choices = self._object_info_combo_choices(object_info, "UltralyticsDetectorProvider", "model_name")
        meta["foot_detailer_detector_choices"] = len(choices)
        if not missing:
            choice_set = {str(item) for item in choices}
            if detector_value not in choice_set:
                bare_match = detector_name in choice_set
                if not bare_match:
                    missing.append(f"{detector_value} (foot-yolov8l.pt)")
        if missing:
            return False, self._format_illustrious_foot_detailer_preflight_error(missing, detector_name), meta
        return True, "", meta

    def _build_illustrious_foot_detailer_workflow(
        self,
        image_value: str,
        prompt: str,
        negative_prompt: str,
        checkpoint_name: str,
        detector_model: str = None,
        steps: int = 26,
        cfg: float = 4.6,
        denoise: float = 0.24,
        seed: int = None,
        base_meta: dict = None,
    ) -> tuple:
        base_meta = base_meta or {}
        detector_value = self._normalize_illustrious_foot_detector_model(detector_model)
        ckpt_name = str(checkpoint_name or base_meta.get("checkpoint_override") or base_meta.get("checkpoint_name") or "").strip()
        if not ckpt_name:
            raise RuntimeError("足部 Detailer 无法确定当前 Illustrious checkpoint，请先刷新/切换模型族配置")

        workflow = {}

        def add_node(class_type: str, inputs: dict, title: str) -> str:
            node_id = str(len(workflow) + 1)
            workflow[node_id] = {
                "inputs": inputs,
                "class_type": class_type,
                "_meta": {"title": title},
            }
            return node_id

        ckpt_id = add_node(
            "CheckpointLoaderSimple",
            {"ckpt_name": ckpt_name},
            "AstrBot Foot Detailer Checkpoint",
        )
        model_ref = [ckpt_id, 0]
        clip_ref = [ckpt_id, 1]
        vae_ref = [ckpt_id, 2]

        lora_name = str(base_meta.get("lora_name") or "").strip()
        if lora_name:
            lora_id = add_node(
                "LoraLoader",
                {
                    "model": model_ref,
                    "clip": clip_ref,
                    "lora_name": lora_name,
                    "strength_model": _safe_float(base_meta.get("strength_model"), self.strength_model),
                    "strength_clip": _safe_float(base_meta.get("strength_clip"), self.strength_clip),
                },
                "AstrBot Foot Detailer Character LoRA",
            )
            model_ref = [lora_id, 0]
            clip_ref = [lora_id, 1]

        style_lora_name = str(base_meta.get("style_lora_name") or "").strip()
        if (
            style_lora_name
            and not style_lora_name.startswith("工作流默认")
            and style_lora_name not in {"已关闭", "disabled", "none"}
        ):
            style_id = add_node(
                "LoraLoader",
                {
                    "model": model_ref,
                    "clip": clip_ref,
                    "lora_name": style_lora_name,
                    "strength_model": _safe_float(base_meta.get("style_strength_model"), self.style_strength_model),
                    "strength_clip": _safe_float(base_meta.get("style_strength_clip"), self.style_strength_clip),
                },
                "AstrBot Foot Detailer Style LoRA",
            )
            model_ref = [style_id, 0]
            clip_ref = [style_id, 1]

        image_id = add_node(
            "LoadImage",
            {"image": image_value},
            "AstrBot Foot Detailer Source",
        )
        positive_id = add_node(
            "CLIPTextEncode",
            {"clip": clip_ref, "text": str(prompt or "").strip()},
            "AstrBot Foot Detailer Positive",
        )
        negative_id = add_node(
            "CLIPTextEncode",
            {"clip": clip_ref, "text": str(negative_prompt or "").strip()},
            "AstrBot Foot Detailer Negative",
        )
        detector_id = add_node(
            "UltralyticsDetectorProvider",
            {"model_name": detector_value},
            "AstrBot Foot YOLO Detector",
        )
        segs_id = add_node(
            "ImpactSimpleDetectorSEGS",
            {
                "bbox_detector": [detector_id, 0],
                "image": [image_id, 0],
                "bbox_threshold": 0.42,
                "bbox_dilation": 12,
                "crop_factor": 3.5,
                "drop_size": 16,
                "sub_threshold": 0.5,
                "sub_dilation": 0,
                "sub_bbox_expansion": 0,
                "sam_mask_hint_threshold": 0.7,
                "post_dilation": 4,
            },
            "AstrBot Foot SEGS",
        )
        detailer_id = add_node(
            "DetailerForEach",
            {
                "image": [image_id, 0],
                "segs": [segs_id, 0],
                "model": model_ref,
                "clip": clip_ref,
                "vae": vae_ref,
                "guide_size": 768,
                "guide_size_for": True,
                "max_size": 1216,
                "seed": max(_safe_int(seed, 0), 0),
                "steps": max(_safe_int(steps, 26), 1),
                "cfg": min(max(_safe_float(cfg, 4.6), 0.0), 100.0),
                "sampler_name": "euler_ancestral",
                "scheduler": "normal",
                "positive": [positive_id, 0],
                "negative": [negative_id, 0],
                "denoise": min(max(_safe_float(denoise, 0.24), 0.0001), 1.0),
                "feather": 10,
                "noise_mask": True,
                "force_inpaint": True,
                "wildcard": "",
                "cycle": 1,
            },
            "AstrBot Foot DetailerForEach",
        )
        save_id = add_node(
            "SaveImage",
            {
                "images": [detailer_id, 0],
                "filename_prefix": "AstrBot_Illustrious_FootDetailer",
            },
            "AstrBot Foot Detailer Save",
        )
        meta = {
            "foot_detailer_workflow": "dynamic_impact_pack",
            "foot_detailer_detector_model": Path(detector_value).name,
            "foot_detailer_detector_value": detector_value,
            "foot_detailer_checkpoint": ckpt_name,
            "foot_detailer_steps": max(_safe_int(steps, 26), 1),
            "foot_detailer_cfg": min(max(_safe_float(cfg, 4.6), 0.0), 100.0),
            "foot_detailer_denoise": min(max(_safe_float(denoise, 0.24), 0.0001), 1.0),
            "foot_detailer_output_id": save_id,
        }
        if "foot_detailer_detected_regions" in base_meta:
            meta["foot_detailer_regions"] = int(base_meta.get("foot_detailer_detected_regions") or 0)
        if lora_name:
            meta["foot_detailer_lora_name"] = lora_name
        if style_lora_name:
            meta["foot_detailer_style_lora_name"] = style_lora_name
        return workflow, save_id, meta

    async def apply_illustrious_foot_detailer(
        self,
        image_path: str,
        prompt: str,
        negative_prompt: str = "",
        base_meta: dict = None,
        detector_model: str = None,
        steps: int = 26,
        cfg: float = 4.6,
        denoise: float = 0.24,
    ) -> tuple:
        started = time.perf_counter()
        base_meta = base_meta or {}
        meta = {"foot_detailer_enabled": True}
        ready, ready_error, ready_meta = await self.check_illustrious_foot_detailer_ready(detector_model)
        meta.update(ready_meta or {})
        if not ready:
            meta["elapsed_seconds"] = time.perf_counter() - started
            return None, ready_error, meta

        timeout = self._generation_client_timeout()
        client_id = str(random.randint(100000, 999999))
        async with self._client_session(timeout) as session:
            try:
                upload_info = await self._upload_input_image(session, image_path)
                image_name = str(upload_info.get("name") or "").strip()
                subfolder = str(upload_info.get("subfolder") or "").strip().strip("/")
                if not image_name:
                    raise RuntimeError("ComfyUI 未返回足部 Detailer 源图上传文件名")
                image_value = f"{subfolder}/{image_name}" if subfolder else image_name
                workflow, output_id, workflow_meta = self._build_illustrious_foot_detailer_workflow(
                    image_value=image_value,
                    prompt=prompt,
                    negative_prompt=negative_prompt or str(self.neg_prompt or ""),
                    checkpoint_name=str(base_meta.get("checkpoint_override") or base_meta.get("checkpoint_name") or ""),
                    detector_model=detector_model,
                    steps=steps,
                    cfg=cfg,
                    denoise=denoise,
                    seed=max(_safe_int(base_meta.get("seed"), 0), 0) + 1009,
                    base_meta=base_meta,
                )
                meta.update(workflow_meta)
                meta["foot_detailer_source_image"] = image_value
            except Exception as exc:
                meta["elapsed_seconds"] = time.perf_counter() - started
                return None, str(exc), meta

            prompt_id, queue_error = await self._queue_workflow(
                session,
                workflow,
                client_id,
                meta,
                neutralize_lora_on_validation_error=False,
            )
            if queue_error:
                meta["elapsed_seconds"] = time.perf_counter() - started
                return None, queue_error, meta

            prompt_submitted_at = time.time()
            for poll_index in range(self.poll_attempts):
                await asyncio.sleep(self._generation_poll_delay(poll_index))
                history = await self._fetch_prompt_history(session, prompt_id, meta)
                if not history or prompt_id not in history:
                    missing_error = await self._check_prompt_disappeared_from_queue(
                        session,
                        prompt_id,
                        meta,
                        prompt_submitted_at,
                    )
                    if missing_error:
                        meta["elapsed_seconds"] = time.perf_counter() - started
                        return None, missing_error, meta
                    continue

                history_item = history[prompt_id] or {}
                status = history_item.get("status", {}) or {}
                if status.get("status_str") == "error":
                    detail = self._format_history_error(history_item)
                    if detail:
                        meta["execution_error"] = detail
                    meta["elapsed_seconds"] = time.perf_counter() - started
                    return None, "ComfyUI 足部 Detailer 工作流执行失败：" + (detail or "请查看 ComfyUI 控制台日志"), meta

                outputs = history_item.get("outputs", {}) or {}
                img_info = self._select_image_output(outputs, output_id=output_id)
                if not img_info:
                    meta["elapsed_seconds"] = time.perf_counter() - started
                    return None, "足部 Detailer 工作流执行完成，但未找到输出图片", meta

                image_bytes, image_error = await self._download_image_bytes(session, img_info, meta)
                meta["elapsed_seconds"] = time.perf_counter() - started
                if image_bytes:
                    return image_bytes, None, meta
                return None, image_error or "足部 Detailer 输出下载失败", meta

            meta["elapsed_seconds"] = time.perf_counter() - started
            return None, "足部 Detailer 生成超时", meta

    def _workflow_missing_node_classes(self, workflow: dict, available_classes: set) -> list:
        if not available_classes:
            return []
        missing = []
        seen = set()
        for node_data in (workflow or {}).values():
            if not isinstance(node_data, dict):
                continue
            class_type = str(node_data.get("class_type") or "").strip()
            if not class_type or class_type in available_classes or class_type in seen:
                continue
            seen.add(class_type)
            missing.append(class_type)
        return missing

    def _workflow_missing_required_inputs(self, workflow: dict, object_info: dict) -> list:
        if not isinstance(object_info, dict) or not object_info:
            return []
        missing = []
        for node_id, node_data in (workflow or {}).items():
            if not isinstance(node_data, dict):
                continue
            class_type = str(node_data.get("class_type") or "").strip()
            required = (((object_info.get(class_type) or {}).get("input") or {}).get("required") or {})
            inputs = node_data.get("inputs") or {}
            if not isinstance(required, dict) or not isinstance(inputs, dict):
                continue
            for key in required.keys():
                if key not in inputs:
                    missing.append(f"{node_id}.{class_type}.{key}")
        return missing

    def _workflow_invalid_combo_inputs(self, workflow: dict, object_info: dict) -> list:
        if not isinstance(object_info, dict) or not object_info:
            return []
        invalid = []
        for node_id, node_data in (workflow or {}).items():
            if not isinstance(node_data, dict):
                continue
            class_type = str(node_data.get("class_type") or "").strip()
            required = (((object_info.get(class_type) or {}).get("input") or {}).get("required") or {})
            inputs = node_data.get("inputs") or {}
            if not isinstance(required, dict) or not isinstance(inputs, dict):
                continue
            for key, value in inputs.items():
                if class_type in {"LoraLoader", "CreateHookLora", "CreateHookLoraModelOnly"} and key == "lora_name":
                    continue
                spec = required.get(key)
                choices = []
                if isinstance(spec, list) and spec:
                    if isinstance(spec[0], list):
                        choices = spec[0]
                    elif (
                        str(spec[0]).upper() == "COMBO"
                        and len(spec) > 1
                        and isinstance(spec[1], dict)
                        and isinstance(spec[1].get("options"), list)
                    ):
                        choices = spec[1].get("options") or []
                if choices and str(value) not in {str(item) for item in choices}:
                    invalid.append(f"{node_id}.{class_type}.{key}={value}")
        return invalid

    async def _fallback_unsupported_workflow(self, session: aiohttp.ClientSession, workflow: dict) -> tuple:
        fallback_name = str(getattr(self, "runtime_fallback_wf_filename", "") or "").strip()
        if not fallback_name:
            return workflow, {}

        object_info = await self._fetch_object_info(session)
        if object_info:
            missing_nodes = self._workflow_missing_node_classes(workflow, set(str(k) for k in object_info.keys()))
            missing_inputs = self._workflow_missing_required_inputs(workflow, object_info)
            invalid_inputs = self._workflow_invalid_combo_inputs(workflow, object_info)
        else:
            missing_nodes = ["object_info unavailable"]
            missing_inputs = []
            invalid_inputs = []
        if not missing_nodes and not missing_inputs and not invalid_inputs:
            return workflow, {}

        requested_name = self.wf_filename
        fallback_path = self.workflow_dir / fallback_name
        if not fallback_path.exists():
            return workflow, {
                "workflow_missing_nodes": missing_nodes,
                "workflow_missing_inputs": missing_inputs,
                "workflow_invalid_inputs": invalid_inputs,
                "workflow_fallback_failed": f"{fallback_name} not found",
            }

        self.wf_filename = fallback_name
        self.runtime_wf_filename = fallback_name
        self.workflow_path = fallback_path
        fallback_workflow = self._load_workflow()
        logger.warning(
            "[ComfyUI] workflow unsupported; "
            f"fallback {requested_name} -> {fallback_name}; "
            f"missing_nodes={', '.join(missing_nodes[:6]) or '-'}; "
            f"missing_inputs={', '.join(missing_inputs[:6]) or '-'}; "
            f"invalid={', '.join(invalid_inputs[:4]) or '-'}"
        )
        return fallback_workflow, {
            "workflow_requested": requested_name,
            "workflow_fallback": fallback_name,
            "workflow_missing_nodes": missing_nodes,
            "workflow_missing_inputs": missing_inputs,
            "workflow_invalid_inputs": invalid_inputs,
        }

    async def _inject_params(self, workflow, prompt, session: aiohttp.ClientSession = None, seed_override: int = None):
        """注入提示词、负面提示词、LoRA、步数覆盖和随机种。返回本次生成元信息。"""
        checkpoint_override = self._apply_checkpoint_override(workflow)
        model_names = self._workflow_model_names(workflow)
        meta = {
            "workflow": self.wf_filename,
            "model_family": str(self.runtime_model_family or ""),
            "model_family_label": self._infer_model_family_label(workflow, model_names),
            "model_names": model_names,
            "checkpoint_name": model_names[0] if model_names else "",
            "input_id": self.input_id,
            "output_id": self.output_id or "自动",
            "prompt": prompt,
        }
        if checkpoint_override:
            meta["checkpoint_override"] = checkpoint_override
        meta.update(self._apply_runtime_generation_preset(workflow))

        node = workflow.get(self.input_id)
        if not node:
            raise ValueError(f"找不到正面提示词节点 ID {self.input_id}，请检查工作流或配置")

        inputs = node.setdefault("inputs", {})
        target_keys = [
            "text", "opt_text", "string",
            "text_positive", "positive", "prompt", "wildcard_text",
        ]
        injected = False
        for key in target_keys:
            if key in inputs:
                inputs[key] = prompt
                injected = True
                meta["prompt_key"] = key
                break
        if not injected:
            # 兜底：给节点补 text，避免静默失败
            inputs["text"] = prompt
            meta["prompt_key"] = "text"

        if self.neg_node_id and self.neg_prompt:
            neg_node = workflow.get(self.neg_node_id)
            if neg_node:
                n_inputs = neg_node.setdefault("inputs", {})
                n_keys = ["text", "string", "negative", "text_negative", "prompt"]
                for n_key in n_keys:
                    if n_key in n_inputs:
                        existing_neg = str(n_inputs.get(n_key, "")).strip()
                        config_neg = self.neg_prompt.strip()
                        if existing_neg and config_neg:
                            if config_neg.lower() not in existing_neg.lower():
                                n_inputs[n_key] = f"{existing_neg}, {config_neg}"
                            else:
                                n_inputs[n_key] = existing_neg
                        elif config_neg:
                            n_inputs[n_key] = config_neg
                        meta["negative_key"] = n_key
                        break

        if False and self._runtime_multi_lora_entries() and self._find_nodes_by_class_type(workflow, "CreateHookLora"):
            raise RuntimeError(
                "当前多人 LoRA Hook 工作流已禁用：Aki 的 DynamicVRAM 会切到 ModelPatcherDynamic，"
                "该模式明确不支持 CreateHookLora / patch_hook_weight_to_device。请重载插件，"
                "使用 Safe Chain 工作流；如需实验 Hook，请先用 --disable-dynamic-vram 启动 ComfyUI。"
            )

        meta.update(await self._apply_lora_override(workflow, session=session))
        meta.update(self._apply_style_lora_override(workflow))
        meta.update(self._neutralize_incompatible_workflow_lora_defaults(workflow))

        overrides = self._load_steps_override()
        if overrides:
            count = self._apply_steps_override(workflow, overrides)
            meta["steps_override_count"] = count
            meta["steps_overrides"] = overrides
            if count > 0:
                override_info = ", ".join([f"{k}:{v}步" for k, v in overrides.items()])
                logger.info(f"[ComfyUI] ✓ 步数覆盖生效: {override_info} (修改 {count} 处)")
            else:
                logger.info("[ComfyUI] ⚠ 配置了步数覆盖但未找到匹配的引用")

        self._apply_generation_seed(workflow, meta, seed=seed_override, seed_source="prompt" if seed_override is not None else None)
        return meta

    def _resolve_generation_seed(self) -> tuple:
        runtime_seed = getattr(self, "runtime_seed_override", None)
        if runtime_seed is not None:
            return max(_safe_int(runtime_seed, 0), 0), "prompt"
        fixed_seed = max(_safe_int(getattr(self, "fixed_seed", 0), 0), 0)
        if bool(getattr(self, "fixed_seed_enabled", fixed_seed > 0)):
            return fixed_seed, "fixed"
        return random.randint(1, 999999999999999), "random"

    def _apply_generation_seed(self, workflow: dict, meta: dict, seed: int = None, seed_source: str = None) -> dict:
        source = str(seed_source or "").strip()
        if seed is not None:
            base_seed = max(_safe_int(seed, 0), 0)
            source = source or "explicit"
        else:
            base_seed, source = self._resolve_generation_seed()
        seed_count = 0
        offset = 0
        for node_data in (workflow or {}).values():
            if not isinstance(node_data, dict):
                continue
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            changed = False
            if "seed" in inputs:
                inputs["seed"] = base_seed + offset
                offset += 1
                changed = True
            if "noise_seed" in inputs:
                inputs["noise_seed"] = base_seed + offset
                offset += 1
                changed = True
            if changed:
                seed_count += 1
        meta["seed"] = base_seed
        meta["seed_source"] = source
        meta["seed_count"] = seed_count
        logger.info(
            f"[ComfyUI] 本次基础种子: {base_seed} ({source})，"
            f"已写入 {seed_count} 个 seed/noise_seed 输入"
        )
        return meta

    def _load_steps_override(self) -> dict:
        """读取当前工作流的 steps 覆盖配置。"""
        try:
            stem = self.workflow_path.stem
            sidecar = self.workflow_path.parent / f"{stem}.steps.json"
            if not sidecar.exists():
                return {}
            with open(sidecar, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}

            result = {}
            for key, value in data.items():
                if isinstance(value, dict) and "steps" in value:
                    steps = value.get("steps")
                    if isinstance(steps, (int, float)) and steps > 0:
                        result[str(key)] = int(steps)
                elif isinstance(value, (int, float)) and value > 0:
                    result[str(key)] = int(value)
            return result
        except Exception as e:
            logger.warning(f"[ComfyUI] 读取 steps 覆盖文件失败: {e}")
            return {}

    def _apply_steps_override(self, workflow: dict, overrides: dict):
        if not overrides:
            return 0

        pb_nodes = {}
        for nid, node_data in workflow.items():
            if isinstance(node_data, dict) and node_data.get("class_type") == "ParameterBreak":
                pb_nodes[str(nid)] = node_data

        if not pb_nodes:
            logger.debug("[ComfyUI] 未检测到 ParameterBreak 节点")
            return 0

        valid_overrides = {}
        for pb_id, steps in overrides.items():
            if pb_id in pb_nodes:
                valid_overrides[pb_id] = steps
            else:
                logger.warning(f"[ComfyUI] 覆盖配置中的节点 {pb_id} 不存在于当前工作流")

        if not valid_overrides:
            return 0

        override_count = 0
        for nid, node_data in workflow.items():
            if not isinstance(node_data, dict):
                continue
            n_inputs = node_data.get("inputs", {})
            if not isinstance(n_inputs, dict):
                continue
            for key in ("steps", "steps_total"):
                value = n_inputs.get(key)
                if isinstance(value, list) and len(value) == 2:
                    ref_node_id = str(value[0])
                    if ref_node_id in valid_overrides:
                        n_inputs[key] = valid_overrides[ref_node_id]
                        override_count += 1
                        logger.debug(f"[ComfyUI] 节点 {nid}.{key}: [{ref_node_id}] -> {valid_overrides[ref_node_id]}")
        return override_count

    def _next_node_id(self, workflow: dict) -> str:
        numeric_ids = []
        for node_id in workflow.keys():
            try:
                numeric_ids.append(int(str(node_id)))
            except Exception:
                pass
        return str((max(numeric_ids) if numeric_ids else 0) + 1)

    def _node_class_type(self, workflow: dict, node_id: str) -> str:
        node_data = workflow.get(str(node_id))
        if not isinstance(node_data, dict):
            return ""
        return str(node_data.get("class_type") or "")

    def _is_valid_vae_source_ref(self, workflow: dict, ref) -> bool:
        if not isinstance(ref, list) or len(ref) != 2:
            return False
        node_id = str(ref[0])
        try:
            output_index = int(ref[1])
        except Exception:
            return False
        class_type = self._node_class_type(workflow, node_id)
        if not class_type:
            return False
        if class_type == "CheckpointLoaderSimple":
            return output_index == 2
        if class_type in {"VAELoader", "VAELoaderAdvanced", "VAE Loader"}:
            return output_index == 0
        lowered = class_type.lower()
        if "vae" in lowered and "loader" in lowered and "encode" not in lowered and "decode" not in lowered:
            return output_index == 0
        return False

    def _find_vae_source(self, workflow: dict):
        for node_id, node_data in workflow.items():
            if not isinstance(node_data, dict):
                continue
            if str(node_data.get("class_type") or "") == "CheckpointLoaderSimple":
                ref = [str(node_id), 2]
                if self._is_valid_vae_source_ref(workflow, ref):
                    return ref

        for node_id, node_data in workflow.items():
            if not isinstance(node_data, dict):
                continue
            class_type = str(node_data.get("class_type") or "")
            ref = [str(node_id), 0]
            if self._is_valid_vae_source_ref(workflow, ref) and class_type != "CheckpointLoaderSimple":
                return ref

        for node_data in workflow.values():
            if not isinstance(node_data, dict):
                continue
            inputs = node_data.get("inputs", {}) or {}
            if str(node_data.get("class_type") or "") in {"VAEDecode", "VAEDecodeTiled"}:
                vae = inputs.get("vae")
                if self._is_valid_vae_source_ref(workflow, vae):
                    return vae
        return None

    def _repair_img2img_vae_refs(self, workflow: dict, vae_source=None) -> int:
        vae_source = vae_source or self._find_vae_source(workflow)
        if not vae_source:
            return 0
        repaired = 0
        for node_data in workflow.values():
            if not isinstance(node_data, dict):
                continue
            class_type = str(node_data.get("class_type") or "")
            if class_type not in {"VAEEncode", "VAEEncodeTiled", "VAEDecode", "VAEDecodeTiled"}:
                continue
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict) or "vae" not in inputs:
                continue
            if self._is_valid_vae_source_ref(workflow, inputs.get("vae")):
                continue
            inputs["vae"] = list(vae_source)
            repaired += 1
        return repaired

    def _count_unresolved_img2img_sources(self, workflow: dict) -> int:
        unresolved = 0
        for node_data in workflow.values():
            if not isinstance(node_data, dict):
                continue
            if str(node_data.get("class_type") or "") != "LoadImage":
                continue
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            if "__ASTRBOT_IMG2IMG_SOURCE__" in str(inputs.get("image") or ""):
                unresolved += 1
        return unresolved

    async def _upload_input_image(self, session: aiohttp.ClientSession, source_image_path: str) -> dict:
        image_path = Path(str(source_image_path or "")).expanduser()
        if not image_path.exists() or not image_path.is_file():
            raise FileNotFoundError(f"图生图源图片不存在: {source_image_path}")

        suffix = image_path.suffix or ".png"
        upload_name = f"astrbot_img2img_{uuid.uuid4().hex}{suffix}"
        content_type = mimetypes.guess_type(str(image_path))[0] or "application/octet-stream"
        form = aiohttp.FormData()
        with open(image_path, "rb") as fp:
            form.add_field("image", fp, filename=upload_name, content_type=content_type)
            form.add_field("type", "input")
            form.add_field("overwrite", "true")
            if self.img2img_upload_subfolder:
                form.add_field("subfolder", self.img2img_upload_subfolder)
            async with session.post(f"{self.url}/upload/image", data=form) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"上传图生图源图片失败: {resp.status} {text[:200]}")
                try:
                    data = json.loads(text) if text else {}
                except Exception:
                    data = {}

        name = str(data.get("name") or upload_name).strip()
        subfolder = str(data.get("subfolder") or self.img2img_upload_subfolder or "").strip().strip("/")
        return {"name": name, "subfolder": subfolder, "type": str(data.get("type") or "input")}

    def _append_prompt_text(self, current: str, extra: str) -> str:
        current = str(current or "").strip()
        extra = str(extra or "").strip()
        if not extra:
            return current
        if extra.lower() in current.lower():
            return current
        if not current:
            return extra
        return f"{current}, {extra}"

    def _append_img2img_negative_prompt(self, workflow: dict, extra_negative: str):
        extra_negative = str(extra_negative or "").strip()
        if not extra_negative:
            return 0

        target_nodes = []
        if self.neg_node_id and isinstance(workflow.get(self.neg_node_id), dict):
            target_nodes.append(workflow[self.neg_node_id])
        else:
            for node_data in workflow.values():
                if not isinstance(node_data, dict):
                    continue
                title = str((node_data.get("_meta") or {}).get("title") or "").lower()
                class_type = str(node_data.get("class_type") or "").lower()
                if "negative" in title and "text" in class_type:
                    target_nodes.append(node_data)

        updated = 0
        for node_data in target_nodes:
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            for key in ("text", "string", "negative", "text_negative", "prompt"):
                if key in inputs:
                    inputs[key] = self._append_prompt_text(inputs.get(key), extra_negative)
                    updated += 1
                    break
        return updated

    def _fill_prewired_img2img_source(self, workflow: dict, image_value: str) -> int:
        updated = 0
        for node_data in workflow.values():
            if not isinstance(node_data, dict):
                continue
            if str(node_data.get("class_type") or "") != "LoadImage":
                continue
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            title = str((node_data.get("_meta") or {}).get("title") or "").lower()
            current_image = str(inputs.get("image") or "")
            if "__ASTRBOT_IMG2IMG_SOURCE__" in current_image or "astrbot img2img source" in title:
                inputs["image"] = image_value
                updated += 1
        return updated

    def _set_sampler_denoise(self, workflow: dict, denoise: float) -> int:
        updated = 0
        for node_data in workflow.values():
            if not isinstance(node_data, dict):
                continue
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict) or "denoise" not in inputs:
                continue
            class_type = str(node_data.get("class_type") or "")
            if "Sampler" not in class_type and class_type not in {"KSampler", "KSamplerAdvanced"}:
                continue
            inputs["denoise"] = denoise
            updated += 1
        return updated

    def _set_controlnet_strength(self, workflow: dict, strength: float) -> int:
        strength = min(max(_safe_float(strength, 0.42), 0.0), 2.0)
        updated = 0
        for node_data in workflow.values():
            if not isinstance(node_data, dict):
                continue
            if str(node_data.get("class_type") or "") != "ControlNetApplyAdvanced":
                continue
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict) or "strength" not in inputs:
                continue
            inputs["strength"] = strength
            updated += 1
        return updated

    def _has_anima_lllite_control(self, workflow: dict) -> bool:
        for node_data in workflow.values():
            if isinstance(node_data, dict) and str(node_data.get("class_type") or "") == "AnimaLLLiteApply":
                return True
        return False

    def _apply_anima_img2img_reference_params(self, workflow: dict, options: dict, meta: dict) -> dict:
        options = options or {}
        control_strength = options.get("control_strength", self.img2img_character_replace_control_strength)
        control_end = options.get("control_end_percent", self.img2img_character_replace_control_end)
        preprocessor = str(options.get("preprocessor") or self.img2img_character_replace_preprocessor or "").strip()
        preprocessor_resolution = options.get(
            "preprocessor_resolution",
            self.img2img_character_replace_preprocessor_resolution,
        )
        scale_to_length = options.get("scale_to_length", self.img2img_character_replace_scale_to_length)

        control_strength = min(max(_safe_float(control_strength, self.img2img_character_replace_control_strength), 0.0), 2.0)
        control_end = min(max(_safe_float(control_end, self.img2img_character_replace_control_end), 0.0), 1.0)
        preprocessor_resolution = max(_safe_int(preprocessor_resolution, self.img2img_character_replace_preprocessor_resolution), 256)
        scale_to_length = max(_safe_int(scale_to_length, self.img2img_character_replace_scale_to_length), 512)

        updated_lllite = 0
        updated_preprocessor = 0
        updated_scale = 0
        for node_data in workflow.values():
            if not isinstance(node_data, dict):
                continue
            class_type = str(node_data.get("class_type") or "")
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            if class_type == "AnimaLLLiteApply":
                if "strength" in inputs:
                    inputs["strength"] = control_strength
                if "end_percent" in inputs:
                    inputs["end_percent"] = control_end
                if "preserve_wrapper" in inputs:
                    inputs["preserve_wrapper"] = True
                updated_lllite += 1
            elif class_type == "AIO_Preprocessor":
                if preprocessor and "preprocessor" in inputs:
                    inputs["preprocessor"] = preprocessor
                if "resolution" in inputs:
                    inputs["resolution"] = preprocessor_resolution
                updated_preprocessor += 1
            elif class_type == "LayerUtility: ImageScaleByAspectRatio V2":
                if "scale_to_length" in inputs:
                    inputs["scale_to_length"] = scale_to_length
                if "scale_to_side" in inputs:
                    inputs["scale_to_side"] = "longest"
                if "fit" in inputs:
                    inputs["fit"] = str(options.get("scale_fit") or inputs.get("fit") or "crop")
                updated_scale += 1

        if updated_lllite or updated_preprocessor or updated_scale:
            meta["img2img_control_strength"] = control_strength
            meta["img2img_control_end_percent"] = control_end
            meta["img2img_preprocessor"] = preprocessor
            meta["img2img_preprocessor_resolution"] = preprocessor_resolution
            meta["img2img_scale_to_length"] = scale_to_length
            meta["img2img_anima_reference_nodes"] = {
                "lllite": updated_lllite,
                "preprocessor": updated_preprocessor,
                "scale": updated_scale,
            }
            logger.info(
                "[ComfyUI] Anima 图生图参考参数已应用: "
                f"lllite={control_strength}, end={control_end}, "
                f"preprocessor={preprocessor or '-'}@{preprocessor_resolution}, scale={scale_to_length}"
            )
        return meta

    def _looks_like_cuda_oom(self, text: str) -> bool:
        lowered = str(text or "").lower()
        return (
            "outofmemory" in lowered
            or "out of memory" in lowered
            or "cuda oom" in lowered
            or "allocation on device" in lowered
            or "got an oom" in lowered
        )

    def _looks_like_cuda_runtime_failure(self, text: str) -> bool:
        lowered = str(text or "").lower()
        return (
            "cuda error: invalid argument" in lowered
            or "illegal memory access" in lowered
            or "device-side assert" in lowered
            or "cuda error: an illegal" in lowered
            or "cuda kernel errors might be asynchronously reported" in lowered
        )

    async def _free_comfy_memory(self, session: aiohttp.ClientSession, reason: str = "") -> bool:
        try:
            async with session.post(
                f"{self.url}/free",
                json={"unload_models": True, "free_memory": True},
            ) as resp:
                text = await resp.text()
                ok = resp.status < 400
                if ok:
                    logger.info(f"[ComfyUI] 已请求释放显存{f'({reason})' if reason else ''}")
                else:
                    logger.warning(f"[ComfyUI] 请求释放显存失败: {resp.status} {text[:160]}")
                await asyncio.sleep(0.35)
                return ok
        except Exception as e:
            logger.warning(f"[ComfyUI] 请求释放显存异常: {e}")
            return False

    def _generation_client_timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(
            total=None,
            connect=max(float(getattr(self, "comfyui_connect_timeout", DEFAULT_CONNECT_TIMEOUT) or DEFAULT_CONNECT_TIMEOUT), 3.0),
            sock_read=max(float(getattr(self, "comfyui_read_timeout", DEFAULT_READ_TIMEOUT) or DEFAULT_READ_TIMEOUT), 30.0),
        )

    def _history_request_timeout(self) -> aiohttp.ClientTimeout:
        value = max(float(getattr(self, "history_request_timeout", 30) or 30), 8.0)
        connect = min(max(float(getattr(self, "comfyui_connect_timeout", DEFAULT_CONNECT_TIMEOUT) or DEFAULT_CONNECT_TIMEOUT), 3.0), value)
        return aiohttp.ClientTimeout(total=value, connect=connect, sock_read=value)

    def _generation_poll_delay(self, poll_index: int) -> float:
        base = max(float(getattr(self, "poll_interval", 1.0) or 1.0), 0.2)
        cap = max(float(getattr(self, "poll_max_interval", 3.0) or 3.0), base)
        if poll_index < 20:
            return base
        if poll_index < 90:
            return min(base * 1.5, cap)
        return min(base * 2.5, cap)

    def _record_history_poll_failure(self, meta: dict, exc, prompt_id: str):
        meta["history_poll_failures"] = int(meta.get("history_poll_failures") or 0) + 1
        now = time.time()
        last_log = float(meta.get("_history_poll_last_log") or 0)
        if now - last_log >= 20:
            meta["_history_poll_last_log"] = now
            logger.info(f"[ComfyUI] history poll transient failure prompt_id={prompt_id}: {exc}")

    async def _fetch_prompt_history(self, session: aiohttp.ClientSession, prompt_id: str, meta: dict):
        try:
            async with session.get(f"{self.url}/history/{prompt_id}", timeout=self._history_request_timeout()) as h_resp:
                if h_resp.status != 200:
                    meta["history_last_status"] = h_resp.status
                    return None
                return await h_resp.json()
        except Exception as exc:
            self._record_history_poll_failure(meta, exc, prompt_id)
            return None

    def _queue_request_timeout(self) -> aiohttp.ClientTimeout:
        value = max(float(getattr(self, "queue_request_timeout", 8) or 8), 3.0)
        connect = min(max(float(getattr(self, "comfyui_connect_timeout", DEFAULT_CONNECT_TIMEOUT) or DEFAULT_CONNECT_TIMEOUT), 3.0), value)
        return aiohttp.ClientTimeout(total=value, connect=connect, sock_read=value)

    def _queue_payload_contains_prompt_id(self, value, prompt_id: str) -> bool:
        prompt_id = str(prompt_id or "").strip()
        if not prompt_id:
            return False
        if isinstance(value, dict):
            for key in ("prompt_id", "id"):
                if str(value.get(key) or "") == prompt_id:
                    return True
            return any(self._queue_payload_contains_prompt_id(item, prompt_id) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(self._queue_payload_contains_prompt_id(item, prompt_id) for item in value)
        return str(value or "") == prompt_id

    def _queue_snapshot_counts(self, queue_data: dict) -> tuple:
        if not isinstance(queue_data, dict):
            return 0, 0
        running = queue_data.get("queue_running") or queue_data.get("running") or []
        pending = queue_data.get("queue_pending") or queue_data.get("pending") or []
        return (
            len(running) if isinstance(running, (list, tuple)) else 0,
            len(pending) if isinstance(pending, (list, tuple)) else 0,
        )

    async def _fetch_queue_snapshot(self, session: aiohttp.ClientSession, meta: dict):
        try:
            async with session.get(f"{self.url}/queue", timeout=self._queue_request_timeout()) as resp:
                if resp.status != 200:
                    meta["queue_last_status"] = resp.status
                    return None
                data = await resp.json()
                running_count, pending_count = self._queue_snapshot_counts(data)
                meta["queue_running_count"] = running_count
                meta["queue_pending_count"] = pending_count
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            meta["queue_poll_failures"] = int(meta.get("queue_poll_failures") or 0) + 1
            last_log = float(meta.get("_queue_poll_last_log") or 0)
            now = time.time()
            if now - last_log >= 20:
                meta["_queue_poll_last_log"] = now
                logger.info(f"[ComfyUI] queue poll transient failure: {exc}")
            return None

    async def _check_prompt_disappeared_from_queue(
        self,
        session: aiohttp.ClientSession,
        prompt_id: str,
        meta: dict,
        submitted_at: float,
    ) -> str:
        grace = max(float(getattr(self, "queue_missing_grace_seconds", 45) or 45), 10.0)
        if time.time() - float(submitted_at or time.time()) < grace:
            return ""
        queue_data = await self._fetch_queue_snapshot(session, meta)
        if not isinstance(queue_data, dict):
            return ""
        if self._queue_payload_contains_prompt_id(queue_data, prompt_id):
            meta["queue_missing_confirmations_seen"] = 0
            return ""
        seen = int(meta.get("queue_missing_confirmations_seen") or 0) + 1
        meta["queue_missing_confirmations_seen"] = seen
        required = max(int(getattr(self, "queue_missing_confirmations", 3) or 3), 1)
        running_count, pending_count = self._queue_snapshot_counts(queue_data)
        logger.warning(
            f"[ComfyUI] prompt_id 不在远端队列/历史中: prompt_id={prompt_id} "
            f"missing={seen}/{required} queue_running={running_count} queue_pending={pending_count}"
        )
        if seen < required:
            return ""
        meta["queue_prompt_missing"] = True
        meta["queue_missing_confirmations_required"] = required
        return (
            "ComfyUI 远端队列中找不到本次 prompt_id，且 history 还没有结果；"
            "任务可能被 ComfyUI 清空、重启或自定义节点启动刷新打断。请确认 ComfyUI 控制台状态后重试。"
        )

    async def _download_image_bytes(self, session: aiohttp.ClientSession, img_info: dict, meta: dict):
        params = urlencode({
            "filename": img_info.get("filename", ""),
            "subfolder": img_info.get("subfolder", ""),
            "type": img_info.get("type", "output"),
        })
        img_url = f"{self.url}/view?{params}"
        attempts = max(int(getattr(self, "view_download_retries", 3) or 3), 1)
        last_error = ""
        for attempt in range(attempts):
            try:
                async with session.get(img_url, timeout=self._generation_client_timeout()) as img_res:
                    meta["image_filename"] = img_info.get("filename", "")
                    if img_res.status == 200:
                        return await img_res.read(), ""
                    last_error = f"下载图片失败: {img_res.status}"
            except Exception as exc:
                last_error = f"下载图片异常: {exc}"
            if attempt + 1 < attempts:
                await asyncio.sleep(min(0.6 * (attempt + 1), 2.0))
        return None, last_error or "下载图片失败"

    async def _queue_workflow(self, session: aiohttp.ClientSession, workflow: dict, client_id: str, meta: dict, neutralize_lora_on_validation_error: bool = True) -> tuple:
        if (
            isinstance(meta, dict)
            and meta.get("multi_lora_hooks")
            and not meta.get("multi_lora_debug_workflow_path")
        ):
            self._save_multi_lora_debug_workflow(workflow, meta, label=meta.get("multi_lora_mode_effective") or "regional")
        payload = {"prompt": workflow, "client_id": client_id}
        attempts = 2
        for attempt in range(attempts):
            try:
                async with session.post(f"{self.url}/prompt", json=payload, timeout=self._generation_client_timeout()) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        missing_model_error = self._format_validation_missing_model_error(text, workflow)
                        if resp.status == 400 and missing_model_error:
                            logger.warning(
                                f"[ComfyUI] 工作流校验失败：缺少模型文件，停止 LoRA 中和重试: {text[:300]}"
                            )
                            meta["first_validation_error"] = text[:500]
                            meta["validation_missing_model_error"] = missing_model_error
                            return "", missing_model_error
                        if (
                            neutralize_lora_on_validation_error
                            and resp.status == 400
                            and "prompt_outputs_failed_validation" in text
                            and not meta.get("lora_retry_neutralized")
                        ):
                            logger.warning(f"[ComfyUI] 工作流校验失败，自动中和 LoRA 节点后重试一次: {text[:300]}")
                            meta["lora_retry_neutralized"] = True
                            meta["first_validation_error"] = text[:500]
                            self._neutralize_lora_nodes(
                                workflow,
                                meta,
                                "ComfyUI 校验失败，已自动中和 LoRA 节点并重试",
                                valid_loras=self._extract_validation_lora_names(text),
                                target_node_ids=self._extract_validation_lora_node_ids(text),
                            )
                            payload = {"prompt": workflow, "client_id": client_id}
                            async with session.post(f"{self.url}/prompt", json=payload, timeout=self._generation_client_timeout()) as retry_resp:
                                if retry_resp.status != 200:
                                    retry_text = await retry_resp.text()
                                    retry_missing_model_error = self._format_validation_missing_model_error(retry_text, workflow)
                                    if retry_missing_model_error:
                                        meta["validation_missing_model_error"] = retry_missing_model_error
                                        return "", retry_missing_model_error
                                    return "", f"连接 ComfyUI 失败: {retry_resp.status} {retry_text[:300]}"
                                res_json = await retry_resp.json()
                        else:
                            return "", f"连接 ComfyUI 失败: {resp.status} {text[:300]}"
                    else:
                        res_json = await resp.json()
                    prompt_id = res_json.get("prompt_id")
                    if not prompt_id:
                        return "", "ComfyUI 未返回 prompt_id"
                    meta["prompt_id"] = prompt_id
                    meta["queue_submit_attempts"] = attempt + 1
                    return prompt_id, ""
            except Exception as e:
                if attempt + 1 < attempts:
                    meta["queue_submit_retry"] = str(e)
                    await asyncio.sleep(1.2)
                    continue
                return "", f"请求报错: {str(e)}"

    def _validation_model_target_hint(self, field_name: str, filename: str, class_type: str = "") -> str:
        field = str(field_name or "").strip().lower()
        name = str(filename or "").strip().lower()
        cls = str(class_type or "").strip().lower()
        if field in {"lora_name", "lora"}:
            return ""
        if field == "vae_name" or "vaeloader" in cls:
            return "models\\vae"
        if field == "clip_name" or cls == "cliploader":
            return "models\\text_encoders"
        if field == "unet_name" or "unetloader" in cls:
            return "models\\unet" if name.endswith(".gguf") else "models\\diffusion_models"
        if field == "ckpt_name":
            return "models\\checkpoints"
        if field in {"control_net_name", "controlnet_name"}:
            return "models\\controlnet"
        return ""

    def _format_validation_missing_model_error(self, error_text: str, workflow: dict) -> str:
        text = str(error_text or "")
        if "prompt_outputs_failed_validation" not in text or "value_not_in_list" not in text:
            return ""
        try:
            data = json.loads(text)
        except Exception:
            return ""
        node_errors = data.get("node_errors", {}) if isinstance(data, dict) else {}
        if not isinstance(node_errors, dict):
            return ""

        missing = []
        seen = set()
        for node_id, payload in node_errors.items():
            node = workflow.get(str(node_id), {}) if isinstance(workflow, dict) else {}
            class_type = str((node or {}).get("class_type") or "")
            errors = payload.get("errors") if isinstance(payload, dict) else []
            if not isinstance(errors, list):
                continue
            for item in errors:
                if not isinstance(item, dict) or item.get("type") != "value_not_in_list":
                    continue
                details = str(item.get("details") or item.get("message") or "")
                matches = re.findall(r"([A-Za-z0-9_]+_name)\s*:\s*'([^']+)'", details)
                for field, filename in matches:
                    target = self._validation_model_target_hint(field, filename, class_type)
                    if not target:
                        continue
                    marker = (str(node_id), field, filename, target)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    missing.append({
                        "node_id": str(node_id),
                        "class_type": class_type or "Unknown",
                        "field": field,
                        "filename": filename,
                        "target": target,
                    })

        if not missing:
            return ""

        lines = [
            "ComfyUI 工作流校验失败：缺少模型文件，不是 LoRA 问题，已停止 LoRA 中和重试。",
            "缺失/未被 ComfyUI 识别的文件：",
        ]
        for item in missing[:8]:
            lines.append(
                f"- 节点 {item['node_id']} {item['class_type']}：{item['filename']} -> {item['target']}"
            )
        if len(missing) > 8:
            lines.append(f"- 其余 {len(missing) - 8} 个缺失项已省略。")

        wan_names = " ".join(item["filename"].lower() for item in missing)
        if any(token in wan_names for token in ("wan2.1", "wan2.2", "umt5", ".gguf")):
            lines.extend([
                "",
                "处理方法：运行 scripts\\astrbot_comfyui_setup.bat，主菜单选 5 修复/下载 Wan 视频模型；如果 Wan 节点也缺，选 6。",
                "下载完成后必须重启 ComfyUI，让模型列表重新扫描。",
                "如果文件已经存在但仍不在列表，多半是放错目录、文件损坏，或 ComfyUI 没重启；脚本的深度检测会显示 [缺失]/[损坏]。",
            ])
        else:
            lines.extend([
                "",
                "处理方法：把文件放到上面标出的 ComfyUI/models 对应目录，然后重启 ComfyUI。",
                "也可以运行 scripts\\astrbot_comfyui_setup.bat 的深度检测/修复菜单自动补齐。",
            ])
        return "\n".join(lines)

    def _looks_like_multi_lora_regional_failure(self, detail: str) -> bool:
        text = str(detail or "").lower()
        if not text:
            return False
        markers = (
            "createhooklora",
            "setcliphooks",
            "patch_hook_weight_to_device",
            "modelpatcherdynamic",
            "pairconditioningsetpropertiesandcombine",
            "conditioningsetpropertiesandcombine",
            "hookgroup",
            "forced_hooks",
            "too many values to unpack",
        )
        return any(marker in text for marker in markers)

    async def _reload_multi_lora_safe_chain_after_failure(
        self,
        session: aiohttp.ClientSession,
        prompt: str,
        detail: str,
        lora_refresh_meta: dict = None,
    ):
        previous_mode = self._normalize_multi_lora_mode(getattr(self, "runtime_multi_lora_mode", None))
        if previous_mode != "safe_chain":
            return None, {
                "multi_lora_execution_fallback_blocked": True,
                "multi_lora_execution_fallback_reason": (
                    "Safe Chain retry is disabled unless multi_lora_mode is explicitly safe_chain"
                ),
                "multi_lora_previous_mode": str(previous_mode or ""),
            }
        fallback_name = str(getattr(self, "runtime_fallback_wf_filename", "") or "").strip()
        if not fallback_name:
            return None, {}
        fallback_path = self.workflow_dir / fallback_name
        if not fallback_path.exists():
            return None, {"multi_lora_execution_fallback_failed": f"{fallback_name} not found"}

        requested_name = str(getattr(self, "wf_filename", "") or "")
        self.wf_filename = fallback_name
        self.runtime_wf_filename = fallback_name
        self.workflow_path = fallback_path
        self.runtime_multi_lora_mode = "safe_chain"
        workflow = self._load_workflow()
        new_meta = await self._inject_params(workflow, prompt, session=session)
        if lora_refresh_meta:
            new_meta.update(lora_refresh_meta)
        new_meta["workflow_requested"] = requested_name
        new_meta["workflow_fallback"] = fallback_name
        new_meta["multi_lora_execution_fallback"] = True
        new_meta["multi_lora_execution_fallback_mode"] = "safe_chain"
        new_meta["multi_lora_execution_fallback_reason"] = str(detail or "")[:500]
        new_meta["multi_lora_previous_mode"] = str(previous_mode or "")
        logger.warning(
            f"[ComfyUI] Multi-LoRA regional execution failed; "
            f"retrying Safe Chain workflow {requested_name} -> {fallback_name}: {str(detail or '')[:220]}"
        )
        return workflow, new_meta

    def _apply_illustrious_oom_fallback_params(self, workflow: dict, meta: dict) -> int:
        changed = 0
        for node_data in workflow.values():
            if not isinstance(node_data, dict):
                continue
            class_type = str(node_data.get("class_type") or "")
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            if class_type == "LayerUtility: ImageScaleByAspectRatio V2" and "scale_to_length" in inputs:
                old = _safe_int(inputs.get("scale_to_length"), 896)
                inputs["scale_to_length"] = min(old, 768)
                changed += int(inputs["scale_to_length"] != old)
            elif class_type == "AIO_Preprocessor" and "resolution" in inputs:
                old = _safe_int(inputs.get("resolution"), 640)
                inputs["resolution"] = min(old, 512)
                changed += int(inputs["resolution"] != old)
            elif class_type == "ControlNetApplyAdvanced":
                if "strength" in inputs:
                    old = _safe_float(inputs.get("strength"), 0.22)
                    inputs["strength"] = min(old, 0.16)
                    changed += int(inputs["strength"] != old)
                if "end_percent" in inputs:
                    old = _safe_float(inputs.get("end_percent"), 0.7)
                    inputs["end_percent"] = min(old, 0.6)
                    changed += int(inputs["end_percent"] != old)
            elif class_type == "LatentUpscaleBy" and "scale_by" in inputs:
                old = _safe_float(inputs.get("scale_by"), 1.0)
                inputs["scale_by"] = min(old, 1.0)
                changed += int(inputs["scale_by"] != old)
            elif class_type in {"KSampler", "KSamplerAdvanced"} or "Sampler" in class_type:
                if "steps" in inputs:
                    old = _safe_int(inputs.get("steps"), 18)
                    inputs["steps"] = min(old, 14)
                    changed += int(inputs["steps"] != old)
                if "cfg" in inputs:
                    old = _safe_float(inputs.get("cfg"), 5.6)
                    inputs["cfg"] = min(old, 5.3)
                    changed += int(inputs["cfg"] != old)
        if changed:
            meta["oom_fallback_low_vram"] = True
            logger.warning(f"[ComfyUI] OOM 后已切换 Illustrious 图生图低显存重试参数: {changed} 项")
        return changed

    def _apply_illustrious_img2img_params(self, workflow: dict, options: dict, meta: dict, base_denoise: float) -> int:
        options = options or {}
        base_denoise = min(max(_safe_float(base_denoise, 0.6), 0.01), 1.0)
        refine_denoise = min(max(_safe_float(options.get("refine_denoise"), 0.0), 0.0), 1.0)
        control_strength = min(max(_safe_float(options.get("control_strength"), 0.22), 0.0), 2.0)
        scale_to_length = max(_safe_int(options.get("scale_to_length"), 896), 512)
        preprocessor_resolution = max(_safe_int(options.get("preprocessor_resolution"), 640), 256)
        latent_upscale_by = min(max(_safe_float(options.get("latent_upscale_by"), 1.0), 1.0), 2.0)

        sampler_updates = 0
        control_updates = 0
        scale_updates = 0
        preprocessor_updates = 0
        latent_upscale_updates = 0
        for node_data in workflow.values():
            if not isinstance(node_data, dict):
                continue
            class_type = str(node_data.get("class_type") or "")
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            title = str((node_data.get("_meta") or {}).get("title") or "").lower()

            if class_type in {"KSampler", "KSamplerAdvanced"} or "Sampler" in class_type:
                if "denoise" in inputs:
                    inputs["denoise"] = refine_denoise if "refine" in title else base_denoise
                    sampler_updates += 1
                continue

            if class_type == "ControlNetApplyAdvanced":
                if "strength" in inputs:
                    inputs["strength"] = control_strength
                if "end_percent" in inputs:
                    inputs["end_percent"] = min(max(_safe_float(inputs.get("end_percent"), 1.0), 0.0), 1.0)
                control_updates += 1
                continue

            if class_type == "LayerUtility: ImageScaleByAspectRatio V2":
                if "scale_to_length" in inputs:
                    inputs["scale_to_length"] = scale_to_length
                    scale_updates += 1
                if "scale_to_side" in inputs:
                    inputs["scale_to_side"] = "longest"
                if "fit" in inputs:
                    inputs["fit"] = str(options.get("scale_fit") or inputs.get("fit") or "crop")
                continue

            if class_type == "AIO_Preprocessor":
                if "resolution" in inputs:
                    inputs["resolution"] = preprocessor_resolution
                    preprocessor_updates += 1
                continue

            if class_type == "LatentUpscaleBy":
                if "scale_by" in inputs:
                    inputs["scale_by"] = latent_upscale_by
                    latent_upscale_updates += 1

        meta["img2img_refine_denoise"] = refine_denoise
        meta["img2img_control_strength"] = control_strength
        meta["img2img_scale_to_length"] = scale_to_length
        meta["img2img_preprocessor_resolution"] = preprocessor_resolution
        meta["img2img_latent_upscale_by"] = latent_upscale_by
        meta["img2img_illustrious_nodes"] = {
            "samplers": sampler_updates,
            "controlnet": control_updates,
            "scale": scale_updates,
            "preprocessor": preprocessor_updates,
            "latent_upscale": latent_upscale_updates,
        }
        logger.info(
            "[ComfyUI] Illustrious 图生图参数已应用: "
            f"base_denoise={base_denoise}, refine_denoise={refine_denoise}, "
            f"depth_control={control_strength}, scale_to_length={scale_to_length}, "
            f"preprocessor={preprocessor_resolution}, latent_upscale={latent_upscale_by}"
        )
        return sampler_updates

    def _inject_img2img_params(self, workflow: dict, upload_info: dict, meta: dict, options: dict = None):
        if not self.img2img_enabled:
            raise RuntimeError("图生图已在 WebUI 配置中关闭")
        options = options or {}
        denoise = options.get("denoise")
        denoise = min(max(_safe_float(denoise, self.img2img_denoise), 0.01), 1.0)
        extra_negative = str(options.get("negative_prompt") or "").strip()
        if not extra_negative and options.get("mode") == "character_replace":
            extra_negative = self.img2img_negative_prompt

        vae_source = self._find_vae_source(workflow)
        if not vae_source:
            raise RuntimeError("当前工作流未找到可用 VAE，无法自动切换图生图")
        vae_repairs = self._repair_img2img_vae_refs(workflow, vae_source)
        if vae_repairs:
            logger.warning(f"[ComfyUI] 已修复图生图 VAE 接线: {vae_repairs} 个节点 -> {vae_source}")

        image_name = str(upload_info.get("name") or "").strip()
        subfolder = str(upload_info.get("subfolder") or "").strip().strip("/")
        if not image_name:
            raise RuntimeError("ComfyUI 未返回上传图片名，无法图生图")
        image_value = f"{subfolder}/{image_name}" if subfolder else image_name

        prewired_sources = self._fill_prewired_img2img_source(workflow, image_value)
        if prewired_sources:
            unresolved_sources = self._count_unresolved_img2img_sources(workflow)
            if unresolved_sources:
                raise RuntimeError(
                    f"图生图工作流仍有 {unresolved_sources} 个源图占位符未替换，"
                    "请确认使用新版 img2img_illustrious_api.json 或刷新插件工作流"
                )
            keep_workflow_denoise = options.get("mode") == "character_replace" and self._has_anima_lllite_control(workflow)
            is_illustrious_img2img = options.get("mode") == "illustrious_img2img"
            if keep_workflow_denoise:
                self._apply_anima_img2img_reference_params(workflow, options, meta)
            if is_illustrious_img2img:
                sampler_denoise_nodes = self._apply_illustrious_img2img_params(workflow, options, meta, denoise)
            else:
                sampler_denoise_nodes = 0 if keep_workflow_denoise else self._set_sampler_denoise(workflow, denoise)
            mask_sampler_nodes = self._attach_img2img_latent_noise_mask(
                workflow,
                options.get("inpaint_mask_info"),
                meta,
                options=options,
            )
            negative_nodes = self._append_img2img_negative_prompt(workflow, extra_negative)
            meta["mode"] = "img2img"
            if options.get("mode"):
                meta["img2img_mode_hint"] = str(options.get("mode"))
            meta["source_image"] = image_value
            meta["img2img_denoise"] = denoise
            meta["img2img_prewired_sources"] = prewired_sources
            if vae_repairs:
                meta["img2img_vae_repair_nodes"] = vae_repairs
            meta["img2img_rewired_samplers"] = 0
            meta["img2img_sampler_denoise_nodes"] = sampler_denoise_nodes
            if mask_sampler_nodes:
                meta["img2img_inpaint_mask_sampler_nodes"] = mask_sampler_nodes
            meta["img2img_negative_nodes"] = negative_nodes
            if keep_workflow_denoise:
                meta["img2img_workflow_sampler_denoise_kept"] = True
            logger.info(
                f"[ComfyUI] 图生图已启用预接线工作流: {image_value} | "
                f"denoise={denoise} | source_nodes={prewired_sources} | "
                f"sampler_denoise_nodes={sampler_denoise_nodes} | negative_nodes={negative_nodes}"
                f"{' | kept_workflow_sampler_denoise=true' if keep_workflow_denoise else ''}"
            )
            return meta

        load_id = self._next_node_id(workflow)
        workflow[load_id] = {
            "inputs": {"image": image_value},
            "class_type": "LoadImage",
            "_meta": {"title": "AstrBot Img2Img Source"},
        }
        encode_id = self._next_node_id(workflow)
        workflow[encode_id] = {
            "inputs": {"pixels": [load_id, 0], "vae": vae_source},
            "class_type": "VAEEncode",
            "_meta": {"title": "AstrBot Img2Img VAE Encode"},
        }

        latent_ref = [encode_id, 0]
        mask_sampler_nodes = 0
        if options.get("inpaint_mask_info"):
            mask_sampler_nodes = self._attach_img2img_latent_noise_mask(
                workflow,
                options.get("inpaint_mask_info"),
                meta,
                options=options,
                latent_ref_override=latent_ref,
            )
            mask_info = meta.get("img2img_inpaint_mask") if isinstance(meta.get("img2img_inpaint_mask"), dict) else {}
            noise_node = str(mask_info.get("noise_mask_node") or "").strip()
            if noise_node:
                latent_ref = [noise_node, 0]

        rewired = 0
        for node_id, node_data in workflow.items():
            if not isinstance(node_data, dict):
                continue
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict) or "latent_image" not in inputs:
                continue
            class_type = str(node_data.get("class_type") or "")
            if "Sampler" not in class_type and class_type not in {"KSampler", "KSamplerAdvanced"}:
                continue
            inputs["latent_image"] = latent_ref
            if "denoise" in inputs:
                inputs["denoise"] = denoise
            rewired += 1

        if not rewired:
            raise RuntimeError("当前工作流未找到可替换的采样器 latent_image 输入，无法图生图")

        negative_nodes = self._append_img2img_negative_prompt(workflow, extra_negative)
        meta["mode"] = "img2img"
        if options.get("mode"):
            meta["img2img_mode_hint"] = str(options.get("mode"))
        meta["source_image"] = image_value
        meta["img2img_denoise"] = denoise
        meta["img2img_rewired_samplers"] = rewired
        if mask_sampler_nodes:
            meta["img2img_inpaint_mask_sampler_nodes"] = mask_sampler_nodes
        meta["img2img_negative_nodes"] = negative_nodes
        if vae_repairs:
            meta["img2img_vae_repair_nodes"] = vae_repairs
        logger.info(f"[ComfyUI] 图生图已启用: {image_value} | denoise={denoise} | samplers={rewired} | negative_nodes={negative_nodes}")
        return meta

    def _workflow_path_for_filename(self, workflow_filename: str) -> Path:
        name = str(workflow_filename or self.wf_filename or "").strip()
        if not name:
            raise FileNotFoundError("工作流文件名为空")
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = self.workflow_dir / candidate.name
        return candidate

    def _load_workflow_file(self, workflow_filename: str) -> tuple:
        workflow_path = self._workflow_path_for_filename(workflow_filename)
        if not workflow_path.exists():
            raise FileNotFoundError(f"工作流文件不存在: {workflow_path}")
        with open(workflow_path, "r", encoding="utf-8") as f:
            return json.load(f), workflow_path

    def _resolve_staged_text_node_id(self, workflow: dict, preferred_node_id: str, negative: bool = False) -> str:
        preferred = str(preferred_node_id or "").strip()
        if preferred and isinstance(workflow.get(preferred), dict):
            return preferred

        first_text_node = ""
        for node_id, node_data in self._find_nodes_by_class_type(workflow, "CLIPTextEncode"):
            if not first_text_node:
                first_text_node = str(node_id)
            title = str((node_data.get("_meta") or {}).get("title") or "").lower()
            inputs = node_data.get("inputs", {}) if isinstance(node_data.get("inputs"), dict) else {}
            text = str(inputs.get("text") or inputs.get("string") or "").lower()
            looks_negative = any(marker in f"{title} {text}" for marker in ("negative", "neg prompt", "负面", "反向"))
            if negative and looks_negative:
                return str(node_id)
            if not negative and not looks_negative:
                return str(node_id)
        return "" if negative else first_text_node

    def _append_staged_negative_prompt(self, workflow: dict, extra_negative: str, preferred_node_id: str = "12") -> str:
        extra_negative = str(extra_negative or "").strip()
        if not extra_negative:
            return ""
        node_id = self._resolve_staged_text_node_id(workflow, preferred_node_id, negative=True)
        node = workflow.get(str(node_id)) if node_id else None
        if not isinstance(node, dict):
            return ""
        inputs = node.setdefault("inputs", {})
        for key in ("text", "string", "negative", "text_negative", "prompt"):
            if key in inputs:
                inputs[key] = self._append_prompt_text(inputs.get(key), extra_negative)
                return key
        inputs["text"] = extra_negative
        return "text"

    def _staged_multi_lora_base_negative_prompt(self, negative_prompt: str) -> str:
        removed_markers = {
            "multiple people",
            "duplicate character",
            "duplicate body",
            "cloned face",
            "cloned body",
            "identical duplicate",
            "repeated character",
        }
        kept = []
        for tag in re.split(r"[,，;；]", str(negative_prompt or "")):
            tag = tag.strip()
            if not tag:
                continue
            marker = re.sub(r"[^a-z0-9]+", "", tag.lower())
            if any(re.sub(r"[^a-z0-9]+", "", item) in marker for item in removed_markers):
                continue
            kept.append(tag)
        base_negative = ", ".join(kept)
        return self._append_prompt_text(
            base_negative,
            (
                "solo, single person, only one person, one body, one head, "
                "merged bodies, fused bodies, shared body, shared torso, conjoined, "
                "single centered body, one centered subject, merged face, fused face, "
                "split face, vertical split face, half-and-half character, color split down face"
            ),
        )

    def _staged_multi_lora_base_scene_prompt(self, prompt: str) -> str:
        exact_markers = {
            "1girl", "1boy", "2girls", "2boys", "3girls", "3boys",
            "girl", "girls", "boy", "boys", "person", "people",
            "character", "characters",
            "head", "heads", "torso", "torsos", "body", "bodies",
            "face", "faces", "left", "right", "center", "middle",
            "separate", "separated", "distinct", "multiple", "single",
            "solo", "composition", "layout", "placeholder",
        }
        count_substrings = (
            "silhouette", "leftperson", "rightperson", "centerperson",
            "sidebyside", "separatepeople", "separateheads", "separatetorsos",
            "separatedbodies", "separatedfaces", "multiplecharacters",
            "distinctbody", "clearseparatedcomposition", "layoutplaceholder",
            "nodetailedface", "nodetailedhair",
        )
        kept = []
        for tag in re.split(r"[,，;；]", str(prompt or "")):
            tag = tag.strip()
            if not tag:
                continue
            marker = re.sub(r"[^a-z0-9]+", "", tag.lower())
            if not marker:
                continue
            if marker in exact_markers or any(item in marker for item in count_substrings):
                continue
            kept.append(tag)
        scene_prompt = ", ".join(kept[:36]).strip()
        return self._append_prompt_text(
            scene_prompt,
            "shared environment, coherent lighting, coherent perspective",
        )

    def _staged_multi_lora_base_region_prompt(self, prompt: str, slot: int, count: int) -> str:
        count = max(2, min(3, _safe_int(count, 2)))
        if count == 2:
            positions = ["left person", "right person"]
            anchors = ["on the left side", "on the right side"]
        else:
            positions = ["left person", "center person", "right person"]
            anchors = ["on the left side", "in the center", "on the right side"]
        slot = max(0, min(_safe_int(slot, 0), count - 1))

        skipped_markers = {
            "solo", "singlecharacter", "singleperson", "onlyoneperson",
        }
        cleaned = []
        for tag in re.split(r"[,，;；]", str(prompt or "")):
            tag = tag.strip()
            if not tag:
                continue
            marker = re.sub(r"[^a-z0-9]+", "", tag.lower())
            if marker in skipped_markers:
                continue
            cleaned.append(tag)

        region_anchor = ", ".join(x for x in [
            "1girl",
            positions[slot],
            anchors[slot],
            "one separate person in this masked region",
            "own head",
            "own torso",
            "own body",
            "two separate people visible" if count == 2 else f"{count} separate people visible",
            "not fused with the other person",
        ] if x)
        return self._append_prompt_text(
            region_anchor,
            ", ".join(cleaned[:40]),
        )

    async def _apply_staged_multi_lora_base_regional_conditioning(
        self,
        session: aiohttp.ClientSession,
        workflow: dict,
        meta: dict,
        options: dict = None,
    ) -> bool:
        options = dict(options or {})
        if not options.get("staged_multi_lora_base"):
            return False

        raw_prompts = options.get("base_region_prompts") or options.get("region_prompts") or []
        if not isinstance(raw_prompts, (list, tuple)):
            raw_prompts = []
        requested_count = _safe_int(options.get("base_region_count"), len(raw_prompts) or 0)
        count = max(2, min(3, requested_count or len(raw_prompts or []) or 2))

        prompt_node_id = self._resolve_staged_text_node_id(workflow, "11", negative=False)
        prompt_node = workflow.get(str(prompt_node_id)) if prompt_node_id else None
        if not isinstance(prompt_node, dict):
            meta["staged_base_regional_warning"] = "staged base regional conditioning skipped: missing positive prompt node"
            return False
        prompt_inputs = prompt_node.setdefault("inputs", {})
        clip_ref = prompt_inputs.get("clip")
        if not isinstance(clip_ref, list) or len(clip_ref) != 2:
            _model_ref, clip_ref = self._find_primary_generation_refs(workflow)
        if not isinstance(clip_ref, list) or len(clip_ref) != 2:
            meta["staged_base_regional_warning"] = "staged base regional conditioning skipped: missing CLIP reference"
            return False

        raw_global_prompt = str(prompt_inputs.get("text") or prompt_inputs.get("string") or "")
        scene_prompt = self._staged_multi_lora_base_scene_prompt(raw_global_prompt)
        if scene_prompt:
            prompt_inputs["text"] = scene_prompt

        width, height = self._get_workflow_latent_size(workflow)
        width = _safe_int(options.get("regional_mask_width"), width)
        height = _safe_int(options.get("regional_mask_height"), height)
        overlap = _safe_float(options.get("regional_mask_overlap"), 0.06)
        blur = _safe_float(options.get("regional_mask_blur"), 6.0)
        mask_infos = await self._upload_split_masks(
            session,
            width,
            height,
            count,
            overlap_ratio=overlap,
            blur_radius=blur,
            binary=False,
            debug_label="staged_base_regional",
        )
        if len(mask_infos) < count or not all(mask_infos[:count]):
            meta["staged_base_regional_warning"] = "staged base regional masks upload failed; using scene-only global base"
            return False

        current_positive_ref = [str(prompt_node_id), 0]
        prompt_nodes = []
        setmask_nodes = []
        combine_nodes = []
        mask_nodes = []
        for idx in range(count):
            raw_region_prompt = str(raw_prompts[idx] if idx < len(raw_prompts) else "").strip()
            region_prompt = self._staged_multi_lora_base_region_prompt(raw_region_prompt, idx, count)

            region_prompt_id = self._next_node_id(workflow)
            workflow[region_prompt_id] = {
                "inputs": {
                    "text": region_prompt,
                    "clip": copy.deepcopy(clip_ref),
                },
                "class_type": "CLIPTextEncode",
                "_meta": {"title": f"AstrBot Staged Base Regional Prompt {idx + 1}"},
            }

            mask_info = mask_infos[idx]
            load_id = self._next_node_id(workflow)
            workflow[load_id] = {
                "inputs": {"image": mask_info["image_value"]},
                "class_type": "LoadImage",
                "_meta": {"title": f"AstrBot Staged Base Region Mask {idx + 1}"},
            }
            mask_id = self._next_node_id(workflow)
            workflow[mask_id] = {
                "inputs": {"image": [str(load_id), 0], "channel": "red"},
                "class_type": "ImageToMask",
                "_meta": {"title": f"AstrBot Staged Base Region Mask Convert {idx + 1}"},
            }

            setmask_id = self._next_node_id(workflow)
            workflow[setmask_id] = {
                "inputs": {
                    "conditioning": [str(region_prompt_id), 0],
                    "mask": [str(mask_id), 0],
                    "strength": 1.0,
                    "set_cond_area": "default",
                },
                "class_type": "ConditioningSetMask",
                "_meta": {"title": f"AstrBot Staged Base ConditioningSetMask {idx + 1}"},
            }

            combine_id = self._next_node_id(workflow)
            workflow[combine_id] = {
                "inputs": {
                    "conditioning_1": copy.deepcopy(current_positive_ref),
                    "conditioning_2": [str(setmask_id), 0],
                },
                "class_type": "ConditioningCombine",
                "_meta": {"title": f"AstrBot Staged Base ConditioningCombine {idx + 1}"},
            }
            current_positive_ref = [str(combine_id), 0]
            prompt_nodes.append(str(region_prompt_id))
            setmask_nodes.append(str(setmask_id))
            combine_nodes.append(str(combine_id))
            mask_nodes.append({
                "slot": idx + 1,
                "count": count,
                "load_node": str(load_id),
                "mask_node": str(mask_id),
                "image": mask_info["image_value"],
            })

        sampler_count = self._set_sampler_positive_ref(workflow, current_positive_ref)
        meta["staged_base_regional_conditioning"] = True
        meta["staged_base_regional_count"] = count
        meta["staged_base_global_scene_prompt"] = scene_prompt
        meta["staged_base_region_prompt_nodes"] = prompt_nodes
        meta["staged_base_region_setmask_nodes"] = setmask_nodes
        meta["staged_base_region_combine_nodes"] = combine_nodes
        meta["staged_base_region_masks"] = mask_nodes
        meta["staged_base_region_sampler_count"] = sampler_count
        meta["staged_base_region_positive_ref"] = current_positive_ref
        logger.info(
            "[ComfyUI] staged multi-LoRA base regional conditioning enabled: "
            f"count={count} samplers={sampler_count} final_positive={current_positive_ref}"
        )
        self._save_multi_lora_debug_workflow(workflow, meta, label="staged_base_regional")
        return True

    def _inject_staged_prompt_and_seed(self, workflow: dict, prompt: str, meta: dict, options: dict = None) -> dict:
        options = dict(options or {})
        prompt_node_id = self._resolve_staged_text_node_id(workflow, "11", negative=False)
        if not prompt_node_id:
            raise ValueError("staged 工作流未找到正面提示词节点")
        if not self._inject_text_into_node(workflow, prompt_node_id, str(prompt or "").strip()):
            raise ValueError(f"staged 工作流无法写入正面提示词节点 {prompt_node_id}")

        meta["input_id"] = prompt_node_id
        meta["prompt_key"] = "text"
        negative_prompt = str(getattr(self, "neg_prompt", "") or "")
        if options.get("staged_multi_lora_base"):
            negative_prompt = self._staged_multi_lora_base_negative_prompt(negative_prompt)
            meta["staged_multi_lora_base_negative_sanitized"] = True
        negative_key = self._append_staged_negative_prompt(workflow, negative_prompt, preferred_node_id="12")
        if options.get("staged_multi_lora_base"):
            extra_base_negative = str(options.get("negative_prompt") or "").strip()
            if extra_base_negative:
                self._append_staged_negative_prompt(workflow, extra_base_negative, preferred_node_id="12")
        if negative_key:
            meta["negative_key"] = negative_key

        self._apply_generation_seed(
            workflow,
            meta,
            seed=options.get("seed"),
            seed_source=options.get("seed_source"),
        )
        return meta

    def _inject_staged_lora(self, workflow: dict, lora_name: str, strength_model: float, strength_clip: float, meta: dict) -> dict:
        lora_name = str(lora_name or "").strip()
        target_nodes = []
        node_99 = workflow.get("99")
        if isinstance(node_99, dict):
            target_nodes.append(("99", node_99))
        elif lora_name:
            target_nodes.extend(self._find_character_lora_nodes(workflow)[:1])

        if lora_name and not target_nodes:
            raise RuntimeError("staged 工作流未找到角色 LoRA 节点 99")

        node_ids = []
        strength_model = _safe_float(strength_model, 0.8)
        strength_clip = _safe_float(strength_clip, 0.8)
        for node_id, node_data in target_nodes:
            inputs = node_data.setdefault("inputs", {})
            if lora_name:
                self._set_lora_node_inputs(node_data, lora_name, strength_model, strength_clip)
            else:
                self._bypass_lora_node(workflow, node_id, inputs)
                for key in ("strength_model", "strength_clip", "strength", "model_strength", "clip_strength"):
                    if key in inputs:
                        inputs[key] = 0
                if str(node_id) in workflow:
                    workflow.pop(str(node_id), None)
            node_ids.append(str(node_id))

        meta["lora_enabled"] = bool(lora_name)
        meta["lora_name"] = lora_name
        meta["lora_node_ids"] = node_ids
        if not lora_name and node_ids:
            meta["lora_bypassed_node_ids"] = node_ids
            meta["lora_warning"] = "staged base pass bypassed character LoRA nodes"
        meta["strength_model"] = strength_model if lora_name else 0
        meta["strength_clip"] = strength_clip if lora_name else 0
        return meta

    async def _inject_staged_img2img(
        self,
        session: aiohttp.ClientSession,
        workflow: dict,
        source_image_path: str,
        meta: dict,
        options: dict = None,
        inpaint_mask_info: dict = None,
    ) -> dict:
        options = dict(options or {})
        upload_info = await self._upload_input_image(session, source_image_path)
        image_name = str(upload_info.get("name") or "").strip()
        subfolder = str(upload_info.get("subfolder") or "").strip().strip("/")
        if not image_name:
            raise RuntimeError("ComfyUI 未返回上传图片名，无法执行 staged img2img")
        image_value = f"{subfolder}/{image_name}" if subfolder else image_name

        prewired_sources = self._fill_prewired_img2img_source(workflow, image_value)
        if not prewired_sources:
            raise RuntimeError("staged img2img 工作流缺少 __ASTRBOT_IMG2IMG_SOURCE__ 预接线源图占位符")
        unresolved_sources = self._count_unresolved_img2img_sources(workflow)
        if unresolved_sources:
            raise RuntimeError(f"staged img2img 工作流仍有 {unresolved_sources} 个源图占位符未替换")

        denoise = min(max(_safe_float(options.get("denoise"), 0.70), 0.01), 1.0)
        control_strength = min(max(_safe_float(options.get("control_strength"), 0.42), 0.0), 2.0)
        sampler_denoise_nodes = self._set_sampler_denoise(workflow, denoise)
        control_strength_nodes = self._set_controlnet_strength(workflow, control_strength)
        if inpaint_mask_info:
            options["inpaint_mask_info"] = inpaint_mask_info
            mask_sampler_nodes = self._attach_img2img_latent_noise_mask(
                workflow,
                inpaint_mask_info,
                meta,
                options=options,
            )
            if not mask_sampler_nodes:
                raise RuntimeError("staged img2img mask 挂载失败：未能接入 SetLatentNoiseMask")
            meta["img2img_inpaint_mask_sampler_nodes"] = mask_sampler_nodes
        elif options.get("require_inpaint_mask"):
            raise RuntimeError("staged img2img 缺少区域 mask，已停止以避免全图重绘")

        negative_key = self._append_staged_negative_prompt(workflow, options.get("negative_prompt"), preferred_node_id="12")
        meta["mode"] = "img2img"
        meta["img2img_mode_hint"] = "staged_multi_lora"
        meta["source_image"] = image_value
        meta["img2img_denoise"] = denoise
        meta["img2img_control_strength"] = control_strength
        meta["img2img_prewired_sources"] = prewired_sources
        meta["img2img_sampler_denoise_nodes"] = sampler_denoise_nodes
        meta["img2img_control_strength_nodes"] = control_strength_nodes
        if negative_key:
            meta["img2img_negative_key"] = negative_key
        logger.info(
            f"[ComfyUI] staged img2img 注入: source={image_value} denoise={denoise} "
            f"source_nodes={prewired_sources} sampler_denoise_nodes={sampler_denoise_nodes} "
            f"control_strength={control_strength} control_nodes={control_strength_nodes} "
            f"mask={'yes' if inpaint_mask_info else 'no'}"
        )
        return meta

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------
    async def generate_staged_pass(
        self,
        prompt: str,
        source_image_path: str = None,
        lora_name: str = None,
        lora_strength_model: float = 0.8,
        lora_strength_clip: float = 0.8,
        workflow_filename: str = None,
        img2img_options: dict = None,
        inpaint_mask_info: dict = None,
        seed_override: int = None,
    ):
        """单次 staged generation pass；不刷新运行时配置，不触发通用 LoRA/画风 LoRA 自动注入。"""
        started = time.perf_counter()
        client_id = str(random.randint(100000, 999999))
        options = dict(img2img_options or {})
        raw_seed_override = seed_override if seed_override is not None else options.get("seed")
        seed_override = None
        if raw_seed_override is not None and str(raw_seed_override).strip() != "":
            seed_override = max(_safe_int(raw_seed_override, 0), 0)
        if seed_override is not None:
            options["seed"] = seed_override
            options["seed_source"] = options.get("seed_source") or "prompt"
        meta = {"staged_pass": True, "prompt": str(prompt or "").strip()}

        try:
            workflow, workflow_path = self._load_workflow_file(workflow_filename or self.wf_filename)
            model_names = self._workflow_model_names(workflow)
            output_id = "66" if isinstance(workflow.get("66"), dict) else str(self.output_id or "")
            meta.update({
                "workflow": workflow_path.name,
                "model_family": str(getattr(self, "runtime_model_family", "") or ""),
                "model_family_label": self._infer_model_family_label(workflow, model_names),
                "model_names": model_names,
                "checkpoint_name": model_names[0] if model_names else "",
                "output_id": output_id or "自动",
            })
            self._inject_staged_prompt_and_seed(workflow, prompt, meta, options=options)
            self._inject_staged_lora(
                workflow,
                lora_name,
                lora_strength_model,
                lora_strength_clip,
                meta,
            )
        except Exception as e:
            meta["elapsed_seconds"] = time.perf_counter() - started
            return None, str(e), meta

        timeout = self._generation_client_timeout()
        async with self._client_session(timeout) as session:
            try:
                if source_image_path:
                    await self._inject_staged_img2img(
                        session,
                        workflow,
                        source_image_path,
                        meta,
                        options=options,
                        inpaint_mask_info=inpaint_mask_info,
                    )
                elif options.get("staged_multi_lora_base") and options.get("staged_multi_lora_base_regional", True):
                    await self._apply_staged_multi_lora_base_regional_conditioning(
                        session,
                        workflow,
                        meta,
                        options=options,
                    )
                elif options.get("staged_multi_lora_base_background_only"):
                    meta["staged_base_background_only"] = True
                if options.get("free_memory_before"):
                    await self._free_comfy_memory(session, "before_staged_pass")
                    meta["free_memory_before"] = True
            except Exception as e:
                meta["elapsed_seconds"] = time.perf_counter() - started
                return None, str(e), meta

            prompt_id, queue_error = await self._queue_workflow(
                session,
                workflow,
                client_id,
                meta,
                neutralize_lora_on_validation_error=False,
            )
            if queue_error:
                meta["elapsed_seconds"] = time.perf_counter() - started
                return None, queue_error, meta

            prompt_submitted_at = time.time()
            for poll_index in range(self.poll_attempts):
                await asyncio.sleep(self._generation_poll_delay(poll_index))
                history = await self._fetch_prompt_history(session, prompt_id, meta)
                if not history or prompt_id not in history:
                    missing_error = await self._check_prompt_disappeared_from_queue(
                        session,
                        prompt_id,
                        meta,
                        prompt_submitted_at,
                    )
                    if missing_error:
                        meta["elapsed_seconds"] = time.perf_counter() - started
                        return None, missing_error, meta
                    continue

                history_item = history[prompt_id] or {}
                status = history_item.get("status", {}) or {}
                if status.get("status_str") == "error":
                    detail = self._format_history_error(history_item)
                    if detail:
                        meta["execution_error"] = detail
                    meta["elapsed_seconds"] = time.perf_counter() - started
                    return None, "ComfyUI 工作流执行失败：" + (detail or "请查看 ComfyUI 控制台日志"), meta

                outputs = history_item.get("outputs", {}) or {}
                img_info = self._select_image_output(outputs, output_id=str(meta.get("output_id") or ""))
                if not img_info:
                    meta["elapsed_seconds"] = time.perf_counter() - started
                    return None, "工作流执行完成，但未找到输出图片", meta

                image_bytes, image_error = await self._download_image_bytes(session, img_info, meta)
                meta["elapsed_seconds"] = time.perf_counter() - started
                if image_bytes:
                    return image_bytes, None, meta
                return None, image_error or "下载图片失败", meta

            meta["elapsed_seconds"] = time.perf_counter() - started
            return None, "生成超时", meta

    async def generate(self, prompt, source_image_path: str = None, img2img_options: dict = None, seed_override: int = None):
        """异步生成图片，返回 (图片bytes, 错误信息, 元信息)。"""
        started = time.perf_counter()
        client_id = str(random.randint(100000, 999999))
        meta = {}
        lora_refresh_meta = {}

        try:
            self._load_runtime_config()
            if img2img_options and img2img_options.get("mode") == "character_replace":
                if self._activate_character_replace_workflow():
                    meta["character_replace_workflow"] = True
                self.strength_model = _safe_float(
                    img2img_options.get("lora_strength_model"),
                    self.strength_model,
                )
                self.strength_clip = _safe_float(
                    img2img_options.get("lora_strength_clip"),
                    self.strength_clip,
                )
            workflow = self._load_workflow()
            if (
                self.auto_refresh_lora_list
                and (
                    self._find_lora_nodes(workflow)
                    or self._find_nodes_by_class_type(workflow, "CreateHookLora")
                )
            ):
                now = time.time()
                last_refresh = getattr(self, "_last_lora_refresh_time", 0)
                force_refresh = (now - last_refresh) > 30
                loras, warn = await self.fetch_lora_list(force=force_refresh, allow_cache_fallback=True)
                if force_refresh:
                    self._last_lora_refresh_time = now
                lora_refresh_meta["lora_cache_count"] = len(loras)
                if warn:
                    lora_refresh_meta["lora_refresh_warning"] = warn
                    logger.warning(f"[ComfyUI] LoRA 列表刷新提示: {warn}")
                if (
                    img2img_options
                    and img2img_options.get("mode") == "character_replace"
                    and self.wf_filename == LEGACY_UNION_CHARACTER_REPLACE_WORKFLOW
                    and loras
                    and "qwen_image_union_diffsynth_lora.safetensors" not in loras
                ):
                    raise RuntimeError(
                        "角色替换专用工作流缺少 qwen_image_union_diffsynth_lora.safetensors，"
                        "请放到 ComfyUI/models/loras/ 后刷新 LoRA 列表"
                    )
        except Exception as e:
            meta["elapsed_seconds"] = time.perf_counter() - started
            return None, str(e), meta

        timeout = self._generation_client_timeout()
        async with self._client_session(timeout) as session:
            workflow_fallback_meta = {}
            try:
                workflow, workflow_fallback_meta = await self._fallback_unsupported_workflow(session, workflow)
            except Exception as exc:
                logger.debug(f"[ComfyUI] workflow fallback check skipped: {exc}")
            try:
                meta = await self._inject_params(workflow, prompt, session=session, seed_override=seed_override)
                meta.update(workflow_fallback_meta)
                meta.update(lora_refresh_meta)
            except Exception as e:
                meta["elapsed_seconds"] = time.perf_counter() - started
                return None, str(e), meta

            options = img2img_options or {}
            if options.get("mode") == "pose_control":
                try:
                    pose_image_path = str(options.get("pose_image_path") or "").strip()
                    if not pose_image_path:
                        raise RuntimeError("OpenPose 控制图路径为空")
                    upload_info = await self._upload_input_image(session, pose_image_path)
                    self._inject_pose_control_params(workflow, upload_info, meta, options=options)
                except Exception as e:
                    meta["elapsed_seconds"] = time.perf_counter() - started
                    return None, str(e), meta

            if source_image_path:
                try:
                    upload_info = await self._upload_input_image(session, source_image_path)
                    mask_count = _safe_int(options.get("regional_mask_count"), 0)
                    mask_slot = _safe_int(options.get("regional_mask_slot"), -1)
                    if (
                        mask_count >= 2
                        and 0 <= mask_slot < mask_count
                        and not options.get("inpaint_mask_info")
                    ):
                        default_width, default_height = self._get_workflow_latent_size(workflow)
                        mask_width = _safe_int(options.get("regional_mask_width"), default_width)
                        mask_height = _safe_int(options.get("regional_mask_height"), default_height)
                        overlap = _safe_float(options.get("regional_mask_overlap"), 0.10)
                        blur = _safe_float(options.get("regional_mask_blur"), 0.0)
                        mask_infos = await self._upload_split_masks(
                            session,
                            mask_width,
                            mask_height,
                            mask_count,
                            overlap_ratio=overlap,
                            blur_radius=blur,
                        )
                        if len(mask_infos) > mask_slot and mask_infos[mask_slot]:
                            options["inpaint_mask_info"] = mask_infos[mask_slot]
                            meta["staged_inpaint_mask_uploaded"] = True
                            meta["staged_inpaint_mask_slot"] = mask_slot + 1
                            meta["staged_inpaint_mask_count"] = mask_count
                        else:
                            meta["staged_inpaint_mask_warning"] = "Regional staged inpaint mask upload failed; img2img pass will use full-frame denoise"
                    self._inject_img2img_params(workflow, upload_info, meta, options=img2img_options)
                except Exception as e:
                    meta["elapsed_seconds"] = time.perf_counter() - started
                    return None, str(e), meta

            if options.get("free_memory_before"):
                await self._free_comfy_memory(session, "before_illustrious_img2img")
                meta["free_memory_before"] = True

            oom_retry_enabled = bool(options.get("oom_retry_after_free", True))
            oom_retried = False
            for queue_attempt in range(2):
                prompt_id, queue_error = await self._queue_workflow(session, workflow, client_id, meta)
                if queue_error:
                    meta["elapsed_seconds"] = time.perf_counter() - started
                    return None, queue_error, meta

                prompt_submitted_at = time.time()
                retry_due_to_oom = False
                for poll_index in range(self.poll_attempts):
                    await asyncio.sleep(self._generation_poll_delay(poll_index))
                    history = await self._fetch_prompt_history(session, prompt_id, meta)
                    if not history or prompt_id not in history:
                        missing_error = await self._check_prompt_disappeared_from_queue(
                            session,
                            prompt_id,
                            meta,
                            prompt_submitted_at,
                        )
                        if missing_error:
                            meta["elapsed_seconds"] = time.perf_counter() - started
                            return None, missing_error, meta
                        continue

                    history_item = history[prompt_id] or {}
                    status = history_item.get("status", {}) or {}
                    if status.get("status_str") == "error":
                        detail = self._format_history_error(history_item)
                        if detail:
                            meta["execution_error"] = detail
                        if (
                            not source_image_path
                            and self._runtime_multi_lora_entries()
                            and not meta.get("multi_lora_execution_fallback")
                            and self._looks_like_multi_lora_regional_failure(detail)
                        ):
                            fallback_workflow, fallback_meta = await self._reload_multi_lora_safe_chain_after_failure(
                                session,
                                prompt,
                                detail,
                                lora_refresh_meta=lora_refresh_meta,
                            )
                            if fallback_workflow is not None:
                                workflow = fallback_workflow
                                meta = fallback_meta
                                retry_due_to_oom = True
                                await asyncio.sleep(0.5)
                                break
                            if fallback_meta:
                                meta.update(fallback_meta)
                        if (
                            oom_retry_enabled
                            and not oom_retried
                            and self._looks_like_cuda_oom(detail)
                        ):
                            oom_retried = True
                            retry_due_to_oom = True
                            meta["oom_retry_after_free"] = True
                            meta["first_execution_error"] = detail
                            meta.pop("execution_error", None)
                            await self._free_comfy_memory(session, "oom_retry")
                            if options.get("mode") == "illustrious_img2img":
                                self._apply_illustrious_oom_fallback_params(workflow, meta)
                            await asyncio.sleep(0.8)
                            break
                        meta["elapsed_seconds"] = time.perf_counter() - started
                        return None, "ComfyUI 工作流执行失败：" + (detail or "请查看 ComfyUI 控制台日志"), meta

                    outputs = history_item.get("outputs", {}) or {}
                    img_info = self._select_image_output(outputs)
                    if not img_info:
                        meta["elapsed_seconds"] = time.perf_counter() - started
                        return None, "工作流执行完成，但未找到输出图片", meta

                    try:
                        image_bytes, image_error = await self._download_image_bytes(session, img_info, meta)
                        meta["elapsed_seconds"] = time.perf_counter() - started
                        if image_bytes:
                            return image_bytes, None, meta
                        return None, image_error or "下载图片失败", meta
                    except Exception as e:
                        meta["elapsed_seconds"] = time.perf_counter() - started
                        return None, f"下载图片异常: {e}", meta

                if retry_due_to_oom:
                    continue
                meta["elapsed_seconds"] = time.perf_counter() - started
                return None, "生成超时", meta

            meta["elapsed_seconds"] = time.perf_counter() - started
            return None, "ComfyUI 工作流执行失败：显存不足，已清理显存并低显存重试仍失败；请关闭占用显存的任务或用 --lowvram/--medvram-sdxl 启动 ComfyUI", meta

    def _select_image_output(self, outputs: dict, output_id: str = None):
        effective_output_id = str(output_id if output_id is not None else (self.output_id or "")).strip()
        if effective_output_id and effective_output_id in outputs:
            imgs = outputs[effective_output_id].get("images", [])
            if imgs:
                return imgs[0]

        for node_out in outputs.values():
            if isinstance(node_out, dict) and node_out.get("images"):
                return node_out["images"][0]
        return None

    def _is_blank_or_black_image(self, image_bytes) -> tuple:
        """检测 VAE 解码失败导致的纯色填充图（黑屏/纯白/纯灰）。

        返回 (is_bad, reason)。怀疑时一律返回 (False, "")，绝不误删正常图。
        判定依据：灰度图 min==max（完全纯色填充）或像素标准差 < 2（近乎纯色）。
        真实照片（含夜景）总有纹理方差，不会被误杀；解码失败本身也判为坏图。
        """
        try:
            import io
            import math
            from PIL import Image as PILImage
        except Exception:
            return False, ""
        if not image_bytes:
            return True, "empty_bytes"
        try:
            img = PILImage.open(io.BytesIO(image_bytes))
            img.load()
            gray = img.convert("L")
        except Exception as exc:
            return True, f"decode_failed:{str(exc)[:80]}"
        try:
            lo, hi = gray.getextrema()
            if lo == hi:
                return True, f"solid_fill:{lo}"
            hist = gray.histogram()
            total = sum(hist)
            if total <= 0:
                return True, "empty_histogram"
            mean = sum(i * c for i, c in enumerate(hist)) / total
            var = sum(c * (i - mean) ** 2 for i, c in enumerate(hist)) / total
            std = math.sqrt(var)
            if std < 2.0:
                return True, f"near_solid:std={std:.3f}"
        except Exception:
            return False, ""
        return False, ""

    def _fill_prewired_wan_i2v_source(self, workflow: dict, image_value: str) -> int:
        updated = 0
        for node_data in workflow.values():
            if not isinstance(node_data, dict):
                continue
            if str(node_data.get("class_type") or "") != "LoadImage":
                continue
            inputs = node_data.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            title = str((node_data.get("_meta") or {}).get("title") or "").lower()
            current_image = str(inputs.get("image") or "")
            if "__ASTRBOT_WAN_I2V_SOURCE__" in current_image or "wan i2v source" in title:
                inputs["image"] = image_value
                updated += 1
        return updated

    def _find_nodes_by_class(self, workflow: dict, class_type: str) -> list:
        result = []
        for node_id, node_data in workflow.items():
            if isinstance(node_data, dict) and str(node_data.get("class_type") or "") == class_type:
                result.append((str(node_id), node_data))
        return result

    def _inject_text_into_node(self, workflow: dict, node_id: str, text: str) -> bool:
        node = workflow.get(str(node_id))
        if not isinstance(node, dict):
            return False
        inputs = node.setdefault("inputs", {})
        if not isinstance(inputs, dict):
            return False
        for key in ("text", "string", "positive", "negative", "prompt"):
            if key in inputs:
                inputs[key] = text
                return True
        inputs["text"] = text
        return True

    def _apply_wan_i2v_options(self, workflow: dict, options: dict, meta: dict, fallback: bool = False) -> dict:
        options = options or {}

        def opt_int(name: str, default: int, minimum: int = 1, maximum: int = None) -> int:
            value = _safe_int(options.get(name), default)
            value = max(value, minimum)
            return min(value, maximum) if maximum is not None else value

        def opt_float(name: str, default: float, minimum: float = 0.0, maximum: float = None) -> float:
            value = _safe_float(options.get(name), default)
            value = max(value, minimum)
            return min(value, maximum) if maximum is not None else value

        if fallback:
            width = opt_int("fallback_width", 448, 16, 2048)
            height = opt_int("fallback_height", 448, 16, 2048)
            length = opt_int("fallback_frames", 33, 1, 512)
            steps = opt_int("fallback_steps", 4, 1, 10000)
        else:
            width = opt_int("width", 512, 16, 2048)
            height = opt_int("height", 512, 16, 2048)
            length = opt_int("frames", 41, 1, 512)
            steps = opt_int("steps", 4, 1, 10000)

        fps = opt_int("fps", 8, 1, 120)
        crf = opt_int("crf", 19, 0, 100)
        cfg_high = opt_float("cfg_high", 3.0, 0.0, 100.0)
        cfg_low = opt_float("cfg_low", 3.0, 0.0, 100.0)
        boundary = opt_float("boundary", 0.9, 0.0, 1.0)
        sigma_shift = opt_float("sigma_shift", 5.0, 0.0, 100.0)
        sampler_name = str(options.get("sampler_name") or "lcm").strip() or "lcm"
        scheduler = str(options.get("scheduler") or "simple").strip() or "simple"
        seed = _safe_int(options.get("seed"), 0)
        if seed <= 0:
            seed = random.randint(1, 999999999999999)

        # 8.5.1：采样预设。preset 提供一整套采样参数，避免逐字段默认值打架。
        # - "auto"：按工作流文件名推断（含 quality → 画质档 20步 euler；否则快速档 4步 lcm）
        # - "fast_4step" / "quality_20step"：显式强制整套参数
        # 仅在非 fallback（OOM 降级）时生效，fallback 必须保持快速档兜底。
        preset = str(options.get("sampler_preset") or "auto").strip().lower()
        workflow_name = str(options.get("workflow") or "").lower()
        resolved_preset = ""
        if not fallback:
            if preset == "quality_20step" or (preset == "auto" and "quality" in workflow_name):
                steps = 20
                cfg_high = 3.5
                cfg_low = 3.5
                sampler_name = "euler"
                scheduler = "simple"
                sigma_shift = 8.0
                resolved_preset = "quality_20step"
            elif preset == "fast_4step" or (preset == "auto" and "quality" not in workflow_name):
                steps = 4
                cfg_high = 3.0
                cfg_low = 3.0
                sampler_name = "lcm"
                scheduler = "simple"
                sigma_shift = 5.0
                resolved_preset = "fast_4step"
            else:
                resolved_preset = preset

        # 8.5.1：注入 CLIPVisionLoader 的 clip_name（图生视频语义引导必需）。
        # 工作流里 CLIPVisionLoader→CLIPVisionEncode→WanImageToVideo.clip_vision_output
        # 已静态连线，这里只把模型名换成用户配置的。
        clip_vision_model = str(options.get("clip_vision_model") or "clip_vision_h.safetensors").strip() or "clip_vision_h.safetensors"

        for _node_id, node in self._find_nodes_by_class(workflow, "CLIPVisionLoader"):
            inputs = node.setdefault("inputs", {})
            if isinstance(inputs, dict):
                inputs["clip_name"] = clip_vision_model

        for _node_id, node in self._find_nodes_by_class(workflow, "WanImageToVideo"):
            inputs = node.setdefault("inputs", {})
            if isinstance(inputs, dict):
                inputs["width"] = width
                inputs["height"] = height
                inputs["length"] = length
                inputs["batch_size"] = max(_safe_int(inputs.get("batch_size"), 1), 1)

        # 8.5.2：ModelSamplingSD3 节点(官方 Wan2.2 i2v 用 shift=8.0 做 flow 偏移)。
        # 官方工作流 video_wan2_2_14B_i2v.json 里高/低噪各接一个 ModelSamplingSD3(shift=8)。
        # sigma_shift=8.0 对齐官方；fallback 时不注入(快速档 WanMoeKSampler 自带 set_shift)。
        if not fallback:
            for _node_id, node in self._find_nodes_by_class(workflow, "ModelSamplingSD3"):
                inputs = node.setdefault("inputs", {})
                if isinstance(inputs, dict):
                    inputs["shift"] = sigma_shift

        # 8.5.2：双 KSamplerAdvanced 工作流(官方结构:高噪 step0-N + 低噪 stepN-end)。
        # 节点9=高噪(add_noise=enable, return_with_leftover_noise=enable),
        # 节点16=低噪(add_noise=disable, return_with_leftover_noise=disable)。
        # ⚠️ 官方 video_wan2_2_14B_i2v.json 是 steps=20 等分 10/10,这是 step 索引维度,
        # 与 WanMoeKSampler 的 boundary(timestep 维度,默认0.9)不是一回事,故固定用 0.5 等分。
        ks_adv_nodes = self._find_nodes_by_class(workflow, "KSamplerAdvanced")
        if ks_adv_nodes and not fallback:
            split_step = max(1, int(round(steps * 0.5)))
            # 按 add_noise=enable 排序识别高噪(第一个)/低噪(第二个)
            sorted_ks = sorted(ks_adv_nodes, key=lambda kv: 0 if str(kv[1].get("inputs", {}).get("add_noise")) == "enable" else 1)
            for idx, (_node_id, node) in enumerate(sorted_ks):
                inputs = node.setdefault("inputs", {})
                if not isinstance(inputs, dict):
                    continue
                inputs["noise_seed"] = seed
                inputs["steps"] = steps
                inputs["cfg"] = cfg_high if idx == 0 else cfg_low
                inputs["sampler_name"] = sampler_name
                inputs["scheduler"] = scheduler
                if idx == 0:
                    inputs["add_noise"] = "enable"
                    inputs["start_at_step"] = 0
                    inputs["end_at_step"] = split_step
                    inputs["return_with_leftover_noise"] = "enable"
                else:
                    inputs["add_noise"] = "disable"
                    inputs["start_at_step"] = split_step
                    inputs["end_at_step"] = 10000
                    inputs["return_with_leftover_noise"] = "disable"

        for _node_id, node in self._find_nodes_by_class(workflow, "WanMoeKSampler"):
            inputs = node.setdefault("inputs", {})
            if isinstance(inputs, dict):
                inputs["boundary"] = boundary
                inputs["seed"] = seed
                inputs["steps"] = steps
                inputs["cfg_high_noise"] = cfg_high
                inputs["cfg_low_noise"] = cfg_low
                inputs["sampler_name"] = sampler_name
                inputs["scheduler"] = scheduler
                inputs["sigma_shift"] = sigma_shift
                inputs["denoise"] = opt_float("denoise", 1.0, 0.0, 1.0)

        for _node_id, node in self._find_nodes_by_class(workflow, "VHS_VideoCombine"):
            inputs = node.setdefault("inputs", {})
            if isinstance(inputs, dict):
                inputs["frame_rate"] = fps
                inputs["format"] = str(options.get("format") or "video/h264-mp4").strip() or "video/h264-mp4"
                inputs["pix_fmt"] = str(options.get("pix_fmt") or "yuv420p").strip() or "yuv420p"
                inputs["crf"] = crf
                inputs["save_metadata"] = bool(options.get("save_metadata", True))
                inputs["trim_to_audio"] = bool(options.get("trim_to_audio", False))
                inputs["save_output"] = True

        meta.update({
            "mode": "wan_i2v",
            "video_width": width,
            "video_height": height,
            "video_frames": length,
            "video_fps": fps,
            "video_steps": steps,
            "video_cfg_high": cfg_high,
            "video_cfg_low": cfg_low,
            "video_sampler": sampler_name,
            "video_scheduler": scheduler,
            "video_boundary": boundary,
            "video_sigma_shift": sigma_shift,
            "video_crf": crf,
            "seed": seed,
        })
        if resolved_preset:
            meta["sampler_preset"] = resolved_preset
        if clip_vision_model:
            meta["clip_vision_model"] = clip_vision_model
        if fallback:
            meta["wan_i2v_low_vram_fallback"] = True
        return meta

    def _inject_wan_i2v_params(self, workflow: dict, prompt: str, upload_info: dict, meta: dict, options: dict = None, fallback: bool = False):
        options = options or {}
        input_id = str(options.get("input_node_id") or "5")
        neg_id = str(options.get("neg_node_id") or "6")
        output_id = str(options.get("output_node_id") or "11")
        if not self._inject_text_into_node(workflow, input_id, str(prompt or "").strip()):
            raise RuntimeError(f"Wan I2V 工作流缺少正面提示词节点 {input_id}")
        negative = str(options.get("negative_prompt") or "").strip()
        if negative:
            self._inject_text_into_node(workflow, neg_id, negative)

        image_name = str((upload_info or {}).get("name") or "").strip()
        subfolder = str((upload_info or {}).get("subfolder") or "").strip().strip("/")
        if not image_name:
            raise RuntimeError("ComfyUI 未返回上传图片名，无法图生视频")
        image_value = f"{subfolder}/{image_name}" if subfolder else image_name
        source_nodes = self._fill_prewired_wan_i2v_source(workflow, image_value)
        if source_nodes <= 0:
            raise RuntimeError("Wan I2V 工作流未找到源图占位 LoadImage 节点")

        self._apply_wan_i2v_options(workflow, options, meta, fallback=fallback)
        meta.update({
            "workflow": str(options.get("workflow") or "wan_i2v_api.json"),
            "input_id": input_id,
            "neg_node_id": neg_id,
            "output_id": output_id,
            "prompt": prompt,
            "source_image": image_value,
            "wan_i2v_source_nodes": source_nodes,
        })
        return meta

    def _select_video_output(self, outputs: dict):
        preferred_ids = [self.output_id] if self.output_id else []
        preferred_ids.extend([node_id for node_id in (outputs or {}).keys() if node_id not in preferred_ids])
        for node_id in preferred_ids:
            node_out = (outputs or {}).get(str(node_id))
            if not isinstance(node_out, dict):
                continue
            for key in ("gifs", "videos", "animated"):
                files = node_out.get(key)
                if isinstance(files, list) and files:
                    first = files[0]
                    if isinstance(first, dict):
                        return first
            filenames = node_out.get("filenames") or node_out.get("files")
            if isinstance(filenames, list) and filenames:
                first = filenames[0]
                if isinstance(first, dict):
                    return first
        return None

    async def _download_view_file(self, session: aiohttp.ClientSession, file_info: dict, target_path: Path, meta: dict, label: str = "文件") -> bool:
        params = urlencode({
            "filename": file_info.get("filename", ""),
            "subfolder": file_info.get("subfolder", ""),
            "type": file_info.get("type", "output"),
        })
        url = f"{self.url}/view?{params}"
        target_path = Path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        attempts = max(int(getattr(self, "view_download_retries", 3) or 3), 1)
        last_error = ""
        for attempt in range(attempts):
            try:
                async with session.get(url, timeout=self._generation_client_timeout()) as resp:
                    if resp.status != 200:
                        meta[f"{label}_download_status"] = resp.status
                        last_error = f"HTTP {resp.status}"
                    else:
                        with target_path.open("wb") as fp:
                            async for chunk in resp.content.iter_chunked(1024 * 1024):
                                if chunk:
                                    fp.write(chunk)
                        return True
            except Exception as exc:
                last_error = str(exc)
                meta[f"{label}_download_error"] = last_error[:200]
            if attempt + 1 < attempts:
                await asyncio.sleep(min(0.8 * (attempt + 1), 2.5))
        if last_error:
            logger.warning(f"[ComfyUI] 下载 {label} 失败: {last_error}")
        return False

    async def generate_video(self, prompt: str, source_image_path: str, video_options: dict = None, output_dir: str = None):
        """异步图生视频，返回 (本地视频路径, 错误信息, 元信息)。"""
        started = time.perf_counter()
        client_id = str(random.randint(100000, 999999))
        options = dict(video_options or {})
        workflow_filename = str(options.get("workflow") or "wan_i2v_api.json").strip() or "wan_i2v_api.json"
        workflow_path = self.workflow_dir / workflow_filename
        output_id = str(options.get("output_node_id") or "11")
        meta = {
            "workflow": workflow_filename,
            "mode": "wan_i2v",
            "output_id": output_id,
        }
        old_output_id = None

        try:
            self._load_runtime_config()
            old_output_id = self.output_id
            self.output_id = output_id
            if not workflow_path.exists():
                raise FileNotFoundError(f"Wan I2V 工作流文件不存在: {workflow_path}")
            with workflow_path.open("r", encoding="utf-8") as fp:
                workflow = json.load(fp)
        except Exception as e:
            if old_output_id is not None:
                self.output_id = old_output_id
            meta["elapsed_seconds"] = time.perf_counter() - started
            return None, str(e), meta

        timeout = self._generation_client_timeout()
        async with self._client_session(timeout) as session:
            try:
                upload_info = await self._upload_input_image(session, source_image_path)
                self._inject_wan_i2v_params(workflow, prompt, upload_info, meta, options=options)
            except Exception as e:
                self.output_id = old_output_id
                meta["elapsed_seconds"] = time.perf_counter() - started
                return None, str(e), meta

            if bool(options.get("free_memory_before", True)):
                await self._free_comfy_memory(session, "before_wan_i2v")
                meta["free_memory_before"] = True

            oom_retry_enabled = bool(options.get("oom_retry_after_free", True))
            oom_retried = False
            try:
                for _queue_attempt in range(2):
                    prompt_id, queue_error = await self._queue_workflow(session, workflow, client_id, meta)
                    if queue_error:
                        meta["elapsed_seconds"] = time.perf_counter() - started
                        return None, queue_error, meta

                    prompt_submitted_at = time.time()
                    retry_due_to_runtime_failure = False
                    for poll_index in range(self.poll_attempts):
                        await asyncio.sleep(self._generation_poll_delay(poll_index))
                        history = await self._fetch_prompt_history(session, prompt_id, meta)
                        if not history or prompt_id not in history:
                            missing_error = await self._check_prompt_disappeared_from_queue(
                                session,
                                prompt_id,
                                meta,
                                prompt_submitted_at,
                            )
                            if missing_error:
                                meta["elapsed_seconds"] = time.perf_counter() - started
                                return None, missing_error, meta
                            continue

                        history_item = history[prompt_id] or {}
                        status = history_item.get("status", {}) or {}
                        if status.get("status_str") == "error":
                            detail = self._format_history_error(history_item)
                            if detail:
                                meta["execution_error"] = detail
                            retryable_cuda_failure = (
                                self._looks_like_cuda_oom(detail)
                                or self._looks_like_cuda_runtime_failure(detail)
                            )
                            if oom_retry_enabled and not oom_retried and retryable_cuda_failure:
                                oom_retried = True
                                retry_due_to_runtime_failure = True
                                if self._looks_like_cuda_oom(detail):
                                    meta["oom_retry_after_free"] = True
                                else:
                                    meta["cuda_runtime_retry_after_free"] = True
                                meta["first_execution_error"] = detail
                                meta.pop("execution_error", None)
                                await self._free_comfy_memory(session, "wan_i2v_cuda_retry")
                                # 8.5.3：画质档(FP8 14B 双模型)OOM 时,仅改小分辨率救不了——
                                # 瓶颈是 14B 模型加载本身(~14GB/个),5080 16GB 放一个就接近满载。
                                # 真正能救的是整个切回快速档工作流(Q4 GGUF 8.5GB + WanMoeKSampler 4步)。
                                if "quality" in workflow_filename.lower():
                                    try:
                                        fallback_wf_path = self.workflow_dir / "wan_i2v_api.json"
                                        if fallback_wf_path.exists():
                                            with fallback_wf_path.open("r", encoding="utf-8") as fp:
                                                workflow = json.load(fp)
                                            # 切工作流后必须重新注入源图占位+提示词+fallback 参数
                                            self._inject_wan_i2v_params(workflow, prompt, upload_info, meta, options=options, fallback=True)
                                            meta["oom_fallback_workflow_switched"] = "wan_i2v_api.json"
                                            meta["wan_i2v_low_vram_fallback"] = True
                                        else:
                                            self._apply_wan_i2v_options(workflow, options, meta, fallback=True)
                                    except Exception as e:
                                        meta["oom_fallback_switch_error"] = str(e)
                                        self._apply_wan_i2v_options(workflow, options, meta, fallback=True)
                                else:
                                    self._apply_wan_i2v_options(workflow, options, meta, fallback=True)
                                await asyncio.sleep(0.8)
                                break
                            meta["elapsed_seconds"] = time.perf_counter() - started
                            return None, "ComfyUI 视频工作流执行失败：" + (detail or "请查看 ComfyUI 控制台日志"), meta

                        outputs = history_item.get("outputs", {}) or {}
                        video_info = self._select_video_output(outputs)
                        if not video_info:
                            meta["elapsed_seconds"] = time.perf_counter() - started
                            return None, "视频工作流执行完成，但未找到 mp4 输出", meta

                        suffix = ".mp4"
                        filename = str(video_info.get("filename") or "")
                        if Path(filename).suffix:
                            suffix = Path(filename).suffix
                        target_dir = Path(output_dir) if output_dir else self.data_dir / "output"
                        target_path = target_dir / f"{uuid.uuid4()}{suffix}"
                        try:
                            ok = await self._download_view_file(session, video_info, target_path, meta, label="video")
                            meta["elapsed_seconds"] = time.perf_counter() - started
                            meta["video_filename"] = filename
                            meta["video_subfolder"] = str(video_info.get("subfolder") or "")
                            meta["video_type"] = str(video_info.get("type") or "output")
                            meta["video_format"] = str(video_info.get("format") or "")
                            if ok:
                                return target_path, None, meta
                            return None, f"下载视频失败: {meta.get('video_download_status')}", meta
                        except Exception as e:
                            meta["elapsed_seconds"] = time.perf_counter() - started
                            return None, f"下载视频异常: {e}", meta

                    if retry_due_to_runtime_failure:
                        continue
                    meta["elapsed_seconds"] = time.perf_counter() - started
                    return None, "视频生成超时", meta

                meta["elapsed_seconds"] = time.perf_counter() - started
                return None, "ComfyUI 视频工作流执行失败：CUDA 运行异常或显存不足，已降级到 448x448 / 33帧 / 4步重试仍失败", meta
            finally:
                try:
                    if bool(options.get("free_memory_after", True)):
                        await self._free_comfy_memory(session, "after_wan_i2v")
                        meta["free_memory_after"] = True
                finally:
                    self.output_id = old_output_id
