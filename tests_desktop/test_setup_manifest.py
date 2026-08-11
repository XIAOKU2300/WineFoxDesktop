from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from desktop_runtime.setup_manifest import (
    ManifestError,
    load_setup_manifest,
    default_manifest_path,
    parse_manifest,
)


class SetupManifestTests(unittest.TestCase):
    """Verify the real manifest parses and matches the old BAT contract."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_setup_manifest()

    def test_manifest_path_resolves(self) -> None:
        path = default_manifest_path()
        self.assertTrue(path.exists(), f"manifest missing at {path}")
        self.assertEqual(path.suffix, ".json")

    def test_counts_match_bat(self) -> None:
        # 19 auto models + 9 pinned custom nodes (+ Bridge bundled separately).
        self.assertEqual(len(self.manifest.models), 19)
        self.assertEqual(len(self.manifest.nodes), 9)
        # 5 manual files, no auto download.
        self.assertEqual(len(self.manifest.manual_files), 5)

    def test_model_groups_present(self) -> None:
        self.assertEqual(set(self.manifest.model_groups), {"core", "minimal", "wan", "wan_clip_vision", "wan_quality", "anima"})
        self.assertEqual(len(self.manifest.models_in_group("core")), 1)
        self.assertEqual(len(self.manifest.models_in_group("minimal")), 6)
        self.assertEqual(len(self.manifest.models_in_group("wan")), 4)
        self.assertEqual(len(self.manifest.models_in_group("wan_clip_vision")), 1)
        self.assertEqual(len(self.manifest.models_in_group("wan_quality")), 2)
        self.assertEqual(len(self.manifest.models_in_group("anima")), 5)

    def test_every_download_host_is_allow_listed(self) -> None:
        allow = set(self.manifest.bridge.allowed_hosts)
        for model in self.manifest.models.values():
            for source in model.sources:
                self.assertIn(source.host, allow, f"model {model.id} host {source.host}")
                self.assertTrue(source.url.startswith("https://"), f"non-TLS url {source.url}")

    def test_every_model_has_min_gb_and_relative_dest(self) -> None:
        for model in self.manifest.models.values():
            self.assertGreaterEqual(model.min_gb, 0)
            self.assertFalse(Path(model.dest).is_absolute(), model.dest)
            self.assertNotIn("..", model.dest)
            self.assertEqual(model.kind, Path(model.name).suffix.lstrip("."))

    def test_every_node_is_pinned_to_full_sha(self) -> None:
        for node in self.manifest.nodes.values():
            self.assertEqual(node.ref_kind, "commit", node.id)
            self.assertRegex(node.ref, r"^[0-9a-f]{40}$", node.id)
            self.assertIn("/", node.repo)

    def test_node_groups_reference_known_nodes(self) -> None:
        self.assertEqual(len(self.manifest.node_groups["minimal"].nodes), 3)
        self.assertEqual(len(self.manifest.node_groups["anima"].nodes), 2)
        self.assertEqual(len(self.manifest.node_groups["wan"].nodes), 3)
        self.assertEqual(self.manifest.node_groups["minimal"].pip_packages, ("ultralytics",))

    def test_tiers_match_bat_ordering(self) -> None:
        lite = [(s.step, s.group) for s in self.manifest.tiers["lite"]]
        self.assertEqual(lite, [("nodes", "minimal"), ("models", "core"), ("bridge", None)])

        full = [(s.step, s.group) for s in self.manifest.tiers["full"]]
        self.assertEqual(
            full,
            [
                ("nodes", "minimal"),
                ("models", "minimal"),
                ("nodes", "hq"),
                ("nodes", "anima"),
                ("nodes", "wan"),
                ("bridge", None),
            ],
        )

        complete = [(s.step, s.group) for s in self.manifest.tiers["complete"]]
        # complete must NOT include wan_quality (always manual).
        self.assertEqual(
            complete,
            [
                ("nodes", "minimal"),
                ("models", "minimal"),
                ("nodes", "hq"),
                ("nodes", "anima"),
                ("models", "anima"),
                ("nodes", "wan"),
                ("models", "wan"),
                ("models", "wan_clip_vision"),
                ("bridge", None),
            ],
        )
        self.assertNotIn(("models", "wan_quality"), complete)

    def test_bridge_config_defaults_match_old_bat(self) -> None:
        defaults = self.manifest.bridge.config_defaults
        self.assertEqual(defaults["chunk_size_mb"], 1)
        self.assertEqual(defaults["connect_timeout"], 30)
        self.assertEqual(defaults["sock_read_timeout"], 120)
        self.assertEqual(defaults["fetch_timeout"], 45)
        self.assertEqual(defaults["max_concurrent_downloads"], 1)
        self.assertEqual(defaults["proxy_cooldown"], 60)
        self.assertEqual(defaults["proxy"], "")
        self.assertEqual(defaults["token"], "")
        self.assertEqual(len(self.manifest.bridge.allowed_hosts), 10)
        # Desktop hardens: local requests without a token are refused.
        self.assertFalse(self.manifest.bridge.allow_local_requests_without_token)
        self.assertEqual(self.manifest.bridge.version, "1.4.1")

    def test_python_rule(self) -> None:
        self.assertEqual(tuple(self.manifest.python.min_version), (3, 10))
        self.assertTrue(self.manifest.python.probe_requires_pip)
        self.assertGreater(len(self.manifest.python.candidate_priority), 0)

    def test_directories_aliases(self) -> None:
        self.assertEqual(self.manifest.directories["checkpoints"], "models/checkpoints")
        self.assertEqual(self.manifest.directories["ultralytics_bbox"], "models/ultralytics/bbox")


class SetupManifestValidationTests(unittest.TestCase):
    def _data(self) -> dict:
        path = default_manifest_path()
        return json.loads(path.read_text(encoding="utf-8"))

    def test_rejects_dest_escape(self) -> None:
        data = self._data()
        data["models"][0]["dest"] = "../evil.safetensors"
        with self.assertRaises(ManifestError):
            parse_manifest(data)

    def test_rejects_absolute_dest(self) -> None:
        data = self._data()
        data["models"][0]["dest"] = "C:/models/evil.safetensors"
        with self.assertRaises(ManifestError):
            parse_manifest(data)

    def test_rejects_bad_sha256(self) -> None:
        data = self._data()
        data["models"][0]["sha256"] = "not-a-hash"
        with self.assertRaises(ManifestError):
            parse_manifest(data)

    def test_rejects_unpinned_node(self) -> None:
        data = self._data()
        data["nodes"][0]["ref"] = "main"
        with self.assertRaises(ManifestError):
            parse_manifest(data)

    def test_rejects_host_outside_allowlist(self) -> None:
        data = self._data()
        data["models"][0]["sources"][0]["host"] = "evil.example.com"
        with self.assertRaises(ManifestError):
            parse_manifest(data)

    def test_rejects_missing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ManifestError):
                load_setup_manifest(Path(tmp) / "nope.json")


if __name__ == "__main__":
    unittest.main()
