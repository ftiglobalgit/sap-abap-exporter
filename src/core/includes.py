"""
Working out which includes belong to a program - without RFC and without
reading any backend table through a custom program.

Two complementary, purely client-side sources are used and then merged:

  A. **Source parsing.**  The main program's text is parsed for ``INCLUDE``
     statements, and each discovered include is opened and parsed in turn.
     This mirrors exactly how the ABAP compiler assembles a program, so for
     classic reports, module pools and function groups it is authoritative.

  B. **The SE80 object tree.**  The "Includes" folder of the repository
     browser is expanded and its child nodes are read through the GuiTree
     API.  This catches includes that exist for the program but are not
     referenced by a literal INCLUDE statement in the part we have parsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

from .session import SapSession

# Keywords that may follow INCLUDE without meaning "pull in a source include".
STRUCTURE_KEYWORDS = {"TYPE", "STRUCTURE"}

NAME_RE = re.compile(r"^(/[A-Z0-9_]{1,8}/)?[A-Z_][A-Z0-9_]{0,39}$")

CUSTOMER_RE = re.compile(r"^(Z|Y|SAPMZ|SAPMY|\$|/[A-Z0-9_]{1,8}/)", re.I)


@dataclass
class IncludeNode:
    name: str
    parent: Optional[str]
    depth: int
    origin: str = "source"   # "source" | "se80-tree" | "root"


@dataclass
class IncludeTree:
    root: str
    nodes: List[IncludeNode] = field(default_factory=list)

    @property
    def names(self) -> List[str]:
        return [n.name for n in self.nodes]

    def render(self) -> str:
        lines = []
        for node in self.nodes:
            prefix = "" if node.depth == 0 else "   " * (node.depth - 1) + "└─ "
            tag = "" if node.origin in ("root", "source") else f"  [{node.origin}]"
            lines.append(f"{prefix}{node.name}{tag}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# A. Source parsing
# --------------------------------------------------------------------------

def strip_comments(source: str) -> str:
    """Remove ABAP comments while preserving line structure.

    * a ``*`` in column 1 comments out the whole line
    * a ``"`` outside a string literal comments out the rest of the line
    * ``'...'`` literals (with ``''`` escaping) and ``|...|`` templates are
      respected so that a quote inside a literal does not start a comment
    """
    out_lines: List[str] = []
    for line in source.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] == "*":
            out_lines.append("")
            continue
        buf: List[str] = []
        in_quote = False
        in_template = False
        i = 0
        while i < len(line):
            ch = line[i]
            if in_quote:
                buf.append(ch)
                if ch == "'":
                    if i + 1 < len(line) and line[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    in_quote = False
                i += 1
                continue
            if in_template:
                buf.append(ch)
                if ch == "|":
                    in_template = False
                i += 1
                continue
            if ch == "'":
                in_quote = True
                buf.append(ch)
                i += 1
                continue
            if ch == "|":
                in_template = True
                buf.append(ch)
                i += 1
                continue
            if ch == '"':
                break  # rest of line is a comment
            buf.append(ch)
            i += 1
        out_lines.append("".join(buf))
    return "\n".join(out_lines)


def split_statements(source: str) -> Iterable[str]:
    """Yield ABAP statements (text between periods), comments already gone."""
    buf: List[str] = []
    in_quote = False
    for ch in source:
        if in_quote:
            buf.append(ch)
            if ch == "'":
                in_quote = False
            continue
        if ch == "'":
            in_quote = True
            buf.append(ch)
            continue
        if ch == ".":
            statement = " ".join("".join(buf).split())
            if statement:
                yield statement
            buf = []
            continue
        buf.append(ch)
    tail = " ".join("".join(buf).split())
    if tail:
        yield tail


#   INCLUDE zfoo.                 - plain
#   INCLUDE: zfoo.                - chained form with a single operand
#   INCLUDE: ztop, zf01, zo01.    - chained form, comma separated
#   INCLUDE zfoo IF FOUND.        - optional include
INCLUDE_HEAD = re.compile(r"^INCLUDE\s*:?\s*(.*)$", re.I | re.S)


def parse_includes(source: str) -> List[str]:
    """Return the include names referenced by INCLUDE statements, in order.

    Handles ABAP's chained-statement syntax (``INCLUDE: a, b, c.``), which is
    what real programs generated by SE80 usually look like.
    """
    found: List[str] = []
    seen: Set[str] = set()
    for statement in split_statements(strip_comments(source)):
        head = INCLUDE_HEAD.match(statement.strip())
        if not head:
            continue
        # Guard against identifiers that merely start with "include"
        if not re.match(r"^INCLUDE\b\s*:?", statement.strip(), re.I):
            continue

        for part in head.group(1).split(","):
            tokens = part.split()
            if not tokens:
                continue
            candidate = tokens[0].strip().upper()
            # `INCLUDE TYPE ty.` / `INCLUDE STRUCTURE st.` inside a TYPES block
            if candidate in STRUCTURE_KEYWORDS:
                continue
            # system includes such as <ICON>, <SYMBOL>, <LIST>
            if candidate.startswith("<"):
                continue
            if not NAME_RE.match(candidate):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            found.append(candidate)
    return found


# --------------------------------------------------------------------------
# B. SE80 object tree
# --------------------------------------------------------------------------

_INCLUDE_FOLDER = re.compile(r"^(includes?|includes\s*\(\d+\)|include-programme?)$", re.I)

# Other folders in the SE80 object tree that the exporter navigates
_IMPL_CLASS_FOLDER = re.compile(
    r"^(implementing\s+classes?|implementierende\s+klassen?)(\s*\(\d+\))?$", re.I
)
_METHODS_FOLDER = re.compile(r"^(methods?|methoden)(\s*\(\d+\))?$", re.I)
_REDEF_FOLDER = re.compile(r"^(redefinitions?|redefinitionen)(\s*\(\d+\))?$", re.I)


def find_tree(sap: SapSession):
    """The repository browser tree on the current screen, or None."""
    for comp in sap.walk("wnd[0]"):
        if comp.type == "GuiTree" or "tree" in (comp.subtype or "").lower():
            return comp.obj
    return None


def _collection(col) -> List[str]:
    """SAP collections expose ElementAt on some releases and Item on others."""
    try:
        count = int(col.Count)
    except Exception:
        return []
    out: List[str] = []
    for i in range(count):
        for getter in ("ElementAt", "Item"):
            try:
                out.append(getattr(col, getter)(i))
                break
            except Exception:
                continue
    return out


def tree_folder_children(
    sap: SapSession, folder_pattern, exclude: str = ""
) -> List[str]:
    """Expand the first tree folder matching *folder_pattern* and read the
    first word of each child node.

    "Includes", "Implementing Classes", "Methods" and "Redefinitions" are all
    just folders in the same tree, so one reader serves all of them.
    """
    tree = find_tree(sap)
    if tree is None:
        return []

    try:
        keys = _collection(tree.GetAllNodeKeys())
    except Exception:
        return []

    folder_key = None
    for key in keys:
        try:
            text = str(tree.GetNodeTextByKey(key)).strip()
        except Exception:
            continue
        if folder_pattern.match(text):
            folder_key = key
            break
    if folder_key is None:
        return []

    try:
        tree.expandNode(folder_key)
    except Exception:
        pass

    try:
        child_keys = _collection(tree.GetSubNodesCol(folder_key))
    except Exception:
        return []

    names: List[str] = []
    skip = (exclude or "").upper()
    for key in child_keys:
        try:
            text = str(tree.GetNodeTextByKey(key)).strip().upper()
        except Exception:
            continue
        first = text.split()[0] if text else ""
        if first and NAME_RE.match(first) and first != skip and first not in names:
            names.append(first)
    return names


def includes_from_se80_tree(sap: SapSession, program: str) -> List[str]:
    """Expand the "Includes" folder in the SE80 repository browser and read
    its children.  Best effort - returns [] if SE80 is not open or the tree
    cannot be driven on this release."""
    return tree_folder_children(sap, _INCLUDE_FOLDER, exclude=program)


def implementing_classes_from_tree(sap: SapSession, interface: str) -> List[str]:
    """Children of the "Implementing Classes" folder of an interface."""
    return tree_folder_children(sap, _IMPL_CLASS_FOLDER, exclude=interface)


def methods_from_tree(sap: SapSession, owner: str = "") -> List[str]:
    """Children of the "Methods" folder of a class or interface."""
    return tree_folder_children(sap, _METHODS_FOLDER, exclude=owner)


def redefinitions_from_tree(sap: SapSession, owner: str = "") -> List[str]:
    """Children of the "Redefinitions" folder of a class."""
    return tree_folder_children(sap, _REDEF_FOLDER, exclude=owner)


# --------------------------------------------------------------------------
# Merge + filter
# --------------------------------------------------------------------------

def is_customer_object(name: str) -> bool:
    return bool(CUSTOMER_RE.match(name or ""))


def build_tree(
    root: str,
    source_of: Dict[str, str],
    extra: Sequence[str] = (),
    customer_only: bool = False,
    max_depth: int = 10,
) -> IncludeTree:
    """Build the ordered include tree from already-fetched sources.

    *source_of* maps an object name to its source text; the caller fetches
    lazily, so this function is also used incrementally during the export.
    """
    tree = IncludeTree(root=root)
    tree.nodes.append(IncludeNode(root, None, 0, "root"))
    visited: Set[str] = {root.upper()}

    def walk(parent: str, depth: int) -> None:
        if depth > max_depth:
            return
        text = source_of.get(parent.upper())
        if not text:
            return
        for child in parse_includes(text):
            if child in visited:
                continue
            if customer_only and not is_customer_object(child):
                continue
            visited.add(child)
            tree.nodes.append(IncludeNode(child, parent, depth + 1, "source"))
            walk(child, depth + 1)

    walk(root, 0)

    for name in extra:
        upper = name.upper()
        if upper in visited:
            continue
        if customer_only and not is_customer_object(upper):
            continue
        visited.add(upper)
        tree.nodes.append(IncludeNode(upper, root, 1, "se80-tree"))

    return tree
