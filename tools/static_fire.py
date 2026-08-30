#!/usr/bin/env python3
"""
Static Fire Stand - download, analyse and export burn data.

TWO WAYS TO RUN THIS
====================

1) From Python IDLE (the easy way)
   Open this file in IDLE and press F5. With no arguments the script
   shows a menu and asks for everything it needs. Nothing to memorise.

2) From a command line, for repeat work:

   python static_fire.py                     download everything, analyse
   python static_fire.py --port COM5         pick the serial port
   python static_fire.py --fuel 42.5         known propellant mass, grams
   python static_fire.py --from-file dump.txt   re-analyse a saved dump
   python static_fire.py --legacy-csv old.csv   old V0 firmware CSV
   python static_fire.py --info              show stand status
   python static_fire.py --tare              zero the load cell
   python static_fire.py --calibrate 500     calibrate with a 500 g weight
   python static_fire.py --cal-factor 2291.9275  set the counts/N factor directly
   python static_fire.py --ignition 300      set the igniter on-time, in ms
   python static_fire.py --erase             wipe the stand's memory

Results go to results/YYYYMMDD_HHMMSS/ next to this script, unless
--output says otherwise.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from sf_analysis import analyze, summary_rows  # noqa: E402
from sf_protocol import (DEFAULT_CAL_COUNTS_PER_N, DEFAULT_IGNITION_MS,  # noqa: E402
                         Stand, find_port, list_ports, load_legacy_csv, parse_dump)


# =====================================================================
#  Shared helpers
# =====================================================================
def new_output_dir(explicit: str | None = None) -> Path:
    """Timestamped output folder, always next to the script.

    IDLE's working directory is wherever IDLE was started, which is
    rarely where you want the results, so the script's own folder is the
    anchor instead of the current directory.
    """
    if explicit:
        out = Path(explicit).expanduser()
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out = SCRIPT_DIR.parent / "results" / stamp
    out.mkdir(parents=True, exist_ok=True)
    return out


def open_folder(path: Path) -> None:
    """Open the results folder in the system file manager, best effort."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        pass


def print_report(burn, r) -> None:
    print()
    print("=" * 64)
    print(f"  BURN #{r.burn_id}   .   slot {r.slot}   .   {r.motor_designation}")
    print("=" * 64)
    for key, val in summary_rows(burn, r):
        if not key:
            print()
        elif key.startswith("-") and key.endswith("-"):
            print(f"  {key}")
        else:
            print(f"    {key:.<40} {val:>20}")

    notes = list(r.warnings) + burn.quality.problems()
    if notes:
        print("\n  WARNINGS:")
        for note in notes:
            print(f"    ! {note}")


def burn_subdir(outdir: Path, burn_id: int) -> Path:
    """A fresh, collision-safe subfolder for one burn's files.

    burn_001, burn_002, ... - and if that name is already taken (analysing
    into an output folder that already has a burn with this id, e.g. a
    re-download or a fixed --output folder used more than once) it falls
    back to burn_001_1, then burn_001_2, and so on rather than overwriting
    someone else's files.
    """
    base = f"burn_{burn_id:03d}"
    candidate = outdir / base
    n = 0
    while candidate.exists():
        n += 1
        candidate = outdir / f"{base}_{n}"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def run_analysis(burns, outdir: Path, fuel_g: float | None = None,
                 sigma: float = 6.0, make_plots: bool = True,
                 show: bool = False) -> list:
    from sf_report import (export_csv, export_overview_csv, export_summary_csv,
                           export_xlsx, plot_burn, plot_comparison)

    pairs = []
    for burn in burns:
        r = analyze(burn, prop_mass_g=fuel_g, detect_sigma=sigma)
        pairs.append((burn, r))
        print_report(burn, r)

        burn_dir = burn_subdir(outdir, r.burn_id)
        made = [export_csv(burn, r, burn_dir),
                export_summary_csv(summary_rows(burn, r), r, burn_dir)]
        xlsx = export_xlsx(burn, r, burn_dir)
        if xlsx:
            made.append(xlsx)
        else:
            print("    (openpyxl is missing, .xlsx skipped - "
                  "install it with: pip install openpyxl)")
        if make_plots:
            made.append(plot_burn(burn, r, burn_dir, show=show))
        print(f"\n  Written to {burn_dir.relative_to(outdir)}/:")
        for path in made:
            print(f"    {path.name}")

    if len(pairs) > 1:
        print(f"\n  Overview:   {export_overview_csv(pairs, outdir).name}")
        if make_plots:
            cmp_path = plot_comparison(pairs, outdir)
            if cmp_path:
                print(f"  Comparison: {cmp_path.name}")

    print(f"\nDone. Everything is in:\n  {outdir}")
    return pairs


