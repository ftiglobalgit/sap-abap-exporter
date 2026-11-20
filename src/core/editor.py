"""
Opening ABAP objects and getting their complete source text off the screen.

Three independent extraction strategies are tried in order.  All of them
return the *whole* source, not just the visible part - handling scrolling is
precisely the problem they are built to avoid:

  1. ``control_text``  - read the ``text`` property of the editor control.
     The control holds the entire document in memory regardless of what is
     scrolled into view.  Fast, exact, no focus stealing.

  2. ``clipboard``     - focus the editor control, Ctrl+A / Ctrl+C, read the
     Windows clipboard.  Works with the old editor, the new front-end editor
     and read-only display mode.  Needs the SAP window in the foreground.

  3. ``menu_download`` - drive  Utilities > More Utilities > Upload/Download >
     Download  and let SAP itself write the file.  Slowest, but immune to both
     of the above failing.

If all three fail for an object, the exporter records the failure rather than
writing a truncated file.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import winutil
from .session import SapSession, SapGuiError, describe

EDITOR_TYPES = {"GuiTextedit", "GuiTextEdit", "GuiAbapEditor"}
EDITOR_SUBTYPES = {"TEXTEDIT", "ABAPEDIT", "TEXTEDITOR"}

# Statements that always show up in real ABAP source; used as a weak sanity
# check so that an accidentally captured empty editor is not written to disk.
ABAP_TOKENS = re.compile(
    r"\b(report|include|form|endform|data|function|module|class|method|"
    r"select|write|if|endif|loop|endloop|type-pools|tables|constants|"
    r"parameters|select-options|define|start-of-selection)\b",
    re.I,
)


class ExtractionError(RuntimeError):
    pass


@dataclass
class Source:
    name: str
    text: str
    strategy: str
    line_count: int

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


# --------------------------------------------------------------------------
# Finding the editor control
# --------------------------------------------------------------------------

def find_editor(sap: SapSession):
    """Return the editor control on the current screen, or None.

    Rather than hard-coding ids such as
    ``wnd[0]/usr/cntlEDITOR_CONTAINER/shellcont/shell`` - which differ between
    SE38, SE80, SE24 and SAP releases - the whole screen is scanned for a
    component of an editor type.
    """
    best = None
    for comp in sap.walk("wnd[0]"):
        if comp.type in EDITOR_TYPES:
            return comp
        subtype = (comp.subtype or "").upper()
        if comp.type == "GuiShell" and subtype in EDITOR_SUBTYPES:
            return comp
        if comp.type == "GuiShell" and "EDIT" in subtype and best is None:
            best = comp
    return best


def find_source_table(sap: SapSession):
    """The *old* ABAP editor renders source in a table control."""
    for comp in sap.walk("wnd[0]"):
        if comp.type == "GuiTableControl" and "EDIT" in (comp.name or "").upper():
            return comp
    return None


# --------------------------------------------------------------------------
# Navigation
# --------------------------------------------------------------------------

_DISPLAY_MENU = (r"Program|Programm|Object|Objekt", r"Display|Anzeigen")


def open_object(sap: SapSession, name: str, settle: float = 0.4) -> None:
    """Open an ABAP program or include in SE38 in *display* mode.

    Display mode matters: it never takes an enqueue lock, so the export can run
    while colleagues are editing, and nothing can be modified by accident.
    """
    sap.log(f"  opening {name} in SE38 (display)")
    sap.start_transaction("SE38")

    field = None
    for candidate in (
        "wnd[0]/usr/ctxtRS38M-PROGRAMM",
        "wnd[0]/usr/txtRS38M-PROGRAMM",
    ):
        field = sap.maybe(candidate)
        if field is not None:
            break
    if field is None:
        # Fall back to any field whose technical name mentions PROGRAMM
        for comp in sap.walk("wnd[0]/usr"):
            if comp.type in ("GuiCTextField", "GuiTextField") and "PROGRAMM" in (comp.name or "").upper():
                field = comp.obj
                break
    if field is None:
        raise SapGuiError("SE38 initial screen not recognised (program name field not found).")

    field.text = name

    # "Source code" sub-object radio button, when the screen offers it
    radio = sap.maybe("wnd[0]/usr/radRS38M-FUNC_EDIT")
    if radio is not None:
        try:
            radio.select()
        except Exception:
            pass

    # Press "Display".  Try the toolbar button, then the menu, then F7/F2.
    opened = False
    for attempt in ("btn17", "menu", "vkey7", "vkey2"):
        if attempt == "btn17":
            button = sap.maybe("wnd[0]/tbar[1]/btn[17]")
            if button is None:
                continue
            try:
                button.press()
            except Exception:
                continue
        elif attempt == "menu":
            if not sap.press_menu(_DISPLAY_MENU):
                continue
        elif attempt == "vkey7":
            try:
                sap.send_vkey(7)
            except Exception:
                continue
        else:
            try:
                sap.send_vkey(2)
            except Exception:
                continue

        time.sleep(settle)
        sap.dismiss_popups()
        if find_editor(sap) is not None or find_source_table(sap) is not None:
            opened = True
            break
        # Still on the initial screen? restore the name and try the next route.
        if sap.maybe("wnd[0]/usr/ctxtRS38M-PROGRAMM") is not None:
            try:
                sap.find("wnd[0]/usr/ctxtRS38M-PROGRAMM").text = name
            except Exception:
                pass

    if not opened:
        message = sap.status_message()
        raise ExtractionError(
            f"Could not open '{name}' in the ABAP editor."
            + (f" SAP said: {message}" if message else "")
        )


# --------------------------------------------------------------------------
# Strategy 1 - read the control's text property
# --------------------------------------------------------------------------

def _read_control_text(comp) -> Optional[str]:
    obj = comp.obj
    for attr in ("Text", "text"):
        try:
            value = getattr(obj, attr)
        except Exception:
            continue
        if isinstance(value, str) and value.strip():
            return value
    # Some builds expose the document line by line
    try:
        count = int(getattr(obj, "LineCount"))
    except Exception:
        return None
    lines: List[str] = []
    for i in range(count):
        try:
            lines.append(str(obj.GetLineText(i)))
        except Exception:
            return None
    return "\r\n".join(lines) if lines else None


def _read_table_control(sap: SapSession, comp) -> Optional[str]:
    """Old editor: scroll the table control and harvest every row.

    Only used when nothing better is available - the table control genuinely
    does only hold the visible page, so this one *must* scroll.
    """
    table = comp.obj
    try:
        total = int(table.VerticalScrollbar.Maximum)
        page = int(table.VisibleRowCount)
    except Exception:
        return None

    lines: List[str] = []
    position = 0
    guard = 0
    while position <= total and guard < 5000:
        guard += 1
        try:
            table.VerticalScrollbar.Position = position
            time.sleep(0.05)
            table = sap.find(comp.id)  # the control is rebuilt after scrolling
            rows = int(table.VisibleRowCount)
        except Exception:
            break
        for r in range(rows):
            try:
                cell = table.GetCell(r, 0)
                lines.append(str(cell.Text).rstrip())
            except Exception:
                continue
        position += page
    # Trim duplicated overlap rows introduced by paging
    deduped: List[str] = []
    for line in lines:
        if deduped and deduped[-1] == line and line == "":
            continue
        deduped.append(line)
    return "\r\n".join(deduped) if deduped else None


# --------------------------------------------------------------------------
# Strategy 2 - clipboard
# --------------------------------------------------------------------------

def _read_via_clipboard(sap: SapSession, comp, pause: float = 0.25) -> Optional[str]:
    hwnd = sap.hwnd
    if not winutil.bring_to_front(hwnd):
        sap.log("  (could not raise the SAP window; clipboard copy may miss)")
    try:
        comp.obj.setFocus()
    except Exception:
        pass
    time.sleep(pause)

    winutil.clear_clipboard()
    winutil.press(winutil.VK_CONTROL, ord("A"))
    time.sleep(pause)
    winutil.press(winutil.VK_CONTROL, ord("C"))
    time.sleep(pause + 0.2)

    try:
        text = winutil.get_clipboard_text()
    except Exception as exc:
        sap.log(f"  clipboard read failed: {exc}")
        return None
    return text or None


# --------------------------------------------------------------------------
# Strategy 3 - let SAP download the file
# --------------------------------------------------------------------------

_DOWNLOAD_MENU = (
    r"Utilities.*|Hilfsmittel.*",
    r"More Utilities|Weitere Hilfsmittel|More utilities",
    r"Upload/Download",
    r"Download",
)


def _read_via_download(sap: SapSession, name: str, tmp_dir: str) -> Optional[str]:
    target = os.path.join(tmp_dir, f"{name}.dl.txt")
    if os.path.exists(target):
        os.remove(target)

    if not sap.press_menu(_DOWNLOAD_MENU):
        return None
    time.sleep(0.5)

    # Variant A: SAP's own file dialog (GUI_DOWNLOAD style)
    path_field = sap.maybe("wnd[1]/usr/ctxtDY_PATH")
    name_field = sap.maybe("wnd[1]/usr/ctxtDY_FILENAME")
    if path_field is not None and name_field is not None:
        path_field.text = tmp_dir if tmp_dir.endswith(os.sep) else tmp_dir + os.sep
        name_field.text = os.path.basename(target)
        for btn in ("wnd[1]/tbar[0]/btn[0]", "wnd[1]/usr/btnSAVE_BUTTON"):
            button = sap.maybe(btn)
            if button is not None:
                try:
                    button.press()
                    break
                except Exception:
                    continue
        else:
            sap.send_vkey(0, "wnd[1]")
    else:
        # Variant B: the native Windows "Save As" common dialog - type the path
        winutil.bring_to_front(sap.hwnd)
        time.sleep(0.4)
        winutil.type_text(target)
        time.sleep(0.2)
        winutil.press(winutil.VK_RETURN)

    # Wait for the file to materialise
    deadline = time.time() + 12
    while time.time() < deadline:
        if os.path.exists(target) and os.path.getsize(target) > 0:
            break
        sap.dismiss_popups(max_rounds=1)
        time.sleep(0.3)
    else:
        return None

    time.sleep(0.3)
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            with open(target, "r", encoding=encoding) as fh:
                text = fh.read()
            if text.strip():
                return text
        except (UnicodeDecodeError, UnicodeError):
            continue
        except OSError:
            return None
    return None


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def extract_source(
    sap: SapSession,
    name: str,
    tmp_dir: str,
    allow_clipboard: bool = True,
    allow_download: bool = True,
) -> Source:
    """Return the full source of the object currently open in the editor."""
    comp = find_editor(sap)
    attempts: List[Tuple[str, Optional[str]]] = []

    if comp is not None:
        attempts.append(("control_text", _read_control_text(comp)))

    if not _usable(attempts):
        table = find_source_table(sap)
        if table is not None:
            attempts.append(("table_control", _read_table_control(sap, table)))

    if not _usable(attempts) and allow_clipboard and comp is not None:
        attempts.append(("clipboard", _read_via_clipboard(sap, comp)))

    if not _usable(attempts) and allow_download:
        attempts.append(("menu_download", _read_via_download(sap, name, tmp_dir)))

    for strategy, text in attempts:
        if text and text.strip():
            cleaned = _normalise(text)
            return Source(
                name=name,
                text=cleaned,
                strategy=strategy,
                line_count=cleaned.count("\n") + 1 if cleaned else 0,
            )

    tried = ", ".join(s for s, _ in attempts) or "none"
    raise ExtractionError(
        f"No source text could be read for '{name}' (strategies tried: {tried})."
    )


def _usable(attempts) -> bool:
    return any(text and text.strip() for _, text in attempts)


def _normalise(text: str) -> str:
    """Normalise line endings only.  Indentation, casing, comments, blank lines
    and trailing spaces are left exactly as SAP returned them."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # The editor usually hands back one trailing empty line; keep content intact
    # but do not accumulate a growing tail of blanks.
    return text.rstrip("\n") + "\n" if text.strip() else text


def looks_like_abap(text: str) -> bool:
    return bool(ABAP_TOKENS.search(text or ""))
