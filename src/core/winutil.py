"""
Low-level Windows helpers: window focus, synthetic keystrokes, clipboard.

These are used only by the *fallback* extraction strategies (select-all +
clipboard) and by the blind mode.  The primary extraction path reads the
editor control's text directly through the SAP GUI Scripting object model and
needs none of this.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SW_RESTORE = 9

# Virtual key codes we need
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12  # ALT
VK_RETURN = 0x0D
VK_END = 0x23
VK_HOME = 0x24
VK_NEXT = 0x22  # Page Down
VK_PRIOR = 0x21  # Page Up
VK_F6 = 0x75

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("padding", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTunion)]


def _send(inputs):
    n = len(inputs)
    arr = (_INPUT * n)(*inputs)
    user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(_INPUT))


def _key_input(vk: int, up: bool = False) -> _INPUT:
    ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0,
                     time=0, dwExtraInfo=None)
    return _INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=ki))


def _char_input(ch: str, up: bool = False) -> _INPUT:
    ki = _KEYBDINPUT(wVk=0, wScan=ord(ch),
                     dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0),
                     time=0, dwExtraInfo=None)
    return _INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=ki))


def press(*vks: int, delay: float = 0.03) -> None:
    """Press keys as a chord (e.g. press(VK_CONTROL, ord('A')))."""
    _send([_key_input(v) for v in vks])
    time.sleep(delay)
    _send([_key_input(v, up=True) for v in reversed(vks)])
    time.sleep(delay)


def type_text(text: str, delay: float = 0.004) -> None:
    """Type a unicode string, character by character (layout independent)."""
    for ch in text:
        _send([_char_input(ch), _char_input(ch, up=True)])
        time.sleep(delay)


def bring_to_front(hwnd: int) -> bool:
    """Force a window to the foreground, working around Windows focus-stealing
    protection by temporarily attaching to the foreground thread's input queue."""
    if not hwnd:
        return False
    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return True
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        attached = bool(user32.AttachThreadInput(fg_tid, cur_tid, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if attached:
            user32.AttachThreadInput(fg_tid, cur_tid, False)
        time.sleep(0.15)
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def find_window(title_substring: str) -> int:
    """Return the first top-level window whose title contains the substring."""
    found = []
    substring = title_substring.lower()

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if substring in buf.value.lower() and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else 0


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if not length:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


# --------------------------------------------------------------------------
# Clipboard
# --------------------------------------------------------------------------

def get_clipboard_text(retries: int = 8, pause: float = 0.12) -> str:
    """Read CF_UNICODETEXT.  Retries because the clipboard is frequently locked
    by the application that just wrote to it."""
    import win32clipboard  # pywin32

    last_err = None
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_TEXT):
                    raw = win32clipboard.GetClipboardData(win32clipboard.CF_TEXT)
                    if isinstance(raw, bytes):
                        return raw.decode("cp1252", errors="replace")
                    return raw
                return ""
            finally:
                win32clipboard.CloseClipboard()
        except Exception as exc:  # clipboard busy
            last_err = exc
            time.sleep(pause)
    if last_err:
        raise RuntimeError(f"Could not read clipboard: {last_err}")
    return ""


def clear_clipboard() -> None:
    import win32clipboard

    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
            finally:
                win32clipboard.CloseClipboard()
            return
        except Exception:
            time.sleep(0.1)
