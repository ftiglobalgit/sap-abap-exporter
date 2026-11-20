"""Program-detection regression tests.

Field incident (SE80, DE4/300): the window caption was "Object Navigator".
The title pattern treated `object` as a keyword, so it extracted
object=NAVIGATOR and scored it 80, above the SE80 tree selection
(ZMM_CREATEPO, 75).  The app exported a program called NAVIGATOR.
"""

import pytest

from core.detect import GENERIC_TITLES, TITLE_RE, Detection, detect_program, strip_to_main_program
from core.session import Comp


class FakeTree:
    def __init__(self, selected=None, top=None):
        self.selectedNode = selected
        self.topNode = top

    def GetNodeTextByKey(self, key):
        return key


class FakeField:
    def __init__(self, value):
        self.Text = value


class FakeSap:
    """Minimal stand-in exposing only what detect_program touches."""

    def __init__(self, title="", tcode="SE80", fields=(), tree=None):
        self._title = title
        self.transaction = tcode
        self._fields = list(fields)   # (technical_name, value)
        self._tree = tree

    def window_title(self, window="wnd[0]"):
        return self._title

    def walk(self, root_id="wnd[0]", max_depth=25):
        for tech, value in self._fields:
            yield Comp(obj=FakeField(value), id=f"wnd[0]/usr/ctxt{tech}",
                       type="GuiCTextField", subtype="", name=tech, text=value)
        if self._tree is not None:
            yield Comp(obj=self._tree, id="wnd[0]/shellcont/shell",
                       type="GuiTree", subtype="Tree", name="tree", text="")


# -- the reported bug -----------------------------------------------------

def test_object_navigator_caption_does_not_yield_navigator():
    """The exact failure: SE80 open, caption 'Object Navigator'."""
    sap = FakeSap(
        title="Object Navigator",
        tcode="SE80",
        tree=FakeTree(selected="ZMM_CREATEPO"),
    )
    result = detect_program(sap)
    assert result.name == "ZMM_CREATEPO"
    assert "NAVIGATOR" not in [c.name for c in result.candidates]


def test_object_is_not_a_title_keyword():
    assert TITLE_RE.search("Object Navigator") is None


@pytest.mark.parametrize("caption", [
    "Object Navigator", "ABAP Workbench", "Repository Browser",
    "SAP Easy Access", "Repository Information System", "  Object Navigator  ",
])
def test_generic_screen_captions_are_ignored(caption):
    assert GENERIC_TITLES.match(caption.strip())


def test_tree_selection_outranks_a_window_caption():
    """Even with a real caption match, what you selected wins."""
    sap = FakeSap(
        title="ABAP Editor: Display Report ZOLD_PROGRAM",
        tcode="SE80",
        tree=FakeTree(selected="ZMM_CREATEPO"),
    )
    assert detect_program(sap).name == "ZMM_CREATEPO"


def test_tree_node_caption_with_description_is_split():
    sap = FakeSap(
        title="Object Navigator",
        tree=FakeTree(selected="ZMM_CREATEPO  PR Management & PO Creation Dashboard"),
    )
    assert detect_program(sap).name == "ZMM_CREATEPO"


# -- the signals that should still work -----------------------------------

def test_se38_field_is_the_strongest_signal():
    sap = FakeSap(
        title="ABAP Editor: Initial Screen",
        tcode="SE38",
        fields=[("RS38M-PROGRAMM", "ZMM_CREATEPO")],
        tree=FakeTree(selected="ZSOMETHING_ELSE"),
    )
    assert detect_program(sap).name == "ZMM_CREATEPO"


def test_editor_caption_still_detects_the_report():
    sap = FakeSap(title="ABAP Editor: Change Report ZMM_CREATEPO", tcode="SE38")
    assert detect_program(sap).name == "ZMM_CREATEPO"


def test_include_caption_is_detected_then_mapped_by_the_caller():
    """detect_program reports what it saw; the caller maps include -> main."""
    sap = FakeSap(title="ABAP Editor: Display Include ZMM_CREATEPO_F01")
    detected = detect_program(sap).name
    assert detected == "ZMM_CREATEPO_F01"
    assert strip_to_main_program(detected) == "ZMM_CREATEPO"


def test_namespaced_name_from_caption():
    sap = FakeSap(title="ABAP Editor: Display Report /ACME/ZTOOL", tcode="SE38")
    assert detect_program(sap).name == "/ACME/ZTOOL"


def test_nothing_detected_on_a_bare_screen():
    sap = FakeSap(title="SAP Easy Access", tcode="SESSION_MANAGER")
    result = detect_program(sap)
    assert result.name is None
    assert "no program name" in result.evidence


def test_customer_object_wins_a_tie():
    sap = FakeSap(
        title="Object Navigator",
        fields=[("PROGRAM", "RSTXSCRP"), ("PROGRAMM", "ZMM_CREATEPO")],
    )
    assert detect_program(sap).name == "ZMM_CREATEPO"


def test_standard_program_still_detectable_when_unopposed():
    sap = FakeSap(title="ABAP Editor: Display Report RSTXSCRP", tcode="SE38")
    assert detect_program(sap).name == "RSTXSCRP"


def test_evidence_lists_every_candidate():
    sap = FakeSap(
        title="ABAP Editor: Display Report ZOLD",
        tree=FakeTree(selected="ZMM_CREATEPO"),
    )
    evidence = detect_program(sap).evidence
    assert "ZMM_CREATEPO" in evidence and "ZOLD" in evidence


# -- include -> main program ----------------------------------------------

@pytest.mark.parametrize("include,main", [
    ("ZMM_CREATEPO_TOP", "ZMM_CREATEPO"),
    ("ZMM_CREATEPO_F01", "ZMM_CREATEPO"),
    ("ZMM_CREATEPO_O01", "ZMM_CREATEPO"),
    ("ZMM_CREATEPO_I01", "ZMM_CREATEPO"),
])
def test_include_names_map_to_the_main_program(include, main):
    assert strip_to_main_program(include) == main


def test_main_program_is_left_alone():
    assert strip_to_main_program("ZMM_CREATEPO") == "ZMM_CREATEPO"
    assert strip_to_main_program("ZTOP") == "ZTOP"
