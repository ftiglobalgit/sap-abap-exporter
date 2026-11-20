"""Tests for splitting a captured class source into methods."""

import pytest

from core.abapclass import (
    looks_like_class_source,
    parse_class,
    parse_declarations,
)

# Shaped after ZCL_ZO_EP2_PR_DPC_EXT - an OData provider extension class whose
# useful content is entirely in redefined methods.
ODATA = """class ZCL_ZO_EP2_PR_DPC_EXT definition
  public
  inheriting from ZCL_ZO_EP2_PR_DPC
  create public .

public section.

  methods /IWBEP/IF_MGW_APPL_SRV_RUNTIME~CREATE_DEEP_ENTITY
    redefinition .
  methods /IWBEP/IF_MGW_APPL_SRV_RUNTIME~EXECUTE_ACTION
    redefinition .
  methods PR_HEADERSET_GET_ENTITYSET
    redefinition .
  methods PR_ITEMSET_GET_ENTITY
    redefinition .
  methods CONSTRUCTOR .
protected section.
private section.

  methods HELPER_NOT_REDEFINED
    importing !IV_X type STRING .
ENDCLASS.



CLASS ZCL_ZO_EP2_PR_DPC_EXT IMPLEMENTATION.

  method /IWBEP/IF_MGW_APPL_SRV_RUNTIME~CREATE_DEEP_ENTITY.
* deep insert for PR creation
    DATA: ls_pr TYPE zpr_header.
    CALL METHOD super->/iwbep/if_mgw_appl_srv_runtime~create_deep_entity.
  endmethod.

  method /IWBEP/IF_MGW_APPL_SRV_RUNTIME~EXECUTE_ACTION.
    RETURN.
  endmethod.

  METHOD PR_HEADERSET_GET_ENTITYSET.
    SELECT * FROM eban INTO TABLE @DATA(lt_eban)
      WHERE banfn IN @lt_range.
  ENDMETHOD.

  method PR_ITEMSET_GET_ENTITY.
    " single item read
    CLEAR er_entity.
  endmethod.

  method CONSTRUCTOR.
    super->constructor( ).
  endmethod.

  method HELPER_NOT_REDEFINED.
    WRITE iv_x.
  endmethod.

ENDCLASS.
"""

# Shaped after ZCL_SIIS_GR_PULL_REQ - a generated proxy class whose only
# content is the interface method.
PROXY = """class ZCL_SIIS_GR_PULL_REQ definition
  public
  create public .

public section.
  interfaces ZII_SIIS_GR_PULL_REQ .
protected section.
private section.
ENDCLASS.

CLASS ZCL_SIIS_GR_PULL_REQ IMPLEMENTATION.

  method ZII_SIIS_GR_PULL_REQ~SIIS_GR_PULL_REQ.
*** Proxy Interface (generated)
    DATA: lv_req TYPE string.
    CALL FUNCTION 'Z_GR_PULL'.
  endmethod.

ENDCLASS.
"""


# -- declarations ---------------------------------------------------------

def test_class_name_is_read():
    assert parse_declarations(ODATA).name == "ZCL_ZO_EP2_PR_DPC_EXT"


def test_redefinitions_are_identified():
    info = parse_declarations(ODATA)
    assert "PR_HEADERSET_GET_ENTITYSET" in info.declared_redefinitions
    assert "/IWBEP/IF_MGW_APPL_SRV_RUNTIME~CREATE_DEEP_ENTITY" in info.declared_redefinitions
    assert "HELPER_NOT_REDEFINED" not in info.declared_redefinitions
    assert "CONSTRUCTOR" not in info.declared_redefinitions


def test_implemented_interfaces_are_listed():
    assert parse_declarations(PROXY).interfaces == ["ZII_SIIS_GR_PULL_REQ"]


def test_chained_method_declarations():
    source = "class ZCL_X definition public.\npublic section.\n" \
             "  methods: a redefinition, b, c redefinition .\nENDCLASS.\n"
    info = parse_declarations(source)
    assert info.declared_methods == {"A", "B", "C"}
    assert info.declared_redefinitions == {"A", "C"}


# -- implementation slicing -----------------------------------------------

def test_every_method_is_found():
    info = parse_class(ODATA)
    assert [m.name for m in info.methods] == [
        "/IWBEP/IF_MGW_APPL_SRV_RUNTIME~CREATE_DEEP_ENTITY",
        "/IWBEP/IF_MGW_APPL_SRV_RUNTIME~EXECUTE_ACTION",
        "PR_HEADERSET_GET_ENTITYSET",
        "PR_ITEMSET_GET_ENTITY",
        "CONSTRUCTOR",
        "HELPER_NOT_REDEFINED",
    ]


