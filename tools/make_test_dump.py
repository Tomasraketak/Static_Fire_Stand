#!/usr/bin/env python3
"""
Generátor syntetického výpisu pro testování analýzy bez připojeného stendu.

Vyrobí přesně ten formát, který posílá firmware (příkaz 'p'), včetně CRC,
takže se dá otestovat celý řetězec:

    python make_test_dump.py test.txt
    python static_fire.py --ze-souboru test.txt

Volitelně umí simulovat výpadek napájení uprostřed zážehu (--vypadek),
tedy chybějící závěrečnou stránku a díru v časové ose.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sf_protocol import (MAGIC_DATA, MAGIC_FOOTER, MAGIC_HEADER, PAGE_SIZE,  # noqa: E402
                         SAMPLES_PER_PAGE, SF_CONT, SF_PYRO)

CAL = 250.0        # counts na newton
TARE = -123456     # klidový offset ADC


def _finish(page: bytearray, crc_off: int) -> bytes:
    struct.pack_into("<I", page, crc_off, 0)
    crc = binascii.crc32(bytes(page)) & 0xFFFFFFFF
    struct.pack_into("<I", page, crc_off, crc)
    return bytes(page)


def header_page(burn_id: int) -> bytes:
    p = bytearray(PAGE_SIZE)
    struct.pack_into("<5Ifi5I4I", p, 0,
                     MAGIC_HEADER, burn_id, 0, 7, (2 << 16),
                     CAL, TARE,
                     3000, 20000, 3000, 15000, 0,
                     0, 0, 0, 0)
    return _finish(p, 8)


def data_page(burn_id: int, idx: int, samples, flags_page: int = 0) -> bytes:
    p = bytearray(PAGE_SIZE)
    struct.pack_into("<IIHHII", p, 0, MAGIC_DATA, burn_id, idx, len(samples),
                     flags_page, 0)
    for i, (t_us, raw, fl) in enumerate(samples):
        packed = (int(raw) & 0x00FFFFFF) | (int(fl) << 24)
        struct.pack_into("<iI", p, 20 + i * 8, int(t_us), packed)
    return _finish(p, 16)


def footer_page(burn_id: int, n_pages: int, n_samples: int, t_last: int,
                pyro_off_us: int) -> bytes:
    p = bytearray(PAGE_SIZE)
    struct.pack_into("<5Ii4I6I", p, 0,
                     MAGIC_FOOTER, burn_id, 0, n_pages, n_samples,
                     t_last, 0, pyro_off_us, 0, 1,
                     0, 0, 0, 0, 0, 0)
    return _finish(p, 8)


def thrust_curve(t: np.ndarray, delay: float, peak: float, burn: float) -> np.ndarray:
    """Progresivní křivka s ostrým náběhem a exponenciálním doběhem."""
    y = np.zeros_like(t)
    m = t >= delay
    x = (t[m] - delay) / burn
    core = np.where(x <= 1.0,
                    peak * (1 - np.exp(-x / 0.035)) * (0.55 + 0.45 * np.cos(np.pi * x) ** 2)
                    * np.exp(-1.4 * x),
                    0.0)
    tail = np.where(x > 1.0, peak * 0.10 * np.exp(-(x - 1.0) / 0.05), 0.0)
    y[m] = core + tail
    return y


def build(burn_id: int, delay: float, peak: float, burn: float,
          prop_g: float, rate: float, cut_at: float | None) -> list[bytes]:
    preroll, record = 3.0, 20.0
    n = int((preroll + record) * rate)
    t = np.linspace(-preroll, record, n)
    t += np.random.normal(0, 0.0004, n)          # jitter vzorkování
    t = np.sort(t)

    thrust = thrust_curve(t, delay, peak, burn)

    # úbytek hmoty: klidová hodnota klesá úměrně spálenému impulsu
    cum = np.cumsum(np.maximum(thrust, 0))
    frac = cum / cum[-1] if cum[-1] > 0 else np.zeros_like(cum)
    baseline = 12.0 - (prop_g / 1000.0 * 9.80665) * frac

    signal = baseline + thrust + np.random.normal(0, 0.035, n)
    raw = (signal * CAL + TARE).astype(np.int64)

    pyro = (t >= 0) & (t < 3.0)
    flags = np.where(pyro, SF_PYRO | SF_CONT, SF_CONT).astype(np.int64)
    flags[t >= 3.0] = 0                            # palník po zážehu přerušený

    if cut_at is not None:
        keep = t < cut_at
        t, raw, flags = t[keep], raw[keep], flags[keep]

    pages = [header_page(burn_id)]
    idx, total = 1, 0
    t_us = (t * 1e6).astype(np.int64)
    for start in range(0, len(t_us), SAMPLES_PER_PAGE):
        chunk = list(zip(t_us[start:start + SAMPLES_PER_PAGE],
                         raw[start:start + SAMPLES_PER_PAGE],
                         flags[start:start + SAMPLES_PER_PAGE]))
        pages.append(data_page(burn_id, idx, chunk))
        idx += 1
        total += len(chunk)
    if cut_at is None:
        pages.append(footer_page(burn_id, idx, total, int(t_us[-1]), 3_000_000))
    return pages


def main() -> int:
    ap = argparse.ArgumentParser(description="Vyrobí testovací SFDUMP výpis")
    ap.add_argument("out", nargs="?", default="test_dump.txt")
    ap.add_argument("--pocet", type=int, default=2, help="kolik zážehů vyrobit")
    ap.add_argument("--rate", type=float, default=80.0)
    ap.add_argument("--vypadek", action="store_true",
                    help="useknout druhý zážeh uprostřed hoření")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    np.random.seed(args.seed)
    lines = ["#BEGIN SFDUMP v2",
             '#INFO {"fw":"2.0.0","boots":7,"resumes":0,"burns":%d,"slots":6,'
             '"pages_per_slot":256,"samples_per_page":29,"cal_counts_per_n":%.6f,'
             '"tare":%d,"log_ok":1,"log_err":"","preroll_ms":3000,'
             '"recording_ms":20000,"ignition_ms":3000,"countdown_ms":15000,"state":1}'
             % (args.pocet, CAL, TARE)]

    for i in range(args.pocet):
        cut = 1.9 if (args.vypadek and i == 1) else None
        pages = build(burn_id=i + 1,
                      delay=0.180 + 0.05 * i,
                      peak=180.0 - 15.0 * i,
                      burn=1.6 + 0.2 * i,
                      prop_g=42.0 - 3.0 * i,
                      rate=args.rate,
                      cut_at=cut)
        lines.append(f"#SLOT {i} pages={len(pages)}")
        lines += [base64.b64encode(p).decode() for p in pages]
        lines.append(f"#ENDSLOT {i}")

    lines.append("#END SFDUMP")
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Zapsáno: {args.out} ({len(lines)} řádků)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
