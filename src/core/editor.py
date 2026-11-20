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

from . import includes, objects, winutil
from .objects import Kind
from .session import SapSession, SapGuiError, describe

EDITOR_TYPES = {"GuiTextedit", "GuiTextEdit", "GuiAbapEditor"}
EDITOR_SUBTYPES = {"TEXTEDIT", "ABAPEDIT", "ABAPEDITOR", "TEXTEDITOR"}

# A GuiShell's `Text` property returns the ProgID of the embedded OLE control
# ("SAPGUI.AbapEditor.1", "SAPGUI.TextEdit.1", ...) - NOT the document.  The
# new ABAP editor is such a shell, so this string must never be mistaken for
# source code.
PROGID_RE = re.compile(r"^[A-Za-z][\w]*(?:\.[A-Za-z][\w]*)+(?:\.\d+)?$")

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
    quality: str = "good"       # "good" | "weak"

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def classify_capture(text: Optional[str]) -> Tuple[str, str]:
    """Judge whether a captured string can possibly be ABAP source.

    Returns ``(verdict, reason)`` where verdict is:

    ``bad``   - certainly not source; try the next strategy
    ``weak``  - might be a very small or comment-only include; keep as a
                fallback but prefer anything better
    ``good``  - looks like real ABAP

    The ``bad`` case exists because of a real failure mode: reading ``.Text``
    on the new ABAP editor's shell yields ``SAPGUI.AbapEditor.1`` - a
    perfectly non-empty string that is not the document at all.
    """
    if text is None:
        return "bad", "strategy returned nothing"
    stripped = text.strip()
    if not stripped:
        return "bad", "empty document"

    non_empty = [line for line in stripped.splitlines() if line.strip()]
    if len(non_empty) == 1 and PROGID_RE.match(non_empty[0]):
        return "bad", (
            f"control returned its OLE ProgID ({non_empty[0]!r}) instead of the "
            f"document - this control is not readable through its text property"
        )
    if len(stripped) < 8:
        return "bad", f"only {len(stripped)} characters captured"
    if ABAP_TOKENS.search(stripped):
        return "good", ""
    if len(non_empty) >= 5:
        return "good", ""
    return "weak", "no recognisable ABAP statements"


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

# What a "Display" control calls itself, across the common logon languages.
DISPLAY_LABEL = re.compile(
    r"\b(display|anzeigen|afficher|visualizar|visualizza|weergeven|显示|表示)\b", re.I
)

# Anything that could START the program.  A control matching this is never
# pressed, even if it also happens to match DISPLAY_LABEL.
EXECUTE_LABEL = re.compile(
    r"\b(execute|ausf[uü]hren|run|start|test|debug|debugging|exécuter|"
    r"ejecutar|eseguire|direct\s*processing|F8)\b", re.I
)


@dataclass
class Opened:
    """What actually ended up on screen after a navigation request."""

    name: str                 # the object whose source is now displayed
    kind: Kind
    note: str = ""            # e.g. "implementing class of ZII_..."
    source_based: bool = False


# Class Builder's whole-class view.  Switching to it means one capture
# returns every section and every method body, instead of navigating to each
# method separately.
SOURCE_BASED_LABEL = re.compile(
    r"source\s*code[\s-]*based|quellcode[\s-]*basiert|source\s*code\s*view", re.I
)
_SOURCE_BASED_MENU = (
    r"Goto|Springen|Utilities.*|Hilfsmittel.*",
    r".*Source\s*Code[\s-]*Based.*|.*Quellcode.*",
)

_SE24_DISPLAY_MENU = (
    r"Class/?Interface|Klasse/?Interface|Object\s*Type|Objekttyp|Program|Programm",
    r"Display|Anzeigen",
)

_SE24_NAME_FIELDS = (
    "wnd[0]/usr/ctxtSEOCLASS-CLSNAME",
    "wnd[0]/usr/txtSEOCLASS-CLSNAME",
)

_SWO1_NAME_FIELDS = (
    "wnd[0]/usr/ctxtTOJTB-NAME",
    "wnd[0]/usr/txtTOJTB-NAME",
    "wnd[0]/usr/ctxtRSWO-OBJTYPE",
)


