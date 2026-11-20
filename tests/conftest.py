"""Test bootstrap.

``core.winutil`` loads user32.dll at import time, so on a non-Windows CI box
(and when running the parser tests on Linux) a stub is injected before the
modules that import it.  The Windows-only code paths are not exercised by
these tests - they need a live SAP GUI.
"""

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

if sys.platform != "win32":
    stub = types.ModuleType("core.winutil")
    stub.VK_CONTROL = 0x11
    stub.VK_RETURN = 0x0D
    stub.VK_SHIFT = 0x10
    stub.VK_MENU = 0x12
    stub.VK_END = 0x23
    stub.VK_HOME = 0x24
    stub.VK_NEXT = 0x22
    stub.VK_PRIOR = 0x21
    stub.VK_F6 = 0x75
    stub.press = lambda *a, **k: None
    stub.type_text = lambda *a, **k: None
    stub.bring_to_front = lambda *a, **k: True
    stub.find_window = lambda *a, **k: 0
    stub.window_title = lambda *a, **k: ""
    stub.get_clipboard_text = lambda *a, **k: ""
    stub.clear_clipboard = lambda *a, **k: None

    import core  # noqa: E402  (package import must come first)

    sys.modules["core.winutil"] = stub
    core.winutil = stub
