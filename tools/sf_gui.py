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
from tkinter import font as tkfont

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from sf_protocol import (DEFAULT_ARM_CODE, DEFAULT_CAL_COUNTS_PER_N,  # noqa: E402
                         DEFAULT_IGNITION_MS, Stand, find_port, list_ports,
                         load_legacy_csv, parse_dump)
from static_fire import download, new_output_dir, open_folder, run_analysis  # noqa: E402

APP_TITLE = "Static Fire Stand"
FW_IGNITION_CEILING_MS = 5000

# Look and feel: the VoltinoLabs design system (the same tokens as the
# Daidalos Actuator Console: deep indigo ground, one amber accent, outlined
# buttons, mono numbers). Two themes:
#   "dark"      the VoltinoLabs palette, for the desk;
#   "sunlight"  the same layout in a high-contrast light palette for a laptop
#               screen outdoors - a dark UI mostly turns into a mirror in
#               direct sun, so this one uses near-black text on a light, low-
#               glare ground and a deliberately darkened amber.
def _mix(fg: str, bg: str, a: float) -> str:
    """`fg` at opacity `a` over `bg` (tkinter has no rgba)."""
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * a + y * (1 - a)):02x}" for x, y in zip(f, b))


def _palette(bg, raised, surface, text, muted, subtle, accent, accent_soft, accent_strong,
             positive, danger, on_accent) -> dict:
    return dict(
        bg=bg, card=raised, card2=surface, ink=text, mut=muted, subtle=subtle,
        line=_mix(text, bg, 0.10), line_strong=_mix(text, bg, 0.18), hover_line=_mix(text, bg, 0.35),
        ok=positive, warn=accent, crit=danger,
        acc=accent, acc_soft=accent_soft, acc_active=accent_strong, acc_tint=_mix(accent, bg, 0.12),
        danger=danger, danger_active=accent_strong, danger_tint=_mix(danger, bg, 0.14),
        entry_bg=surface, select=_mix(accent, bg, 0.30), on_accent=on_accent,
        dim=_mix(muted, bg, 0.45),
    )


