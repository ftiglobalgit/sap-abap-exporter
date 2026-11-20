"""Regression tests for the capture validator.

Field failure (SAP GUI 7.70, DE4/300, new ABAP editor): reading `.Text` on the
editor shell returned the OLE ProgID "SAPGUI.AbapEditor.1".  It was non-empty,
so it was accepted and written to ZMM_CREATEPO.txt as the whole "source".
These tests pin that down.
"""

import pytest

from core.editor import Source, classify_capture, extract_source


@pytest.mark.parametrize("progid", [
    "SAPGUI.AbapEditor.1",
    "SAPGUI.TextEdit.1",
    "SAPGUI.HTMLViewer.1",
    "SAPGUI.GridView.1",
    "Sapgui.Shell.2",
])
def test_ole_progid_is_rejected(progid):
    verdict, reason = classify_capture(progid)
    assert verdict == "bad"
    assert "ProgID" in reason


def test_progid_with_trailing_newline_is_rejected():
    """What actually landed in the file: the ProgID plus a line break."""
    verdict, _ = classify_capture("SAPGUI.AbapEditor.1\r\n")
    assert verdict == "bad"


def test_empty_and_whitespace_are_rejected():
    assert classify_capture("")[0] == "bad"
    assert classify_capture("   \r\n  \t ")[0] == "bad"
    assert classify_capture(None)[0] == "bad"


def test_real_abap_is_accepted():
    source = "REPORT zmm_createpo.\nINCLUDE: zmm_createpo_top.\n"
    assert classify_capture(source)[0] == "good"


def test_short_but_real_include_is_accepted():
    assert classify_capture("DATA: gv_x TYPE i.\nTABLES eban.\n")[0] == "good"


def test_comment_only_include_is_weak_not_bad():
    """A legitimately near-empty include must not be thrown away."""
    verdict, _ = classify_capture("* placeholder - nothing here yet\n")
    assert verdict == "weak"


def test_long_text_without_keywords_is_accepted():
    text = "\n".join(f"  line number {i} of something" for i in range(20))
    assert classify_capture(text)[0] == "good"


def test_a_dotted_identifier_inside_real_source_is_not_mistaken_for_progid():
    source = "REPORT ztest.\nWRITE cl_abap_char_utilities=>cr_lf.\n"
    assert classify_capture(source)[0] == "good"


# -- strategy fall-through ------------------------------------------------

class _FakeComp:
    id = "wnd[0]/usr/cntlEDITOR/shellcont/shell"
    type = "GuiShell"
    subtype = "AbapEditor"

    class obj:
        Text = "SAPGUI.AbapEditor.1"


class _FakeSap:
    def __init__(self):
        self.logged = []

    def log(self, message):
        self.logged.append(message)


REAL = "REPORT zmm_createpo.\nINCLUDE: zmm_createpo_top.\n"


def test_falls_through_from_progid_to_clipboard(monkeypatch):
    """The exact production bug: control_text must not win with a ProgID."""
    from core import editor as mod

    sap = _FakeSap()
    monkeypatch.setattr(mod, "find_editor", lambda _s: _FakeComp())
    monkeypatch.setattr(mod, "find_source_table", lambda _s: None)
    monkeypatch.setattr(mod, "_read_via_clipboard", lambda *a, **k: REAL)

    source = extract_source(sap, "ZMM_CREATEPO", "/tmp")
    assert source.strategy == "clipboard"
    assert source.text.startswith("REPORT zmm_createpo.")
    assert "SAPGUI.AbapEditor" not in source.text
    assert any("rejected" in m for m in sap.logged)


def test_falls_through_to_download_when_clipboard_also_fails(monkeypatch):
    from core import editor as mod

    sap = _FakeSap()
    monkeypatch.setattr(mod, "find_editor", lambda _s: _FakeComp())
    monkeypatch.setattr(mod, "find_source_table", lambda _s: None)
    monkeypatch.setattr(mod, "_read_via_clipboard", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_read_via_download", lambda *a, **k: REAL)

    source = extract_source(sap, "ZMM_CREATEPO", "/tmp")
    assert source.strategy == "menu_download"


def test_raises_when_every_strategy_produces_junk(monkeypatch):
    from core import editor as mod

    sap = _FakeSap()
    monkeypatch.setattr(mod, "find_editor", lambda _s: _FakeComp())
    monkeypatch.setattr(mod, "find_source_table", lambda _s: None)
    monkeypatch.setattr(mod, "_read_via_clipboard", lambda *a, **k: "")
    monkeypatch.setattr(mod, "_read_via_download", lambda *a, **k: None)

    with pytest.raises(mod.ExtractionError) as excinfo:
        extract_source(sap, "ZMM_CREATEPO", "/tmp")
    assert "ProgID" in str(excinfo.value)


def test_weak_capture_is_used_only_as_last_resort(monkeypatch):
    from core import editor as mod

    sap = _FakeSap()
    monkeypatch.setattr(mod, "find_editor", lambda _s: _FakeComp())
    monkeypatch.setattr(mod, "find_source_table", lambda _s: None)
    monkeypatch.setattr(mod, "_read_via_clipboard", lambda *a, **k: "* nothing here yet\n")
    monkeypatch.setattr(mod, "_read_via_download", lambda *a, **k: None)

    source = extract_source(sap, "ZMM_EMPTY", "/tmp")
    assert source.quality == "weak"
    assert source.strategy == "clipboard"


def test_good_capture_wins_over_earlier_weak_one(monkeypatch):
    from core import editor as mod

    sap = _FakeSap()
    monkeypatch.setattr(mod, "find_editor", lambda _s: _FakeComp())
    monkeypatch.setattr(mod, "find_source_table", lambda _s: None)
    monkeypatch.setattr(mod, "_read_via_clipboard", lambda *a, **k: "* weak\n")
    monkeypatch.setattr(mod, "_read_via_download", lambda *a, **k: REAL)

    source = extract_source(sap, "ZMM_CREATEPO", "/tmp")
    assert source.strategy == "menu_download"
    assert source.quality == "good"


def test_strategy_exception_does_not_abort_the_chain(monkeypatch):
    from core import editor as mod

    def boom(*_a, **_k):
        raise RuntimeError("clipboard locked by another process")

    sap = _FakeSap()
    monkeypatch.setattr(mod, "find_editor", lambda _s: _FakeComp())
    monkeypatch.setattr(mod, "find_source_table", lambda _s: None)
    monkeypatch.setattr(mod, "_read_via_clipboard", boom)
    monkeypatch.setattr(mod, "_read_via_download", lambda *a, **k: REAL)

    source = extract_source(sap, "ZMM_CREATEPO", "/tmp")
    assert source.strategy == "menu_download"