def _set_named_field(sap: SapSession, candidates, hints, value: str) -> bool:
    """Fill the first field that exists, falling back to a name scan."""
    for element_id in candidates:
        field = sap.maybe(element_id)
        if field is not None:
            try:
                field.text = value
                return True
            except Exception:
                continue
    for comp in sap.walk("wnd[0]/usr"):
        if comp.type not in ("GuiCTextField", "GuiTextField"):
            continue
        tech = (comp.name or "").upper()
        if any(hint in tech for hint in hints):
            try:
                comp.obj.text = value
                return True
            except Exception:
                continue
    return False


def _press_display(sap: SapSession, menu, what: str) -> bool:
    """Press Display via the menu or a self-identifying toolbar button.

    Same rule as everywhere else in this file: never press a control that has
    not told us what it does, and never one that looks like Execute.
    """
    for route in ("menu", "button"):
        sap.assert_safe_to_navigate()
        if route == "menu":
            if not sap.press_menu(menu, forbid=EXECUTE_LABEL):
                continue
            used = "menu > Display"
        else:
            button, label = sap.find_button(DISPLAY_LABEL, forbid=EXECUTE_LABEL)
            if button is None:
                continue
            try:
                button.press()
            except Exception:
                continue
            used = f"toolbar button {label!r}"

        time.sleep(0.45)
        sap.assert_safe_to_navigate()
        sap.dismiss_popups()
        sap.assert_safe_to_navigate()
        sap.log(f"  {what}: opened via {used}")
        return True
    return False


def switch_to_source_based(sap: SapSession) -> bool:
    """Flip the Class Builder into its whole-class source view."""
    button, label = sap.find_button(SOURCE_BASED_LABEL, forbid=EXECUTE_LABEL,
                                    toolbar="wnd[0]")
    if button is not None:
        try:
            button.press()
            time.sleep(0.5)
            sap.dismiss_popups()
            sap.log(f"  switched to source-code-based view ({label!r})")
            return True
        except Exception:
            pass

    if sap.press_menu(_SOURCE_BASED_MENU, forbid=EXECUTE_LABEL):
        time.sleep(0.5)
        sap.dismiss_popups()
        sap.log("  switched to source-code-based view (menu)")
        return True

    return False


def open_class(sap: SapSession, name: str, settle: float = 0.4) -> Opened:
    """Open a global class in SE24, display mode, whole-source view.

    Falls back to SE38 on the generated class pool (`ZCL_FOO===...===CP`),
    which is an ordinary ABAP include and therefore readable by the existing
    program path.
    """
    sap.log(f"  opening class {name} in SE24 (display)")
    sap.assert_safe_to_navigate()
    sap.start_transaction("SE24")

    if not _set_named_field(sap, _SE24_NAME_FIELDS, ("CLSNAME", "CLASS"), name):
        raise SapGuiError("SE24 initial screen not recognised (class name field not found).")

    if _press_display(sap, _SE24_DISPLAY_MENU, f"class {name}"):
        source_based = switch_to_source_based(sap)
        if find_editor(sap) is not None or find_source_table(sap) is not None:
            if not source_based:
                sap.log("  (source-code-based view unavailable - capturing what is shown)")
            return Opened(name, Kind.CLASS, source_based=source_based)

    status = sap.status_message()
    sap.log(f"  SE24 route did not yield an editor{f' ({status})' if status else ''}"
            f" - trying the generated class pool")

    pool = objects.class_pool(name)
    open_program(sap, pool, settle)
    return Opened(pool, Kind.PROGRAM, note=f"class pool of {name}")


def open_interface(sap: SapSession, name: str, settle: float = 0.4) -> Opened:
    """An interface has no method bodies - its implementing class does.

    The SE80 tree's "Implementing Classes" folder is consulted first; if that
    cannot be read, SAP's proxy naming convention (ZII_X <-> ZCL_X) supplies
    the candidates.
    """
    candidates: List[str] = []
    try:
        candidates = includes.implementing_classes_from_tree(sap, name)
        if candidates:
            sap.log(f"  SE80 tree lists implementing class(es): {', '.join(candidates)}")
    except Exception:
        pass

    for guess in objects.implementing_class_candidates(name):
        if guess not in candidates:
            candidates.append(guess)

    if not candidates:
        raise ExtractionError(
            f"'{name}' is an interface and has no method implementations.\n"
            f"No implementing class could be determined from the SE80 tree or "
            f"from the naming convention."
        )

    errors = []
    for candidate in candidates:
        try:
            opened = open_class(sap, candidate, settle)
            opened.note = f"implementing class of {name}"
            sap.log(f"  using implementing class {candidate}")
            return opened
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
            continue

    raise ExtractionError(
        f"Could not open any implementing class for interface '{name}'. "
        f"Tried: {'; '.join(errors)}"
    )


