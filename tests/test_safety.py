"""Safety regression tests.

Field incident (SAP GUI 7.x, DE4/300): `open_object` pressed
`wnd[0]/tbar[1]/btn[17]` on the assumption that index 17 is "Display".  On
that system it was not, the program was EXECUTED, and the ABAP Debugger
opened at START-OF-SELECTION.

Two rules now hold, and these tests enforce them:

  1. A control is pressed only when its own label or tooltip identifies it,
     and never when that label looks like Execute/Run/Test/Debug.
  2. The moment the ABAP Debugger is on screen, all GUI driving stops.
"""

import re

import pytest

from core.editor import DISPLAY_LABEL, EXECUTE_LABEL
from core.session import DEBUGGER_TITLE, Comp, DebuggerOpenError, SapSession


class FakeButton:
    def __init__(self, text="", tooltip=""):
        self.Text = text
        self.Tooltip = tooltip
        self.pressed = False

    def press(self):
        self.pressed = True


class FakeSession(SapSession):
    """SapSession with the COM layer replaced by plain objects."""

    def __init__(self, buttons=(), title="SAP Easy Access"):
        super().__init__(session=None, log=lambda _m: None)
        self._buttons = list(buttons)
        self._title = title

    def window_title(self, window="wnd[0]"):
        return self._title

    def walk(self, root_id="wnd[0]", max_depth=25):
        for index, button in enumerate(self._buttons):
            yield Comp(
                obj=button,
                id=f"wnd[0]/tbar[1]/btn[{index}]",
                type="GuiButton",
                subtype="",
                name=f"btn[{index}]",
                text=button.Text,
            )

    def is_debugger_open(self):
        return bool(DEBUGGER_TITLE.search(self._title or ""))


# -- label matching -------------------------------------------------------

@pytest.mark.parametrize("label", [
    "Display", "Anzeigen", "Display (F7)", "Afficher", "Visualizar",
])
def test_display_labels_are_recognised(label):
    assert DISPLAY_LABEL.search(label)


@pytest.mark.parametrize("label", [
    "Execute", "Ausführen", "Execute (F8)", "Run", "Test", "Debugging",
    "Direct Processing", "Execute with variant",
])
def test_execute_labels_are_blacklisted(label):
    assert EXECUTE_LABEL.search(label)


def test_execute_is_never_chosen_even_when_listed_first():
    """The exact shape of the incident: Execute sits before Display."""
    execute = FakeButton("Execute", "Execute (F8)")
    display = FakeButton("Display", "Display (F7)")
    sap = FakeSession([execute, display])

    button, label = sap.find_button(DISPLAY_LABEL, forbid=EXECUTE_LABEL)
    assert button is display
    assert "Display" in label


def test_unlabelled_buttons_are_never_returned():
    """An index-only button carries no evidence of what it does."""
    sap = FakeSession([FakeButton("", ""), FakeButton("", "")])
    button, _ = sap.find_button(DISPLAY_LABEL, forbid=EXECUTE_LABEL)
    assert button is None


def test_no_button_is_pressed_during_lookup():
    buttons = [FakeButton("Execute", "Execute (F8)"), FakeButton("Check", "Check")]
    sap = FakeSession(buttons)
    sap.find_button(DISPLAY_LABEL, forbid=EXECUTE_LABEL)
    assert not any(b.pressed for b in buttons)


def test_display_with_execute_wording_is_skipped():
    """'Test/Execute' must lose even though a display-ish word appears."""
    trap = FakeButton("Test display run", "Execute in test mode")
    sap = FakeSession([trap])
    button, _ = sap.find_button(DISPLAY_LABEL, forbid=EXECUTE_LABEL)
    assert button is None


# -- debugger detection ---------------------------------------------------

@pytest.mark.parametrize("title", [
    "ABAP Debugger(1) (Exclusive) (erpdevclci_DE4_01)",
    "ABAP Debugger (Exclusive)",
    "ABAP-Debugger",
    "New ABAP Debugger",
])
def test_debugger_titles_are_detected(title):
    assert DEBUGGER_TITLE.search(title)


@pytest.mark.parametrize("title", [
    "ABAP Editor: Change Report ZMM_CREATEPO",
    "Object Navigator",
    "SAP Easy Access",
])
def test_normal_titles_are_not_flagged_as_debugger(title):
    assert not DEBUGGER_TITLE.search(title)


def test_navigation_is_refused_while_debugger_is_open():
    sap = FakeSession(title="ABAP Debugger(1) (Exclusive) (erpdevclci_DE4_01)")
    with pytest.raises(DebuggerOpenError) as excinfo:
        sap.assert_safe_to_navigate()
    message = str(excinfo.value)
    assert "Debugger" in message
    assert "breakpoints" in message.lower()


def test_navigation_allowed_on_a_normal_screen():
    sap = FakeSession(title="ABAP Editor: Display Report ZMM_CREATEPO")
    sap.assert_safe_to_navigate()  # must not raise


def test_export_aborts_whole_run_when_debugger_appears(monkeypatch, tmp_path):
    """One object failing is survivable; a debugger is not - stop everything."""
    from core import exporter as mod
    from core.exporter import ExportOptions, export_program

    calls = {"n": 0}

    def fake_open(_sap, name, kind=None, settle=0.0):
        calls["n"] += 1
        if calls["n"] == 2:
            raise DebuggerOpenError("ABAP Debugger is open")
        from core.editor import Opened
        from core.objects import Kind
        return Opened(name, Kind.PROGRAM)

    def fake_extract(_sap, name, _tmp, **_kw):
        from core.editor import Source
        text = "REPORT z.\nINCLUDE: z_top.\nINCLUDE z_f01.\n"
        return Source(name=name, text=text, strategy="clipboard", line_count=3)

    class Dummy:
        transaction = "SE80"

        def log(self, _m):
            pass

    monkeypatch.setattr(mod.editor, "open_object", fake_open)
    monkeypatch.setattr(mod.editor, "extract_source", fake_extract)
    monkeypatch.setattr(mod.includes, "includes_from_se80_tree", lambda _s, _p: [])

    logged = []
    result = export_program(Dummy(), "ZMAIN", ExportOptions(output_root=str(tmp_path)),
                            log=logged.append)

    assert calls["n"] == 2, "must stop immediately, not continue to object 3"
    assert any("Debugger" in line for line in logged)
    assert any("aborted" in (o.error or "") for o in result.objects)
