"""End-to-end export of classes, interfaces and business objects.

The SAP side is simulated; what is exercised is the orchestration: which
files land on disk, under which names, and what the report says.
"""

import json

import pytest

from core import editor as editor_mod
from core import exporter as exporter_mod
from core.exporter import ExportOptions, export_program
from core.objects import Kind

from test_abapclass import ODATA, PROXY

REPORT = "REPORT zmm_createpo.\nINCLUDE: zmm_createpo_top.\n"
TOP = "DATA: gv_x TYPE i.\n"


class FakeSap:
    def __init__(self, system, routes=None):
        self.system = system
        self.routes = routes or {}      # requested name -> name actually opened
        self.opened = []
        self.current = None
        self.transaction = "SE24"

    def log(self, _message):
        pass


@pytest.fixture
def patched(monkeypatch):
    sap = FakeSap(
        system={
            "ZCL_ZO_EP2_PR_DPC_EXT": ODATA,
            "ZCL_SIIS_GR_PULL_REQ": PROXY,
            "ZMM_CREATEPO": REPORT,
            "ZMM_CREATEPO_TOP": TOP,
            "ZBUS2105_IMPL": REPORT,
        },
        routes={
            # interface resolves to its implementing class
            "ZII_SIIS_GR_PULL_REQ": ("ZCL_SIIS_GR_PULL_REQ", Kind.CLASS,
                                     "implementing class of ZII_SIIS_GR_PULL_REQ"),
            # business object resolves to its implementation program
            "ZBUS2105": ("ZBUS2105_IMPL", Kind.PROGRAM,
                         "implementation of business object ZBUS2105"),
        },
    )

    def fake_open(session, name, kind=None, settle=0.0):
        target, target_kind, note = session.routes.get(
            name, (name, Kind.CLASS if name.startswith("ZCL_") else Kind.PROGRAM, "")
        )
        if target not in session.system:
            raise editor_mod.ExtractionError(f"{target} does not exist")
        session.opened.append(target)
        session.current = target
        return editor_mod.Opened(target, target_kind, note=note)

    def fake_extract(session, name, _tmp, **_kw):
        text = session.system[session.current]
        return editor_mod.Source(name=name, text=text, strategy="clipboard",
                                 line_count=text.count("\n"))

    monkeypatch.setattr(exporter_mod.editor, "open_object", fake_open)
    monkeypatch.setattr(exporter_mod.editor, "extract_source", fake_extract)
    monkeypatch.setattr(exporter_mod.includes, "includes_from_se80_tree",
                        lambda _s, _p: [])
    return sap


# -- OData class: redefinitions -------------------------------------------

def test_odata_class_writes_full_source_and_every_method(patched, tmp_path):
    options = ExportOptions(output_root=str(tmp_path))
    result = export_program(patched, "ZCL_ZO_EP2_PR_DPC_EXT", options)

    folder = tmp_path / "ZCL_ZO_EP2_PR_DPC_EXT"
    assert (folder / "ZCL_ZO_EP2_PR_DPC_EXT.txt").exists()

    methods = {p.name for p in (folder / "methods").glob("*.txt")}
    assert "PR_HEADERSET_GET_ENTITYSET.txt" in methods
    assert "CONSTRUCTOR.txt" in methods
    # '/' is illegal in a filename, so the namespace is encoded
    assert "#IWBEP#IF_MGW_APPL_SRV_RUNTIME~CREATE_DEEP_ENTITY.txt" in methods
    assert len(methods) == 6
    assert not result.failed


def test_only_redefinitions_option(patched, tmp_path):
    options = ExportOptions(output_root=str(tmp_path), only_redefinitions=True)
    export_program(patched, "ZCL_ZO_EP2_PR_DPC_EXT", options)

    methods = {p.name for p in (tmp_path / "ZCL_ZO_EP2_PR_DPC_EXT" / "methods").glob("*.txt")}
    assert len(methods) == 4
    assert "PR_HEADERSET_GET_ENTITYSET.txt" in methods
    assert "CONSTRUCTOR.txt" not in methods          # declared, not redefined
    assert "HELPER_NOT_REDEFINED.txt" not in methods


def test_method_file_content_is_the_method_only(patched, tmp_path):
    export_program(patched, "ZCL_ZO_EP2_PR_DPC_EXT",
                   ExportOptions(output_root=str(tmp_path), line_ending="\n"))
    body = (tmp_path / "ZCL_ZO_EP2_PR_DPC_EXT" / "methods" /
            "PR_HEADERSET_GET_ENTITYSET.txt").read_text()
    assert body.strip().upper().startswith("METHOD PR_HEADERSET_GET_ENTITYSET.")
    assert body.strip().upper().endswith("ENDMETHOD.")
    assert "SELECT * FROM eban" in body
    assert "CONSTRUCTOR" not in body