def test_method_body_is_verbatim_including_comments():
    info = parse_class(ODATA)
    body = info.find("PR_ITEMSET_GET_ENTITY").text
    assert body.startswith("  method PR_ITEMSET_GET_ENTITY.")
    assert '" single item read' in body          # comment preserved
    assert body.rstrip().endswith("endmethod.")
    assert "CLEAR er_entity." in body


def test_uppercase_and_lowercase_keywords_both_parse():
    info = parse_class(ODATA)
    assert info.find("PR_HEADERSET_GET_ENTITYSET") is not None   # METHOD/ENDMETHOD
    assert info.find("CONSTRUCTOR") is not None                  # method/endmethod


def test_redefinition_flag_reaches_the_blocks():
    info = parse_class(ODATA)
    names = {m.name for m in info.redefinitions}
    assert "PR_HEADERSET_GET_ENTITYSET" in names
    assert "/IWBEP/IF_MGW_APPL_SRV_RUNTIME~CREATE_DEEP_ENTITY" in names
    assert "HELPER_NOT_REDEFINED" not in names
    assert len(info.redefinitions) == 4


def test_interface_method_of_a_proxy_class():
    info = parse_class(PROXY)
    assert len(info.methods) == 1
    method = info.methods[0]
    assert method.name == "ZII_SIIS_GR_PULL_REQ~SIIS_GR_PULL_REQ"
    assert method.interface == "ZII_SIIS_GR_PULL_REQ"
    assert method.short_name == "SIIS_GR_PULL_REQ"
    assert method.is_interface_method
    assert "CALL FUNCTION 'Z_GR_PULL'." in method.text


def test_lookup_by_short_name():
    info = parse_class(PROXY)
    assert info.find("SIIS_GR_PULL_REQ") is info.methods[0]
    assert info.find("ZII_SIIS_GR_PULL_REQ~SIIS_GR_PULL_REQ") is info.methods[0]


def test_methods_declaration_is_not_mistaken_for_an_implementation():
    """`methods FOO.` declares; `method FOO.` implements. Only the second one."""
    info = parse_class(ODATA)
    assert all(m.line_count >= 2 for m in info.methods)
    assert len(info.methods) == 6   # not 11 (5 extra declarations)


def test_line_numbers_point_at_the_real_lines():
    info = parse_class(PROXY)
    lines = PROXY.split("\n")
    method = info.methods[0]
    assert lines[method.start_line - 1].strip().lower().startswith("method ")
    assert lines[method.end_line - 1].strip().lower().startswith("endmethod")


def test_method_containing_the_word_endmethod_in_a_comment():
    source = (
        "CLASS ZCL_X IMPLEMENTATION.\n"
        "  method A.\n"
        "*   this endmethod. is only a comment\n"
        "    WRITE 1.\n"
        "  endmethod.\n"
        "ENDCLASS.\n"
    )
    info = parse_class(source)
    assert len(info.methods) == 1
    assert "WRITE 1." in info.methods[0].text
    assert info.methods[0].line_count == 4


def test_local_classes_keep_their_own_context():
    source = (
        "CLASS lcl_helper IMPLEMENTATION.\n"
        "  method HELP.\n    WRITE 1.\n  endmethod.\n"
        "ENDCLASS.\n"
        "CLASS ZCL_MAIN IMPLEMENTATION.\n"
        "  method RUN.\n    WRITE 2.\n  endmethod.\n"
        "ENDCLASS.\n"
    )
    info = parse_class(source)
    assert [(m.class_name, m.name) for m in info.methods] == [
        ("LCL_HELPER", "HELP"),
        ("ZCL_MAIN", "RUN"),
    ]


def test_empty_method_body():
    source = "CLASS ZCL_X IMPLEMENTATION.\n  method NOOP.\n  endmethod.\nENDCLASS.\n"
    info = parse_class(source)
    assert len(info.methods) == 1
    assert info.methods[0].line_count == 2


def test_unterminated_method_is_skipped_not_crashed():
    source = "CLASS ZCL_X IMPLEMENTATION.\n  method BROKEN.\n    WRITE 1.\n"
    assert parse_class(source).methods == []


# -- guardrails -----------------------------------------------------------

def test_class_source_is_recognised():
    assert looks_like_class_source(ODATA)
    assert looks_like_class_source(PROXY)


def test_a_report_is_not_class_source():
    assert not looks_like_class_source("REPORT zmm_createpo.\nWRITE 'hi'.\n")


def test_empty_capture_is_not_class_source():
    assert not looks_like_class_source("")
    assert not looks_like_class_source("SAPGUI.AbapEditor.1")