def download(port: str | None, baud: int, slot: int | None,
             outdir: Path) -> str | None:
    """Connect, pull the dump, save it to disk before parsing anything."""
    port = port or find_port()
    if not port:
        print("No serial port found. Is the stand plugged in?")
        return None
    print(f"Connecting to {port} at {baud} baud...")
    try:
        stand = Stand(port, baud)
    except Exception as exc:
        print(f"Could not open the port: {exc}")
        return None

    with stand:
        info = stand.info()
        if info:
            print(f"  firmware {info.get('fw')}, "
                  f"{info.get('burns')} burns stored, "
                  f"{info.get('boots')} boots, "
                  f"{info.get('resumes')} resumes after a power loss")
            if not info.get("log_ok", 1):
                print(f"  WARNING: flash logging is disabled ({info.get('log_err')})")
        print("Downloading...")
        raw = stand.download(slot)

    backup = outdir / "raw_dump.txt"
    backup.write_text(raw, encoding="utf-8")
    print(f"Raw dump saved: {backup.name}")
    return raw


# =====================================================================
#  Interactive mode - this is what IDLE users get
# =====================================================================
def ask(prompt: str, default: str = "") -> str:
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return answer or default


def ask_float(prompt: str, default: float | None = None) -> float | None:
    while True:
        raw = ask(prompt)
        if not raw:
            return default
        try:
            return float(raw.replace(",", "."))
        except ValueError:
            print("  That is not a number, try again (or press Enter to skip).")


def ask_yes(prompt: str, default: bool = False) -> bool:
    raw = ask(prompt).lower()
    if not raw:
        return default
    return raw[0] in ("y", "a")   # yes / ano


def choose_port() -> str | None:
    ports = list_ports()
    if not ports:
        print("  No serial ports found. Check the USB cable.")
        manual = ask("  Type a port name anyway (or Enter to cancel): ")
        return manual or None
    if len(ports) == 1:
        print(f"  Using the only port available: {ports[0][0]}  ({ports[0][1]})")
        return ports[0][0]
    print("  Available ports:")
    for i, (dev, label) in enumerate(ports, start=1):
        print(f"    {i}) {dev:<14} {label}")
    raw = ask(f"  Which one? [1-{len(ports)}, Enter = 1]: ", "1")
    try:
        return ports[int(raw) - 1][0]
    except (ValueError, IndexError):
        return ports[0][0]


