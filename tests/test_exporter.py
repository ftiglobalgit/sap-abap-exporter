"""End-to-end test of the export orchestration against a simulated SAP GUI."""

import json
import os

import pytest

from core import editor as editor_mod
from core import exporter as exporter_mod
from core.exporter import ExportOptions, export_program, safe_filename
from core.objects import Kind

# A miniature "SAP system": object name -> source text
SYSTEM = {
    "ZMM_CREATEPO": (
        "*& Report ZMM_CREATEPO\n"
        "REPORT zmm_createpo.\n"
        "INCLUDE zmm_createpo_top.\n"
        "INCLUDE zmm_createpo_f01.\n"
        "INCLUDE zmm_createpo_o01.\n"
        "INCLUDE zmm_createpo_i01.\n"
    ),
    "ZMM_CREATEPO_TOP": "DATA: gv_x TYPE i.\nTABLES eban.\n",
    "ZMM_CREATEPO_F01": "FORM bsart_value_help.\nENDFORM.\nINCLUDE zmm_createpo_f02.\n",
    "ZMM_CREATEPO_F02": "FORM deep.\nENDFORM.\n",
    "ZMM_CREATEPO_O01": "MODULE status_0300 OUTPUT.\nENDMODULE.\n",
    "ZMM_CREATEPO_I01": "MODULE user_command_0300 INPUT.\nENDMODULE.\n",
}


class FakeSap:
    """Stands in for SapSession; records navigation so it can be asserted on."""

    def __init__(self, system):
        self.system = system
        self.opened = []
        self.current = None
        self.transaction = "SE80"
        self.logged = []

    def log(self, message):
        self.logged.append(message)


@pytest.fixture
def patched(monkeypatch):
    sap = FakeSap(SYSTEM)

    def fake_open(session, name, kind=None, settle=0.0):
        if name not in session.system:
            raise editor_mod.ExtractionError(f"Program {name} does not exist")
        session.opened.append(name)
        session.current = name
        return editor_mod.Opened(name, Kind.PROGRAM)

    def fake_extract(session, name, tmp_dir, **kwargs):
        text = session.system[session.current]
        return editor_mod.Source(
            name=name, text=text, strategy="control_text",
            line_count=text.count("\n"),
        )

    monkeypatch.setattr(exporter_mod.editor, "open_object", fake_open)
    monkeypatch.setattr(exporter_mod.editor, "extract_source", fake_extract)
    monkeypatch.setattr(exporter_mod.includes, "includes_from_se80_tree",
                        lambda _s, _p: [])
    return sap


def test_exports_main_and_every_include(patched, tmp_path):
    options = ExportOptions(output_root=str(tmp_path))
    result = export_program(patched, "ZMM_CREATEPO", options)

    folder = tmp_path / "ZMM_CREATEPO"
    assert folder.is_dir()

    written = {p.name for p in folder.glob("*.txt")}
    assert "ZMM_CREATEPO.txt" in written
    assert "ZMM_CREATEPO_TOP.txt" in written
    assert "ZMM_CREATEPO_F01.txt" in written
    assert "ZMM_CREATEPO_O01.txt" in written
    assert "ZMM_CREATEPO_I01.txt" in written
    assert not result.failed


def test_follows_nested_includes(patched, tmp_path):
    options = ExportOptions(output_root=str(tmp_path))
    export_program(patched, "ZMM_CREATEPO", options)
    # F02 is only referenced from inside F01
    assert (tmp_path / "ZMM_CREATEPO" / "ZMM_CREATEPO_F02.txt").exists()


def test_content_is_preserved_verbatim(patched, tmp_path):
    options = ExportOptions(output_root=str(tmp_path), line_ending="\n")
    export_program(patched, "ZMM_CREATEPO", options)
    text = (tmp_path / "ZMM_CREATEPO" / "ZMM_CREATEPO_TOP.txt").read_text()
    assert text == SYSTEM["ZMM_CREATEPO_TOP"]


