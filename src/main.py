"""
Entry point.

With no arguments the graphical app opens.  Everything is also available
headless, which is what you want for a scheduled nightly export:

    sap-abap-exporter.exe --cli                      # export the detected program
    sap-abap-exporter.exe --cli --program ZMM_CREATEPO --out D:\\src
    sap-abap-exporter.exe --cli --list-sessions
    sap-abap-exporter.exe --cli --dump-tree tree.txt  # diagnostics
    sap-abap-exporter.exe --cli --blind               # no-scripting fallback
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_OUTPUT = os.path.join(os.path.expanduser("~"), "Documents", "ABAP_Export")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sap-abap-exporter",
        description="Export an ABAP program and all of its includes to .txt files "
                    "by reading the running SAP GUI screen (no RFC).",
    )
    parser.add_argument("--cli", action="store_true",
                        help="run headless instead of opening the window")
    parser.add_argument("--program", "-p", default=None,
                        help="program name; omit to auto-detect from the SAP screen")
    parser.add_argument("--out", "-o", default=DEFAULT_OUTPUT,
                        help=f"output root folder (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--connection", type=int, default=0, help="SAP connection index")
    parser.add_argument("--session", type=int, default=0, help="SAP session index")
    parser.add_argument("--kind", default=None,
                        choices=["program", "class", "interface", "business_object"],
                        help="force the object kind instead of auto-detecting")
    parser.add_argument("--no-split-methods", action="store_true",
                        help="do not write one .txt per class method")
    parser.add_argument("--only-redefinitions", action="store_true",
                        help="for OData *_DPC_EXT classes: export only redefined methods")
    parser.add_argument("--methods-flat", action="store_true",
                        help="write method files beside the class, not in methods/")
    parser.add_argument("--customer-only", action="store_true",
                        help="skip SAP standard includes")
    parser.add_argument("--no-se80-tree", action="store_true",
                        help="do not read the SE80 'Includes' folder")
    parser.add_argument("--no-clipboard", action="store_true",
                        help="disable the clipboard fallback strategy")
    parser.add_argument("--no-download", action="store_true",
                        help="disable the SAP menu-download fallback strategy")
    parser.add_argument("--max-depth", type=int, default=10,
                        help="maximum include nesting depth (default 10)")
    parser.add_argument("--encoding", default="utf-8", help="output file encoding")
    parser.add_argument("--lf", action="store_true",
                        help="write Unix line endings instead of CRLF")
    parser.add_argument("--list-sessions", action="store_true",
                        help="list open SAP GUI sessions and exit")
    parser.add_argument("--dump-tree", metavar="FILE", default=None,
                        help="dump the SAP GUI component tree for diagnostics and exit")
    parser.add_argument("--blind", action="store_true",
                        help="fallback mode for systems with scripting disabled")
    return parser


def run_cli(args) -> int:
    from core.detect import detect_program, strip_to_main_program
    from core.objects import Kind
    from core.exporter import ExportOptions, export_program
    from core.session import SapGuiError, SapSession

    def log(message: str = "") -> None:
        print(message, flush=True)

    if args.blind:
        return run_blind(args, log)

    if args.list_sessions:
        from core.session import enumerate_sessions

        entries, notes = enumerate_sessions(log)
        for entry in entries:
            log(("   " if entry.usable else "!! ") + entry.label)
        for note in notes:
            log("")
            log(note)
        return 0 if any(e.usable for e in entries) else 2

    try:
        sap = SapSession.attach(args.connection, args.session, log=log)
    except SapGuiError as exc:
        log(f"ERROR: {exc}")
        return 2

    if args.dump_tree:
        path = sap.dump_tree(args.dump_tree)
        log(f"Component tree written to {path}")
        return 0

    program = (args.program or "").strip().upper()
    if not program:
        detection = detect_program(sap)
        if not detection.name:
            log("ERROR: could not detect a program on the current SAP screen.")
            log("       Open the program in SE80/SE38, or pass --program NAME.")
            return 3
        program = strip_to_main_program(detection.name, detection.kind)
        log(f"Detected {detection.kind.value}: {program}  ({detection.evidence})")

    options = ExportOptions(
        output_root=args.out,
        split_methods=not args.no_split_methods,
        only_redefinitions=args.only_redefinitions,
        methods_subfolder="" if args.methods_flat else "methods",
        root_kind=Kind(args.kind) if args.kind else None,
        customer_only=args.customer_only,
        use_se80_tree=not args.no_se80_tree,
        allow_clipboard=not args.no_clipboard,
        allow_download=not args.no_download,
        max_depth=args.max_depth,
        encoding=args.encoding,
        line_ending="\n" if args.lf else "\r\n",
    )

    result = export_program(sap, program, options, log=log)
    return 0 if not result.failed else 1


def run_blind(args, log) -> int:
    """Keystroke + clipboard mode, for systems where scripting is disabled."""
    from core.blind import BlindSession, BlindModeError
    from core.exporter import ExportOptions, safe_filename
    from core.includes import parse_includes, is_customer_object

    try:
        blind = BlindSession.find(log=log)
    except BlindModeError as exc:
        log(f"ERROR: {exc}")
        return 2

    log("")
    log("BLIND MODE - SAP GUI Scripting is not being used.")
    log("This mode is less reliable; prefer enabling scripting if you can.")
    log("")

    program = (args.program or "").strip().upper() or blind.detect_program()
    if not program:
        log("ERROR: could not read a program name from the window caption. "
            "Pass --program NAME.")
        return 3
    log(f"Program: {program}")

    folder = os.path.join(args.out, safe_filename(program))
    os.makedirs(folder, exist_ok=True)

    pending = [program]
    done = set()
    failures = []

    while pending:
        name = pending.pop(0)
        if name in done:
            continue
        done.add(name)

        log(f"\n--- {name} ---")
        if name != program or not blind.detect_program() == program:
            if not blind.open_object(name):
                log("  Could not open it automatically.")
                input(f"  Open {name} in the ABAP editor, click in the source, "
                      f"then press Enter here... ")
        try:
            text = blind.capture_editor()
        except BlindModeError as exc:
            log(f"  !! {exc}")
            failures.append(name)
            continue

        path = os.path.join(folder, f"{safe_filename(name)}.txt")
        body = text.replace("\r\n", "\n").replace("\r", "\n")
        if not args.lf:
            body = body.replace("\n", "\r\n")
        with open(path, "wb") as fh:
            fh.write(body.encode(args.encoding, errors="replace"))
        log(f"  {body.count(chr(10)) + 1} lines -> {os.path.basename(path)}")

        for child in parse_includes(text):
            if child in done or child in pending:
                continue
            if args.customer_only and not is_customer_object(child):
                continue
            pending.append(child)

    log("")
    log(f"Exported {len(done) - len(failures)} object(s) to {folder}")
    if failures:
        log(f"Failed: {', '.join(failures)}")
    return 0 if not failures else 1


def main() -> int:
    args = build_parser().parse_args()
    headless = args.cli or args.list_sessions or args.dump_tree or args.blind
    if headless:
        return run_cli(args)
    from app_gui import run
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
