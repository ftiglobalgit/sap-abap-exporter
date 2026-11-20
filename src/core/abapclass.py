"""
Parsing an ABAP class source into its methods.

The Class Builder's "Source Code-Based" view hands back the complete class -
definition sections and every method implementation - as one document.  That
is a single screen capture, instead of navigating to thirty methods one at a
time, and it is what makes per-method export cheap and reliable.

Everything here is pure text processing on the captured source, so it is
fully testable without SAP.

Line structure matters: `strip_comments` emits exactly one output line per
input line, so boundaries found in the stripped text can be used to slice the
*original* lines.  Method files therefore keep their comments, indentation
and blank lines byte for byte.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

from .includes import split_statements, strip_comments

# `method foo.` / `method if_x~foo.` / `method foo by database procedure ...`
# Note the mandatory whitespace after `method`, which keeps the *declaration*
# keyword `methods` (plural) from matching.
METHOD_START = re.compile(
    r"^\s*method\s+([A-Za-z_/][\w/]*(?:~[A-Za-z_/][\w/]*)?)\s*(?:\.|\s)", re.I
)
METHOD_END = re.compile(r"^\s*endmethod\s*\.", re.I)

CLASS_IMPL_START = re.compile(
    r"^\s*class\s+([A-Za-z_/][\w/]*)\s+implementation\s*\.", re.I
)
CLASS_DEF_START = re.compile(
    r"^\s*class\s+([A-Za-z_/][\w/]*)\s+definition", re.I
)
CLASS_END = re.compile(r"^\s*endclass\s*\.", re.I)

# Declarations inside the definition part
DECL_METHODS = re.compile(r"^(class-)?methods$", re.I)
REDEFINITION = re.compile(r"\bredefinition\b", re.I)
ABSTRACT = re.compile(r"\babstract\b", re.I)
INTERFACES_DECL = re.compile(r"^interfaces$", re.I)


@dataclass
class MethodBlock:
    """One `METHOD ... ENDMETHOD.` block, sliced from the original source."""

    name: str                      # as written, e.g. "ZII_X~BAR"
    class_name: str                # implementing class it was found in
    start_line: int                # 1-based, inclusive
    end_line: int                  # 1-based, inclusive
    text: str                      # verbatim original lines
    is_redefinition: bool = False
    is_interface_method: bool = False

    @property
    def interface(self) -> Optional[str]:
        return self.name.split("~")[0] if "~" in self.name else None

    @property
    def short_name(self) -> str:
        return self.name.split("~")[-1]

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass
class ClassInfo:
    """What a class source tells us about itself."""

    name: Optional[str] = None
    methods: List[MethodBlock] = field(default_factory=list)
    declared_redefinitions: Set[str] = field(default_factory=set)
    declared_methods: Set[str] = field(default_factory=set)
    interfaces: List[str] = field(default_factory=list)

    @property
    def redefinitions(self) -> List[MethodBlock]:
        return [m for m in self.methods if m.is_redefinition]

    @property
    def interface_methods(self) -> List[MethodBlock]:
        return [m for m in self.methods if m.is_interface_method]

    def find(self, name: str) -> Optional[MethodBlock]:
        """Look up a method by full or short name, case-insensitively."""
        wanted = (name or "").strip().upper()
        if not wanted:
            return None
        for method in self.methods:
            if method.name.upper() == wanted:
                return method
        for method in self.methods:
            if method.short_name.upper() == wanted.split("~")[-1]:
                return method
        return None


# --------------------------------------------------------------------------
# Definition part: which methods are declared, and which are redefinitions
# --------------------------------------------------------------------------

def parse_declarations(source: str) -> ClassInfo:
    """Read the definition sections for method declarations and interfaces."""
    info = ClassInfo()

    stripped = strip_comments(source)
    for line in stripped.split("\n"):
        match = CLASS_DEF_START.match(line)
        if match:
            info.name = match.group(1).upper()
            break

    for statement in split_statements(stripped):
        tokens = statement.split()
        if not tokens:
            continue
        head = tokens[0].rstrip(":")

        if INTERFACES_DECL.match(head):
            for part in " ".join(tokens[1:]).split(","):
                words = part.split()
                if words and re.match(r"^[A-Za-z_/][\w/]*$", words[0]):
                    name = words[0].upper()
                    if name not in info.interfaces:
                        info.interfaces.append(name)
            continue

        if not DECL_METHODS.match(head):
            continue

        # `methods: a redefinition, b, c redefinition .`
        chained = tokens[0].endswith(":")
        rest = " ".join(tokens[1:])
        parts = rest.split(",") if chained else [rest]
        for part in parts:
            words = part.split()
            if not words:
                continue
            name = words[0].strip().upper()
            if not re.match(r"^[A-Za-z_/][\w/~]*$", name):
                continue
            info.declared_methods.add(name)
            if REDEFINITION.search(part):
                info.declared_redefinitions.add(name)

    return info


# --------------------------------------------------------------------------
# Implementation part: slice out every METHOD ... ENDMETHOD block
# --------------------------------------------------------------------------

def parse_class(source: str) -> ClassInfo:
    """Full parse: declarations plus every method implementation."""
    info = parse_declarations(source)

    normalised = (source or "").replace("\r\n", "\n").replace("\r", "\n")
    original = normalised.split("\n")
    stripped = strip_comments(normalised).split("\n")

    current_class = info.name or ""
    open_method: Optional[tuple] = None   # (name, start_index)

    for index, line in enumerate(stripped):
        impl = CLASS_IMPL_START.match(line)
        if impl:
            current_class = impl.group(1).upper()
            continue

        if open_method is None:
            start = METHOD_START.match(line)
            if start:
                open_method = (start.group(1).upper(), index)
            continue

        if METHOD_END.match(line):
            name, start_index = open_method
            body = "\n".join(original[start_index:index + 1])
            short = name.split("~")[-1]
            info.methods.append(
                MethodBlock(
                    name=name,
                    class_name=current_class,
                    start_line=start_index + 1,
                    end_line=index + 1,
                    text=body.rstrip("\n") + "\n",
                    is_redefinition=(
                        short in info.declared_redefinitions
                        or name in info.declared_redefinitions
                    ),
                    is_interface_method="~" in name,
                )
            )
            open_method = None

    return info


def looks_like_class_source(text: str) -> bool:
    """Does this capture actually contain a class, rather than a report?"""
    if not text:
        return False
    stripped = strip_comments(text)
    return bool(
        CLASS_DEF_START.search(stripped) or CLASS_IMPL_START.search(stripped)
    )


def method_filename(method: MethodBlock) -> str:
    """Stable, filesystem-safe name for one method's file.

    `/IWBEP/IF_MGW_APPL_SRV_RUNTIME~CREATE_DEEP_ENTITY` keeps its interface
    qualifier so redefinitions of different interfaces never collide; the
    illegal `/` is dealt with by the caller's `safe_filename`.
    """
    return method.name
