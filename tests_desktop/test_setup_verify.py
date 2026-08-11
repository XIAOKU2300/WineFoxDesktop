from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

from desktop_runtime.setup_verify import (
    VerifyFailure,
    sha256_of,
    verify_gguf,
    verify_min_size,
    verify_model_file,
    verify_node_dir,
    verify_safetensors,
)


def make_safetensors(tensors: dict[str, tuple[int, int]]) -> bytes:
    """tensors: name -> (start, end) offsets within the data region."""
    header: dict[str, object] = {"__metadata__": {}}
    for name, (start, end) in tensors.items():
        header[name] = {"dtype": "F32", "shape": [end - start], "data_offsets": [start, end]}
    header_bytes = json.dumps(header).encode("utf-8")
    data_len = max((end for _, (_, end) in tensors.items()), default=0)
    return struct.pack("<Q", len(header_bytes)) + header_bytes + b"\x00" * data_len


class SafetensorsVerifyTests(unittest.TestCase):
    def test_valid_single_tensor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.safetensors"
            path.write_bytes(make_safetensors({"a": (0, 4)}))
            verify_safetensors(path)  # must not raise

    def test_valid_metadata_only_no_tensors_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.safetensors"
            header = json.dumps({"__metadata__": {}}).encode("utf-8")
            path.write_bytes(struct.pack("<Q", len(header)) + header + b"\x00" * 8)
            with self.assertRaises(VerifyFailure):
                verify_safetensors(path)

    def test_truncated_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.safetensors"
            path.write_bytes(b"\x00" * 8)
            with self.assertRaises(VerifyFailure):
                verify_safetensors(path)

    def test_bad_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.safetensors"
            # header claims a 999-byte tensor but only 4 data bytes exist
            header = json.dumps({"__metadata__": {}, "a": {"dtype": "F32", "shape": [999], "data_offsets": [0, 999]}}).encode("utf-8")
            path.write_bytes(struct.pack("<Q", len(header)) + header + b"\x00" * 4)
            with self.assertRaises(VerifyFailure):
                verify_safetensors(path)

    def test_data_padding_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.safetensors"
            # max_end (4) != data_len (8)
            path.write_bytes(make_safetensors({"a": (0, 4)}) + b"\x00" * 4)
            with self.assertRaises(VerifyFailure):
                verify_safetensors(path)

    def test_header_too_large(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.safetensors"
            path.write_bytes(struct.pack("<Q", 1 << 40) + b"\x00" * 64)
            with self.assertRaises(VerifyFailure):
                verify_safetensors(path)


class GgufVerifyTests(unittest.TestCase):
    def test_valid_gguf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.gguf"
            path.write_bytes(b"GGUF" + b"\x00" * 16)
            verify_gguf(path)

    def test_bad_magic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.gguf"
            path.write_bytes(b"NOPE" + b"\x00" * 16)
            with self.assertRaises(VerifyFailure):
                verify_gguf(path)


class ModelFileTests(unittest.TestCase):
    def test_verify_by_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.safetensors"
            path.write_bytes(make_safetensors({"a": (0, 4)}))
            verify_model_file(path, "safetensors", min_gb=0.0)
            with self.assertRaises(VerifyFailure):
                verify_model_file(path, "gguf")

    def test_min_size_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.pt"
            path.write_bytes(b"\x00" * 10)
            verify_model_file(path, "pt")  # no min -> ok
            with self.assertRaises(VerifyFailure):
                verify_min_size(path, 0.000001)

    def test_sha256_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.bin"
            path.write_bytes(b"hello" * 1000)
            digest = sha256_of(path)
            self.assertEqual(len(digest), 64)
            self.assertEqual(digest, sha256_of(path))


class NodeDirTests(unittest.TestCase):
    def test_node_dir_requires_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            node = Path(tmp) / "ComfyUI-Demo"
            node.mkdir()
            self.assertFalse(verify_node_dir(node))
            (node / "__init__.py").write_text("", encoding="utf-8")
            self.assertTrue(verify_node_dir(node))


if __name__ == "__main__":
    unittest.main()
