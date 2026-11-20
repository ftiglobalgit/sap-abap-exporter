"""
Work out which ABAP program the user currently has selected in SAP GUI.

Several independent signals are read off the live screen and scored; the
highest-scoring candidate wins.  Nothing is guessed silently - the result
carries the evidence so the UI can show *why* a name was chosen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .objects import Kind, classify
from .session import SapSession

# An ABAP program / include name: optional /namespace/, then letters, digits,
# underscore.  SAP allows up to 40 characters for program names.
ABAP_NAME = re.compile(r"^(/[A-Z0-9_]{1,8}/)?[A-Z_][A-Z0-9_]{1,39}$")

# Field names that carry a program name on the various editor screens.
# The score is how much that signal is trusted, 0-100.
FIELD_HINTS = (
    ("RS38M-PROGRAMM", 92),       # SE38 initial screen - unambiguous
    ("RSEUX-OBJECT_NAME", 88),    # SE80 object name field
    ("RSEUI-OBJECT", 86),
    ("PROGRAMM", 76),
    ("PROGRAM", 74),
    ("REPID", 70),
    ("SEOCLASS-CLSNAME", 92),      # SE24 initial screen
    ("TOJTB-NAME", 92),            # SWO1 object type
    ("CLSNAME", 86),
    ("OBJTYPE", 84),
    ("OBJECT_NAME", 66),
    ("OBJ_NAME", 64),
    ("INCLUDE", 62),
)

# What the SE80 repository tree says you have selected.  This is the single
# most direct expression of "the program the user picked", so it outranks
# everything except a program name typed into SE38 itself.
TREE_SCORE = 90

# "ABAP Editor: Change Report ZMM_CREATEPO", "ABAP Editor: Display Include ..."
#
# NOTE: `object`/`objekt` are deliberately NOT keywords here.  SE80's own
# window caption is "Object Navigator", which parsed as object=NAVIGATOR and
# beat the correct answer.  A caption is weak evidence anyway - it is scored
# below every field and tree signal.
# `object type` is matched as a two-word phrase ("Object Type ZBUS2105"),
# never bare `object`, which would swallow "Object Navigator".
TITLE_RE = re.compile(
    r"(?:report|include|programm?|modulpool|module\s+pool|function\s+group|"
    r"class|klasse|interface|object\s+type|objekttyp|business\s+object)\s+"
    r"((?:/[A-Z0-9_]{1,8}/)?[A-Z_][A-Z0-9_]{1,39})",
    re.I,
)
TITLE_SCORE = 70

# Whole captions that describe a *screen*, never an object.  If the window
# title is one of these, no name is taken from it at all.
GENERIC_TITLES = re.compile(
    r"^\s*(object navigator|abap workbench|repository browser|"
    r"repository information system|sap easy access|object selection|"
    r"abap editor|initial screen|sap r/3)\s*$",
    re.I,
)

# Words that are part of SAP's UI vocabulary, never a customer program name.
NOISE = {
    "DISPLAY", "CHANGE", "ACTIVE", "INACTIVE", "REPORT", "INCLUDE", "PROGRAM",
    "OBJECT", "EDITOR", "SAP", "ABAP", "NONE", "SOURCE", "CODE", "MAIN",
    "NAVIGATOR", "WORKBENCH", "BROWSER", "REPOSITORY", "INFORMATION",
    "SYSTEM", "EASY", "ACCESS", "OVERVIEW", "INITIAL", "SCREEN", "WELCOME",
    "SELECTION", "LIST", "TOOLS", "UTILITIES", "ENVIRONMENT", "POOL",
    "GROUP", "CLASS", "INTERFACE", "STRUCTURE", "TABLE", "VIEW",
}

# Customer namespaces get a small tie-break bonus: this tool is used almost
# exclusively on custom code, and it breaks ties without excluding standard
# programs outright.
CUSTOMER_BONUS = 6
CUSTOMER_RE = re.compile(r"^(Z|Y|SAPMZ|SAPMY|\$|/[A-Z0-9_]{1,8}/)", re.I)


@dataclass
class Candidate:
    name: str
    score: int
    source: str


@dataclass
class Detection:
    name: Optional[str]
    candidates: List[Candidate] = field(default_factory=list)
    transaction: str = ""
    title: str = ""
    kind: "Kind" = Kind.UNKNOWN

    @property
    def evidence(self) -> str:
        if not self.candidates:
            return "no program name found on the current screen"
        return "; ".join(f"{c.name} ({c.source})" for c in self.candidates[:4])


def _plausible(name: str) -> bool:
    name = (name or "").strip().upper()
    if not name or len(name) < 3:
        return False
    if name in NOISE:
        return False
    return bool(ABAP_NAME.match(name))


def detect_program(sap: SapSession) -> Detection:
    """Read the current screen and return the most likely program name."""
    scores: Dict[str, Candidate] = {}

    def add(raw: str, score: int, source: str) -> None:
        name = (raw or "").strip().upper()
        if not _plausible(name):
            return
        if CUSTOMER_RE.match(name):
            score += CUSTOMER_BONUS
        existing = scores.get(name)
        if existing is None or score > existing.score:
            scores[name] = Candidate(name, score, source)

    title = sap.window_title()
    tcode = sap.transaction

    # 1) Window title - "ABAP Editor: Change Report ZMM_CREATEPO".
    #    Skipped entirely for captions that name a screen rather than an object.
    if title and not GENERIC_TITLES.match(title.strip()):
        match = TITLE_RE.search(title)
        if match:
            add(match.group(1), TITLE_SCORE, "window title")

    # 2) Input / text fields whose technical name points at a program
    for comp in sap.walk("wnd[0]"):
        if comp.type not in ("GuiCTextField", "GuiTextField", "GuiComboBox"):
            continue
        tech = (comp.name or "").upper()
        value = comp.text or ""
        for hint, score in FIELD_HINTS:
            if hint in tech:
                add(value, score, f"field {comp.name}")
                break

    # 3) The selected node of the SE80 repository browser tree
    for comp in sap.walk("wnd[0]"):
        if comp.type != "GuiTree" and "tree" not in (comp.subtype or "").lower():
            continue
        tree = comp.obj
        for getter in ("selectedNode", "topNode"):
            try:
                key = getattr(tree, getter)
            except Exception:
                continue
            if not key:
                continue
            try:
                text = str(tree.GetNodeTextByKey(key))
            except Exception:
                continue
            # A node caption can be "ZMM_CREATEPO  PR Management Dashboard"
            first = text.strip().split()[0] if text.strip() else ""
            score = TREE_SCORE if getter == "selectedNode" else TREE_SCORE - 15
            add(first, score, f"SE80 tree {getter}")
        break

    # 4) Anything in a GuiTitlebar / header label that looks like a program
    for comp in sap.walk("wnd[0]", max_depth=6):
        if comp.type in ("GuiTitlebar", "GuiLabel") and comp.text:
            if GENERIC_TITLES.match(comp.text.strip()):
                continue
            match = TITLE_RE.search(comp.text)
            if match:
                add(match.group(1), 55, "screen header")

    ordered = sorted(scores.values(), key=lambda c: (-c.score, c.name))
    best = ordered[0].name if ordered else None
    kind = classify(best, tcode, title) if best else Kind.UNKNOWN
    return Detection(name=best, candidates=ordered, transaction=tcode,
                     title=title, kind=kind)


def strip_to_main_program(name: str, kind: Optional[Kind] = None) -> str:
    """Best-effort mapping from an include back to its main program.

    `ZMM_CREATEPO_TOP` / `_F01` / `_O01` ... all belong to `ZMM_CREATEPO`.
    Purely a naming-convention heuristic; it is only used as a *suggestion*
    which the user can override before the export starts.

    Only applied to programs: a class called `ZCL_FOO_C01` is a class in its
    own right, not an include of `ZCL_FOO`.
    """
    upper = (name or "").upper()
    resolved = kind or classify(upper)
    if resolved in (Kind.CLASS, Kind.INTERFACE, Kind.BUSINESS_OBJECT,
                    Kind.FUNCTION_GROUP):
        return upper
    match = re.match(r"^(.*?)_(TOP|F\d{2}|O\d{2}|I\d{2}|E\d{2}|P\d{2}|C\d{2}|CL\d{2})$", upper)
    if match and len(match.group(1)) >= 3:
        return match.group(1)
    return upper
