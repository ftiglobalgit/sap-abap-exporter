"""
Tkinter front-end for the SAP ABAP source exporter.

Deliberately a single small window: the whole point is that the user has
already selected the program in SAP GUI, so the app detects it, shows it for
confirmation, and exports on one click.
"""

from __future__ import annotations

import os
import queue
import threading
import traceback
import webbrowser
from datetime import datetime
from tkinter import BooleanVar, StringVar, Tk, filedialog, messagebox
from tkinter import ttk, scrolledtext

from core import VERSION
from core.detect import detect_program, strip_to_main_program
from core.exporter import ExportOptions, export_program
from core.session import SapGuiError, SapSession, ScriptingDisabledError

DEFAULT_OUTPUT = os.path.join(os.path.expanduser("~"), "Documents", "ABAP_Export")


class ExporterApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(f"SAP ABAP Source Exporter {VERSION}")
        self.root.geometry("820x620")
        self.root.minsize(720, 560)

        self.messages: "queue.Queue[tuple]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_flag = threading.Event()

        self.var_session = StringVar(value="")
        self.var_program = StringVar(value="")
        self.var_output = StringVar(value=DEFAULT_OUTPUT)
        self.var_status = StringVar(value="Not connected")
        self.var_customer_only = BooleanVar(value=False)
        self.var_use_tree = BooleanVar(value=True)
        self.var_report = BooleanVar(value=True)
        self.var_clipboard = BooleanVar(value=True)
        self.var_download = BooleanVar(value=True)

        self._build()
        self.root.after(120, self._pump)
        self.root.after(300, self.refresh_sessions)

    # -- layout ----------------------------------------------------------

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(main, text="SAP session").grid(row=row, column=0, sticky="w", **pad)
        self.combo_session = ttk.Combobox(main, textvariable=self.var_session, state="readonly")
        self.combo_session.grid(row=row, column=1, sticky="ew", **pad)
        ttk.Button(main, text="Refresh", command=self.refresh_sessions, width=12)\
            .grid(row=row, column=2, sticky="e", **pad)

        row += 1
        ttk.Label(main, textvariable=self.var_status, foreground="#555")\
            .grid(row=row, column=1, columnspan=2, sticky="w", padx=8)

        row += 1
        ttk.Separator(main).grid(row=row, column=0, columnspan=3, sticky="ew", pady=8)

        row += 1
        ttk.Label(main, text="Program").grid(row=row, column=0, sticky="w", **pad)
        entry = ttk.Entry(main, textvariable=self.var_program, font=("Consolas", 11))
        entry.grid(row=row, column=1, sticky="ew", **pad)
        ttk.Button(main, text="Detect from SAP", command=self.detect, width=16)\
            .grid(row=row, column=2, sticky="e", **pad)

        row += 1
        ttk.Label(main, text="Output folder").grid(row=row, column=0, sticky="w", **pad)
        ttk.Entry(main, textvariable=self.var_output).grid(row=row, column=1, sticky="ew", **pad)
        ttk.Button(main, text="Browse...", command=self.browse, width=16)\
            .grid(row=row, column=2, sticky="e", **pad)

        row += 1
        opts = ttk.LabelFrame(main, text="Options", padding=8)
        opts.grid(row=row, column=0, columnspan=3, sticky="ew", padx=8, pady=8)
        ttk.Checkbutton(opts, text="Only customer objects (Z*, Y*, /namespace/)",
                        variable=self.var_customer_only).grid(row=0, column=0, sticky="w", padx=4)
        ttk.Checkbutton(opts, text="Also read the SE80 'Includes' tree",
                        variable=self.var_use_tree).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Checkbutton(opts, text="Write export report",
                        variable=self.var_report).grid(row=1, column=0, sticky="w", padx=4)
        ttk.Checkbutton(opts, text="Allow clipboard fallback",
                        variable=self.var_clipboard).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Checkbutton(opts, text="Allow SAP download fallback",
                        variable=self.var_download).grid(row=2, column=0, sticky="w", padx=4)

        row += 1
        buttons = ttk.Frame(main)
        buttons.grid(row=row, column=0, columnspan=3, sticky="ew", padx=8)
        self.btn_export = ttk.Button(buttons, text="Export source", command=self.start_export)
        self.btn_export.pack(side="left")
        self.btn_stop = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(buttons, text="Open folder", command=self.open_folder).pack(side="left", padx=6)
        ttk.Button(buttons, text="Diagnostics", command=self.dump_tree).pack(side="right")

        row += 1
        self.progress = ttk.Progressbar(main, mode="determinate")
        self.progress.grid(row=row, column=0, columnspan=3, sticky="ew", padx=8, pady=(10, 4))

        row += 1
        main.rowconfigure(row, weight=1)
        self.log_box = scrolledtext.ScrolledText(
            main, height=16, font=("Consolas", 9), wrap="word", state="disabled"
        )
        self.log_box.grid(row=row, column=0, columnspan=3, sticky="nsew", padx=8, pady=4)

        self.log(f"SAP ABAP Source Exporter {VERSION}")
        self.log("Screen-based extraction via the SAP GUI Scripting interface - no RFC.")
        self.log("")

    # -- plumbing --------------------------------------------------------

    def log(self, message: str = "") -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _post(self, kind: str, payload) -> None:
        self.messages.put((kind, payload))

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "status":
                    self.var_status.set(payload)
                elif kind == "program":
                    self.var_program.set(payload)
                elif kind == "sessions":
                    self.combo_session["values"] = payload
                    if payload and not self.var_session.get():
                        self.var_session.set(payload[0])
                elif kind == "progress":
                    done, total = payload
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"] = done
                elif kind == "busy":
                    busy = bool(payload)
                    self.btn_export.configure(state="disabled" if busy else "normal")
                    self.btn_stop.configure(state="normal" if busy else "disabled")
                elif kind == "error":
                    messagebox.showerror("SAP ABAP Source Exporter", payload)
                elif kind == "info":
                    messagebox.showinfo("SAP ABAP Source Exporter", payload)
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def _session_indices(self):
        raw = self.var_session.get()
        try:
            left = raw.split()[0]
            ci, si = left.split(":")
            return int(ci), int(si)
        except Exception:
            return 0, 0

    def _run_worker(self, fn) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "An operation is already running.")
            return
        self.stop_flag.clear()
        self._post("busy", True)

        def wrapper():
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            try:
                fn()
            except (SapGuiError, ScriptingDisabledError) as exc:
                self._post("log", f"\nERROR: {exc}")
                self._post("error", str(exc))
            except Exception as exc:
                self._post("log", "\n" + traceback.format_exc())
                self._post("error", f"Unexpected error: {exc}")
            finally:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
                self._post("busy", False)

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def _connect(self) -> SapSession:
        ci, si = self._session_indices()
        return SapSession.attach(ci, si, log=lambda m: self._post("log", m))

    # -- actions ---------------------------------------------------------

    def refresh_sessions(self) -> None:
        def job():
            sessions = SapSession.list_sessions()
            self._post("sessions", sessions)
            if sessions:
                self._post("status", f"{len(sessions)} SAP session(s) available")
                self._post("log", "Available SAP sessions:")
                for s in sessions:
                    self._post("log", f"  {s}")
            else:
                self._post("status", "No SAP GUI session found")
                self._post("log",
                           "No SAP GUI session found. Log on to SAP, then press Refresh.")
        self._run_worker(job)

    def detect(self) -> None:
        def job():
            sap = self._connect()
            self._post("status", sap.describe_session())
            detection = detect_program(sap)
            self._post("log", f"Window title: {detection.title!r}")
            self._post("log", f"Transaction : {detection.transaction}")
            if detection.name:
                suggestion = strip_to_main_program(detection.name)
                self._post("log", f"Candidates  : {detection.evidence}")
                ranked = detection.candidates
                if len(ranked) > 1 and ranked[0].score - ranked[1].score < 10:
                    self._post(
                        "log",
                        f"NOTE: '{ranked[0].name}' and '{ranked[1].name}' scored "
                        f"almost equally - please confirm the name before exporting.",
                    )
                if suggestion != detection.name:
                    self._post(
                        "log",
                        f"'{detection.name}' looks like an include of "
                        f"'{suggestion}' - using the main program.",
                    )
                self._post("program", suggestion)
                self._post("log", f"Detected program: {suggestion}")
            else:
                self._post("log",
                           "Could not detect a program. Open the program in SE80/SE38 "
                           "and press Detect again, or type the name manually.")
        self._run_worker(job)

    def browse(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.var_output.get() or os.getcwd())
        if folder:
            self.var_output.set(folder)

    def open_folder(self) -> None:
        program = (self.var_program.get() or "").strip().upper()
        target = os.path.join(self.var_output.get(), program) if program else self.var_output.get()
        if not os.path.isdir(target):
            target = self.var_output.get()
        if os.path.isdir(target):
            webbrowser.open(f"file:///{target}")
        else:
            messagebox.showinfo("Nothing yet", "Nothing has been exported to that folder yet.")

    def stop(self) -> None:
        self.stop_flag.set()
        self.log("Stop requested - finishing the current object...")

    def dump_tree(self) -> None:
        def job():
            from core.editor import probe_editor

            sap = self._connect()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = os.path.join(self.var_output.get() or os.getcwd(),
                               f"sapgui_tree_{stamp}.txt")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            sap.dump_tree(out)
            try:
                probe = probe_editor(sap)
                with open(out, "a", encoding="utf-8") as fh:
                    fh.write("\n" + probe + "\n")
                for line in probe.splitlines():
                    self._post("log", line)
            except Exception as exc:
                self._post("log", f"(editor probe failed: {exc})")
            self._post("log", f"Component tree written to {out}")
            self._post("info", f"Diagnostics written to:\n{out}")
        self._run_worker(job)

    def start_export(self) -> None:
        program = (self.var_program.get() or "").strip().upper()
        output = (self.var_output.get() or "").strip()
        if not program:
            messagebox.showwarning("No program", "Detect or type a program name first.")
            return
        if not output:
            messagebox.showwarning("No folder", "Choose an output folder.")
            return

        options = ExportOptions(
            output_root=output,
            customer_only=self.var_customer_only.get(),
            use_se80_tree=self.var_use_tree.get(),
            write_report=self.var_report.get(),
            allow_clipboard=self.var_clipboard.get(),
            allow_download=self.var_download.get(),
        )

        def job():
            sap = self._connect()
            self._post("status", sap.describe_session())
            self._post("log", "")
            self._post("log", f"=== Exporting {program} ===")
            result = export_program(
                sap,
                program,
                options,
                log=lambda m: self._post("log", m),
                progress=lambda d, t, _n: self._post("progress", (d, t)),
                should_stop=self.stop_flag.is_set,
            )
            summary = (
                f"{len(result.succeeded)} object(s) exported to:\n{result.folder}"
            )
            if result.failed:
                summary += f"\n\nFailed: {', '.join(o.name for o in result.failed)}"
            self._post("info", summary)

        self._run_worker(job)


def run() -> None:
    root = Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    ExporterApp(root)
    root.mainloop()
