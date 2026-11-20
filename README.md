# SAP ABAP Source Exporter

Export an ABAP program **and every include it pulls in** from your running SAP GUI
session into separate `.txt` files — one click, no copy-paste, no RFC.

```
ZMM_CREATEPO/
├── ZMM_CREATEPO.txt
├── ZMM_CREATEPO_TOP.txt
├── ZMM_CREATEPO_F01.txt
├── ZMM_CREATEPO_O01.txt
├── ZMM_CREATEPO_I01.txt
├── _export_report.txt        (what was exported, how, how many lines)
└── _export_manifest.json
```

---

## 1. Read this first: how "screen-based" is implemented, and why

You asked for screen-based extraction and explicitly excluded RFC. That is the right
call, and it is achievable — but *how* matters, so here is the honest engineering
picture before anything else.

### Pixel OCR is not a viable way to read source code

Reading the editor as an image (screenshot → OCR) fails on source code for reasons
that cannot be engineered away:

| Problem | Consequence |
|---|---|
| `l` / `1` / `I`, `0` / `O`, `.` / `,` are near-identical in SAP's editor font | Silent corruption inside identifiers and literals |
| Leading whitespace is not reliably recoverable | ABAP indentation destroyed |
| Only ~40 lines are visible at a time | Needs scroll-stitching, which drifts and duplicates/loses lines |
| No checksum or line count to verify against | You cannot tell a correct export from a corrupt one |

A source export that is 99.5% correct is worse than useless — you would not know
which 0.5% is wrong. So OCR is **not** used.

### What is used instead: the SAP GUI Scripting object model

SAP GUI exposes its **own live screen** as a COM object tree (`SAPGUI` running
object → `GetScriptingEngine`). Every control physically on your screen — fields,
trees, menus, the ABAP editor — is addressable, with its exact text.

This satisfies every constraint you set:

| Your requirement | Status |
|---|---|
| Screen-based / GUI automation | ✅ Reads the actual on-screen control tree |
| No RFC, no SAP RFC SDK | ✅ Zero network protocol involvement; pure client-side COM |
| No backend programs or modifications | ✅ Nothing installed or transported into SAP |
| Uses the existing authenticated session | ✅ Attaches to the session you are already logged into |
| Keyboard / mouse / clipboard automation | ✅ Used as fallback strategies (below) |
| Must not assume source fits on one screen | ✅ The editor control holds the whole document in memory — scrolling is irrelevant |

**SAP GUI Scripting is not RFC.** It is the same client-side automation layer behind
SAP's own script recorder (`Alt+F12 → Script Recording and Playback`). No
`sapnwrfc.dll`, no `pyrfc`, no service user, no function module.

### The one real prerequisite

Scripting must be switched on. It is off by default in some landscapes:

**Client side** — SAP Logon → `Options` → `Accessibility & Scripting` → `Scripting`:
- ☑ Enable scripting
- ☐ Notify when a script attaches to SAP GUI *(uncheck)*
- ☐ Notify when a script opens a connection *(uncheck)*

**Server side** — profile parameter `sapgui/user_scripting = TRUE` (Basis, via `RZ11`).
Read-only scripting is enough; this tool never writes to SAP.

If your Basis team will not enable it, see §6 — a scripting-free fallback mode ships
with the tool, and §7 lists the alternatives.

---

## 2. How the extraction works

### Finding the program you selected

No typing. The app reads the live screen and scores several independent signals:

1. The window caption — `ABAP Editor: Change Report ZMM_CREATEPO`
2. Input fields whose technical name carries a program (`RS38M-PROGRAMM`,
   `RSEUX-OBJECT_NAME`, …)
3. The selected node of the SE80 repository-browser tree
4. Screen headers

The highest-scoring candidate is shown in the app **with its evidence**, so you can
see *why* it was chosen. If you had an include selected (say `ZMM_CREATEPO_TOP`),
the naming convention is recognised and the main program `ZMM_CREATEPO` is proposed
instead. You can always overtype it.

### Finding the includes

Two complementary sources, merged:

