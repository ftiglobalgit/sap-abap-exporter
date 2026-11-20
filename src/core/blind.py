"""
Fallback mode for systems where SAP GUI Scripting is switched off.

Nothing here uses COM.  The SAP window is located by its caption, keystrokes
are injected with SendInput and the source is retrieved through the Windows
clipboard.  That is genuinely all the automation Windows allows against a
process that refuses to expose an automation interface.

This mode is **less reliable by construction** and says so loudly:

* the program name can only be read from the window caption;
* navigation depends on the command field shortcut (Ctrl+/) and on F7 meaning
  "Display" on the SE38 initial screen;
* every step is verified against the window caption, and the run stops and
  asks the user rather than pressing unknown keys - in particular F8
  (Execute) is never sent, so a program can never be started by accident.

Prefer the scripting engine.  Use this only to get unblocked.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import winutil
from .detect import TITLE_RE

LogFn = Callable[[str], None]

SAP_TITLE_HINTS = (
    "SAP Easy Access",
    "ABAP Editor",
    "Object Navigator",
    "SAP R/3",
    "Repository Browser",
)

VK_SLASH = 0xBF  # OEM_2 - the '/' key on a US layout


class BlindModeError(RuntimeError):
    pass


@dataclass
class BlindSession:
    hwnd: int
    log: LogFn = lambda _m: None

    @classmethod
    def find(cls, log: LogFn = lambda _m: None) -> "BlindSession":
        for hint in SAP_TITLE_HINTS:
            hwnd = winutil.find_window(hint)
            if hwnd:
                log(f"Found SAP window: {winutil.window_title(hwnd)!r}")
                return cls(hwnd=hwnd, log=log)
        raise BlindModeError(
            "No SAP GUI window found. Open SAP GUI (SE80 or SE38) and try again."
        )

    # -- basics ----------------------------------------------------------

    @property
    def title(self) -> str:
        return winutil.window_title(self.hwnd)

    def focus(self) -> bool:
        return winutil.bring_to_front(self.hwnd)

    def detect_program(self) -> Optional[str]:
        match = TITLE_RE.search(self.title or "")
        return match.group(1).upper() if match else None

    # -- navigation ------------------------------------------------------

    def command(self, okcode: str, wait: float = 1.2) -> None:
        """Type an OK code into the SAP command field via Ctrl+/."""
        if not self.focus():
            raise BlindModeError("Could not bring the SAP window to the foreground.")
        winutil.press(winutil.VK_CONTROL, VK_SLASH)
        time.sleep(0.25)
        winutil.type_text(okcode)
        time.sleep(0.15)
        winutil.press(winutil.VK_RETURN)
        time.sleep(wait)

    def open_object(self, name: str, wait: float = 1.5) -> bool:
        """SE38 -> type name -> F7 (Display).  Verified against the caption."""
        self.command("/nSE38")
        if not self.focus():
            return False
        # On the SE38 initial screen the cursor sits in the program field.
        winutil.type_text(name)
        time.sleep(0.2)
        winutil.press(0x76)  # F7 = Display.  F8 (Execute) is never sent.
        time.sleep(wait)

        caption = (self.title or "").upper()
        if name.upper() in caption:
            return True
        self.log(
            f"  could not confirm that {name} opened "
            f"(window caption is {self.title!r})"
        )
        return False

    # -- extraction ------------------------------------------------------

    def capture_editor(self, pause: float = 0.35) -> str:
        """Select all + copy from whatever control currently has focus.

        The caller must make sure the caret is inside the ABAP editor - in
        practice, clicking once in the source area.  Ctrl+A grabs the whole
        document, so scrolling and document size are irrelevant.
        """
        if not self.focus():
            raise BlindModeError("Could not bring the SAP window to the foreground.")
        time.sleep(pause)
        winutil.clear_clipboard()
        winutil.press(winutil.VK_CONTROL, ord("A"))
        time.sleep(pause)
        winutil.press(winutil.VK_CONTROL, ord("C"))
        time.sleep(pause + 0.25)
        text = winutil.get_clipboard_text()
        if not text.strip():
            raise BlindModeError(
                "The clipboard stayed empty. Click inside the source code area "
                "of the ABAP editor, then try again."
            )
        return text
