#!/usr/bin/env python3
"""
Static Fire Stand - graphical front end.

A tkinter window over the same functions static_fire.py uses from the
command line: download from the stand, analyse a saved dump, import a
legacy CSV, or run the no-hardware demo - plus the stand-side settings
(tare, calibration, igniter on-time). Everything the console script
prints shows up in the log pane instead, and the output folder can be
opened with one click when a run finishes.

Run it with:

    python tools/sf_gui.py

tkinter ships with the standard Python installer on Windows and macOS. On
Linux, install it separately if "python3 -m tkinter" errors out - e.g.
`sudo apt install python3-tk` on Debian/Ubuntu.
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import traceback
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from sf_protocol import (DEFAULT_ARM_CODE, DEFAULT_CAL_COUNTS_PER_N,  # noqa: E402
                         DEFAULT_IGNITION_MS, Stand, find_port, list_ports,
                         load_legacy_csv, parse_dump)
from static_fire import download, new_output_dir, open_folder, run_analysis  # noqa: E402

APP_TITLE = "Static Fire Stand"
FW_IGNITION_CEILING_MS = 5000

# Two themes: "dark" matches the stand's own web UI for indoor/desk use.
# "sunlight" is a high-contrast light theme for a laptop screen outdoors -
# a dark UI mostly turns into a mirror in direct sun, so this instead uses
# near-black text on a light, low-glare ground with deliberately darkened
# accent colours (the web UI's amber/green read as washed-out pastel on a
# bright screen at max brightness).
PALETTES = {
    "dark": dict(
        bg="#0d0d0d", card="#1a1a19", card2="#141413", ink="#f4f4f2", mut="#9b9a94",
        line="#2c2c2a", ok="#0ca30c", warn="#fab219", crit="#d03b3b", acc="#3987e5",
        acc_active="#5aa0f0", danger="#a92e2e", danger_active="#c23a3a",
        entry_bg="#000000", select="#233247",
    ),
    "sunlight": dict(
        bg="#f2f1ec", card="#ffffff", card2="#e4e2d9", ink="#000000", mut="#3a3a36",
        line="#8a887e", ok="#0a6e0a", warn="#a15c00", crit="#a3241c", acc="#0a4fb0",
        acc_active="#083c87", danger="#a3241c", danger_active="#7e1c16",
        entry_bg="#ffffff", select="#cfe0ff",
    ),
}
DEFAULT_THEME = "dark"
THEME_LABEL = {"dark": "Dark", "sunlight": "Sunlight"}
MONO = ("Consolas", 10) if sys.platform.startswith("win") else ("Menlo", 10) \
    if sys.platform == "darwin" else ("Monospace", 10)


# ---------------------------------------------------------------------
#  Plumbing: run work off the UI thread, forward prints to the log pane
# ---------------------------------------------------------------------
class QueueWriter:
    """A file-like object that pushes writes onto a thread-safe queue."""

    def __init__(self, q: "queue.Queue[str]"):
        self.q = q

    def write(self, text: str) -> None:
        if text:
            self.q.put(text)

    def flush(self) -> None:
        pass


class Tooltip:
    """A small delayed hover label - the closest tkinter gets to a title
    attribute. Purely cosmetic: never raises even if the widget is gone."""

    def __init__(self, widget: tk.Widget, text: str):
        self.widget = widget
        self.text = text
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        self._after_id: str | None = None

    def _schedule(self, _evt=None) -> None:
        self._after_id = self.widget.after(450, self._show)

    def _show(self) -> None:
        if self.tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        try:
            self.tip.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", background="#26261f",
                foreground="#eeeeee", relief="solid", borderwidth=1,
                font=("Segoe UI", 9), padx=7, pady=4, wraplength=300).pack()

    def _hide(self, _evt=None) -> None:
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


class App(tk.Tk):
    POLL_MS = 80

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("920x700")
        self.minsize(720, 520)

        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.busy = False
        self.debug_var = tk.BooleanVar(value=False)
        self.last_outdir: Path | None = None
        self.theme = DEFAULT_THEME
        self.palette = PALETTES[self.theme]

        self._build_style()
        self._build_menubar()
        self._build_widgets()
        self._refresh_ports()
        self.after(self.POLL_MS, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- look and feel -----------------------------------------------------
    def _build_style(self) -> None:
        p = self.palette
        self.configure(bg=p["bg"])
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=p["bg"], foreground=p["ink"],
                        fieldbackground=p["entry_bg"], bordercolor=p["line"],
                        darkcolor=p["line"], lightcolor=p["line"],
                        troughcolor=p["card"], font=("Segoe UI", 10))
        style.configure("TFrame", background=p["bg"])
        style.configure("Card.TFrame", background=p["card"])
        style.configure("TLabel", background=p["bg"], foreground=p["ink"])
        style.configure("Mut.TLabel", background=p["bg"], foreground=p["mut"])
        style.configure("Header.TLabel", background=p["bg"], foreground=p["ink"],
                        font=("Segoe UI", 15, "bold"))
        style.configure("SubHeader.TLabel", background=p["bg"], foreground=p["mut"],
                        font=("Segoe UI", 9))
        style.configure("TLabelframe", background=p["bg"], bordercolor=p["line"])
        style.configure("TLabelframe.Label", background=p["bg"], foreground=p["mut"],
                        font=("Segoe UI", 9, "bold"))
        style.configure("TCheckbutton", background=p["bg"], foreground=p["ink"])
        style.map("TCheckbutton", background=[("active", p["bg"])])
        style.configure("TEntry", fieldbackground=p["entry_bg"], foreground=p["ink"],
                        insertcolor=p["ink"], bordercolor=p["line"])
        style.configure("TCombobox", fieldbackground=p["entry_bg"], foreground=p["ink"],
                        background=p["card"], arrowcolor=p["mut"])
        style.map("TCombobox", fieldbackground=[("readonly", p["entry_bg"])],
                  foreground=[("readonly", p["ink"])])
        self.option_add("*TCombobox*Listbox.background", p["entry_bg"])
        self.option_add("*TCombobox*Listbox.foreground", p["ink"])
        self.option_add("*TCombobox*Listbox.selectBackground", p["select"])

        style.configure("TNotebook", background=p["bg"], bordercolor=p["line"])
        style.configure("TNotebook.Tab", background=p["card"], foreground=p["mut"],
                        padding=(14, 7), font=("Segoe UI", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", p["bg"])],
                  foreground=[("selected", p["ink"])])

        style.configure("TButton", background=p["card"], foreground=p["ink"],
                        bordercolor=p["line"], focusthickness=1, padding=(10, 8))
        style.map("TButton",
                  background=[("disabled", p["card2"]), ("active", p["line"])],
                  foreground=[("disabled", p["mut"])])
        style.configure("Accent.TButton", background=p["acc"], foreground="#ffffff",
                        bordercolor=p["acc"], padding=(10, 9), font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton",
                  background=[("disabled", p["card2"]), ("active", p["acc_active"])],
                  foreground=[("disabled", p["mut"])])
        style.configure("Danger.TButton", background=p["danger"], foreground="#ffffff",
                        bordercolor=p["danger"], padding=(10, 8))
        style.map("Danger.TButton",
                  background=[("disabled", p["card2"]), ("active", p["danger_active"])],
                  foreground=[("disabled", p["mut"])])
        style.configure("Small.TButton", padding=(6, 3), font=("Segoe UI", 8))

        style.configure("TProgressbar", background=p["acc"], troughcolor=p["card"],
                        bordercolor=p["card"], lightcolor=p["acc"], darkcolor=p["acc"])
        style.configure("Vertical.TScrollbar", background=p["card"], troughcolor=p["bg"],
                        bordercolor=p["bg"], arrowcolor=p["mut"])

    def _build_menubar(self) -> None:
        menubar = tk.Menu(self)
        filem = tk.Menu(menubar, tearoff=0)
        filem.add_command(label="Open results folder", command=self._open_results)
        filem.add_separator()
        filem.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=filem)

        viewm = tk.Menu(menubar, tearoff=0)
        self.theme_menu_var = tk.StringVar(value=self.theme)
        for key in ("dark", "sunlight"):
            viewm.add_radiobutton(label=f"{THEME_LABEL[key]} theme", value=key,
                                  variable=self.theme_menu_var,
                                  command=lambda k=key: self._set_theme(k))
        menubar.add_cascade(label="View", menu=viewm)

        helpm = tk.Menu(menubar, tearoff=0)
        helpm.add_command(label="Open README", command=self._open_readme)
        helpm.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=helpm)
        self.config(menu=menubar)

    def _theme_btn_text(self) -> str:
        return "☀ Sunlight mode" if self.theme == "dark" else "🌙 Dark mode"

    def _toggle_theme(self) -> None:
        self._set_theme("sunlight" if self.theme == "dark" else "dark")

    def _set_theme(self, name: str) -> None:
        if name == self.theme or name not in PALETTES:
            return
        self.theme = name
        self.palette = PALETTES[name]
        self.theme_menu_var.set(name)
        self._build_style()  # ttk widgets re-render automatically on style change
        p = self.palette
        # plain tk widgets (not ttk) need their colours poked by hand
        self.log.configure(background=p["entry_bg"], foreground=p["ink"],
                           insertbackground=p["ink"])
        self.log.tag_configure("err", foreground=p["crit"])
        self.log.tag_configure("warn", foreground=p["warn"])
        self.log.tag_configure("ok", foreground=p["ok"])
        self.log.tag_configure("head", foreground=p["acc"], font=(MONO[0], MONO[1], "bold"))
        self.status_dot.configure(bg=p["bg"])
        self.status_dot.itemconfig(self._dot, fill=p["warn"] if self.busy else p["ok"])
        self.theme_btn.configure(text=self._theme_btn_text())
        self.status_var.set(f"Switched to the {THEME_LABEL[name]} theme.")

    # -- layout --------------------------------------------------------------
    def _build_widgets(self) -> None:
        p = self.palette
        header = ttk.Frame(self, padding=(16, 14, 16, 6))
        header.pack(fill="x")
        title_row = ttk.Frame(header)
        title_row.pack(fill="x")
        ttk.Label(title_row, text="STATIC FIRE STAND", style="Header.TLabel").pack(
            side="left", anchor="w")
        self.theme_btn = ttk.Button(title_row, text=self._theme_btn_text(),
                                    style="Small.TButton", command=self._toggle_theme)
        self.theme_btn.pack(side="right", anchor="e")
        Tooltip(self.theme_btn, "Switch between the dark theme and a high-contrast "
                                "light theme that stays readable on a laptop screen "
                                "in direct sunlight.")
        ttk.Label(header, text="Extract, analyse and configure - GUI front end for the tools "
                              "in this folder.", style="SubHeader.TLabel").pack(anchor="w")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=16, pady=(4, 8))

        extract = ttk.Frame(nb, padding=4)
        stand = ttk.Frame(nb, padding=4)
        nb.add(extract, text="  Extract && Analyse  ")
        nb.add(stand, text="  Stand Tools  ")
        self._build_extract_tab(extract)
        self._build_stand_tab(stand)

        # -- log pane, shared by both tabs -----------------------------------
        log_frame = ttk.LabelFrame(self, text="LOG")
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 6))

        log_toolbar = ttk.Frame(log_frame)
        log_toolbar.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Button(log_toolbar, text="Copy", style="Small.TButton",
                  command=self._copy_log).pack(side="right", padx=2)
        ttk.Button(log_toolbar, text="Clear", style="Small.TButton",
                  command=self._clear_log).pack(side="right", padx=2)
        ttk.Checkbutton(log_toolbar, text="Show full tracebacks (dev)",
                        variable=self.debug_var).pack(side="left", padx=2)
        Tooltip(log_toolbar, "When on, an action that fails prints the full Python "
                             "traceback here instead of just the error message.")

        self.log = scrolledtext.ScrolledText(
            log_frame, height=14, state="disabled", wrap="word", font=MONO,
            background=p["entry_bg"], foreground=p["ink"], insertbackground=p["ink"],
            relief="flat", borderwidth=0, padx=8, pady=6)
        self.log.pack(fill="both", expand=True, padx=4, pady=4)
        self.log.tag_configure("err", foreground=p["crit"])
        self.log.tag_configure("warn", foreground=p["warn"])
        self.log.tag_configure("ok", foreground=p["ok"])
        self.log.tag_configure("head", foreground=p["acc"], font=(MONO[0], MONO[1], "bold"))
        self._log_write_welcome()

        status_bar = ttk.Frame(self, padding=(16, 0, 16, 12))
        status_bar.pack(fill="x")
        self.status_dot = tk.Canvas(status_bar, width=10, height=10, bg=p["bg"],
                                    highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 6))
        self._dot = self.status_dot.create_oval(1, 1, 9, 9, fill=p["ok"], outline="")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(status_bar, textvariable=self.status_var, style="Mut.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(status_bar, mode="indeterminate", length=140)
        self.progress.pack(side="right")
        self.open_btn = ttk.Button(status_bar, text="Open results folder",
                                   command=self._open_results, state="disabled")
        self.open_btn.pack(side="right", padx=(0, 10))

    def _log_write_welcome(self) -> None:
        self.log.configure(state="normal")
        self.log.insert(
            "end",
            "Ready. Pick an action on the left tab to extract and analyse data, or "
            "use Stand Tools to tare / calibrate / configure the hardware.\n",
            ("head",))
        self.log.configure(state="disabled")

    def _build_extract_tab(self, parent: ttk.Frame) -> None:
        pad = dict(padx=6, pady=5)

        port_frame = ttk.LabelFrame(parent, text="SERIAL PORT  (only needed to download)")
        port_frame.pack(fill="x", **pad)
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(port_frame, textvariable=self.port_var, width=42,
                                       state="readonly")
        self.port_combo.pack(side="left", padx=6, pady=6)
        refresh_btn = ttk.Button(port_frame, text="⟳ Refresh", command=self._refresh_ports)
        refresh_btn.pack(side="left", padx=6)
        Tooltip(refresh_btn, "Re-scan serial ports. The Raspberry Pi Pico is "
                             "auto-selected when it is the only match.")

        opt_frame = ttk.LabelFrame(parent, text="OPTIONS")
        opt_frame.pack(fill="x", **pad)
        opt_frame.columnconfigure(1, weight=1)

        r = 0
        lbl = ttk.Label(opt_frame, text="Propellant mass [g]:")
        lbl.grid(row=r, column=0, sticky="w", padx=6, pady=4)
        self.fuel_var = tk.StringVar()
        fuel_entry = ttk.Entry(opt_frame, textvariable=self.fuel_var, width=12)
        fuel_entry.grid(row=r, column=1, sticky="w", padx=6, pady=4)
        for w in (lbl, fuel_entry):
            Tooltip(w, "Leave blank to estimate propellant mass from how much the "
                       "load cell's resting reading drops across the burn. That only "
                       "works if the stand settles back to rest afterwards - weigh the "
                       "motor before/after for a trustworthy Isp.")
        self.plots_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_frame, text="Generate charts (PNG)",
                        variable=self.plots_var).grid(row=r, column=2, sticky="w",
                                                       padx=16, pady=4)

        r += 1
        lbl = ttk.Label(opt_frame, text="Ignition threshold [σ of noise]:")
        lbl.grid(row=r, column=0, sticky="w", padx=6, pady=4)
        self.sigma_var = tk.StringVar(value="6")
        sigma_entry = ttk.Entry(opt_frame, textvariable=self.sigma_var, width=12)
        sigma_entry.grid(row=r, column=1, sticky="w", padx=6, pady=4)
        for w in (lbl, sigma_entry):
            Tooltip(w, "How many standard deviations of baseline noise the thrust must "
                       "clear, for 3 samples in a row, to count as first motion. Higher "
                       "= less sensitive to noise spikes. Default 6.")

        r += 1
        lbl = ttk.Label(opt_frame, text="Output folder:")
        lbl.grid(row=r, column=0, sticky="w", padx=6, pady=4)
        self.outdir_var = tk.StringVar()
        outdir_entry = ttk.Entry(opt_frame, textvariable=self.outdir_var, width=46)
        outdir_entry.grid(row=r, column=1, columnspan=2, sticky="we", padx=6, pady=4)
        Tooltip(outdir_entry, "Leave blank to use results/<timestamp> next to the tools folder.")
        ttk.Button(opt_frame, text="Browse...", command=self._pick_outdir).grid(
            row=r, column=3, padx=6, pady=4)

        actions = ttk.LabelFrame(parent, text="ACTIONS")
        actions.pack(fill="both", expand=True, **pad)
        self.action_buttons: list[ttk.Button] = []
        rows = (
            ("⬇  Download from stand & analyse", self._act_download, "Accent.TButton",
             "Connects over serial, downloads every stored burn, saves the raw dump "
             "to disk, then runs the full analysis."),
            ("📂  Analyse a saved raw dump...", self._act_from_file, "TButton",
             "Re-run the analysis on a raw_dump.txt saved by an earlier download."),
            ("📜  Import a legacy V0 CSV...", self._act_legacy_csv, "TButton",
             "Read one or more Time(ms)/Thrust(N)/Pyro_Active CSV files from the "
             "original firmware."),
            ("🧪  Run demo (no hardware needed)", self._act_demo, "TButton",
             "Generates a realistic synthetic dataset and runs the whole pipeline on "
             "it - useful for trying the tool before you have a motor on the stand."),
        )
        for text, cmd, sty, tip in rows:
            b = ttk.Button(actions, text=text, command=cmd, style=sty)
            b.pack(fill="x", padx=8, pady=5)
            Tooltip(b, tip)
            self.action_buttons.append(b)

    def _build_stand_tab(self, parent: ttk.Frame) -> None:
        pad = dict(padx=6, pady=5)
        ttk.Label(
            parent,
            text="These talk to the stand directly over serial. They do not touch "
                 "the Extract tab's output folder.",
            style="Mut.TLabel", wraplength=760, justify="left").pack(fill="x", **pad)

        actions = ttk.LabelFrame(parent, text="ACTIONS")
        actions.pack(fill="x", **pad)
        self.stand_buttons: list[ttk.Button] = []
        rows = (
            ("ℹ  Show stand status", self._act_status,
             "Firmware version, calibration factor, igniter on-time, storage and "
             "boot/resume counters."),
            ("⚖  Zero (tare) the load cell", self._act_tare,
             "Zeroes the load cell with whatever is on the stand right now."),
            ("🎯  Calibrate with a known weight...", self._act_calibrate,
             "Place a known weight where the motor pushes, then compute the "
             "counts/N factor from how much the reading moves."),
            (f"🔢  Calibrate: enter counts/N directly...", self._act_calibrate_manual,
             f"Type the calibration factor straight in, if you already know it. "
             f"Firmware default: {DEFAULT_CAL_COUNTS_PER_N:g} counts/N."),
            (f"⏱  Set igniter on-time...", self._act_set_ignition,
             f"How long the pyro channel is energised. Firmware default: "
             f"{DEFAULT_IGNITION_MS} ms. Hard ceiling: {FW_IGNITION_CEILING_MS} ms, "
             f"enforced by the firmware regardless of this setting. Also changeable "
             f"from the stand's web UI."),
        )
        for text, cmd, tip in rows:
            b = ttk.Button(actions, text=text, command=cmd)
            b.pack(fill="x", padx=8, pady=4)
            Tooltip(b, tip)
            self.stand_buttons.append(b)

        note = ttk.Frame(parent, padding=(6, 2))
        note.pack(fill="x", **pad)
        ttk.Label(note, text=f"Default arming code for the web UI: {DEFAULT_ARM_CODE} "
                            f"(set in firmware/StaticFire_Stand/config.h - change it "
                            f"before a live firing).",
                 style="Mut.TLabel", wraplength=760, justify="left").pack(anchor="w")

        danger = ttk.LabelFrame(parent, text="DANGER ZONE")
        danger.pack(fill="x", **pad)
        b = ttk.Button(danger, text="🗑  Erase ALL data on the stand...",
                       command=self._act_erase, style="Danger.TButton")
        b.pack(fill="x", padx=8, pady=4)
        Tooltip(b, "Deletes every burn stored on the stand. Cannot be undone - "
                   "download and check your data first.")
        self.stand_buttons.append(b)

    # -- helpers -----------------------------------------------------------
    def _refresh_ports(self) -> None:
        ports = list_ports()
        values = [f"{dev}  ({label})" for dev, label in ports]
        self.port_combo["values"] = values
        if values:
            auto = find_port()
            idx = next((i for i, (d, _) in enumerate(ports) if d == auto), 0)
            self.port_combo.current(idx)
        else:
            self.port_var.set("")

    def _selected_port(self) -> str | None:
        raw = self.port_var.get()
        return raw.split()[0] if raw else None

    def _pick_outdir(self) -> None:
        chosen = filedialog.askdirectory(title="Choose an output folder")
        if chosen:
            self.outdir_var.set(chosen)

    def _fuel_grams(self) -> float | None:
        raw = self.fuel_var.get().strip().replace(",", ".")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            messagebox.showerror(APP_TITLE, "Propellant mass must be a number.")
            raise

    def _sigma(self) -> float:
        raw = self.sigma_var.get().strip().replace(",", ".")
        try:
            return float(raw) if raw else 6.0
        except ValueError:
            return 6.0

    def _open_results(self) -> None:
        if self.last_outdir:
            open_folder(self.last_outdir)
        else:
            messagebox.showinfo(APP_TITLE, "No results folder yet - run an action first.")

    def _open_readme(self) -> None:
        readme = SCRIPT_DIR.parent / "README.md"
        if not readme.exists():
            messagebox.showinfo(APP_TITLE, "README.md not found next to the tools folder.")
            return
        try:
            if sys.platform.startswith("win"):
                import os
                os.startfile(readme)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", str(readme)], check=False)
            else:
                subprocess.run(["xdg-open", str(readme)], check=False)
        except Exception:
            webbrowser.open(readme.as_uri())

    def _show_about(self) -> None:
        messagebox.showinfo(
            APP_TITLE,
            "Static Fire Stand - GUI front end\n\n"
            "Wraps the same download/analysis code as static_fire.py and the "
            "console menu; nothing here changes how the numbers are computed.\n\n"
            f"Default calibration factor: {DEFAULT_CAL_COUNTS_PER_N:g} counts/N\n"
            f"Default igniter on-time: {DEFAULT_IGNITION_MS} ms\n"
            f"Default web arming code: {DEFAULT_ARM_CODE}\n\n"
            "See README.md for wiring, safety and the full operating tutorial.")

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno(
                APP_TITLE,
                "A task is still running. Closing now will not stop it cleanly. Close anyway?"):
                return
        self.destroy()

    # -- log pane ------------------------------------------------------------
    def _copy_log(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.log.get("1.0", "end-1c"))
        self.status_var.set("Log copied to the clipboard.")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        start = self.log.index("end-1c")
        self.log.insert("end", text)
        pos = start
        for line in text.splitlines(keepends=True):
            end = self.log.index(f"{pos}+{len(line)}c")
            low = line.lower()
            if "error" in low or "traceback" in low or low.lstrip().startswith("! "):
                self.log.tag_add("err", pos, end)
            elif "warning" in low or line.lstrip().startswith("!"):
                self.log.tag_add("warn", pos, end)
            elif line.strip().startswith("=") and len(line.strip()) > 8:
                self.log.tag_add("head", pos, end)
            elif line.lstrip().startswith(("done", "Done", "calibrat", "tared",
                                           "igniter on-time set", "Stand memory erased")):
                self.log.tag_add("ok", pos, end)
            pos = end
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- background task runner --------------------------------------------
    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        for b in self.action_buttons + self.stand_buttons:
            b.configure(state="disabled" if busy else "normal")
        self.status_dot.itemconfig(self._dot, fill=self.palette["warn"] if busy else self.palette["ok"])
        if busy:
            self.progress.start(12)
            self.status_var.set(message or "Working...")
        else:
            self.progress.stop()
            self.status_var.set(message or "Ready.")

    def _run_task(self, label: str, fn, *args, on_done=None) -> None:
        if self.busy:
            messagebox.showinfo(APP_TITLE, "Another task is already running.")
            return
        self._set_busy(True, label)
        debug = self.debug_var.get()

        def worker() -> None:
            old_stdout = sys.stdout
            sys.stdout = QueueWriter(self.log_q)
            error: Exception | None = None
            result = None
            try:
                result = fn(*args)
            except Exception as exc:  # noqa: BLE001
                error = exc
                if debug:
                    print(f"\nERROR: {exc}\n{traceback.format_exc()}")
                else:
                    print(f"\nERROR: {exc}")
            finally:
                sys.stdout = old_stdout
            self.after(0, lambda: self._task_finished(label, error, result, on_done))

        threading.Thread(target=worker, daemon=True).start()

    def _task_finished(self, label: str, error: Exception | None, result, on_done) -> None:
        self._set_busy(False, "Done." if error is None else f"{label} failed - see log.")
        if error is not None:
            messagebox.showerror(APP_TITLE, str(error))
        elif on_done is not None:
            on_done(result)

    def _drain_log(self) -> None:
        try:
            while True:
                text = self.log_q.get_nowait()
                self._append_log(text)
        except queue.Empty:
            pass
        self.after(self.POLL_MS, self._drain_log)

    # -- Extract && Analyse actions -------------------------------------------
    def _act_download(self) -> None:
        try:
            fuel = self._fuel_grams()
        except ValueError:
            return
        port = self._selected_port()
        sigma = self._sigma()
        make_plots = self.plots_var.get()
        outdir_hint = self.outdir_var.get().strip() or None

        def job():
            outdir = new_output_dir(outdir_hint)
            raw = download(port, 115200, None, outdir)
            if raw is None:
                raise RuntimeError("Could not reach the stand. Check the port and the cable.")
            if "#BEGIN SFDUMP" not in raw:
                raise RuntimeError(
                    "The stand did not send a valid dump. Check that it runs firmware 2.x.")
            _info, burns = parse_dump(raw)
            if not burns:
                raise RuntimeError("The dump is valid but contains no burns yet.")
            print(f"\nBurns found: {len(burns)}")
            run_analysis(burns, outdir, fuel_g=fuel, sigma=sigma, make_plots=make_plots)
            return outdir

        self._run_task("Downloading and analysing", job, on_done=self._remember_outdir)

    def _act_from_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a saved raw dump",
            filetypes=[("Dump files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            fuel = self._fuel_grams()
        except ValueError:
            return
        sigma = self._sigma()
        make_plots = self.plots_var.get()
        outdir_hint = self.outdir_var.get().strip() or None

        def job():
            raw = Path(path).read_text(encoding="utf-8", errors="replace")
            if "#BEGIN SFDUMP" not in raw:
                raise RuntimeError("That file does not contain an SFDUMP block.")
            _info, burns = parse_dump(raw)
            if not burns:
                raise RuntimeError("No burns in that file.")
            outdir = new_output_dir(outdir_hint)
            print(f"Loaded {path}\nBurns found: {len(burns)}")
            run_analysis(burns, outdir, fuel_g=fuel, sigma=sigma, make_plots=make_plots)
            return outdir

        self._run_task("Analysing dump", job, on_done=self._remember_outdir)

    def _act_legacy_csv(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Choose CSV file(s) from the old V0 firmware",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not paths:
            return
        try:
            fuel = self._fuel_grams()
        except ValueError:
            return
        sigma = self._sigma()
        make_plots = self.plots_var.get()
        outdir_hint = self.outdir_var.get().strip() or None

        def job():
            burns = []
            for p in paths:
                burns.append(load_legacy_csv(p))
            for i, b in enumerate(burns):
                if b.burn_id == 0:
                    b.burn_id = i + 1
            outdir = new_output_dir(outdir_hint)
            print(f"Legacy records loaded: {len(burns)}")
            run_analysis(burns, outdir, fuel_g=fuel, sigma=sigma, make_plots=make_plots)
            return outdir

        self._run_task("Importing legacy CSV", job, on_done=self._remember_outdir)

    def _act_demo(self) -> None:
        outdir_hint = self.outdir_var.get().strip() or None

        def job():
            import make_test_dump
            outdir = new_output_dir(outdir_hint)
            dump = outdir / "demo_dump.txt"
            saved_argv = sys.argv
            try:
                sys.argv = ["make_test_dump.py", str(dump), "--count", "2"]
                make_test_dump.main()
            finally:
                sys.argv = saved_argv
            raw = dump.read_text(encoding="utf-8")
            _info, burns = parse_dump(raw)
            print(f"\nDemo burns generated: {len(burns)}")
            run_analysis(burns, outdir, fuel_g=None, sigma=self._sigma(),
                        make_plots=self.plots_var.get())
            return outdir

        self._run_task("Generating demo data", job, on_done=self._remember_outdir)

    def _remember_outdir(self, outdir: Path) -> None:
        self.last_outdir = outdir
        self.open_btn.configure(state="normal")
        if messagebox.askyesno(APP_TITLE,
                               f"Done. Results are in:\n{outdir}\n\nOpen the folder now?"):
            open_folder(outdir)

    # -- Stand Tools actions ---------------------------------------------------
    def _connect(self):
        port = self._selected_port() or find_port()
        if not port:
            raise RuntimeError("No serial port found. Is the stand plugged in?")
        return Stand(port, 115200)

    def _act_status(self) -> None:
        def job():
            with self._connect() as stand:
                info = stand.info()
                if not info:
                    raise RuntimeError("The stand did not answer. Wrong port, or old firmware?")
                for key, val in info.items():
                    print(f"  {key:.<26} {val}")
                print("\nSlot listing:")
                stand.command("l")
                print(stand.drain(2.0).rstrip())

        self._run_task("Reading stand status", job)

    def _act_tare(self) -> None:
        if not messagebox.askyesno(
            APP_TITLE,
            "Take everything off the stand except the empty motor mount. Ready to zero it?"):
            return

        def job():
            with self._connect() as stand:
                stand.tare()
                print(stand.drain(2.0).strip())

        self._run_task("Zeroing the load cell", job)

    def _act_calibrate(self) -> None:
        grams = self._ask_number(
            "Calibrate with a known weight",
            "Zero the stand first. Then place a known weight where the motor "
            "pushes and enter its mass in grams:",
            minvalue=0.001)
        if grams is None:
            return
        if not messagebox.askyesno(
            APP_TITLE, f"Is the {grams:.0f} g weight in place right now?"):
            return

        def job():
            with self._connect() as stand:
                stand.calibrate_grams(grams)
                print(stand.drain(3.0).strip())

        self._run_task("Calibrating", job)

    def _act_calibrate_manual(self) -> None:
        factor = self._ask_number(
            "Calibrate: enter counts/N directly",
            "Set the calibration factor without a weight step - useful if you "
            "already measured it, or to restore a known-good value.\n\n"
            "Raw ADC counts per newton:",
            initial=DEFAULT_CAL_COUNTS_PER_N)
        if factor is None or factor == 0:
            return

        def job():
            with self._connect() as stand:
                stand.calibrate_factor(factor)
                print(stand.drain(2.0).strip())

        self._run_task("Setting the calibration factor", job)

    def _act_set_ignition(self) -> None:
        ms = self._ask_number(
            "Set igniter on-time",
            "How long the pyro channel is energised, in milliseconds. The "
            f"firmware enforces a hard ceiling of {FW_IGNITION_CEILING_MS} ms "
            "regardless of this setting.",
            initial=DEFAULT_IGNITION_MS, minvalue=10, maxvalue=FW_IGNITION_CEILING_MS)
        if ms is None:
            return

        def job():
            with self._connect() as stand:
                stand.set_ignition_ms(int(ms))
                print(stand.drain(2.0).strip())

        self._run_task("Setting the igniter on-time", job)

    def _act_erase(self) -> None:
        answer = self._ask_text(
            "Erase stand memory",
            "This deletes EVERY burn stored on the stand. Download and check your "
            "data first - this cannot be undone.\n\nType ERASE to confirm:")
        if answer != "ERASE":
            return

        def job():
            with self._connect() as stand:
                stand.erase()
                print("Stand memory erased.")

        self._run_task("Erasing stand memory", job)

    @staticmethod
    def _ask_number(title: str, prompt: str, initial: float | None = None,
                    minvalue: float | None = None,
                    maxvalue: float | None = None) -> float | None:
        kwargs = {}
        if initial is not None:
            kwargs["initialvalue"] = initial
        if minvalue is not None:
            kwargs["minvalue"] = minvalue
        if maxvalue is not None:
            kwargs["maxvalue"] = maxvalue
        return simpledialog.askfloat(title, prompt, **kwargs)

    @staticmethod
    def _ask_text(title: str, prompt: str) -> str | None:
        return simpledialog.askstring(title, prompt)


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
