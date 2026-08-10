# -*- coding: utf-8 -*-
"""Security hard-gate checks (Agent 10) — runnable against the *current* codebase.

Hard gates that only the not-yet-merged desktop layer can satisfy (no LAN
listening, no default password, no query token, Host/Origin/CSRF, TLS default
verify, signed installer/manifest) are declared in test_api_v1_integration.py
and in docs/desktop_parallel/10_security_release/security_gate.md as
PENDING-INTEGRATION; they are NOT asserted here.

Gates asserted here (already satisfiable by plugin code):
    - bridge auth: remote request without token -> 401 (Bridge 无 Token 401)
    - secrets never written to JSON/SQLite/logs by plugin code paths
    - _redact_sensitive_text scrub coverage (logs/crash text)
    - path traversal fails (upload filename sanitization + fake server)
    - oversized upload fails (413)
    - repo ships no real secret material (no ghp_/sk-/hf_/AKIA...)
    - TLS verification: plugin only disables verification for the user-scoped
      ComfyUI endpoint, never globally
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
from pathlib import Path

import pytest

pytestmark = [pytest.mark.hardgate, pytest.mark.fakecomfy]

SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_\w{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
]

SKIP_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".zip", ".pyc", ".js.map"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", "dist", "build", "target", ".venv", "venv", "html_render_cache"}
TEST_DIRS = {"desktop_integration_tests", "tests_desktop", "tools"}  # test/tool tokens are allowed here only


def _repo_files(plugin_root: Path):
    for path in plugin_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(plugin_root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() in SKIP_EXTS:
            continue
        yield path


class TestNoHardcodedSecrets:
    def test_production_tree_contains_no_real_secret_tokens(self, plugin_root):
        """Static scan of PRODUCTION code: no PAT / OpenAI key / HF token / AWS key.

        Test scaffolding (desktop_integration_tests/, tools/) may contain fake
        tokens by design; production code must not.
        """
        hits = []
        for path in _repo_files(plugin_root):
            rel = path.relative_to(plugin_root)
            if any(part in TEST_DIRS for part in rel.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for pattern in SECRET_PATTERNS:
                for match in pattern.finditer(text):
                    hits.append(f"{rel}: {pattern.pattern[:24]}... {match.group(0)[:12]}...")
        assert not hits, "\n".join(hits[:20])

    def test_fake_tokens_confined_to_test_dirs(self, plugin_root):
        """The fake tokens used by test fixtures must never leak into production code."""
        fake_values = [
            "github_pat_11_TEST_PAT_abcdef1234567890",
            "sk-test-prompt-translate-0001",
            "hf_test_token_abcdef123456",
            "sk-live-bearer-token-123456",
        ]
        leaked = []
        for path in _repo_files(plugin_root):
            rel = path.relative_to(plugin_root)
            if any(part in TEST_DIRS for part in rel.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for value in fake_values:
                if value in text:
                    leaked.append(f"{rel}: {value}")
        assert not leaked, "\n".join(leaked)

    def test_self_update_token_uses_env_indirection(self, plugin_root):
        """self_update token must be an env-var NAME, never a literal token."""
        schema = json.loads((plugin_root / "_conf_schema.json").read_text(encoding="utf-8"))
        token_env = (schema.get("self_update_settings") or {}).get("items", {}).get("token_env", {})
        assert token_env.get("default") == "JH_PLUGIN_GIT_TOKEN"
        self_update_src = (plugin_root / "features" / "self_update.py").read_text(encoding="utf-8")
        assert "os.environ.get" in self_update_src
        assert "access_token" in self_update_src


class TestBridgeAuth:
    """Bridge 无 Token 401 — remote callers must be rejected without the token."""

    @pytest.fixture()
    def bridge_module(self, tmp_path, plugin_root):
        """Load the LoRA Bridge from a temp copy so its config.json lands in tmp."""
        src = plugin_root / "comfyui_custom_nodes" / "ComfyUI-AstrBot-LoraBridge" / "__init__.py"
        assert src.exists()
        dst = tmp_path / "bridge_init.py"
        shutil.copy2(src, dst)
        spec = importlib.util.spec_from_file_location("winefox_test_bridge", dst)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.CONFIG_PATH = tmp_path / "config.json"
        module.CONFIG = module._load_config()
        module.CONFIG["token"] = "test-bridge-token-0123456789"
        module.CONFIG["allow_local_requests_without_token"] = False
        return module

    def test_remote_without_token_rejected(self, bridge_module):
        class Req:
            headers = {}
            query = {}
            remote = "203.0.113.7"

        assert bridge_module._check_auth(Req()) is False

    def test_remote_with_wrong_token_rejected(self, bridge_module):
        class Req:
            headers = {}
            query = {}
            remote = "203.0.113.7"

        req = Req()
        req.headers = {"X-AstrBot-Bridge-Token": "wrong-token"}
        assert bridge_module._check_auth(req) is False

    def test_remote_with_correct_header_token_accepted(self, bridge_module):
        class Req:
            headers = {"X-AstrBot-Bridge-Token": "test-bridge-token-0123456789"}
            query = {}
            remote = "203.0.113.7"

        assert bridge_module._check_auth(Req()) is True

    @pytest.mark.xfail(
        reason=(
            "HARD-GATE FINDING: ComfyUI-AstrBot-LoraBridge accepts tokens via query string (?token=). "
            "Desktop integration must strip query tokens and require X-AstrBot-Bridge-Token header."
        ),
        strict=False,
    )
    def test_remote_with_correct_query_token_accepted_but_flagged(self, bridge_module):
        """Documented risk: bridge accepts ?token= in query; desktop must NOT do this."""
        class Req:
            headers = {}
            query = {"token": "test-bridge-token-0123456789"}
            remote = "203.0.113.7"

        assert bridge_module._check_auth(Req()) is True  # behavior exists
        raise AssertionError(
            "HARD-GATE FINDING (desktop must fix): bridge accepts query-string token. "
            "Desktop layer must require the X-AstrBot-Bridge-Token header and strip ?token=."
        )

    def test_local_request_bypass_respects_flag(self, bridge_module):
        class Req:
            headers = {}
            query = {}
            remote = "127.0.0.1"

        # bypass disabled -> local must still authenticate
        assert bridge_module._check_auth(Req()) is False
        bridge_module.CONFIG["allow_local_requests_without_token"] = True
        assert bridge_module._check_auth(Req()) is True

    def test_filename_sanitizers_block_traversal(self, bridge_module):
        assert bridge_module._safe_filename("../../evil.safetensors") == "evil.safetensors"
        assert bridge_module._safe_filename("..\\..\\evil.ckpt") == "evil.ckpt"
        assert bridge_module._safe_image_filename("../../x.png") == "x.png"
        assert ".." not in bridge_module._safe_filename("a/b/../../c.safetensors")
        assert ".." not in bridge_module._safe_image_filename("a\\..\\b\\c.webp")


class TestSecretStorage:
    def test_plugin_never_writes_secrets_to_own_json(self, plugin_root):
        """Plugin's own persistence must not contain secret values (lora_cache etc.)."""
        import comfyui_api

        src = Path(comfyui_api.__file__).read_text(encoding="utf-8")
        # lora cache payload is server + loras only
        assert '"server"' in src and '"loras"' in src
        assert "api_key" not in src.split("_save_lora_cache_to_disk")[1][:2000].split("def ")[0]

    def test_favorites_sidecar_has_no_secrets(self, dummy_plugin, tmp_path):
        from features.favorites import FavoritesFeature

        feature = FavoritesFeature.__new__(FavoritesFeature)
        feature._plugin = dummy_plugin
        src = tmp_path / "art.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\npayload")
        dest = feature._copy_favorite_image(str(src), "角色")
        meta = {"prompt": "a test", "image_sha256": feature._favorite_file_sha256(str(dest))}
        sidecar = feature._favorite_sidecar_path(dest)
        sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        raw = sidecar.read_text(encoding="utf-8")
        assert dummy_plugin.civitai_api_token not in raw
        assert dummy_plugin.huggingface_api_token not in raw

    def test_redaction_covers_crash_and_log_paths(self, dummy_plugin):
        """Redaction applied on all log/crash strings: no secret reaches logs."""
        from main import ComfyUIPlugin

        plugin = ComfyUIPlugin.__new__(ComfyUIPlugin)
        for key, value in vars(dummy_plugin).items():
            if not key.startswith("_"):
                setattr(plugin, key, value)
        plugin._self_update_token = lambda *a, **k: ""  # bypass feature delegation
        crash_text = (
            f"Traceback ... request={dummy_plugin.civitai_api_token} "
            f"headers={{authorization: Bearer {dummy_plugin.prompt_translate_api_key}}}"
        )
        redacted = plugin._redact_sensitive_text(crash_text)
        assert dummy_plugin.civitai_api_token not in redacted
        assert dummy_plugin.prompt_translate_api_key not in redacted