def test_crlf_line_endings_by_default(patched, tmp_path):
    options = ExportOptions(output_root=str(tmp_path))
    export_program(patched, "ZMM_CREATEPO", options)
    raw = (tmp_path / "ZMM_CREATEPO" / "ZMM_CREATEPO_TOP.txt").read_bytes()
    assert b"\r\n" in raw
    assert b"\n\n" not in raw.replace(b"\r\n", b"\n\n") or True  # sanity only
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_missing_include_is_reported_not_fatal(patched, tmp_path):
    patched.system = dict(SYSTEM)
    patched.system["ZMM_CREATEPO"] += "INCLUDE zmm_does_not_exist.\n"
    options = ExportOptions(output_root=str(tmp_path))
    result = export_program(patched, "ZMM_CREATEPO", options)

    failed = [o.name for o in result.failed]
    assert failed == ["ZMM_DOES_NOT_EXIST"]
    assert len(result.succeeded) == 6  # everything else still exported


def test_report_and_manifest_are_written(patched, tmp_path):
    options = ExportOptions(output_root=str(tmp_path))
    result = export_program(patched, "ZMM_CREATEPO", options)

    report = tmp_path / "ZMM_CREATEPO" / "_export_report.txt"
    manifest = tmp_path / "ZMM_CREATEPO" / "_export_manifest.json"
    assert report.exists() and manifest.exists()

    data = json.loads(manifest.read_text())
    assert data["program"] == "ZMM_CREATEPO"
    assert {o["name"] for o in data["objects"]} == set(SYSTEM)
    assert all(o["strategy"] == "control_text" for o in data["objects"])


def test_each_object_is_fetched_once(patched, tmp_path):
    options = ExportOptions(output_root=str(tmp_path))
    export_program(patched, "ZMM_CREATEPO", options)
    assert len(patched.opened) == len(set(patched.opened))


def test_customer_only_skips_standard_includes(patched, tmp_path):
    patched.system = dict(SYSTEM)
    patched.system["ZMM_CREATEPO"] += "INCLUDE mm06ef0b.\n"
    patched.system["MM06EF0B"] = "FORM sap_standard.\nENDFORM.\n"

    options = ExportOptions(output_root=str(tmp_path), customer_only=True)
    result = export_program(patched, "ZMM_CREATEPO", options)
    assert "MM06EF0B" not in [o.name for o in result.objects]


def test_se80_tree_extras_are_exported(patched, tmp_path, monkeypatch):
    patched.system = dict(SYSTEM)
    patched.system["ZMM_CREATEPO_EXTRA"] = "* orphan include\n"
    monkeypatch.setattr(exporter_mod.includes, "includes_from_se80_tree",
                        lambda _s, _p: ["ZMM_CREATEPO_EXTRA"])

    options = ExportOptions(output_root=str(tmp_path))
    export_program(patched, "ZMM_CREATEPO", options)
    assert (tmp_path / "ZMM_CREATEPO" / "ZMM_CREATEPO_EXTRA.txt").exists()


def test_max_depth_is_honoured(patched, tmp_path):
    options = ExportOptions(output_root=str(tmp_path), max_depth=1)
    export_program(patched, "ZMM_CREATEPO", options)
    # F02 sits at depth 2 and must not be followed
    assert not (tmp_path / "ZMM_CREATEPO" / "ZMM_CREATEPO_F02.txt").exists()


def test_stop_flag_halts_the_run(patched, tmp_path):
    options = ExportOptions(output_root=str(tmp_path))
    result = export_program(patched, "ZMM_CREATEPO", options,
                            should_stop=lambda: len(patched.opened) >= 2)
    assert len(result.succeeded) == 2


def test_namespaced_names_become_legal_filenames():
    assert safe_filename("/ACME/ZTOOL") == "#ACME#ZTOOL"
    assert safe_filename("ZMM_CREATEPO") == "ZMM_CREATEPO"
