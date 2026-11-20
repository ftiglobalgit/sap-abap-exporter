"""Object-kind classification and generated pool naming."""

import pytest

from core.detect import strip_to_main_program
from core.objects import (
    Kind,
    class_from_pool,
    class_pool,
    class_section_includes,
    classify,
    implementing_class_candidates,
    is_pool_name,
    method_include,
    pool_name,
)


# -- classification -------------------------------------------------------

@pytest.mark.parametrize("name,kind", [
    ("ZCL_ZO_EP2_PR_DPC_EXT", Kind.CLASS),
    ("ZCL_SI_MHPLUS_IFPAYMENT_RECEIV", Kind.CLASS),
    ("CL_GUI_ALV_GRID", Kind.CLASS),
    ("ZII_SIIS_GR_PULL_REQ", Kind.INTERFACE),
    ("ZIF_PAYMENT", Kind.INTERFACE),
    ("IF_SERIALIZABLE_OBJECT", Kind.INTERFACE),
    ("ZBUS2105", Kind.BUSINESS_OBJECT),
    ("BUS2012", Kind.BUSINESS_OBJECT),
    ("SAPLZFG_TOOLS", Kind.FUNCTION_GROUP),
    ("SAPMZTEST", Kind.PROGRAM),
    ("ZMM_CREATEPO", Kind.PROGRAM),
])
def test_names_classify_by_convention(name, kind):
    assert classify(name) == kind


def test_naming_beats_the_transaction():
    """ZCL_* is a class even when looked at from SE38."""
    assert classify("ZCL_FOO", tcode="SE38") == Kind.CLASS


def test_transaction_decides_when_the_name_is_neutral():
    assert classify("ZWHATEVER", tcode="SE24") == Kind.CLASS
    assert classify("ZWHATEVER", tcode="SWO1") == Kind.BUSINESS_OBJECT


def test_title_is_the_last_resort():
    assert classify("ZWHATEVER", title="Class Builder Class ZWHATEVER Change") == Kind.CLASS
    assert classify("ZWHATEVER", title="Object Type ZWHATEVER Change") == Kind.BUSINESS_OBJECT


def test_empty_name_is_unknown():
    assert classify("") == Kind.UNKNOWN


# -- generated pool names -------------------------------------------------

def test_class_pool_is_padded_to_thirty_characters():
    pool = class_pool("ZCL_FOO")
    assert pool == "ZCL_FOO" + "=" * 23 + "CP"
    assert len(pool) == 32
    assert pool.index("CP") == 30


def test_real_class_name_pads_correctly():
    pool = class_pool("ZCL_ZO_EP2_PR_DPC_EXT")       # 21 chars
    assert pool == "ZCL_ZO_EP2_PR_DPC_EXT" + "=" * 9 + "CP"
    assert len(pool) == 32


def test_section_includes_cover_the_local_class_parts():
    sections = class_section_includes("ZCL_FOO")
    assert any(s.endswith("CCIMP") for s in sections)
    assert any(s.endswith("CCDEF") for s in sections)
    assert any(s.endswith("CCAU") for s in sections)
    assert all(len(s.split("=")[0]) == len("ZCL_FOO") for s in sections)


def test_method_includes_are_numbered_in_base36():
    assert method_include("ZCL_FOO", 1).endswith("CM001")
    assert method_include("ZCL_FOO", 9).endswith("CM009")
    assert method_include("ZCL_FOO", 10).endswith("CM00A")
    assert method_include("ZCL_FOO", 35).endswith("CM00Z")
    assert method_include("ZCL_FOO", 36).endswith("CM010")


def test_method_include_rejects_zero():
    with pytest.raises(ValueError):
        method_include("ZCL_FOO", 0)


def test_pool_names_round_trip():
    pool = class_pool("ZCL_SIIS_GR_PULL_REQ")
    assert is_pool_name(pool)
    assert class_from_pool(pool) == "ZCL_SIIS_GR_PULL_REQ"


def test_plain_name_is_not_a_pool_name():
    assert not is_pool_name("ZMM_CREATEPO")
    assert class_from_pool("ZMM_CREATEPO") is None


def test_overlong_class_name_is_truncated_not_crashed():
    assert len(pool_name("Z" * 40, "CP")) == 32


# -- interface -> implementing class --------------------------------------

def test_proxy_interface_maps_to_its_class():
    assert implementing_class_candidates("ZII_SIIS_GR_PULL_REQ") == ["ZCL_SIIS_GR_PULL_REQ"]


def test_second_real_example():
    assert implementing_class_candidates("ZII_SI_MHPLUS_IFPAYMENT_RECEIV") == \
        ["ZCL_SI_MHPLUS_IFPAYMENT_RECEIV"]


def test_zif_interface_maps_too():
    assert implementing_class_candidates("ZIF_PAYMENT") == ["ZCL_PAYMENT"]


def test_sap_standard_interface_maps():
    assert implementing_class_candidates("IF_PAYMENT") == ["CL_PAYMENT"]


def test_a_class_has_no_implementing_class_candidates():
    assert implementing_class_candidates("ZCL_FOO") == []


# -- include-to-main-program mapping must not touch classes ---------------

def test_class_named_like_an_include_is_left_alone():
    """ZCL_FOO_C01 is a class, not an include of ZCL_FOO."""
    assert strip_to_main_program("ZCL_FOO_C01") == "ZCL_FOO_C01"


def test_interface_is_left_alone():
    assert strip_to_main_program("ZII_SIIS_GR_PULL_REQ") == "ZII_SIIS_GR_PULL_REQ"


def test_business_object_is_left_alone():
    assert strip_to_main_program("ZBUS2105") == "ZBUS2105"


def test_program_include_still_maps():
    assert strip_to_main_program("ZMM_CREATEPO_F01") == "ZMM_CREATEPO"
