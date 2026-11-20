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

from .session import SapSession

# An ABAP program / include name: optional /namespace/, then letters, digits,
# underscore.  SAP allows up to 40 characters for program names.
ABAP_NAME = re.compile(r"^(/[A-Z0-9_]{1,8}/)?[A-Z_][A-Z0-9_]{1,39}$")

# Field names that carry a program name on the various editor screens.
FIELD_HINTS = (
    ("RS38M-PROGRAMM", 95),       # SE38 initial screen
    ("RSEUX-OBJECT_NAME", 90),    # SE80 object field
    ("RSEUI-OBJECT", 85),
    ("PROGRAM", 70),
    ("PROGRAMM", 70),
    ("REPID", 65),
    ("OBJECT_NAME", 60),
    ("INCLUDE", 60),
)

# "ABAP Editor: Change Report ZMM_CREATEPO", "ABAP Editor: Display Include ..."
TITLE_RE = re.compile(
    r"(?:report|include|program|programm|objekt|object|modulpool|module pool)\s+"
    r"((?:/[A-Z0-9_]{1,8}/)?[A-Z_][A-Z0-9_]{1,39})",
    re.I,
)

NOISE = {
    "DISPLAY", "CHANGE", "ACTIVE", "INACTIVE", "REPORT", "INCLUDE", "PROGRAM",
    "OBJECT", "EDITOR", "SAP", "ABAP", "NONE", "SOURCE", "CODE", "MAIN",
}


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
        existing = scores.get(name)
        if existing is None or score > existing.score:
            scores[name] = Candidate(name, score, source)

    title = sap.window_title()
    tcode = sap.transaction

    # 1) Window title - "ABAP Editor: Change Report ZMM_CREATEPO"
    match = TITLE_RE.search(title or "")
    if match:
        add(match.group(1), 80, "window title")

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
                add(str(tree.GetNodeTextByKey(key)), 75, "SE80 tree selection")
            except Exception:
                pass
        break

    # 4) Anything in a GuiTitlebar / header label that looks like a program
    for comp in sap.walk("wnd[0]", max_depth=6):
        if comp.type in ("GuiTitlebar", "GuiLabel") and comp.text:
            match = TITLE_RE.search(comp.text)
            if match:
                add(match.group(1), 55, "screen header")

    ordered = sorted(scores.values(), key=lambda c: (-c.score, c.name))
    best = ordered[0].name if ordered else None
    return Detection(name=best, candidates=ordered, transaction=tcode, title=title)


def strip_to_main_program(name: str) -> str:
    """Best-effort mapping from an include back to its main program.

    `ZMM_CREATEPO_TOP` / `_F01` / `_O01` ... all belong to `ZMM_CREATEPO`.
    Purely a naming-convention heuristic; it is only used as a *suggestion*
    which the user can override before the export starts.
    """
    upper = (name or "").upper()
    match = re.match(r"^(.*?)_(TOP|F\d{2}|O\d{2}|I\d{2}|E\d{2}|P\d{2}|C\d{2}|CL\d{2})$", upper)
    if match and len(match.group(1)) >= 3:
        return match.group(1)
    return upper
