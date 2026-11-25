"""
Orchestration: walk the program + its includes, extract each one, write files.

Traversal is breadth-first and lazy - an object's source is fetched, written
and *then* parsed for further INCLUDE statements, so nested include trees of
any depth are followed without knowing the shape in advance.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

from . import abapclass, editor, includes, objects
from .session import DebuggerOpenError, SapSession

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, str], None]

INVALID_FS = re.compile(r'[<>:"/\\|?*]')


@dataclass
class ExportOptions:
    output_root: str
    # Class handling
    split_methods: bool = True          # one .txt per METHOD ... ENDMETHOD
    only_redefinitions: bool = False    # for OData *_DPC_EXT / *_MPC_EXT classes
    methods_subfolder: str = "methods"  # "" writes them beside the class file
    root_kind: Optional[object] = None  # force the kind of the requested object
    customer_only: bool = False
    use_se80_tree: bool = True
    max_depth: int = 10
    max_objects: int = 500
    encoding: str = "utf-8"
    line_ending: str = "\r\n"          # Windows-friendly by default
    write_report: bool = True
    overwrite: bool = True
    allow_clipboard: bool = True
    allow_download: bool = True
    return_to_start: bool = True


@dataclass
class ObjectResult:
    name: str
    path: Optional[str]
    lines: int
    bytes_written: int
    strategy: str
    parent: Optional[str]
    depth: int
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class ExportResult:
    program: str
    folder: str
    objects: List[ObjectResult] = field(default_factory=list)
    started: str = ""
    finished: str = ""

    @property
    def succeeded(self) -> List[ObjectResult]:
        return [o for o in self.objects if o.ok]

    @property
    def failed(self) -> List[ObjectResult]:
        return [o for o in self.objects if not o.ok]

    @property
    def total_lines(self) -> int:
        return sum(o.lines for o in self.succeeded)


def safe_filename(name: str) -> str:
    """SAP names may contain /NAMESPACE/ which is illegal in a filename."""
    cleaned = INVALID_FS.sub("#", name.strip())
    return cleaned.strip(". ") or "UNNAMED"


def export_program(
    sap: SapSession,
    program: str,
    options: ExportOptions,
    log: LogFn = lambda _m: None,
    progress: ProgressFn = lambda _d, _t, _n: None,
    should_stop: Callable[[], bool] = lambda: False,
) -> ExportResult:
    program = program.strip().upper()
    folder = os.path.join(options.output_root, safe_filename(program))
    os.makedirs(folder, exist_ok=True)

    result = ExportResult(
        program=program,
        folder=folder,
        started=datetime.now().isoformat(timespec="seconds"),
    )

    log(f"Output folder: {folder}")

    # Read the SE80 include folder *before* navigating away from SE80.
    tree_extras: List[str] = []
    if options.use_se80_tree:
        try:
            tree_extras = includes.includes_from_se80_tree(sap, program)
            if tree_extras:
                log(f"SE80 object tree reported {len(tree_extras)} include(s).")
        except Exception as exc:
            log(f"SE80 tree could not be read ({exc}); relying on source parsing.")

    start_tcode = sap.transaction

    tmp_dir = tempfile.mkdtemp(prefix="abapexp_")
    sources: Dict[str, str] = {}
    queue: deque = deque()
    queue.append((program, None, 0))
    queued = {program}
    processed = 0

    while queue:
        if should_stop():
            log("Stopped by user.")
            break
        if processed >= options.max_objects:
            log(f"Object limit ({options.max_objects}) reached; stopping.")
            break

        name, parent, depth = queue.popleft()
        processed += 1
        total_known = processed + len(queue)
        progress(processed, total_known, name)
        log(f"[{processed}/{total_known}] {name}")

        try:
            opened = editor.open_object(
                sap, name, kind=options.root_kind if depth == 0 else None
            )
            effective = opened.name
            if effective != name:
                log(f"  -> {effective}" + (f"  ({opened.note})" if opened.note else ""))
            source = editor.extract_source(
                sap,
                effective,
                tmp_dir,
                allow_clipboard=options.allow_clipboard,
                allow_download=options.allow_download,
            )
        except DebuggerOpenError as exc:
            # Never keep driving the screen while a program is executing.
            log("")
            log("!! STOPPED - the ABAP Debugger is open.")
            log(str(exc))
            result.objects.append(
                ObjectResult(name, None, 0, 0, "-", parent, depth,
                             "aborted: ABAP Debugger open")
            )
            break
        except Exception as exc:
            log(f"  !! {exc}")
            result.objects.append(
                ObjectResult(name, None, 0, 0, "-", parent, depth, str(exc))
            )
            continue

        if source.is_empty:
            msg = "editor returned an empty document"
            log(f"  !! {msg}")
            result.objects.append(
                ObjectResult(name, None, 0, 0, source.strategy, parent, depth, msg)
            )
            continue

        if source.quality != "good":
            log("  (warning: captured text does not look like ABAP source - "
                "check this file before relying on it)")

        path, written = _write_source(folder, effective, source.text, options)
        sources[effective] = source.text
        log(f"  {source.line_count} lines via {source.strategy} -> {os.path.basename(path)}")

        result.objects.append(
            ObjectResult(effective, path, source.line_count, written,
                         source.strategy, parent, depth)
        )

        # A class capture is one document holding every method - split it
        if options.split_methods and abapclass.looks_like_class_source(source.text):
            _write_methods(result, folder, source.text, effective, depth, options, log)
        elif options.split_methods and abapclass.looks_like_bor_source(source.text):
            _write_methods(result, folder, source.text, effective, depth, options,
                           log, bor=True)

        # Discover the next generation of includes from what we just read
        if depth < options.max_depth:
            for child in includes.parse_includes(source.text):
                if child in queued:
                    continue
                if options.customer_only and not includes.is_customer_object(child):
                    log(f"  skipping standard include {child} (customer-only mode)")
                    continue
                queued.add(child)
                queue.append((child, name, depth + 1))

        # Includes the SE80 tree knows about but no INCLUDE statement reached
        if depth == 0:
            for extra in tree_extras:
                if extra in queued:
                    continue
                if options.customer_only and not includes.is_customer_object(extra):
                    continue
                queued.add(extra)
                queue.append((extra, name, 1))
                log(f"  queued {extra} (from SE80 tree)")

    result.finished = datetime.now().isoformat(timespec="seconds")

    if options.write_report:
        _write_report(result, options)

    if options.return_to_start and start_tcode:
        try:
            sap.start_transaction(start_tcode)
        except Exception:
            pass

    log("")
    log(f"Done: {len(result.succeeded)} object(s), {result.total_lines} lines.")
    if result.failed:
        log(f"Failed: {', '.join(o.name for o in result.failed)}")
    return result


def _write_methods(
    result: "ExportResult",
    folder: str,
    source: str,
    class_name: str,
    depth: int,
    options: ExportOptions,
    log: LogFn,
    bor: bool = False,
) -> None:
    """Split a captured class into one .txt per method.

    No extra SAP round-trips: the whole class arrived in a single capture, so
    this is pure text slicing.  Each file keeps the method's own comments and
    indentation exactly as SAP returned them.
    """
    try:
        if bor:
            info = abapclass.ClassInfo(
                name=class_name,
                methods=abapclass.parse_bor_methods(source, class_name),
            )
        else:
            info = abapclass.parse_class(source)
    except Exception as exc:
        log(f"  (could not split methods: {exc})")
        return

    if not info.methods:
        log("  no METHOD...ENDMETHOD blocks found in this class")
        return

    if options.only_redefinitions:
        selected = info.redefinitions
        log(f"  {len(info.methods)} method(s) found, "
            f"{len(selected)} redefinition(s) selected")
        if not selected:
            log("  (no redefinitions - nothing to split; "
                "untick 'Only redefined methods' to export them all)")
            return
    else:
        selected = info.methods
        log(f"  {len(selected)} method(s) found "
            f"({len(info.redefinitions)} redefinition(s))")

    target = os.path.join(folder, options.methods_subfolder) \
        if options.methods_subfolder else folder
    os.makedirs(target, exist_ok=True)

    for method in selected:
        path, written = _write_source(target, method.name, method.text, options)
        tags = []
        if method.is_redefinition:
            tags.append("redefinition")
        if method.is_interface_method:
            tags.append("interface")
        strategy = "method-split" + (f" [{'/'.join(tags)}]" if tags else "")
        result.objects.append(
            ObjectResult(
                name=f"{class_name}~{method.name}" if "~" not in method.name
                     else method.name,
                path=path,
                lines=method.line_count,
                bytes_written=written,
                strategy=strategy,
                parent=class_name,
                depth=depth + 1,
            )
        )

    log(f"  wrote {len(selected)} method file(s) to "
        f"{options.methods_subfolder or os.path.basename(folder)}/")


def _write_source(folder: str, name: str, text: str, options: ExportOptions):
    path = os.path.join(folder, f"{safe_filename(name)}.txt")
    if os.path.exists(path) and not options.overwrite:
        stem, ext = os.path.splitext(path)
        path = f"{stem}_{int(time.time())}{ext}"
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    if options.line_ending != "\n":
        body = body.replace("\n", options.line_ending)
    data = body.encode(options.encoding, errors="replace")
    with open(path, "wb") as fh:
        fh.write(data)
    return path, len(data)


def _write_report(result: ExportResult, options: ExportOptions) -> None:
    lines = [
        "ABAP source export report",
        f"Program   : {result.program}",
        f"Folder    : {result.folder}",
        f"Started   : {result.started}",
        f"Finished  : {result.finished}",
        f"Objects   : {len(result.succeeded)} exported, {len(result.failed)} failed",
        f"Total lines: {result.total_lines}",
        "",
        f"{'OBJECT':<42}{'LINES':>8}  {'DEPTH':>5}  {'METHOD':<14}PARENT",
        "-" * 100,
    ]
    for obj in result.objects:
        status = obj.strategy if obj.ok else "FAILED"
        lines.append(
            f"{obj.name:<42}{obj.lines:>8}  {obj.depth:>5}  {status:<14}{obj.parent or '-'}"
        )
        if obj.error:
            lines.append(f"{'':<42}        error: {obj.error}")
    report_path = os.path.join(result.folder, "_export_report.txt")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    manifest = {
        "program": result.program,
        "started": result.started,
        "finished": result.finished,
        "objects": [
            {
                "name": o.name,
                "file": os.path.basename(o.path) if o.path else None,
                "lines": o.lines,
                "bytes": o.bytes_written,
                "strategy": o.strategy,
                "parent": o.parent,
                "depth": o.depth,
                "error": o.error,
            }
            for o in result.objects
        ],
    }
    with open(os.path.join(result.folder, "_export_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
