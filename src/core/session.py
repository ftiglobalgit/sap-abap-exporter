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
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

LogFn = Callable[[str], None]


class SapGuiError(RuntimeError):
    """Any problem reaching or driving the SAP GUI."""


class ScriptingDisabledError(SapGuiError):
    """SAP GUI Scripting is switched off on the client or on the server."""


class DebuggerOpenError(SapGuiError):
    """The ABAP Debugger is on screen - a program is executing.

    The exporter must never continue in this state: it would be typing and
    pressing buttons inside a live debugging session.
    """


# The debugger titles itself e.g. "ABAP Debugger(1) (Exclusive) (host_DE4_01)"
DEBUGGER_TITLE = re.compile(r"\bABAP[\s-]?Debugger\b|\bDebugger\b", re.I)


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


@dataclass
class SessionEntry:
    """One SAP GUI session, as far as we were able to read it.

    A session that cannot be read is still listed, with the reason, because
    "this system blocks scripting" is a far more useful thing to show the
    user than an empty dropdown.
    """

    connection_index: int
    session_index: int
    system: str = ""
    client: str = ""
    user: str = ""
    transaction: str = ""
    description: str = ""
    error: str = ""
    reason: str = ""          # "blocked" | "no_session" | "unreadable"

    @property
    def usable(self) -> bool:
        return not self.error

    @property
    def label(self) -> str:
        head = f"{self.connection_index}:{self.session_index}"
        if self.error:
            name = self.system or self.description or "unknown system"
            return f"{head}  {name}  -- {self.error}"
        return (
            f"{head}  {self.system} client {self.client} "
            f"user {self.user} [{self.transaction}]"
        )


def _scripting_engine(retries: int = 3, pause: float = 0.4):
    """Attach to the running SAP GUI, retrying briefly.

    The ROT registration can lag a moment after SAP Logon starts, and the
    first call right after the user presses Refresh sometimes misses it.
    """
    try:
        import win32com.client
    except ImportError as exc:  # pragma: no cover - Windows only
        return None, RuntimeError(
            "pywin32 is not installed - run: pip install pywin32"
        )

    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            sap_gui_auto = win32com.client.GetObject("SAPGUI")
        except Exception as exc:
            last = exc
            time.sleep(pause)
            continue
        try:
            engine = sap_gui_auto.GetScriptingEngine
            if engine is not None:
                return engine, None
            last = RuntimeError("the scripting engine returned nothing")
        except Exception as exc:
            last = exc
        time.sleep(pause)
    return None, last


def _elevation_hint() -> str:
    """COM cannot cross integrity levels - a very common cause of 'not found'."""
    try:
        import ctypes

        if ctypes.windll.shell32.IsUserAnAdmin():
            return (
                "This exporter is running as Administrator. SAP GUI is probably "
                "not, and COM cannot see across elevation levels.\n"
                "Close it and start it normally (not 'Run as administrator')."
            )
    except Exception:
        pass
    return ""


def enumerate_sessions(log: LogFn = _noop) -> Tuple[List[SessionEntry], List[str]]:
    """Every session of every connection, with per-item error isolation.

    Each connection and each session is read inside its own try/except.  One
    system that refuses to be read must never hide the others: SAP GUI
    Scripting is authorised *per system* by the server profile parameter
    `sapgui/user_scripting`, so it is completely normal for one system in the
    list to answer and the next to refuse.
    """
    entries: List[SessionEntry] = []
    notes: List[str] = []

    engine, error = _scripting_engine()
    if engine is None:
        notes.append(f"Could not reach the SAP GUI scripting engine: {error}")
        hint = _elevation_hint()
        if hint:
            notes.append(hint)
        return entries, notes

    # How many connections?  Connections.Count is the documented way;
    # Children.Count is the fallback some releases prefer.
    count = None
    for attr in ("Connections", "Children"):
        try:
            count = int(getattr(engine, attr).Count)
            break
        except Exception:
            continue
    if count is None:
        notes.append("The scripting engine is reachable but refused to list connections.")
        return entries, notes

    if count == 0:
        notes.append(
            "SAP GUI is running but exposes no connections to scripting.\n"
            "Either you are not logged on, or SAP GUI was started before "
            "scripting was enabled - log off and on again."
        )
        return entries, notes

    for ci in range(count):
        try:
            conn = engine.Children(ci)
        except Exception as exc:
            entries.append(SessionEntry(
                ci, 0, error=f"connection unreadable ({exc})", reason="unreadable"))
            continue

        description = ""
        for attr in ("Description", "ConnectionString"):
            try:
                value = str(getattr(conn, attr) or "").strip()
            except Exception:
                continue
            if value:
                description = value[:60]
                break

        try:
            session_count = int(conn.Children.Count)
        except Exception as exc:
            entries.append(SessionEntry(
                ci, 0, description=description,
                error=_blocked_reason(exc), reason="blocked",
            ))
            continue

        if session_count == 0:
            # The connection is visible but exposes no session to scripting.
            # This is NOT the same as the server refusing scripting, and the
            # overwhelmingly common cause is that the connection was opened
            # before sapgui/user_scripting was switched on: capability is
            # negotiated at logon, so an existing connection keeps the old
            # state until you log off and on again.
            entries.append(SessionEntry(
                ci, 0, description=description,
                error="no session visible to scripting "
                      "(opened before scripting was enabled?)",
                reason="no_session",
            ))
            continue

        for si in range(session_count):
            entries.append(_read_session(conn, ci, si, description))

    notes.extend(_advice(entries))
    return entries, notes


