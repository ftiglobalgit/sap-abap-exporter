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


def includes_from_se80_tree(sap: SapSession, program: str) -> List[str]:
    """Expand the "Includes" folder in the SE80 repository browser and read
    its children.  Best effort - returns [] if SE80 is not open or the tree
    cannot be driven on this release."""
    names: List[str] = []
    tree_comp = None
    for comp in sap.walk("wnd[0]"):
        if comp.type == "GuiTree" or "tree" in (comp.subtype or "").lower():
            tree_comp = comp
            break
    if tree_comp is None:
        return names

    tree = tree_comp.obj
    try:
        keys = tree.GetAllNodeKeys()
        key_list = [keys.ElementAt(i) for i in range(int(keys.Count))]
    except Exception:
        try:
            key_list = [keys.Item(i) for i in range(int(keys.Count))]
        except Exception:
            return names

    folder_key = None
    for key in key_list:
        try:
            text = str(tree.GetNodeTextByKey(key)).strip()
        except Exception:
            continue
        if _INCLUDE_FOLDER.match(text):
            folder_key = key
            break
    if folder_key is None:
        return names

    try:
        tree.expandNode(folder_key)
    except Exception:
        pass

    try:
        children = tree.GetSubNodesCol(folder_key)
        child_keys = [children.ElementAt(i) for i in range(int(children.Count))]
    except Exception:
        return names

    for key in child_keys:
        try:
            text = str(tree.GetNodeTextByKey(key)).strip().upper()
        except Exception:
            continue
        text = text.split()[0] if text else ""
        if text and NAME_RE.match(text) and text != program.upper():
            names.append(text)
    return names


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
