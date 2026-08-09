# -*- coding: utf-8 -*-
"""Fake ComfyUI server for WineFox Desktop integration tests (Agent 10).

Test-only code. Never shipped. Binds 127.0.0.1 only.
"""
from .server import FakeComfyUI, start_fake_comfyui  # noqa: F401

__all__ = ["FakeComfyUI", "start_fake_comfyui"]