def _name_of(entry: "SessionEntry") -> str:
    return (entry.system or entry.description
            or f"connection {entry.connection_index}")


def _advice(entries: List["SessionEntry"]) -> List[str]:
    """Turn the failures into advice that matches what actually went wrong.

    Keeping these apart matters: telling someone to set a profile parameter
    that their RZ11 screen already shows as TRUE sends them down the wrong
    path entirely.
    """
    out: List[str] = []
    blocked = [e for e in entries if e.reason == "blocked"]
    stale = [e for e in entries if e.reason == "no_session"]

    if blocked:
        names = ", ".join(_name_of(e) for e in blocked)
        out.append(
            f"{len(blocked)} connection(s) refused scripting access ({names}).\n"
            "The server has scripting switched off for that system. Ask Basis for:\n"
            "    sapgui/user_scripting              = TRUE\n"
            "    sapgui/user_scripting_set_readonly = FALSE"
        )

    if stale:
        names = ", ".join(_name_of(e) for e in stale)
        out.append(
            f"{len(stale)} connection(s) expose no session to scripting ({names}).\n"
            "\n"
            "If RZ11 already shows sapgui/user_scripting = TRUE on that system,\n"
            "the parameter is fine and the CONNECTION is stale. SAP GUI decides\n"
            "whether scripting is allowed when you log on, so a connection that\n"
            "was already open keeps the old setting.\n"
            "\n"
            "  1. Log off that system completely (close the connection in SAP\n"
            "     Logon, not just the window), then log on again.\n"
            "  2. Press Refresh here.\n"
            "\n"
            "If it still does not appear, the parameter was probably set on one\n"
            "application server only. In RZ11, change it again with the\n"
            "'Switch on all servers' box ticked - with load balancing you may\n"
            "land on a different server each time you log on."
        )

    return out


def _blocked_reason(exc: Exception) -> str:
    text = str(exc).lower()
    if "disabled" in text or "not permitted" in text or "denied" in text:
        return "scripting disabled for this system (sapgui/user_scripting)"
    return "scripting refused by this system (sapgui/user_scripting?)"