PALETTES = {
    # tokens.json of the VoltinoLabs artifact
    "dark": _palette(bg="#161826", raised="#1c1e2b", surface="#232532", text="#e9e9ed",
                     muted="#b2b6ca", subtle="#75798c", accent="#f5a51c", accent_soft="#ffcf7a",
                     accent_strong="#ffb734", positive="#5dd39e", danger="#f2777a",
                     on_accent="#161826"),
    "sunlight": _palette(bg="#f4f2ec", raised="#ffffff", surface="#e9e6dc", text="#161826",
                         muted="#3d4055", subtle="#5c6075", accent="#9a5b00", accent_soft="#7a4800",
                         accent_strong="#7a4800", positive="#0a6e46", danger="#b3261e",
                         on_accent="#ffffff"),
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
        pal = getattr(self.widget.winfo_toplevel(), "palette", None) or PALETTES[DEFAULT_THEME]
        tk.Label(self.tip, text=self.text, justify="left", background=pal["card2"],
                foreground=pal["ink"], relief="solid", borderwidth=1,
                font=(getattr(self.widget.winfo_toplevel(), "ui_font", "Segoe UI"), 9),
                padx=8, pady=5, wraplength=300).pack()

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
        self._set_window_identity()

        self._build_style()
        self._build_menubar()
        self._build_widgets()
        self._refresh_ports()
        self.after(self.POLL_MS, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_window_identity(self) -> None:
        """Own taskbar identity and the window icon (Windows); harmless elsewhere."""
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VoltinoLabs.StaticFireStand")
        except Exception:
            pass
        ico = SCRIPT_DIR / "windows" / "sf_stand.ico"
        try:
            if ico.exists():
                self.iconbitmap(default=str(ico))
        except tk.TclError:
            pass

    # -- look and feel -----------------------------------------------------
    def _pick_fonts(self) -> None:
        have = set(tkfont.families(self))
        self.ui_font = next((f for f in ("Inter", "Segoe UI Variable Text", "Segoe UI", "Helvetica Neue",
                                         "Helvetica", "DejaVu Sans") if f in have), "TkDefaultFont")
        self.mono_font = next((f for f in ("JetBrains Mono", "Cascadia Mono", "Consolas", "Menlo",
                                           "DejaVu Sans Mono") if f in have), MONO[0])

    def _build_style(self) -> None:
        p = self.palette
        if not hasattr(self, "ui_font"):
            self._pick_fonts()
        F = self.ui_font
        self.configure(bg=p["bg"])
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=p["bg"], foreground=p["ink"],
                        fieldbackground=p["entry_bg"], bordercolor=p["line_strong"],
                        darkcolor=p["bg"], lightcolor=p["bg"], focuscolor=p["acc"],
                        troughcolor=p["card2"], font=(F, 10))
        style.configure("TFrame", background=p["bg"])
        style.configure("Card.TFrame", background=p["card"])
        style.configure("Rule.TFrame", background=p["line"])
        style.configure("TLabel", background=p["bg"], foreground=p["ink"])
        style.configure("Mut.TLabel", background=p["bg"], foreground=p["mut"])
        style.configure("Kicker.TLabel", background=p["bg"], foreground=p["acc_soft"],
                        font=(F, 8, "bold"))
        style.configure("Header.TLabel", background=p["bg"], foreground=p["ink"],
                        font=(F, 18))
        style.configure("HeaderAccent.TLabel", background=p["bg"], foreground=p["acc"],
                        font=(F, 18))
        style.configure("SubHeader.TLabel", background=p["bg"], foreground=p["subtle"],
                        font=(F, 9))
        # a section: hairline border, small accent kicker for the caption
        style.configure("TLabelframe", background=p["bg"], bordercolor=p["line"],
                        darkcolor=p["line"], lightcolor=p["line"], relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=p["bg"], foreground=p["acc_soft"],
                        font=(F, 8, "bold"))
        style.configure("TCheckbutton", background=p["bg"], foreground=p["mut"], focuscolor=p["bg"])
        style.map("TCheckbutton", background=[("active", p["bg"])],
                  foreground=[("active", p["ink"])],
                  indicatorcolor=[("selected", p["acc"]), ("!selected", p["card2"])],
                  bordercolor=[("focus", p["acc"])])
        style.configure("TEntry", fieldbackground=p["entry_bg"], foreground=p["ink"],
                        insertcolor=p["ink"], bordercolor=p["line_strong"], padding=4)
        style.map("TEntry", bordercolor=[("focus", p["acc"])], lightcolor=[("focus", p["acc"])],
                  darkcolor=[("focus", p["acc"])])
        style.configure("TCombobox", fieldbackground=p["entry_bg"], foreground=p["ink"],
                        background=p["card2"], arrowcolor=p["mut"], bordercolor=p["line_strong"],
                        padding=4)
        style.map("TCombobox", fieldbackground=[("readonly", p["entry_bg"])],
                  foreground=[("readonly", p["ink"])], bordercolor=[("focus", p["acc"])],
                  selectbackground=[("readonly", p["entry_bg"])],
                  selectforeground=[("readonly", p["ink"])])
        self.option_add("*TCombobox*Listbox.background", p["entry_bg"])
        self.option_add("*TCombobox*Listbox.foreground", p["ink"])
        self.option_add("*TCombobox*Listbox.selectBackground", p["select"])
        self.option_add("*TCombobox*Listbox.selectForeground", p["ink"])
        self.option_add("*Menu.background", p["card2"])
        self.option_add("*Menu.foreground", p["ink"])
        self.option_add("*Menu.activeBackground", p["select"])
        self.option_add("*Menu.activeForeground", p["ink"])

        style.configure("TNotebook", background=p["bg"], bordercolor=p["line"], borderwidth=1,
                        lightcolor=p["line"], darkcolor=p["line"], tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=p["bg"], foreground=p["subtle"],
                        padding=(16, 8), font=(F, 10), bordercolor=p["bg"], borderwidth=1,
                        lightcolor=p["bg"], darkcolor=p["bg"])
        style.map("TNotebook.Tab",
                  background=[("selected", p["card"]), ("active", p["bg"])],
                  foreground=[("selected", p["acc"]), ("active", p["ink"])],
                  bordercolor=[("selected", p["line"]), ("!selected", p["bg"])],
                  lightcolor=[("selected", p["card"])], darkcolor=[("selected", p["card"])])

        # Buttons are OUTLINED, as everywhere in VoltinoLabs: text + hairline,
        # amber for the one primary action, red for the dangerous one.
        style.configure("TButton", background=p["bg"], foreground=p["mut"],
                        bordercolor=p["line_strong"], focusthickness=1, focuscolor=p["acc"],
                        padding=(12, 8), font=(F, 10), borderwidth=1, relief="solid",
                        lightcolor=p["bg"], darkcolor=p["bg"])
        style.map("TButton",
                  background=[("disabled", p["bg"]), ("active", p["bg"])],
                  foreground=[("disabled", p["dim"]), ("active", p["ink"])],
                  bordercolor=[("disabled", p["line"]), ("active", p["hover_line"]),
                               ("focus", p["acc"])])
        style.configure("Accent.TButton", background=p["bg"], foreground=p["acc"],
                        bordercolor=p["acc"], padding=(12, 10), font=(F, 10, "bold"),
                        lightcolor=p["bg"], darkcolor=p["bg"])
        style.map("Accent.TButton",
                  background=[("disabled", p["bg"]), ("active", p["acc_tint"])],
                  foreground=[("disabled", _mix(p["acc"], p["bg"], 0.45)), ("active", p["acc_soft"])],
                  bordercolor=[("disabled", _mix(p["acc"], p["bg"], 0.25)), ("active", p["acc_soft"])])
        style.configure("Danger.TButton", background=p["bg"], foreground=p["danger"],
                        bordercolor=p["danger"], padding=(12, 8))
        style.map("Danger.TButton",
                  background=[("disabled", p["bg"]), ("active", p["danger_tint"])],
                  foreground=[("disabled", _mix(p["danger"], p["bg"], 0.4))],
                  bordercolor=[("disabled", _mix(p["danger"], p["bg"], 0.2))])
        style.configure("Small.TButton", padding=(8, 3), font=(F, 8))

        style.configure("TProgressbar", background=p["acc"], troughcolor=p["card2"],
                        bordercolor=p["card2"], lightcolor=p["acc"], darkcolor=p["acc"],
                        thickness=4)
        for orient in ("Vertical", "Horizontal"):
            style.configure(f"{orient}.TScrollbar", background=p["card2"], troughcolor=p["bg"],
                            bordercolor=p["bg"], arrowcolor=p["subtle"], lightcolor=p["card2"],
                            darkcolor=p["card2"])
            style.map(f"{orient}.TScrollbar", background=[("active", p["line_strong"])])

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
        self.log.configure(background=p["card2"], foreground=p["mut"],
                           insertbackground=p["ink"], selectbackground=p["select"],
                           selectforeground=p["ink"])
        self.log.tag_configure("err", foreground=p["crit"])
        self.log.tag_configure("warn", foreground=p["warn"])
        self.log.tag_configure("ok", foreground=p["ok"])
        self.log.tag_configure("head", foreground=p["acc"], font=(self.mono_font, 10, "bold"))
        self.status_dot.configure(bg=p["bg"])
        self.status_dot.itemconfig(self._dot, fill=p["warn"] if self.busy else p["ok"])
        self.theme_btn.configure(text=self._theme_btn_text())
        self.status_var.set(f"Switched to the {THEME_LABEL[name]} theme.")

    # -- layout --------------------------------------------------------------
    def _build_widgets(self) -> None:
        p = self.palette
        header = ttk.Frame(self, padding=(20, 16, 20, 10))
        header.pack(fill="x")
        title_row = ttk.Frame(header)
        title_row.pack(fill="x")
        title_col = ttk.Frame(title_row)
        title_col.pack(side="left", anchor="w")
        ttk.Label(title_col, text="VOLTINOLABS  \u00b7  DAIDALOS", style="Kicker.TLabel").pack(anchor="w")
        title_line = ttk.Frame(title_col)
        title_line.pack(anchor="w")
        ttk.Label(title_line, text="Static Fire ", style="Header.TLabel").pack(side="left")
        ttk.Label(title_line, text="Stand", style="HeaderAccent.TLabel").pack(side="left")
        self.theme_btn = ttk.Button(title_row, text=self._theme_btn_text(),
                                    style="Small.TButton", command=self._toggle_theme)
        self.theme_btn.pack(side="right", anchor="e")
        Tooltip(self.theme_btn, "Switch between the dark theme and a high-contrast "
                                "light theme that stays readable on a laptop screen "
                                "in direct sunlight.")
        ttk.Label(header, text="Extract, analyse and configure - GUI front end for the tools "
                              "in this folder.", style="SubHeader.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Frame(self, height=1, style="Rule.TFrame").pack(fill="x")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=20, pady=(10, 8))

        extract = ttk.Frame(nb, padding=4)
        stand = ttk.Frame(nb, padding=4)
        nb.add(extract, text="  Extract && Analyse  ")
        nb.add(stand, text="  Stand Tools  ")
        self._build_extract_tab(extract)
        self._build_stand_tab(stand)

        # -- log pane, shared by both tabs -----------------------------------
        log_frame = ttk.LabelFrame(self, text="LOG")
        log_frame.pack(fill="both", expand=True, padx=20, pady=(0, 6))

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
            log_frame, height=14, state="disabled", wrap="word",
            font=(self.mono_font, 10),
            background=p["card2"], foreground=p["mut"], insertbackground=p["ink"],
            selectbackground=p["select"], selectforeground=p["ink"],
            relief="flat", borderwidth=0, padx=10, pady=8)
        self.log.pack(fill="both", expand=True, padx=4, pady=4)
        self.log.tag_configure("err", foreground=p["crit"])
        self.log.tag_configure("warn", foreground=p["warn"])
        self.log.tag_configure("ok", foreground=p["ok"])
        self.log.tag_configure("head", foreground=p["acc"], font=(self.mono_font, 10, "bold"))
        self._log_write_welcome()

        status_bar = ttk.Frame(self, padding=(20, 0, 20, 14))
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