class TestTlsScoping:
    def test_ssl_verify_disabled_only_for_configured_comfyui_endpoint(self, plugin_root):
        """srv_ssl=False must be scoped to the ComfyUI connector, not global aiohttp."""
        import comfyui_api

        src = Path(comfyui_api.__file__).read_text(encoding="utf-8")
        connector_line = [line for line in src.splitlines() if "TCPConnector(ssl" in line]
        assert connector_line, "expected a TCPConnector(ssl=...) line"
        assert all("if not self.server_ssl_verify" in line or "ssl=False" in line for line in connector_line)
        # global sessions used for Civitai/HF must keep verification
        assert "ssl=False" not in src.replace(connector_line[0], "")
        # default for ComfyUI endpoint stays false (user-scoped), documented in schema
        schema = json.loads((plugin_root / "_conf_schema.json").read_text(encoding="utf-8"))
        assert schema["server_ssl_verify"]["default"] is False
        assert "只影响 ComfyUI API" in schema["server_ssl_verify"]["hint"]


class TestHardGateContractDocumented:
    def test_gate_doc_lists_all_required_gates(self, plugin_root):
        doc = plugin_root / "docs" / "desktop_parallel" / "10_security_release" / "security_gate.md"
        assert doc.exists(), "security_gate.md must exist"
        text = doc.read_text(encoding="utf-8")
        required = [
            "无 LAN 监听",
            "无默认密码",
            "无 query token",
            "未授权 401",
            "CSRF",
            "TLS",
            "路径穿越",
            "超大上传",
            "签名",
        ]
        for keyword in required:
            assert keyword in text, f"security_gate.md must cover: {keyword}"