def test_report_marks_redefinitions(patched, tmp_path):
    export_program(patched, "ZCL_ZO_EP2_PR_DPC_EXT",
                   ExportOptions(output_root=str(tmp_path)))
    manifest = json.loads(
        (tmp_path / "ZCL_ZO_EP2_PR_DPC_EXT" / "_export_manifest.json").read_text()
    )
    by_name = {o["name"]: o for o in manifest["objects"]}

    # methods are reported fully qualified: CLASS~METHOD, or IFACE~METHOD
    # for interface methods, so two interfaces can never collide
    redefined = by_name["ZCL_ZO_EP2_PR_DPC_EXT~PR_HEADERSET_GET_ENTITYSET"]
    assert "redefinition" in redefined["strategy"]

    iface = by_name["/IWBEP/IF_MGW_APPL_SRV_RUNTIME~CREATE_DEEP_ENTITY"]
    assert "redefinition" in iface["strategy"] and "interface" in iface["strategy"]

    plain = by_name["ZCL_ZO_EP2_PR_DPC_EXT~CONSTRUCTOR"]
    assert "redefinition" not in plain["strategy"]


def test_methods_can_be_written_flat(patched, tmp_path):
    options = ExportOptions(output_root=str(tmp_path), methods_subfolder="")
    export_program(patched, "ZCL_ZO_EP2_PR_DPC_EXT", options)
    folder = tmp_path / "ZCL_ZO_EP2_PR_DPC_EXT"
    assert (folder / "PR_HEADERSET_GET_ENTITYSET.txt").exists()
    assert not (folder / "methods").exists()


def test_splitting_can_be_switched_off(patched, tmp_path):
    options = ExportOptions(output_root=str(tmp_path), split_methods=False)
    export_program(patched, "ZCL_ZO_EP2_PR_DPC_EXT", options)
    folder = tmp_path / "ZCL_ZO_EP2_PR_DPC_EXT"
    assert (folder / "ZCL_ZO_EP2_PR_DPC_EXT.txt").exists()
    assert not (folder / "methods").exists()


# -- interface -> implementing class --------------------------------------

def test_interface_exports_its_implementing_class(patched, tmp_path):
    options = ExportOptions(output_root=str(tmp_path))
    result = export_program(patched, "ZII_SIIS_GR_PULL_REQ", options)

    # folder keeps the requested name, file carries what was actually opened
    folder = tmp_path / "ZII_SIIS_GR_PULL_REQ"
    assert (folder / "ZCL_SIIS_GR_PULL_REQ.txt").exists()
    assert not result.failed


def test_interface_method_file_is_written(patched, tmp_path):
    export_program(patched, "ZII_SIIS_GR_PULL_REQ",
                   ExportOptions(output_root=str(tmp_path), line_ending="\n"))
    path = (tmp_path / "ZII_SIIS_GR_PULL_REQ" / "methods" /
            "ZII_SIIS_GR_PULL_REQ~SIIS_GR_PULL_REQ.txt")
    assert path.exists()
    assert "CALL FUNCTION 'Z_GR_PULL'." in path.read_text()


# -- business object ------------------------------------------------------

def test_business_object_exports_its_implementation_program(patched, tmp_path):
    result = export_program(patched, "ZBUS2105",
                            ExportOptions(output_root=str(tmp_path)))
    folder = tmp_path / "ZBUS2105"
    assert (folder / "ZBUS2105_IMPL.txt").exists()
    assert not result.failed


# -- regression: programs must be unaffected ------------------------------

def test_plain_program_export_is_unchanged(patched, tmp_path):
    result = export_program(patched, "ZMM_CREATEPO",
                            ExportOptions(output_root=str(tmp_path)))
    folder = tmp_path / "ZMM_CREATEPO"
    assert (folder / "ZMM_CREATEPO.txt").exists()
    assert (folder / "ZMM_CREATEPO_TOP.txt").exists()
    assert not (folder / "methods").exists()   # a report has no methods
    assert not result.failed


# -- business object: one file per BOR method ----------------------------

from test_abapclass import BOR


def test_business_object_methods_are_split(monkeypatch, tmp_path):
    from core import editor as ed

    class Sap:
        transaction = "SWO1"
        def log(self, _m): pass

    monkeypatch.setattr(exporter_mod.editor, "open_object",
                        lambda s, n, kind=None, settle=0: ed.Opened(
                            "ZBUS_2008", Kind.PROGRAM,
                            note="implementation of business object ZBUS_2008"))
    monkeypatch.setattr(exporter_mod.editor, "extract_source",
                        lambda s, n, t, **k: ed.Source(n, BOR, "clipboard", 30))
    monkeypatch.setattr(exporter_mod.includes, "includes_from_se80_tree",
                        lambda s, p: [])

    export_program(Sap(), "ZBUS_2008",
                   ExportOptions(output_root=str(tmp_path), line_ending="\n"))

    folder = tmp_path / "ZBUS_2008"
    assert (folder / "ZBUS_2008.txt").exists()
    methods = {p.name for p in (folder / "methods").glob("*.txt")}
    assert methods == {"GET_USER_LIST.txt", "MAIL_APPROVE.txt"}

    body = (folder / "methods" / "GET_USER_LIST.txt").read_text()
    assert body.startswith("begin_method get_user_list")
    assert body.rstrip().endswith("end_method.")
