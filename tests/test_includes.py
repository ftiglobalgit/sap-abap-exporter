"""Tests for the ABAP source parser that discovers includes."""

from core.includes import (
    build_tree,
    is_customer_object,
    parse_includes,
    split_statements,
    strip_comments,
)

MAIN = """*&---------------------------------------------------------------------*
*& Report ZMM_CREATEPO
*&---------------------------------------------------------------------*
*& PR Management & PO Creation Dashboard
*&---------------------------------------------------------------------*
REPORT zmm_createpo.

INCLUDE: zmm_createpo_top.

SELECTION-SCREEN BEGIN OF SCREEN 300.
  SELECT-OPTIONS: s_banfn FOR eban-banfn.
SELECTION-SCREEN END OF SCREEN 300.

" a trailing comment mentioning INCLUDE zzz_not_real.
DATA: lv_text TYPE string VALUE 'INCLUDE zzz_also_not_real.'.

TYPES: BEGIN OF ty_row,
         INCLUDE TYPE mara,
         INCLUDE STRUCTURE makt,
         extra TYPE char10,
       END OF ty_row.

INCLUDE zmm_createpo_f01.
INCLUDE zmm_createpo_o01 IF FOUND.
INCLUDE zmm_createpo_i01.
INCLUDE <icon>.
include zmm_createpo_f01.   " duplicate, different case
"""


def test_strip_comments_removes_star_lines():
    stripped = strip_comments(MAIN)
    assert "PR Management" not in stripped
    assert "REPORT zmm_createpo" in stripped


def test_strip_comments_keeps_string_literals():
    stripped = strip_comments("""DATA: x TYPE string VALUE 'say "hi" ok'. " tail""")
    assert '\'say "hi" ok\'' in stripped
    assert "tail" not in stripped


def test_strip_comments_handles_doubled_quote_escape():
    stripped = strip_comments("""WRITE 'it''s fine'. " gone""")
    assert "it''s fine" in stripped
    assert "gone" not in stripped


def test_split_statements_ignores_periods_in_literals():
    statements = list(split_statements("WRITE 'a.b.c'. WRITE 2."))
    assert statements == ["WRITE 'a.b.c'", "WRITE 2"]


def test_parse_includes_finds_all_real_includes():
    found = parse_includes(MAIN)
    assert found == [
        "ZMM_CREATEPO_TOP",
        "ZMM_CREATEPO_F01",
        "ZMM_CREATEPO_O01",
        "ZMM_CREATEPO_I01",
    ]


def test_parse_includes_skips_structure_includes():
    found = parse_includes(MAIN)
    assert "TYPE" not in found and "STRUCTURE" not in found
    assert "MARA" not in found and "MAKT" not in found


def test_parse_includes_skips_system_and_commented_names():
    found = parse_includes(MAIN)
    assert not any(name.startswith("<") for name in found)
    assert "ZZZ_NOT_REAL" not in found
    assert "ZZZ_ALSO_NOT_REAL" not in found


def test_parse_includes_handles_chained_statements():
    """`INCLUDE: a, b, c.` is the form SE80 generates - it must not be missed."""
    source = "INCLUDE: zmm_top,\n         zmm_f01,\n         zmm_o01.\n"
    assert parse_includes(source) == ["ZMM_TOP", "ZMM_F01", "ZMM_O01"]


def test_parse_includes_handles_chained_single_operand():
    assert parse_includes("INCLUDE: zmm_createpo_top.") == ["ZMM_CREATEPO_TOP"]


def test_parse_includes_handles_chain_with_if_found():
    source = "INCLUDE: za IF FOUND, zb IF FOUND."
    assert parse_includes(source) == ["ZA", "ZB"]


def test_parse_includes_ignores_identifiers_starting_with_include():
    source = "DATA include_flag TYPE abap_bool.\nMOVE include_flag TO lv_x."
    assert parse_includes(source) == []


def test_parse_includes_handles_namespaces():
    found = parse_includes("INCLUDE /acme/zmm_helper.\nINCLUDE lzfg_utilsu01.")
    assert found == ["/ACME/ZMM_HELPER", "LZFG_UTILSU01"]


def test_customer_object_detection():
    assert is_customer_object("ZMM_CREATEPO")
    assert is_customer_object("/ACME/ZTOOL")
    assert is_customer_object("YTEST")
    assert not is_customer_object("MM06EF0B")
    assert not is_customer_object("LSVIMF01")


def test_build_tree_is_depth_first_and_deduplicates():
    sources = {
        "ZMAIN": "INCLUDE zmain_top.\nINCLUDE zmain_f01.",
        "ZMAIN_TOP": "INCLUDE zmain_const.",
        "ZMAIN_F01": "INCLUDE zmain_top.",   # already seen
        "ZMAIN_CONST": "DATA: x TYPE i.",
    }
    tree = build_tree("ZMAIN", sources)
    assert tree.names == ["ZMAIN", "ZMAIN_TOP", "ZMAIN_CONST", "ZMAIN_F01"]


def test_build_tree_respects_customer_only():
    sources = {"ZMAIN": "INCLUDE zmine.\nINCLUDE mm06ef0b."}
    tree = build_tree("ZMAIN", sources, customer_only=True)
    assert tree.names == ["ZMAIN", "ZMINE"]


def test_build_tree_merges_se80_extras():
    sources = {"ZMAIN": "INCLUDE zmain_top."}
    tree = build_tree("ZMAIN", sources, extra=["ZMAIN_TOP", "ZMAIN_SCREEN"])
    assert tree.names == ["ZMAIN", "ZMAIN_TOP", "ZMAIN_SCREEN"]
    assert tree.nodes[-1].origin == "se80-tree"


def test_build_tree_survives_circular_includes():
    sources = {"A": "INCLUDE b.", "B": "INCLUDE a."}
    tree = build_tree("A", sources)
    assert tree.names == ["A", "B"]
