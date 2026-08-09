# -*- coding: utf-8 -*-
"""Materialize a minimal astrbot stub package into a temp dir.

astrbot is not installed in the test environment. main.py imports from
``astrbot.api.event/star/message_components/provider`` and
``astrbot.core.message.message_event_result`` at module level, so a full
package skeleton is required (in-memory modules are not importable as
submodules). Only enough surface for *import* is provided; runtime behavior is
not simulated.
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

_FILES = {
    "astrbot/__init__.py": "",
    "astrbot/api/__init__.py": (
        "import logging\n"
        "logger = logging.getLogger('astrbot.stub')\n"
    ),
    "astrbot/api/event.py": (
        "class AstrMessageEvent:\n"
        "    def __init__(self, *a, **k):\n"
        "        pass\n"
        "\n"
        "class MessageEventResult:\n"
        "    pass\n"
        "\n"
        "class EventMessageType:\n"
        "    ALL = 'all'\n"
        "    GROUP_MESSAGE = 'group_message'\n"
        "    PRIVATE_MESSAGE = 'private_message'\n"
        "    NOTICE = 'notice'\n"
        "    REQUEST = 'request'\n"
        "\n"
        "class _Filter:\n"
        "    EventMessageType = EventMessageType\n"
        "\n"
        "    def command(self, name=None, **kwargs):\n"
        "        def deco(func):\n"
        "            func._filter_command = (name, kwargs)\n"
        "            return func\n"
        "        return deco\n"
        "\n"
        "    def on_llm_request(self, **kwargs):\n"
        "        def deco(func):\n"
        "            func._filter_llm_request = kwargs\n"
        "            return func\n"
        "        return deco\n"
        "\n"
        "    def on_llm_response(self, **kwargs):\n"
        "        def deco(func):\n"
        "            func._filter_llm_response = kwargs\n"
        "            return func\n"
        "        return deco\n"
        "\n"
        "    def event_message_type(self, *args, **kwargs):\n"
        "        def deco(func):\n"
        "            func._filter_event_message_type = (args, kwargs)\n"
        "            return func\n"
        "        return deco\n"
        "\n"
        "    def on_decorating_result(self, *args, **kwargs):\n"
        "        def deco(func):\n"
        "            func._filter_on_decorating_result = (args, kwargs)\n"
        "            return func\n"
        "        return deco\n"
        "\n"
        "    def llm_tool(self, *args, **kwargs):\n"
        "        def deco(func):\n"
        "            func._filter_llm_tool = (args, kwargs)\n"
        "            return func\n"
        "        return deco\n"
        "\n"
        "filter = _Filter()\n"
    ),
    "astrbot/api/star.py": (
        "class Context:\n"
        "    def __init__(self, *a, **k):\n"
        "        pass\n"
        "\n"
        "class Star:\n"
        "    def __init__(self, context: Context, config: dict = None):\n"
        "        self.context = context\n"
        "        self.config = config or {}\n"
        "        self.api = None\n"
        "\n"
        "class StarTools:\n"
        "    @staticmethod\n"
        "    def get_data_dir(*a, **k):\n"
        "        return ''\n"
        "\n"
        "def register(*args, **kwargs):\n"
        "    def deco(cls):\n"
        "        return cls\n"
        "    return deco\n"
    ),
    "astrbot/api/message_components.py": (
        "class Message:\n"
        "    pass\n"
        "\n"
        "class MessageChain:\n"
        "    pass\n"
        "\n"
        "class Plain:\n"
        "    def __init__(self, text=''):\n"
        "        self.text = str(text)\n"
        "\n"
        "    def __str__(self):\n"
        "        return self.text\n"
        "\n"
        "class Image:\n"
        "    pass\n"
        "\n"
        "class At:\n"
        "    pass\n"
        "\n"
        "class AtAll:\n"
        "    pass\n"
        "\n"
        "class Face:\n"
        "    pass\n"
        "\n"
        "class Reply:\n"
        "    pass\n"
        "\n"
        "class Record:\n"
        "    pass\n"
        "\n"
        "class Video:\n"
        "    pass\n"
        "\n"
        "class File:\n"
        "    pass\n"
        "\n"
        "class Audio:\n"
        "    pass\n"
        "\n"
        "class Music:\n"
        "    pass\n"
        "\n"
        "class Node:\n"
        "    pass\n"
        "\n"
        "class Nodes:\n"
        "    pass\n"
        "\n"
        "class Chinese:\n"
        "    pass\n"
        "\n"
        "class Custom:\n"
        "    pass\n"
        "\n"
        "__all__ = [\n"
        "    'Message', 'MessageChain', 'Plain', 'Image', 'At', 'AtAll', 'Face',\n"
        "    'Reply', 'Record', 'Video', 'File', 'Audio', 'Music', 'Node', 'Nodes',\n"
        "    'Chinese', 'Custom',\n"
        "]\n"
    ),
    "astrbot/api/provider.py": (
        "class LLMResponse:\n"
        "    def __init__(self, *a, **k):\n"
        "        pass\n"
    ),
    "astrbot/core/__init__.py": "",
    "astrbot/core/message/__init__.py": "",
    "astrbot/core/message/message_event_result.py": (
        "class MessageChain:\n"
        "    pass\n"
    ),
}


def install_astrbot_stub() -> Path:
    """Write the stub package to a temp dir and prepend it to sys.path."""
    root = Path(tempfile.mkdtemp(prefix="astrbot_stub_"))
    for rel, content in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root