**A. Source parsing (primary).** The main program's text is parsed for `INCLUDE`
statements; each include found is opened and parsed in turn, breadth-first, to any
nesting depth. This mirrors exactly how the ABAP compiler assembles a program.

The parser is not a naive regex — it correctly handles:

- chained statements — `INCLUDE: ztop, zf01, zo01.` ← *this is what your screenshot uses*
- `INCLUDE zfoo IF FOUND.`
- `*` full-line comments and `"` trailing comments
- the word `INCLUDE` appearing inside a string literal
- `INCLUDE TYPE ty.` / `INCLUDE STRUCTURE st.` inside `TYPES BEGIN OF` blocks — these are
  **not** source includes and are skipped
- system includes such as `<ICON>`
- `/NAMESPACE/` names
- circular and duplicate references

**B. The SE80 object tree (secondary).** The `Includes` folder in the repository
browser — the one in your screenshot — is expanded through the `GuiTree` API and its
children are read. This catches includes registered against the program that no
parsed `INCLUDE` statement reached.

### Getting the source text — three strategies, in order

The source is read in whole, never a visible page at a time:

1. **`control_text`** — read the `text` property of the ABAP editor control.
   The control holds the **entire** document regardless of scroll position. Fast,
   exact, does not steal focus. *Used in the vast majority of cases.*
2. **`clipboard`** — focus the editor control via scripting, `Ctrl+A`, `Ctrl+C`,
   read the Windows clipboard. `Ctrl+A` selects the whole document, so again
   scrolling and file size are irrelevant. Covers the old editor, the new front-end
   editor, and read-only display mode.
3. **`menu_download`** — drive `Utilities → More Utilities → Upload/Download →
   Download` and let SAP itself write the file out. Slowest, but immune to the
   other two failing.

There is also a scroll-and-harvest reader for the legacy table-control editor, the
one editor that genuinely only holds the visible page.

If **all** strategies fail for an object, that object is recorded as failed in the
report — a truncated file is never written. Which strategy was used for each object
is logged in `_export_report.txt`, so you can always tell how a file was obtained.

### Safety properties

- Objects are opened in **display mode** (SE38 Display, never Change) — no enqueue
  locks are taken, so you can export while colleagues are editing, and nothing can
  be modified by accident.
- **A control is only pressed when it identifies itself.** Navigation uses the
  `Program → Display` menu entry, or a toolbar button whose own label/tooltip reads
  "Display". Hardcoded button indices and blind function keys are not used.
- **Execute-like controls are blacklisted.** Anything whose label matches
  Execute / Run / Test / Debug / Ausführen is skipped even if it also matches
  "Display".
- **The ABAP Debugger halts everything.** Before and after every navigation step the
  window title is checked; if the debugger is on screen the run aborts immediately
  rather than pressing buttons inside a live debugging session.
- If no identifiable Display control exists, the export **fails with an error**
  instead of guessing.
- Modal dialogs are handled conservatively: anything resembling *"save your
  changes?"* is answered **No**, never Yes.
- Blind typing is gated: the download fallback only types a path once a real
  Windows dialog (class `#32770`) is confirmed on screen.
- Nothing is ever written back to SAP.

> #### Incident that produced these rules (v1.0.0 → v1.1.0)
> v1.0.0 pressed `wnd[0]/tbar[1]/btn[17]` on the assumption that index 17 is
> "Display" on the SE38 screen. On at least one system it is not: the program was
> **executed** and stopped in the ABAP Debugger at `START-OF-SELECTION`.
> No source was lost, but executing a program the user only wanted to *read* is a
> serious defect. Index-based pressing has been removed entirely; see
> `tests/test_safety.py`, which fails if it ever comes back.

---

## 3. Installing and building the `.exe`

Requirements: Windows, Python 3.10+, SAP GUI for Windows 7.40+.

```bat
git clone <this repo>
cd sap-abap-exporter
build.bat
```

`build.bat` creates a virtual environment, installs dependencies, runs the test
suite and produces:

```
dist\sap-abap-exporter.exe
```

A single self-contained file (~12 MB). Copy it anywhere; it needs no installer and
no admin rights.

