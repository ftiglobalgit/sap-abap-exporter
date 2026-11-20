"""
SAP GUI Scripting session layer.

This talks to the *running* SAP GUI process over its COM automation interface
(`SAPGUI` running object).  It is a client-side, screen-level API: every object
we touch corresponds to a control that is physically on the SAP screen.  There
is no RFC connection, no SAP NetWeaver RFC SDK, and nothing is installed on the
backend.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional, Sequence

LogFn = Callable[[str], None]


class SapGuiError(RuntimeError):
    """Any problem reaching or driving the SAP GUI."""


class ScriptingDisabledError(SapGuiError):
    """SAP GUI Scripting is switched off on the client or on the server."""


def _noop(_msg: str) -> None:
    pass


# --------------------------------------------------------------------------
# Component tree helpers
# --------------------------------------------------------------------------

@dataclass
class Comp:
    """A thin, forgiving wrapper around a GuiComponent."""

    obj: object
    id: str
    type: str
    subtype: str
    name: str
    text: str

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.type}{'/' + self.subtype if self.subtype else ''} {self.id!r} text={self.text!r}>"


def _safe(obj, attr, default=""):
    try:
        value = getattr(obj, attr)
    except Exception:
        return default
    if value is None:
        return default
    return value


def describe(obj) -> Comp:
    return Comp(
        obj=obj,
        id=str(_safe(obj, "Id")),
        type=str(_safe(obj, "Type")),
        subtype=str(_safe(obj, "SubType")),
        name=str(_safe(obj, "Name")),
        text=str(_safe(obj, "Text")),
    )


class SapSession:
    """Drives one SAP GUI session window."""

    def __init__(self, session, log: LogFn = _noop):
        self.session = session
        self.log = log

    # -- connection ------------------------------------------------------

    @classmethod
    def attach(
        cls,
        connection_index: int = 0,
        session_index: int = 0,
        log: LogFn = _noop,
    ) -> "SapSession":
        try:
            import win32com.client
        except ImportError as exc:  # pragma: no cover
            raise SapGuiError(
                "pywin32 is not installed - run: pip install pywin32"
            ) from exc

        try:
            sap_gui_auto = win32com.client.GetObject("SAPGUI")
        except Exception as exc:
            raise SapGuiError(
                "No running SAP GUI found.\n\n"
                "Start SAP Logon, open a session and log on to your system first."
            ) from exc

        try:
            application = sap_gui_auto.GetScriptingEngine
        except Exception as exc:
            raise ScriptingDisabledError(
                "SAP GUI is running but the scripting engine is not reachable.\n\n"
                "Enable it with:\n"
                "  SAP Logon > Options > Accessibility & Scripting > Scripting >\n"
                "  [x] Enable scripting   (and clear the two 'Notify when...' boxes)\n\n"
                "If it is still blocked, the server profile parameter\n"
                "  sapgui/user_scripting = TRUE\n"
                "must be set by your Basis team (read-only scripting is enough)."
            ) from exc

        if application is None:
            raise ScriptingDisabledError("The SAP GUI scripting engine returned nothing.")

        try:
            conn_count = int(application.Connections.Count)
        except Exception as exc:
            raise SapGuiError("Could not enumerate SAP GUI connections.") from exc

        if conn_count == 0:
            raise SapGuiError("SAP GUI is running but you are not logged on to any system.")

        if connection_index >= conn_count:
            connection_index = 0
        connection = application.Children(connection_index)

        try:
            sess_count = int(connection.Children.Count)
        except Exception:
            sess_count = 1
        if sess_count == 0:
            raise SapGuiError("The SAP connection has no open session.")
        if session_index >= sess_count:
            session_index = 0

        session = connection.Children(session_index)

        try:
            if bool(session.Busy):
                raise SapGuiError(
                    "The SAP session is busy. Wait for the current action to finish "
                    "and try again."
                )
        except SapGuiError:
            raise
        except Exception:
            pass

        obj = cls(session, log)
        log(f"Connected to SAP session: {obj.describe_session()}")
        return obj

    @staticmethod
    def list_sessions(log: LogFn = _noop) -> List[str]:
        """Human-readable list of every open session, for the session picker."""
        import win32com.client

        out: List[str] = []
        try:
            app = win32com.client.GetObject("SAPGUI").GetScriptingEngine
        except Exception:
            return out
        try:
            for ci in range(int(app.Connections.Count)):
                conn = app.Children(ci)
                for si in range(int(conn.Children.Count)):
                    sess = conn.Children(si)
                    info = sess.Info
                    out.append(
                        f"{ci}:{si}  {_safe(info, 'SystemName')} "
                        f"client {_safe(info, 'Client')} "
                        f"user {_safe(info, 'User')} "
                        f"[{_safe(info, 'Transaction')}]"
                    )
        except Exception:
            pass
        return out

    def describe_session(self) -> str:
        try:
            info = self.session.Info
            return (
                f"{_safe(info, 'SystemName')} / client {_safe(info, 'Client')} / "
                f"user {_safe(info, 'User')} / tcode {_safe(info, 'Transaction')}"
            )
        except Exception:
            return "unknown system"

    @property
    def transaction(self) -> str:
        try:
            return str(self.session.Info.Transaction or "")
        except Exception:
            return ""

    @property
    def hwnd(self) -> int:
        """Native window handle of the main window (for keystroke fallbacks)."""
        for attr in ("Handle", "WindowHandle"):
            try:
                value = getattr(self.find("wnd[0]"), attr)
                if value:
                    return int(value)
            except Exception:
                continue
        return 0

    # -- primitives ------------------------------------------------------

    def find(self, element_id: str):
        return self.session.findById(element_id)

    def maybe(self, element_id: str):
        """findById that returns None instead of raising."""
        try:
            return self.session.findById(element_id)
        except Exception:
            return None

    def exists(self, element_id: str) -> bool:
        return self.maybe(element_id) is not None

    def walk(self, root_id: str = "wnd[0]", max_depth: int = 25) -> Iterator[Comp]:
        """Depth-first walk over the live screen's component tree."""
        root = self.maybe(root_id)
        if root is None:
            return
        stack = [(root, 0)]
        seen = set()
        while stack:
            node, depth = stack.pop()
            try:
                comp = describe(node)
            except Exception:
                continue
            if comp.id and comp.id in seen:
                continue
            if comp.id:
                seen.add(comp.id)
            yield comp
            if depth >= max_depth:
                continue
            try:
                if not bool(_safe(node, "ContainerType", False)):
                    continue
                children = node.Children
                count = int(children.Count)
            except Exception:
                continue
            for i in range(count - 1, -1, -1):
                try:
                    stack.append((children.Item(i), depth + 1))
                except Exception:
                    try:
                        stack.append((children.ElementAt(i), depth + 1))
                    except Exception:
                        continue

    def dump_tree(self, path: str, root_id: str = "wnd[0]") -> str:
        """Write the whole component tree to a text file.

        This is the single most useful diagnostic when something does not work
        on a particular SAP release - send the file along with a bug report.
        """
        lines = [f"# SAP GUI component tree for {root_id}",
                 f"# session: {self.describe_session()}",
                 f"# window title: {self.window_title()}", ""]
        for comp in self.walk(root_id):
            lines.append(
                f"{comp.id}\n    type={comp.type} subtype={comp.subtype} "
                f"name={comp.name!r} text={comp.text!r}"
            )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        return path

    def window_title(self, window: str = "wnd[0]") -> str:
        win = self.maybe(window)
        return str(_safe(win, "Text")) if win is not None else ""

    def send_vkey(self, key: int, window: str = "wnd[0]") -> None:
        self.find(window).sendVKey(key)

    def status_message(self) -> str:
        bar = self.maybe("wnd[0]/sbar")
        if bar is None:
            return ""
        return str(_safe(bar, "Text")).strip()

    def status_type(self) -> str:
        bar = self.maybe("wnd[0]/sbar")
        if bar is None:
            return ""
        return str(_safe(bar, "MessageType")).upper()

    # -- popups ----------------------------------------------------------

    _NEGATIVE = re.compile(r"\b(no|nein|non|cancel|abbrechen)\b", re.I)
    _SAVE_HINT = re.compile(r"(save|sichern|speichern|changes|änderungen|unsaved)", re.I)

    def popup_text(self, window: str = "wnd[1]") -> str:
        parts = []
        for comp in self.walk(window, max_depth=12):
            if comp.type in ("GuiLabel", "GuiTextField", "GuiStatusPane") and comp.text:
                parts.append(comp.text.strip())
        return " | ".join(p for p in parts if p)

    def dismiss_popups(self, max_rounds: int = 6) -> List[str]:
        """Close modal dialogs that get in the way.

        Deliberately conservative: anything that looks like a 'do you want to
        save?' question is answered with *No*, never with Yes.  The exporter
        only ever opens objects in display mode, so there should be nothing to
        save in the first place.
        """
        messages: List[str] = []
        for _ in range(max_rounds):
            if not self.exists("wnd[1]"):
                break
            text = self.popup_text("wnd[1]")
            if text:
                messages.append(text)
                self.log(f"  popup: {text[:160]}")

            pressed = False
            if self._SAVE_HINT.search(text):
                pressed = self._press_button_matching("wnd[1]", self._NEGATIVE)
            if not pressed:
                try:
                    self.find("wnd[1]").sendVKey(0)  # Enter / Continue
                    pressed = True
                except Exception:
                    pressed = False
            if not pressed:
                try:
                    self.find("wnd[1]").sendVKey(12)  # F12 / Cancel
                except Exception:
                    try:
                        self.find("wnd[1]").close()
                    except Exception:
                        break
            time.sleep(0.15)
        return messages

    def _press_button_matching(self, window: str, pattern: re.Pattern) -> bool:
        for comp in self.walk(window, max_depth=12):
            if comp.type == "GuiButton" and pattern.search(comp.text or ""):
                try:
                    comp.obj.press()
                    return True
                except Exception:
                    continue
        return False

    # -- navigation ------------------------------------------------------

    def start_transaction(self, tcode: str, wait: float = 0.25) -> None:
        """Enter /n<tcode> in the command field. Works from any screen."""
        okcd = self.maybe("wnd[0]/tbar[0]/okcd")
        if okcd is None:
            raise SapGuiError(
                "The SAP command field is not available on this screen "
                "(it may be hidden by the GUI theme)."
            )
        okcd.text = f"/n{tcode}"
        self.find("wnd[0]").sendVKey(0)
        time.sleep(wait)
        self.dismiss_popups()

    def press_menu(self, path: Sequence[str], window: str = "wnd[0]") -> bool:
        """Select a menu entry by visible text, matching case-insensitively and
        ignoring the '&' accelerator markers.  Each element of *path* may be a
        regular expression, which lets us carry English + German labels."""
        node = self.maybe(f"{window}/mbar")
        if node is None:
            return False
        for raw in path:
            pattern = re.compile(raw, re.I)
            nxt = None
            try:
                children = node.Children
                count = int(children.Count)
            except Exception:
                return False
            for i in range(count):
                try:
                    child = children.Item(i)
                except Exception:
                    continue
                label = str(_safe(child, "Text")).replace("&", "").strip()
                if pattern.fullmatch(label) or pattern.search(label):
                    nxt = child
                    break
            if nxt is None:
                return False
            node = nxt
        try:
            node.select()
            time.sleep(0.2)
            return True
        except Exception:
            return False
