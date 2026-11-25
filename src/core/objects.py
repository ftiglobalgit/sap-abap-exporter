"""
Repository object kinds, and the naming rules that let us reach them.

The exporter originally handled only programs and includes, which live in
SE38.  Classes, interfaces, OData provider classes and BOR business objects
each live somewhere else, so the first thing any export has to decide is
*what kind of object* it is looking at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class Kind(str, Enum):
    PROGRAM = "program"
    INCLUDE = "include"
    CLASS = "class"
    INTERFACE = "interface"
    FUNCTION_GROUP = "function_group"
    BUSINESS_OBJECT = "business_object"
    UNKNOWN = "unknown"


# -- naming conventions ---------------------------------------------------

CLASS_PREFIX = re.compile(r"^(/[A-Z0-9_]{1,8}/)?(Z?Y?)CL_", re.I)
INTERFACE_PREFIX = re.compile(r"^(/[A-Z0-9_]{1,8}/)?(Z|Y)?(IF_|II_)", re.I)
BUSINESS_OBJECT = re.compile(r"^(Z|Y)?BUS_?\d{3,6}$", re.I)
FUNCTION_GROUP_POOL = re.compile(r"^SAPL", re.I)
MODULE_POOL = re.compile(r"^SAPM", re.I)

# Transactions that tell us the kind without any guessing
TCODE_KIND = {
    "SE24": Kind.CLASS,
    "SE19": Kind.CLASS,
    "SWO1": Kind.BUSINESS_OBJECT,
    "SWO2": Kind.BUSINESS_OBJECT,
    "SE37": Kind.FUNCTION_GROUP,
    "SE38": Kind.PROGRAM,
    "SE39": Kind.PROGRAM,
}

# Window captions that name the tool, which implies the kind
TITLE_KIND = (
    (re.compile(r"class\s+builder|class\s+interface", re.I), Kind.CLASS),
    (re.compile(r"object\s+type|business\s+object\s+builder", re.I), Kind.BUSINESS_OBJECT),
    (re.compile(r"function\s+builder|function\s+group", re.I), Kind.FUNCTION_GROUP),
    (re.compile(r"abap\s+editor", re.I), Kind.PROGRAM),
)


def classify(
    name: str,
    tcode: Optional[str] = None,
    title: Optional[str] = None,
) -> Kind:
    """Work out what kind of repository object *name* is.

    Evidence is weighed in order of reliability: an unambiguous naming prefix
    beats the transaction, which beats the window caption.  `ZCL_*` is a class
    no matter which screen you are looking at it from.
    """
    upper = (name or "").strip().upper()

    if upper:
        if BUSINESS_OBJECT.match(upper):
            return Kind.BUSINESS_OBJECT
        if CLASS_PREFIX.match(upper):
            return Kind.CLASS
        if INTERFACE_PREFIX.match(upper):
            return Kind.INTERFACE
        if FUNCTION_GROUP_POOL.match(upper):
            return Kind.FUNCTION_GROUP
        if MODULE_POOL.match(upper):
            return Kind.PROGRAM

    if tcode:
        kind = TCODE_KIND.get(tcode.strip().upper())
        if kind:
            return kind

    if title:
        for pattern, kind in TITLE_KIND:
            if pattern.search(title):
                return kind

    return Kind.PROGRAM if upper else Kind.UNKNOWN


# -- generated program names for class / interface pools ------------------
#
# A global class is compiled into a program pool whose name is the class name
# padded with '=' to exactly 30 characters, followed by a suffix.  Those
# programs are ordinary ABAP includes and SE38 can display them, which gives
# a complete fallback route when the Class Builder cannot be driven.
#
#   ZCL_FOO.......................CP     class pool (main)
#   ZCL_FOO.......................CCDEF  local class definitions
#   ZCL_FOO.......................CCIMP  local class implementations
#   ZCL_FOO.......................CCMAC  macros
#   ZCL_FOO.......................CCAU   unit tests
#   ZCL_FOO.......................CM001  method implementation #1
#
POOL_WIDTH = 30

CLASS_SECTIONS = ("CP", "CCDEF", "CCIMP", "CCMAC", "CCAU", "CU", "CO", "CI", "CT")
INTERFACE_SECTIONS = ("IP", "IU")


def pool_name(name: str, suffix: str) -> str:
    """`ZCL_FOO` + `CCIMP` -> `ZCL_FOO=======================CCIMP`."""
    base = (name or "").strip().upper()
    if len(base) > POOL_WIDTH:
        base = base[:POOL_WIDTH]
    return base.ljust(POOL_WIDTH, "=") + suffix.upper()


def class_pool(name: str) -> str:
    """The main class pool program for a global class."""
    return pool_name(name, "CP")


def method_include(name: str, index: int) -> str:
    """Method implementation include `...CM001`, `...CM002`, ...

    SAP numbers these in base 36 (001-009, 00A-00Z, 010, ...).
    """
    if index < 1:
        raise ValueError("method include numbering starts at 1")
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    value = index
    out = ""
    while value:
        value, rem = divmod(value, 36)
        out = digits[rem] + out
    return pool_name(name, "CM" + out.rjust(3, "0"))


def class_section_includes(name: str) -> List[str]:
    """Every well-known section include of a class pool, in a sensible order."""
    return [pool_name(name, suffix) for suffix in CLASS_SECTIONS]


def interface_section_includes(name: str) -> List[str]:
    return [pool_name(name, suffix) for suffix in INTERFACE_SECTIONS]


def is_pool_name(name: str) -> bool:
    """True for a generated pool program such as `ZCL_FOO====...====CM001`."""
    return "==" in (name or "")


def class_from_pool(name: str) -> Optional[str]:
    """Recover `ZCL_FOO` from `ZCL_FOO=====...=====CM001`."""
    if not is_pool_name(name):
        return None
    return name.split("=", 1)[0].strip().upper() or None


# -- interface -> implementing class --------------------------------------
#
# SAP's proxy generator pairs an interface with a class of the same stem:
#   ZII_SIIS_GR_PULL_REQ  <->  ZCL_SIIS_GR_PULL_REQ
#   ZIF_PAYMENT           <->  ZCL_PAYMENT
# The SE80 tree is still consulted first ("Implementing Classes"); these are
# the fallbacks when the tree cannot be read.
_IMPL_RULES = (
    (re.compile(r"^(Z|Y)II_(.+)$", re.I), r"\1CL_\2"),
    (re.compile(r"^(Z|Y)IF_(.+)$", re.I), r"\1CL_\2"),
    (re.compile(r"^II_(.+)$", re.I), r"CL_\1"),
    (re.compile(r"^IF_(.+)$", re.I), r"CL_\1"),
    (re.compile(r"^(/[A-Z0-9_]{1,8}/)(Z|Y)?I(I|F)_(.+)$", re.I), r"\1\2CL_\4"),
)


def implementing_class_candidates(interface: str) -> List[str]:
    """Class names that conventionally implement *interface*, best first."""
    upper = (interface or "").strip().upper()
    out: List[str] = []
    for pattern, replacement in _IMPL_RULES:
        if pattern.match(upper):
            candidate = pattern.sub(replacement, upper).upper()
            if candidate != upper and candidate not in out:
                out.append(candidate)
    return out


@dataclass
class ObjectRef:
    """An object to export, with how we decided what it is."""

    name: str
    kind: Kind
    origin: str = "requested"

    @property
    def is_class_like(self) -> bool:
        return self.kind in (Kind.CLASS, Kind.INTERFACE)

    def __str__(self) -> str:
        return f"{self.name} ({self.kind.value})"