# Fields on the SWO1 basic-data screen that carry the implementation program
_BO_PROGRAM_HINTS = ("PROGRAM", "PROGRAMM", "IMPL", "REPID")


def open_business_object(sap: SapSession, name: str, settle: float = 0.4) -> Opened:
    """BOR object type (SWO1).

    A business object has no source of its own: its methods are implemented
    in an ABAP program named on the object type's basic-data screen.  We open
    the object type, read that program name off the screen, and then export
    the program through the normal path.
    """
    sap.log(f"  opening business object {name} in SWO1 (display)")
    sap.assert_safe_to_navigate()
    sap.start_transaction("SWO1")

    if not _set_named_field(sap, _SWO1_NAME_FIELDS, ("OBJTYPE", "TOJTB", "OBJECT"), name):
        raise SapGuiError("SWO1 initial screen not recognised (object type field not found).")

    if not _press_display(sap, _SE24_DISPLAY_MENU, f"object type {name}"):
        raise ExtractionError(f"Could not open business object '{name}' in display mode.")

    program = ""
    for comp in sap.walk("wnd[0]"):
        if comp.type not in ("GuiCTextField", "GuiTextField"):
            continue
        tech = (comp.name or "").upper()
        value = (comp.text or "").strip().upper()
        if not value or not any(hint in tech for hint in _BO_PROGRAM_HINTS):
            continue
        if re.match(r"^(/[A-Z0-9_]{1,8}/)?[A-Z_][A-Z0-9_]{2,39}$", value):
            program = value
            sap.log(f"  implementation program from field {comp.name}: {program}")
            break

    if not program:
        raise ExtractionError(
            f"Business object '{name}' was opened, but no implementation program "
            f"name could be read from the SWO1 screen.\n"
            f"Open the object type in SWO1, note the program under Basic Data, "
            f"and export that program name directly."
        )

    open_program(sap, program, settle)
    return Opened(program, Kind.PROGRAM, note=f"implementation of business object {name}")


def open_object(
    sap: SapSession,
    name: str,
    kind: Optional[Kind] = None,
    settle: float = 0.4,
) -> Opened:
    """Open any supported repository object in display mode.

    Dispatches on the object kind; the program/include path is unchanged.
    """
    resolved = kind or objects.classify(name, sap.transaction, sap.window_title())

    if resolved == Kind.CLASS:
        return open_class(sap, name, settle)
    if resolved == Kind.INTERFACE:
        return open_interface(sap, name, settle)
    if resolved == Kind.BUSINESS_OBJECT:
        return open_business_object(sap, name, settle)

    open_program(sap, name, settle)
    return Opened(name, Kind.PROGRAM if resolved == Kind.UNKNOWN else resolved)