> **Note:** a Windows `.exe` can only be produced on Windows — PyInstaller does not
> cross-compile. If you have no Python on your machine, push the repo to GitHub and
> the included workflow (`.github/workflows/build-windows.yml`) builds the `.exe` for
> you on a hosted Windows runner and publishes it as a downloadable artifact.

Run from source instead, if you prefer:

```bat
pip install -r requirements.txt
python src\main.py
```

---

## 4. Using it

1. In SAP GUI, open your program in **SE80** (or SE38) — exactly as in your screenshot.
2. Start `sap-abap-exporter.exe`.
3. The SAP session is listed automatically. Click **Detect from SAP** → `ZMM_CREATEPO` appears.
4. Pick an output folder.
5. Click **Export source**.

The log shows each object as it is processed; the folder appears with one `.txt`
per object, named exactly as the SAP object.

### Command line

Useful for scheduled or batch exports:

```bat
sap-abap-exporter.exe --cli                                   ;; export whatever is selected
sap-abap-exporter.exe --cli --program ZMM_CREATEPO --out D:\src
sap-abap-exporter.exe --cli --customer-only                   ;; skip SAP standard includes
sap-abap-exporter.exe --cli --list-sessions
sap-abap-exporter.exe --cli --dump-tree diag.txt              ;; diagnostics
sap-abap-exporter.exe --cli --blind                           ;; no-scripting fallback
```

| Option | Meaning |
|---|---|
| `--program` / `-p` | Program name; omit to auto-detect |
| `--out` / `-o` | Output root (default `%USERPROFILE%\Documents\ABAP_Export`) |
| `--customer-only` | Only `Z*`, `Y*`, `/namespace/` objects |
| `--no-se80-tree` | Rely on source parsing alone |
| `--no-clipboard` / `--no-download` | Disable a fallback strategy |
| `--max-depth` | Include nesting limit (default 10) |
| `--encoding`, `--lf` | Output encoding; Unix line endings |
| `--connection`, `--session` | Pick among multiple SAP sessions |

---

## 5. Known limitations

Stated plainly, because you will hit some of these eventually.

| # | Limitation | Effect | Mitigation |
|---|---|---|---|
| 1 | Scripting may be disabled client- or server-side | Tool cannot attach at all | §1 setup, or `--blind` mode, or §7 alternatives |
| 2 | Some landscapes force a *"a script is opening a connection"* popup | Needs one click per run | Clear the two notify checkboxes in SAP Logon options |
| 3 | **Class pools** (`CL_*`, SE24) are not include-based | Local class sections (CCDEF/CCIMP/CCMAC/CCAU) are not discovered | Scope is reports, module pools and function groups. Export class sources via SE80 → Download, or extend `editor.open_object` to use SE24 |
| 4 | Generated includes with no `INCLUDE` statement (certain function-group and enhancement includes) | Could be missed by parsing alone | Covered by the SE80 tree reader (option B); leave it enabled |
| 5 | Dynamically generated / `GENERATE SUBROUTINE POOL` code | Not discoverable from static source | Out of scope for any static exporter, including abapGit |
| 6 | The clipboard fallback needs the SAP window in the foreground | Do not type elsewhere while it runs | Strategy 1 is used almost always and does not steal focus |
| 7 | Very old SAP GUI with the legacy table-control editor | Slower scroll-and-harvest path | Switch to the front-end editor in `Utilities → Settings → ABAP Editor` |
| 7b | **New ABAP editor** (`SAPGUI.AbapEditor.1`) is an embedded OCX whose `Text` property returns its ProgID, not the document | Strategy 1 cannot read it | Detected and rejected automatically; the clipboard strategy takes over. **Both "Notify when a script…" boxes must be unchecked** or the popup steals focus mid-copy |
| 8 | Non-ASCII in comments on non-Unicode systems | Needs matching encoding | `--encoding cp1252` (default is UTF-8) |
| 9 | Only the **active** version is exported | Inactive versions are not written | Activate first, or open the inactive version before exporting |
| 10 | Exports **source**, not metadata | No screens, text elements, GUI statuses, DDIC | By design — see §7 if you need a complete object |

