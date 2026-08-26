#!/usr/bin/env python3
"""
Static Fire Stand - graphical front end.

A plain tkinter window over the same functions static_fire.py uses from
the command line: download from the stand, analyse a saved dump, import a
legacy CSV, or run the no-hardware demo. Everything the console script
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
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from sf_protocol import Stand, find_port, list_ports, load_legacy_csv, parse_dump  # noqa: E402
from static_fire import download, new_output_dir, open_folder, run_analysis  # noqa: E402


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


class App(tk.Tk):
    POLL_MS = 80

    def __init__(self) -> None:
        super().__init__()
        self.title("Static Fire Stand")
        self.geometry("880x640")
        self.minsize(680, 480)

        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.busy = False
        self.last_outdir: Path | None = None

        self._build_widgets()
        self._refresh_ports()
        self.after(self.POLL_MS, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- layout ----------------------------------------------------------
    def _build_widgets(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        extract = ttk.Frame(nb)
        stand = ttk.Frame(nb)
        nb.add(extract, text="Extract && Analyse")
        nb.add(stand, text="Stand Tools")
        self._build_extract_tab(extract)
        self._build_stand_tab(stand)

        # -- log pane, shared by both tabs -------------------------------
        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log = scrolledtext.ScrolledText(log_frame, height=14, state="disabled",
                                             font=("Consolas", 9) if sys.platform.startswith("win")
                                             else ("Monospace", 9))
        self.log.pack(fill="both", expand=True, padx=4, pady=4)

        status_bar = ttk.Frame(self)
        status_bar.pack(fill="x", padx=8, pady=(0, 8))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(status_bar, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(status_bar, mode="indeterminate", length=140)
        self.progress.pack(side="right")
        self.open_btn = ttk.Button(status_bar, text="Open results folder",
                                   command=self._open_results, state="disabled")
        self.open_btn.pack(side="right", padx=(0, 10))

    def _build_extract_tab(self, parent: ttk.Frame) -> None:
        pad = dict(padx=6, pady=5)

        port_frame = ttk.LabelFrame(parent, text="Serial port (only needed to download)")
        port_frame.pack(fill="x", **pad)
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(port_frame, textvariable=self.port_var, width=42,
                                       state="readonly")
        self.port_combo.pack(side="left", padx=6, pady=6)
        ttk.Button(port_frame, text="Refresh", command=self._refresh_ports).pack(
            side="left", padx=6)

        opt_frame = ttk.LabelFrame(parent, text="Options")
        opt_frame.pack(fill="x", **pad)
        ttk.Label(opt_frame, text="Propellant mass [g] (blank = estimate from load cell):").grid(
            row=0, column=0, sticky="w", padx=6, pady=4)
        self.fuel_var = tk.StringVar()
        ttk.Entry(opt_frame, textvariable=self.fuel_var, width=12).grid(
            row=0, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(opt_frame, text="Ignition threshold [σ of noise]:").grid(
            row=1, column=0, sticky="w", padx=6, pady=4)
        self.sigma_var = tk.StringVar(value="6")
        ttk.Entry(opt_frame, textvariable=self.sigma_var, width=12).grid(
            row=1, column=1, sticky="w", padx=6, pady=4)

        self.plots_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_frame, text="Generate charts (PNG)",
                        variable=self.plots_var).grid(
            row=0, column=2, sticky="w", padx=16, pady=4)

        ttk.Label(opt_frame, text="Output folder (blank = results/<timestamp>):").grid(
            row=2, column=0, sticky="w", padx=6, pady=4)
        self.outdir_var = tk.StringVar()
        ttk.Entry(opt_frame, textvariable=self.outdir_var, width=46).grid(
            row=2, column=1, columnspan=2, sticky="we", padx=6, pady=4)
        ttk.Button(opt_frame, text="Browse...", command=self._pick_outdir).grid(
            row=2, column=3, padx=6, pady=4)
        opt_frame.columnconfigure(1, weight=1)

        actions = ttk.LabelFrame(parent, text="Actions")
        actions.pack(fill="x", **pad)
        self.action_buttons = []
        for text, cmd in (
            ("Download from stand && analyse", self._act_download),
            ("Analyse a saved raw dump...", self._act_from_file),
            ("Import a legacy V0 CSV...", self._act_legacy_csv),
            ("Run demo (no hardware needed)", self._act_demo),
        ):
            b = ttk.Button(actions, text=text, command=cmd)
            b.pack(fill="x", padx=8, pady=4)
            self.action_buttons.append(b)

    def _build_stand_tab(self, parent: ttk.Frame) -> None:
        pad = dict(padx=6, pady=5)
        info = ttk.Label(
            parent,
            text="These talk to the stand directly over serial. They do not "
                 "touch the Extract tab's output folder.",
            wraplength=760, justify="left")
        info.pack(fill="x", **pad)

        actions = ttk.LabelFrame(parent, text="Actions")
        actions.pack(fill="x", **pad)
        self.stand_buttons = []
        for text, cmd in (
            ("Show stand status", self._act_status),
            ("Zero (tare) the load cell", self._act_tare),
            ("Calibrate with a known weight...", self._act_calibrate),
        ):
            b = ttk.Button(actions, text=text, command=cmd)
            b.pack(fill="x", padx=8, pady=4)
            self.stand_buttons.append(b)

        danger = ttk.LabelFrame(parent, text="Danger zone")
        danger.pack(fill="x", **pad)
        b = ttk.Button(danger, text="Erase ALL data on the stand...", command=self._act_erase)
        b.pack(fill="x", padx=8, pady=4)
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
            messagebox.showerror("Static Fire Stand", "Propellant mass must be a number.")
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

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno(
                "Static Fire Stand",
                "A task is still running. Closing now will not stop it cleanly. Close anyway?"):
                return
        self.destroy()

    # -- background task runner --------------------------------------------
    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        for b in self.action_buttons + self.stand_buttons:
            b.configure(state="disabled" if busy else "normal")
        if busy:
            self.progress.start(12)
            self.status_var.set(message or "Working...")
        else:
            self.progress.stop()
            self.status_var.set(message or "Ready.")

    def _run_task(self, label: str, fn, *args, on_done=None) -> None:
        if self.busy:
            messagebox.showinfo("Static Fire Stand", "Another task is already running.")
            return
        self._set_busy(True, label)

        def worker() -> None:
            old_stdout = sys.stdout
            sys.stdout = QueueWriter(self.log_q)
            error: Exception | None = None
            result = None
            try:
                result = fn(*args)
            except Exception as exc:  # noqa: BLE001
                error = exc
                print(f"\nERROR: {exc}")
            finally:
                sys.stdout = old_stdout
            self.after(0, lambda: self._task_finished(label, error, result, on_done))

        threading.Thread(target=worker, daemon=True).start()

    def _task_finished(self, label: str, error: Exception | None, result, on_done) -> None:
        self._set_busy(False, "Done." if error is None else f"{label} failed - see log.")
        if error is not None:
            messagebox.showerror("Static Fire Stand", str(error))
        elif on_done is not None:
            on_done(result)

    def _drain_log(self) -> None:
        try:
            while True:
                text = self.log_q.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", text)
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(self.POLL_MS, self._drain_log)

    # -- Extract && Analyse actions -----------------------------------------
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
        if messagebox.askyesno("Static Fire Stand",
                               f"Done. Results are in:\n{outdir}\n\nOpen the folder now?"):
            open_folder(outdir)

    # -- Stand Tools actions -------------------------------------------------
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
            "Static Fire Stand",
            "Take everything off the stand except the empty motor mount. Ready to zero it?"):
            return

        def job():
            with self._connect() as stand:
                stand.tare()
                print(stand.drain(2.0).strip())

        self._run_task("Zeroing the load cell", job)

    def _act_calibrate(self) -> None:
        grams = self._ask_number(
            "Calibrate",
            "Zero the stand first. Then place a known weight where the motor "
            "pushes and enter its mass in grams:")
        if grams is None or grams <= 0:
            return
        if not messagebox.askyesno(
            "Static Fire Stand", f"Is the {grams:.0f} g weight in place right now?"):
            return

        def job():
            with self._connect() as stand:
                stand.calibrate_grams(grams)
                print(stand.drain(3.0).strip())

        self._run_task("Calibrating", job)

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
    def _ask_number(title: str, prompt: str) -> float | None:
        import tkinter.simpledialog as sd
        raw = sd.askstring(title, prompt)
        if raw is None:
            return None
        try:
            return float(raw.strip().replace(",", "."))
        except ValueError:
            messagebox.showerror("Static Fire Stand", "That is not a number.")
            return None

    @staticmethod
    def _ask_text(title: str, prompt: str) -> str | None:
        import tkinter.simpledialog as sd
        return sd.askstring(title, prompt)


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