def open_program(sap: SapSession, name: str, settle: float = 0.4) -> None:
    """Open an ABAP program or include in SE38 in *display* mode.

    Display mode matters: it never takes an enqueue lock, so the export can run
    while colleagues are editing, and nothing can be modified by accident.
    """
    sap.log(f"  opening {name} in SE38 (display)")
    sap.assert_safe_to_navigate()
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

    # --------------------------------------------------------------
    # Open in DISPLAY mode.
    #
    # Only two routes are allowed, and both are self-identifying:
    #   1. the menu entry  Program > Display
    #   2. a toolbar button whose own label/tooltip says "Display"
    #
    # Blind function keys and hardcoded button indices are deliberately NOT
    # used.  An earlier version pressed tbar[1]/btn[17] assuming it was
    # "Display"; on some systems it is not, and the program was executed into
    # the ABAP Debugger.  If neither route is available the export stops with
    # an error rather than pressing something unidentified.
    # --------------------------------------------------------------
    opened = False
    route_used = ""

    for route in ("menu", "button"):
        sap.assert_safe_to_navigate()

        if route == "menu":
            if not sap.press_menu(_DISPLAY_MENU, forbid=EXECUTE_LABEL):
                continue
            route_used = "menu Program > Display"
        else:
            button, label = sap.find_button(DISPLAY_LABEL, forbid=EXECUTE_LABEL)
            if button is None:
                continue
            try:
                button.press()
            except Exception:
                continue
            route_used = f"toolbar button {label!r}"

        time.sleep(settle)
        sap.assert_safe_to_navigate()
        sap.dismiss_popups()
        sap.assert_safe_to_navigate()

        if find_editor(sap) is not None or find_source_table(sap) is not None:
            opened = True
            sap.log(f"  opened via {route_used}")
            break

        # Still on the initial screen? restore the name and try the other route.
        if sap.maybe("wnd[0]/usr/ctxtRS38M-PROGRAMM") is not None:
            try:
                sap.find("wnd[0]/usr/ctxtRS38M-PROGRAMM").text = name
            except Exception:
                pass

    if not opened:
        message = sap.status_message()
        raise ExtractionError(
            f"Could not open '{name}' in display mode.\n"
            f"Neither the 'Program > Display' menu entry nor a toolbar button "
            f"labelled 'Display' was found on the SE38 screen.\n"
            f"No other key was pressed, to avoid executing the program."
            + (f"\nSAP said: {message}" if message else "")
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

def _control_rect(comp) -> Optional[Tuple[int, int, int, int]]:
    """Screen rectangle of a control: (left, top, width, height)."""
    try:
        left = int(getattr(comp.obj, "ScreenLeft"))
        top = int(getattr(comp.obj, "ScreenTop"))
        width = int(getattr(comp.obj, "Width"))
        height = int(getattr(comp.obj, "Height"))
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return left, top, width, height


def _read_via_clipboard(
    sap: SapSession, comp, pause: float = 0.3, click: bool = True
) -> Optional[str]:
    """Select-all + copy out of the editor control.

    The new ABAP editor is an embedded OCX that keeps its own caret, and
    ``setFocus()`` on the hosting shell does not always move it.  So the
    control is physically clicked first - well inside its left edge, away from
    the scrollbar - which reliably puts the caret in the document.
    """
    hwnd = sap.hwnd
    if not winutil.bring_to_front(hwnd):
        sap.log("  (could not raise the SAP window - clipboard copy may fail)")

    try:
        comp.obj.setFocus()
    except Exception:
        pass

    if click:
        rect = _control_rect(comp)
        if rect:
            left, top, width, height = rect
            # 35% across (clear of the line-number gutter and the scrollbar),
            # 40% down (inside the text area even for short documents)
            winutil.click_at(int(left + width * 0.35), int(top + height * 0.40))
        else:
            sap.log("  (editor geometry unavailable - clicking skipped)")

    time.sleep(pause)

    sentinel = "<<<abap-exporter-sentinel>>>"
    try:
        winutil.set_clipboard_text(sentinel)
    except Exception:
        winutil.clear_clipboard()

    winutil.press(winutil.VK_CONTROL, ord("A"))
    time.sleep(pause)
    winutil.press(winutil.VK_CONTROL, ord("C"))
    time.sleep(pause + 0.25)

    try:
        text = winutil.get_clipboard_text()
    except Exception as exc:
        sap.log(f"  clipboard read failed: {exc}")
        return None

    if text == sentinel:
        sap.log("  clipboard unchanged - the copy did not reach the editor")
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
        # Variant B: the native Windows "Save As" common dialog.
        #
        # Only type if a real dialog window (class #32770) is actually on
        # screen.  Typing a path blindly into the SAP window would scatter
        # text into whatever field has focus and then press Enter on it.
        dialog = 0
        deadline = time.time() + 4
        while time.time() < deadline:
            dialog = winutil.find_dialog_window()
            if dialog:
                break
            time.sleep(0.2)

        if not dialog:
            sap.log("  download dialog never appeared - skipping this strategy")
            return None

        winutil.bring_to_front(dialog)
        time.sleep(0.3)
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
    """Return the full source of the object currently open in the editor.

    Every strategy's output is validated before it is accepted.  A strategy
    that produces something that cannot be source (an empty document, an OLE
    ProgID) is rejected and the next one is tried; only if nothing better
    turns up is a 'weak' capture used, and then it is flagged as such.
    """
    comp = find_editor(sap)
    table = find_source_table(sap)

    if comp is None and table is None:
        raise ExtractionError(
            f"No ABAP editor control is present on the screen for '{name}'."
        )

    plan: List[Tuple[str, callable]] = []
    if comp is not None:
        plan.append(("control_text", lambda: _read_control_text(comp)))
    if table is not None:
        plan.append(("table_control", lambda: _read_table_control(sap, table)))
    if allow_clipboard and comp is not None:
        plan.append(("clipboard", lambda: _read_via_clipboard(sap, comp)))
    if allow_download:
        plan.append(("menu_download", lambda: _read_via_download(sap, name, tmp_dir)))

    rejected: List[str] = []
    weak: Optional[Source] = None

    for strategy, run in plan:
        try:
            text = run()
        except Exception as exc:
            rejected.append(f"{strategy}: raised {exc}")
            continue

        verdict, reason = classify_capture(text)
        if verdict == "bad":
            rejected.append(f"{strategy}: {reason}")
            sap.log(f"  {strategy} rejected - {reason}")
            continue

        cleaned = _normalise(text)
        source = Source(
            name=name,
            text=cleaned,
            strategy=strategy,
            line_count=cleaned.count("\n") + 1 if cleaned else 0,
            quality=verdict,
        )
        if verdict == "good":
            return source
        if weak is None:
            weak = source
            sap.log(f"  {strategy} gave a doubtful capture ({reason}) - trying the next")

    if weak is not None:
        sap.log(f"  using the {weak.strategy} capture despite the warning")
        return weak

    detail = "; ".join(rejected) if rejected else "no strategy available"
    raise ExtractionError(f"No source text could be read for '{name}' ({detail}).")


def _normalise(text: str) -> str:
    """Normalise line endings only.  Indentation, casing, comments, blank lines
    and trailing spaces are left exactly as SAP returned them."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # The editor usually hands back one trailing empty line; keep content intact
    # but do not accumulate a growing tail of blanks.
    return text.rstrip("\n") + "\n" if text.strip() else text


def looks_like_abap(text: str) -> bool:
    return bool(ABAP_TOKENS.search(text or ""))


PROBE_PROPERTIES = (
    "Type", "SubType", "Name", "Id", "Text", "OcxEvents", "DragDropSupported",
    "ScreenLeft", "ScreenTop", "Width", "Height", "Changeable", "LineCount",
    "NumberOfUnprotectedTextParts", "CurrentLine", "CurrentColumn",
)

PROBE_METHODS = (
    "getNumberOfUnprotectedTextParts", "getUnprotectedTextPart",
    "getSelectionIndexStart", "getSelectionIndexEnd", "selectAll",
    "getLineText", "GetLineText", "setSelectionIndexes", "doubleClick",
)


def probe_editor(sap: SapSession) -> str:
    """Describe the editor control on the current screen.

    Written into the Diagnostics dump so an unsupported editor on any SAP
    release can be identified without guesswork: it shows which properties
    exist, what they return, and - critically - whether ``Text`` is the
    document or just the control's ProgID.
    """
    lines = ["", "=" * 72, "ABAP EDITOR PROBE", "=" * 72]

    lines.append(f"Transaction : {sap.transaction}")
    lines.append(f"Window title: {sap.window_title()!r}")
    detected = objects.classify("", sap.transaction, sap.window_title())
    lines.append(f"Screen suggests kind: {detected.value}")
    lines.append("")

    comp = find_editor(sap)
    table = find_source_table(sap)

    if comp is None and table is None:
        lines.append("No editor control found on the current screen.")
        lines.append("Open a program in SE38/SE80 display mode and probe again.")
        return "\n".join(lines)

    if comp is not None:
        lines.append(f"Editor control id : {comp.id}")
        lines.append(f"            type  : {comp.type}")
        lines.append(f"            subtype: {comp.subtype}")
        lines.append("")
        lines.append("-- properties --")
        for prop in PROBE_PROPERTIES:
            try:
                value = getattr(comp.obj, prop)
            except Exception as exc:
                lines.append(f"  {prop:<32} <unavailable: {type(exc).__name__}>")
                continue
            text = repr(value)
            if len(text) > 160:
                text = text[:160] + f"... ({len(str(value))} chars total)"
            lines.append(f"  {prop:<32} {text}")

        lines.append("")
        lines.append("-- methods present --")
        for method in PROBE_METHODS:
            lines.append(f"  {method:<36} {'yes' if hasattr(comp.obj, method) else 'no'}")

        lines.append("")
        raw = _read_control_text(comp)
        verdict, reason = classify_capture(raw)
        lines.append(f"-- control_text verdict: {verdict} --")
        if reason:
            lines.append(f"   reason: {reason}")
        if raw:
            preview = raw[:300].replace("\r", "")
            lines.append(f"   first 300 chars:\n{preview}")

        rect = _control_rect(comp)
        lines.append("")
        lines.append(f"-- screen rectangle: {rect} --")

    if table is not None:
        lines.append("")
        lines.append(f"Legacy table-control editor present: {table.id}")

    return "\n".join(lines)