**Not a limitation:** source longer than the screen. Strategies 1 and 2 both return
the entire document irrespective of scroll state. This was a design requirement.

---

## 6. `--blind` mode (when scripting is refused)

A COM-free fallback: finds the SAP window by caption, injects keystrokes with
`SendInput`, retrieves source via the clipboard, and navigates with the command
field (`Ctrl+/` → `/nSE38` → name → `F7`).

It is **less reliable by construction** and says so at startup:

- the program name can only be read from the window caption;
- navigation assumes `F7` = Display on the SE38 initial screen;
- every step is verified against the caption, and when verification fails the run
  **stops and asks you** rather than pressing unknown keys — `F8` is never sent;
- you must not use the keyboard or mouse while it runs.

Use it to get unblocked, not as the standard path.

---

## 7. Alternatives, if this approach is blocked

Ranked by how well they fit your stated constraints:

1. **Ask Basis for read-only scripting** (`sapgui/user_scripting = TRUE`). Usually
   approved: it grants a script nothing the logged-on user cannot already do by hand.
2. **abapGit** — the ABAP community standard. Exports complete objects (source *and*
   metadata) to files or Git. Needs a one-time report installed in the system, which
   your constraints exclude, but it is the correct long-term answer for source control.
3. **ADT / Eclipse + ABAP Development Tools** — select a program, `Export → File system`.
   No GUI automation needed, but it does use an HTTP/ADT connection, not SAP GUI.
4. **SE38 `Utilities → More Utilities → Upload/Download → Download`**, run manually per
   object. Exactly what strategy 3 automates — reliable, but it is the copy-paste
   drudgery you asked to remove.
5. **RFC / `pyrfc` with `RPY_PROGRAM_READ`** — technically the cleanest and most robust
   route. Excluded by your requirements; noted only for completeness.

---

## 8. Project layout

```
src/
├── main.py              CLI + entry point
├── app_gui.py           Tkinter front-end
└── core/
    ├── session.py       COM attach, component-tree walk, popups, navigation, menus
    ├── detect.py        Identify the selected program from the live screen
    ├── editor.py        Open objects; three extraction strategies
    ├── includes.py      ABAP comment/statement/INCLUDE parser; SE80 tree reader
    ├── exporter.py      Breadth-first traversal, file writing, report + manifest
    ├── blind.py         Scripting-free fallback
    └── winutil.py       SendInput, window focus, clipboard
tests/                   74 tests, no SAP required
```

### Tests

```bat
python -m pytest tests -q
```

74 tests covering:

* **the ABAP parser** - chained statements, comments, string literals,
  `INCLUDE TYPE`, namespaces, circular references;
* **capture validation** - OLE ProgIDs rejected, empty documents rejected,
  fall-through between strategies, weak captures used only as a last resort;
* **safety** - Execute-labelled controls never chosen, unlabelled buttons never
  pressed, debugger detection, whole-run abort when the debugger appears;
* **export orchestration** - nested includes, missing objects, depth limits,
  customer-only filtering, stop handling, verbatim content preservation.

The COM, keystroke and clipboard layers cannot be unit-tested without a live SAP
GUI; they are isolated in `session.py`, `editor.py` and `winutil.py` and are
exercised by the `Diagnostics` button (see below).

---

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| *"No running SAP GUI found"* | SAP Logon is not running, or the app runs elevated and SAP does not (COM cannot cross integrity levels — run both the same way) |
| *"scripting engine is not reachable"* | Scripting disabled — §1 |
| *"you are not logged on to any system"* | Connection open but no session; log on |
| Detection picks the wrong name | Overtype it — it is just a pre-filled suggestion |
| An include fails on your release | Press **Diagnostics** — it dumps the whole GUI component tree to a text file showing exact control ids for *your* SAP version. That file is all that is needed to adapt `editor.find_editor` or `editor.open_object` |
| Export is slow | Each object is a GUI round-trip, ~0.5–1 s. 40 includes ≈ 30 s. Strategy 1 is the fast path; `menu_download` is much slower |

---

## 10. Licence

MIT. Provided as-is; verify exports before relying on them for anything regulated.
