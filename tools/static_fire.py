#!/usr/bin/env python3
"""
Static Fire Stand – stažení, vyhodnocení a export dat.

Typické použití:

    python static_fire.py                      stáhne vše a vyhodnotí
    python static_fire.py --port COM5          konkrétní port
    python static_fire.py --zaloha zaloha.txt  jen stáhne a uloží syrový výpis
    python static_fire.py --ze-souboru z.txt   vyhodnotí dřív stažený výpis
    python static_fire.py --palivo 24.5        známá hmotnost paliva v gramech
    python static_fire.py --smazat             vymaže paměť stendu
    python static_fire.py --tara               vynuluje tenzometr
    python static_fire.py --kalibrace 500      kalibrace závažím 500 g

Výstupy skončí v adresáři --vystup (výchozí: vysledky/RRRRMMDD_HHMMSS/).
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sf_analysis import analyze, summary_rows  # noqa: E402
from sf_protocol import Stand, find_port, load_legacy_csv, parse_dump  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Stažení a analýza dat ze static fire stendu",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--port", help="sériový port (jinak se hledá automaticky)")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--slot", type=int, help="stáhnout jen jeden slot")
    p.add_argument("--ze-souboru", dest="from_file",
                   help="nepřipojovat se, vyhodnotit dřív uložený výpis")
    p.add_argument("--stare-csv", dest="legacy_csv", nargs="+", metavar="CSV",
                   help="vyhodnotit CSV z původního firmwaru V0")
    p.add_argument("--zaloha", dest="backup",
                   help="kam uložit syrový výpis (výchozí: do adresáře s výsledky)")
    p.add_argument("--vystup", dest="outdir", help="adresář pro výsledky")
    p.add_argument("--palivo", dest="prop_mass", type=float,
                   help="hmotnost paliva v gramech; jinak se odhadne z posunu váhy")
    p.add_argument("--sigma", type=float, default=6.0,
                   help="práh detekce zapálení v násobcích šumu (výchozí 6)")
    p.add_argument("--bez-grafu", dest="no_plots", action="store_true")
    p.add_argument("--ukaz", dest="show", action="store_true",
                   help="zobrazit grafy v okně")
    p.add_argument("--smazat", action="store_true", help="vymazat data ve stendu")
    p.add_argument("--tara", action="store_true", help="vynulovat tenzometr")
    p.add_argument("--kalibrace", type=float, metavar="GRAMY",
                   help="kalibrovat známým závažím (v gramech)")
    p.add_argument("--info", action="store_true", help="jen vypsat stav stendu")
    return p


def connect(args) -> Stand:
    port = args.port or find_port()
    if not port:
        print("Nenašel jsem žádný sériový port. Zadej ho ručně přes --port.")
        sys.exit(2)
    print(f"Připojuji se k {port} ({args.baud} Bd)…")
    try:
        return Stand(port, args.baud)
    except Exception as exc:
        print(f"Nepodařilo se otevřít port: {exc}")
        sys.exit(2)


def print_report(burn, r) -> None:
    print()
    print("=" * 62)
    print(f"  ZÁŽEH #{r.burn_id}   ·   slot {r.slot}   ·   {r.motor_designation}")
    print("=" * 62)
    for key, val in summary_rows(burn, r):
        if not key:
            print()
        elif key.startswith("—"):
            print(f"  {key}")
        else:
            print(f"    {key:.<40} {val:>18}")

    notes = list(r.warnings) + burn.quality.problems()
    if notes:
        print("\n  UPOZORNĚNÍ:")
        for note in notes:
            print(f"    ! {note}")


def main() -> int:
    args = build_parser().parse_args()

    # --- akce, které nepotřebují analýzu -------------------------------
    if args.smazat or args.tara or args.kalibrace is not None or args.info:
        with connect(args) as stand:
            if args.info:
                info = stand.info()
                if not info:
                    print("Stend neodpověděl na dotaz na stav.")
                    return 1
                for k, v in info.items():
                    print(f"  {k:.<26} {v}")
            if args.tara:
                stand.tare()
                print("Tenzometr vynulován.")
            if args.kalibrace is not None:
                stand.calibrate_grams(args.kalibrace)
                print(f"Kalibrace závažím {args.kalibrace} g odeslána.")
            if args.smazat:
                confirm = input("Opravdu vymazat VŠECHNA data ve stendu? [ano/ne] ")
                if confirm.strip().lower() in ("ano", "a", "yes", "y"):
                    stand.erase()
                    print("Paměť stendu vymazána.")
                else:
                    print("Zrušeno.")
        return 0

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = Path(args.outdir) if args.outdir else Path("vysledky") / stamp
    outdir.mkdir(parents=True, exist_ok=True)

    # --- získání dat ----------------------------------------------------
    if args.legacy_csv:
        burns = []
        for i, path in enumerate(args.legacy_csv):
            try:
                burns.append(load_legacy_csv(path, burn_id=0))
            except Exception as exc:
                print(f"  {path}: {exc}")
        if not burns:
            return 1
        for i, b in enumerate(burns):
            if b.burn_id == 0:
                b.burn_id = i + 1
        print(f"Načteno starých záznamů: {len(burns)}")
        return run_analysis(burns, args, outdir)

    if args.from_file:
        raw = Path(args.from_file).read_text(encoding="utf-8", errors="replace")
        print(f"Načteno ze souboru {args.from_file} ({len(raw)} znaků).")
    else:
        with connect(args) as stand:
            info = stand.info()
            if info:
                print(f"  firmware {info.get('fw')}, "
                      f"zážehů v paměti {info.get('burns')}, "
                      f"restartů {info.get('boots')}, "
                      f"navázání po výpadku {info.get('resumes')}")
            print("Stahuji data…")
            raw = stand.download(args.slot)
        backup = Path(args.backup) if args.backup else outdir / f"vypis_{stamp}.txt"
        backup.write_text(raw, encoding="utf-8")
        print(f"Syrový výpis uložen: {backup}")

    if "#BEGIN SFDUMP" not in raw:
        print("Ve výpisu nejsou žádná data ve formátu SFDUMP.")
        print("Zkontroluj, že ve stendu běží firmware 2.x (příkaz 'i' na sériové lince).")
        return 1

    info, burns = parse_dump(raw)
    if not burns:
        print("Výpis je platný, ale neobsahuje žádný zážeh.")
        return 1
    print(f"Nalezeno zážehů: {len(burns)}")
    return run_analysis(burns, args, outdir)


def run_analysis(burns, args, outdir: Path) -> int:
    from sf_report import (export_csv_cz, export_overview_csv, export_summary_csv_cz,
                           export_xlsx, plot_burn, plot_comparison)

    pairs = []
    for burn in burns:
        r = analyze(burn, prop_mass_g=args.prop_mass, detect_sigma=args.sigma)
        pairs.append((burn, r))
        print_report(burn, r)

        rows = summary_rows(burn, r)
        made = [export_csv_cz(burn, r, outdir),
                export_summary_csv_cz(rows, r, outdir)]
        xlsx = export_xlsx(burn, r, outdir)
        if xlsx:
            made.append(xlsx)
        else:
            print("    (openpyxl není nainstalované – .xlsx přeskočeno, "
                  "nainstaluj: pip install openpyxl)")
        if not args.no_plots:
            made.append(plot_burn(burn, r, outdir, show=args.show))
        print("\n  Uloženo:")
        for path in made:
            print(f"    {path}")

    if len(pairs) > 1:
        print(f"\n  Přehled: {export_overview_csv(pairs, outdir)}")
        if not args.no_plots:
            cmp_path = plot_comparison(pairs, outdir)
            if cmp_path:
                print(f"  Porovnání: {cmp_path}")

    print(f"\nHotovo. Všechny výstupy jsou v: {outdir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