def choose_file(title: str, patterns: list[tuple[str, str]]) -> str | None:
    """A file picker, falling back to a typed path if tkinter is absent."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.update()
        path = filedialog.askopenfilename(title=title, filetypes=patterns)
        root.destroy()
        if path:
            return path
    except Exception:
        pass
    typed = ask("  Full path to the file (Enter to cancel): ").strip('"').strip()
    return typed or None


def ask_fuel() -> float | None:
    print("\n  Propellant mass lets the script compute Isp properly.")
    print("  Leave it empty and it will be estimated from how much the")
    print("  load cell reading drops across the burn - which only works")
    print("  if the stand settles back to rest afterwards.")
    return ask_float("  Propellant mass in grams [Enter = estimate]: ")


def menu_download() -> None:
    port = choose_port()
    if not port:
        return
    fuel = ask_fuel()
    outdir = new_output_dir()
    raw = download(port, 115200, None, outdir)
    if not raw:
        return
    if "#BEGIN SFDUMP" not in raw:
        print("\nThe stand did not send a valid dump.")
        print("Check that it is running firmware 2.x (send 'i' over serial).")
        return
    _info, burns = parse_dump(raw)
    if not burns:
        print("\nThe dump is valid but contains no burns yet.")
        return
    print(f"\nBurns found: {len(burns)}")
    run_analysis(burns, outdir, fuel_g=fuel)
    if ask_yes("\nOpen the results folder? [y/N]: "):
        open_folder(outdir)


def menu_from_file() -> None:
    path = choose_file("Choose a saved raw dump",
                       [("Dump files", "*.txt"), ("All files", "*.*")])
    if not path:
        return
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    if "#BEGIN SFDUMP" not in raw:
        print("  That file does not contain an SFDUMP block.")
        return
    fuel = ask_fuel()
    _info, burns = parse_dump(raw)
    if not burns:
        print("  No burns in that file.")
        return
    outdir = new_output_dir()
    print(f"\nBurns found: {len(burns)}")
    run_analysis(burns, outdir, fuel_g=fuel)
    if ask_yes("\nOpen the results folder? [y/N]: "):
        open_folder(outdir)


def menu_legacy() -> None:
    path = choose_file("Choose a CSV from the old V0 firmware",
                       [("CSV files", "*.csv"), ("All files", "*.*")])
    if not path:
        return
    try:
        burn = load_legacy_csv(path)
    except Exception as exc:
        print(f"  Could not read that file: {exc}")
        return
    fuel = ask_fuel()
    outdir = new_output_dir()
    run_analysis([burn], outdir, fuel_g=fuel)
    if ask_yes("\nOpen the results folder? [y/N]: "):
        open_folder(outdir)


def menu_status() -> None:
    port = choose_port()
    if not port:
        return
    try:
        with Stand(port) as stand:
            info = stand.info()
            if not info:
                print("  The stand did not answer. Wrong port, or old firmware?")
                return
            print()
            for key, val in info.items():
                print(f"    {key:.<26} {val}")
            print("\n  Slot listing:")
            stand.command("l")
            print("   ", stand.drain(2.0).replace("\n", "\n    ").rstrip())
    except Exception as exc:
        print(f"  Connection failed: {exc}")


def menu_tare() -> None:
    port = choose_port()
    if not port:
        return
    print("\n  Take everything off the stand except the empty motor mount.")
    if not ask_yes("  Ready to zero it? [y/N]: "):
        return
    try:
        with Stand(port) as stand:
            stand.tare()
            print("   ", stand.drain(2.0).strip())
    except Exception as exc:
        print(f"  Connection failed: {exc}")


def menu_calibrate() -> None:
    port = choose_port()
    if not port:
        return
    print("\n  Calibration procedure:")
    print("    1. Zero the stand first (menu option 5).")
    print("    2. Hang or place a known weight where the motor pushes.")
    print("    3. Enter that weight below.")
    print("  Use something heavy enough to matter - a weight near the")
    print("  thrust you expect gives a far better factor than 100 g does.")
    grams = ask_float("\n  Known weight in grams: ")
    if not grams or grams <= 0:
        print("  Cancelled.")
        return
    if not ask_yes(f"  Is the {grams:.0f} g weight in place right now? [y/N]: "):
        print("  Cancelled.")
        return
    try:
        with Stand(port) as stand:
            stand.calibrate_grams(grams)
            print("   ", stand.drain(3.0).strip())
    except Exception as exc:
        print(f"  Connection failed: {exc}")


def menu_calibrate_manual() -> None:
    port = choose_port()
    if not port:
        return
    print(f"\n  Type the calibration factor directly, in raw counts per")
    print(f"  newton, instead of doing a weight calibration. The firmware")
    print(f"  default is {DEFAULT_CAL_COUNTS_PER_N:g} counts/N.")
    factor = ask_float(f"\n  Counts per newton [Enter = {DEFAULT_CAL_COUNTS_PER_N:g}]: ",
                       DEFAULT_CAL_COUNTS_PER_N)
    if not factor or factor == 0:
        print("  Cancelled.")
        return
    try:
        with Stand(port) as stand:
            stand.calibrate_factor(factor)
            print("   ", stand.drain(2.0).strip())
    except Exception as exc:
        print(f"  Connection failed: {exc}")


def menu_ignition() -> None:
    port = choose_port()
    if not port:
        return
    print(f"\n  How long the pyro channel stays energised. The firmware")
    print(f"  default is {DEFAULT_IGNITION_MS} ms; the hard ceiling is 5000 ms")
    print(f"  regardless of what is set here.")
    ms = ask_float(f"\n  Igniter on-time in ms [Enter = {DEFAULT_IGNITION_MS}]: ",
                   DEFAULT_IGNITION_MS)
    if not ms or ms <= 0:
        print("  Cancelled.")
        return
    try:
        with Stand(port) as stand:
            stand.set_ignition_ms(int(ms))
            print("   ", stand.drain(2.0).strip())
    except Exception as exc:
        print(f"  Connection failed: {exc}")


def menu_erase() -> None:
    port = choose_port()
    if not port:
        return
    print("\n  This deletes EVERY burn stored on the stand.")
    print("  Download and check your data first - this cannot be undone.")
    if ask("  Type ERASE to confirm: ") != "ERASE":
        print("  Cancelled.")
        return
    try:
        with Stand(port) as stand:
            stand.erase()
            print("  Stand memory erased.")
    except Exception as exc:
        print(f"  Connection failed: {exc}")


def menu_demo() -> None:
    """Generate and analyse fake data, so the tooling can be tried dry."""
    try:
        import make_test_dump
    except ImportError:
        print("  make_test_dump.py is missing from this folder.")
        return
    outdir = new_output_dir()
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
    run_analysis(burns, outdir, fuel_g=None)
    if ask_yes("\nOpen the results folder? [y/N]: "):
        open_folder(outdir)


MENU = [
    ("Download data from the stand and analyse it", menu_download),
    ("Analyse a raw dump saved earlier", menu_from_file),
    ("Analyse a CSV from the old V0 firmware", menu_legacy),
    ("Show stand status", menu_status),
    ("Zero (tare) the load cell", menu_tare),
    ("Calibrate with a known weight", menu_calibrate),
    ("Calibrate: enter the counts/N factor directly", menu_calibrate_manual),
    ("Set the igniter on-time", menu_ignition),
    ("Erase all data on the stand", menu_erase),
    ("Demo: generate fake data and analyse it (no hardware)", menu_demo),
]


def interactive() -> int:
    print()
    print("=" * 64)
    print("  STATIC FIRE STAND")
    print("=" * 64)
    while True:
        print()
        for i, (label, _) in enumerate(MENU, start=1):
            print(f"  {i}) {label}")
        print("  0) Quit")
        choice = ask("\n  Choice: ")
        if choice in ("0", "q", ""):
            print("  Bye.")
            return 0
        try:
            _, handler = MENU[int(choice) - 1]
        except (ValueError, IndexError):
            print("  Pick a number from the list.")
            continue
        try:
            handler()
        except KeyboardInterrupt:
            print("\n  Interrupted.")
        except Exception as exc:                      # noqa: BLE001
            print(f"\n  Something went wrong: {exc}")
            print("  The raw dump, if one was downloaded, is still on disk.")


# =====================================================================
#  Command line mode
# =====================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download and analyse data from the static fire stand. "
                    "Run with no arguments for an interactive menu.")
    p.add_argument("--port", help="serial port (auto-detected if omitted)")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--slot", type=int, help="download a single slot only")
    p.add_argument("--from-file", dest="from_file",
                   help="do not connect, analyse a previously saved dump")
    p.add_argument("--legacy-csv", dest="legacy_csv", nargs="+", metavar="CSV",
                   help="analyse CSV files from the original V0 firmware")
    p.add_argument("--output", dest="outdir", help="output folder")
    p.add_argument("--fuel", type=float, metavar="GRAMS",
                   help="propellant mass; otherwise estimated from the load cell")
    p.add_argument("--sigma", type=float, default=6.0,
                   help="ignition detection threshold in noise sigmas (default 6)")
    p.add_argument("--no-plots", dest="no_plots", action="store_true")
    p.add_argument("--show", action="store_true", help="display charts in a window")
    p.add_argument("--erase", action="store_true", help="erase all data on the stand")
    p.add_argument("--tare", action="store_true", help="zero the load cell")
    p.add_argument("--calibrate", type=float, metavar="GRAMS",
                   help="calibrate with a known weight, in grams")
    p.add_argument("--cal-factor", dest="cal_factor", type=float, metavar="COUNTS_PER_N",
                   help=f"set the calibration factor directly, in raw counts per "
                       f"newton (firmware default {DEFAULT_CAL_COUNTS_PER_N:g})")
    p.add_argument("--ignition", type=int, metavar="MS",
                   help=f"set the igniter on-time in milliseconds "
                       f"(firmware default {DEFAULT_IGNITION_MS})")
    p.add_argument("--info", action="store_true", help="print stand status and exit")
    return p


def cli(args) -> int:
    # --- actions that never touch the analysis path --------------------
    if (args.erase or args.tare or args.calibrate is not None or args.cal_factor is not None
            or args.ignition is not None or args.info):
        port = args.port or find_port()
        if not port:
            print("No serial port found.")
            return 2
        try:
            stand = Stand(port, args.baud)
        except Exception as exc:
            print(f"Could not open the port: {exc}")
            return 2
        with stand:
            if args.info:
                info = stand.info()
                if not info:
                    print("The stand did not answer.")
                    return 1
                for key, val in info.items():
                    print(f"  {key:.<26} {val}")
            if args.tare:
                stand.tare()
                print("Load cell zeroed.")
            if args.calibrate is not None:
                stand.calibrate_grams(args.calibrate)
                print(stand.drain(2.0).strip())
            if args.cal_factor is not None:
                stand.calibrate_factor(args.cal_factor)
                print(stand.drain(2.0).strip())
            if args.ignition is not None:
                stand.set_ignition_ms(args.ignition)
                print(stand.drain(2.0).strip())
            if args.erase:
                if input("Erase ALL data on the stand? Type ERASE: ").strip() == "ERASE":
                    stand.erase()
                    print("Stand memory erased.")
                else:
                    print("Cancelled.")
        return 0

    outdir = new_output_dir(args.outdir)

    if args.legacy_csv:
        burns = []
        for path in args.legacy_csv:
            try:
                burns.append(load_legacy_csv(path))
            except Exception as exc:
                print(f"  {path}: {exc}")
        if not burns:
            return 1
        for i, b in enumerate(burns):
            if b.burn_id == 0:
                b.burn_id = i + 1
        print(f"Legacy records loaded: {len(burns)}")
        run_analysis(burns, outdir, args.fuel, args.sigma,
                     not args.no_plots, args.show)
        return 0

    if args.from_file:
        raw = Path(args.from_file).read_text(encoding="utf-8", errors="replace")
        print(f"Loaded {args.from_file} ({len(raw)} characters).")
    else:
        raw = download(args.port, args.baud, args.slot, outdir)
        if raw is None:
            return 2

    if "#BEGIN SFDUMP" not in raw:
        print("No SFDUMP data in that input.")
        print("Check that the stand runs firmware 2.x (send 'i' over serial).")
        return 1

    _info, burns = parse_dump(raw)
    if not burns:
        print("The dump is valid but contains no burns.")
        return 1
    print(f"Burns found: {len(burns)}")
    run_analysis(burns, outdir, args.fuel, args.sigma, not args.no_plots, args.show)
    return 0


def main() -> int:
    # No arguments means someone pressed F5 in IDLE or double-clicked the
    # file, so show the menu rather than a usage message.
    if len(sys.argv) <= 1:
        return interactive()
    return cli(build_parser().parse_args())


if __name__ == "__main__":
    sys.exit(main())