def _read_session(conn, ci: int, si: int, description: str) -> SessionEntry:
    try:
        sess = conn.Children(si)
    except Exception as exc:
        return SessionEntry(ci, si, description=description,
                            error=_blocked_reason(exc), reason="blocked")

    try:
        info = sess.Info
    except Exception as exc:
        return SessionEntry(ci, si, description=description,
                            error=_blocked_reason(exc), reason="blocked")

    return SessionEntry(
        connection_index=ci,
        session_index=si,
        system=str(_safe(info, "SystemName")),
        client=str(_safe(info, "Client")),
        user=str(_safe(info, "User")),
        transaction=str(_safe(info, "Transaction")),
        description=description,
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
        application, engine_error = _scripting_engine()
        if application is None:
            hint = _elevation_hint()
            raise ScriptingDisabledError(
                "SAP GUI is running but the scripting engine is not reachable.\n\n"
                f"({engine_error})\n\n"
                "Enable it with:\n"
                "  SAP Logon > Options > Accessibility & Scripting > Scripting >\n"
                "  [x] Enable scripting   (and clear the two 'Notify when...' boxes)\n\n"
                "If it is still blocked, the server profile parameter\n"
                "  sapgui/user_scripting = TRUE\n"
                "must be set by your Basis team, together with\n"
                "  sapgui/user_scripting_set_readonly = FALSE\n"
                "(read-only mode blocks typing a program name and pressing Display)."
                + (f"\n\n{hint}" if hint else "")
            )

        # Enumerate first, so a system that refuses scripting produces a
        # precise message instead of hiding every other system.
        entries, notes = enumerate_sessions(log)
        usable = [e for e in entries if e.usable]

        if not usable:
            detail = "\n".join(notes) if notes else ""
            listed = "\n".join(f"  {e.label}" for e in entries)
            raise ScriptingDisabledError(
                "No SAP session is available to scripting.\n\n"
                + (f"What SAP GUI reported:\n{listed}\n\n" if listed else "")
                + (detail or
                   "Log on to SAP, then press Refresh.")
            )

        chosen = None
        for entry in usable:
            if (entry.connection_index == connection_index
                    and entry.session_index == session_index):
                chosen = entry
                break
        if chosen is None:
            chosen = usable[0]
            log(f"Session {connection_index}:{session_index} is not available; "
                f"using {chosen.connection_index}:{chosen.session_index} instead.")

        connection_index = chosen.connection_index
        session_index = chosen.session_index

        try:
            connection = application.Children(connection_index)
            session = connection.Children(session_index)
        except Exception as exc:
            raise SapGuiError(
                f"Could not open SAP session {connection_index}:{session_index} ({exc})."
            ) from exc

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
        entries, _notes = enumerate_sessions(log)
        return [entry.label for entry in entries]

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

    # -- safety ----------------------------------------------------------

    def is_debugger_open(self) -> bool:
        """True if this session is showing the ABAP Debugger."""
        if DEBUGGER_TITLE.search(self.window_title() or ""):
            return True
        try:
            if DEBUGGER_TITLE.search(str(self.session.Info.Transaction or "")):
                return True
        except Exception:
            pass
        return False

    def assert_safe_to_navigate(self) -> None:
        """Refuse to drive the GUI while a program is executing.

        Called before and after every navigation step.  Driving the screen
        inside a debugger means pressing buttons in a running program, which
        can step, change variables or commit work.
        """
        if self.is_debugger_open():
            raise DebuggerOpenError(
                "The ABAP Debugger is open - a program is currently executing.\n\n"
                f"Window: {self.window_title()!r}\n\n"
                "The export has stopped and will not touch the screen while the "
                "debugger is on.\n\n"
                "What to do:\n"
                "  1. Switch to SAP and leave the debugger (Debugger > Exit, or "
                "Shift+F3).\n"
                "  2. Remove or deactivate breakpoints in this program "
                "(Utilities > Breakpoints > Delete > All).\n"
                "  3. Go back to SE80/SE38 and run the export again."
            )

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
        self.assert_safe_to_navigate()
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

    def find_button(
        self,
        want: re.Pattern,
        forbid: Optional[re.Pattern] = None,
        toolbar: str = "wnd[0]/tbar[1]",
    ):
        """Find a toolbar button by what it *says*, not by its index.

        Button indices are not stable across SAP releases, screen variants or
        user layouts.  Pressing `tbar[1]/btn[17]` because it is usually
        "Display" is how this tool once managed to press "Execute" instead and
        launch the program into the debugger.  Never again: a button is only
        pressed when its own label or tooltip identifies it, and never when it
        matches the *forbid* pattern.
        """
        for comp in self.walk(toolbar, max_depth=4):
            if comp.type != "GuiButton":
                continue
            label = f"{comp.text or ''} {_safe(comp.obj, 'Tooltip')}".strip()
            if not label:
                continue
            if forbid is not None and forbid.search(label):
                continue
            if want.search(label):
                return comp.obj, label
        return None, ""

    def press_menu(
        self,
        path: Sequence[str],
        window: str = "wnd[0]",
        forbid: Optional[re.Pattern] = None,
    ) -> bool:
        """Select a menu entry by visible text, matching case-insensitively and
        ignoring the '&' accelerator markers.  Each element of *path* may be a
        regular expression, which lets us carry English + German labels.

        *forbid* rejects a candidate entry outright - used to make sure a
        loose pattern can never land on Execute/Run/Test."""
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
                if forbid is not None and forbid.search(label):
                    continue
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
