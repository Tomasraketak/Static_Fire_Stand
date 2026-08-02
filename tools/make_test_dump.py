#!/usr/bin/env python3
"""
Synthetic dump generator, for trying the analysis without a stand.

It emits exactly the format the firmware sends for the 'p' command,
CRCs included, so the whole chain can be exercised:

    python make_test_dump.py test.txt
    python static_fire.py --from-file test.txt

It can also simulate a power cut mid-burn (--cut), which produces a
record with no closing page and a truncated timeline - handy for
checking that the analysis says so instead of inventing numbers.
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
from sf_protocol import (MAGIC_DATA, MAGIC_FOOTER, MAGIC_HEADER,  # noqa: E402
                         PAGE_SIZE, SAMPLES_PER_PAGE, SF_CONT, SF_PYRO)

CAL = 250.0        # ADC counts per newton
TARE = -123456     # resting ADC offset
PREROLL_S = 3.0
RECORD_S = 10.0    # matches RECORDING_MS in the firmware config


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
                     int(PREROLL_S * 1000), int(RECORD_S * 1000), 3000, 15000, 0,
                     0, 0, 0, 0)
    return _finish(p, 8)


def data_page(burn_id: int, idx: int, samples, page_flags: int = 0) -> bytes:
    p = bytearray(PAGE_SIZE)
    struct.pack_into("<IIHHII", p, 0, MAGIC_DATA, burn_id, idx, len(samples),
                     page_flags, 0)
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
    """A progressive curve: sharp rise, rounded top, exponential tail."""
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
          fuel_g: float, rate: float, cut_at: float | None) -> list[bytes]:
    n = int((PREROLL_S + RECORD_S) * rate)
    t = np.linspace(-PREROLL_S, RECORD_S, n)
    t += np.random.normal(0, 0.0004, n)          # sampling jitter
    t = np.sort(t)

    thrust = thrust_curve(t, delay, peak, burn)

    # mass loss: the resting reading drops in step with delivered impulse
    cum = np.cumsum(np.maximum(thrust, 0))
    frac = cum / cum[-1] if cum[-1] > 0 else np.zeros_like(cum)
    baseline = 12.0 - (fuel_g / 1000.0 * 9.80665) * frac

    signal = baseline + thrust + np.random.normal(0, 0.035, n)
    raw = (signal * CAL + TARE).astype(np.int64)

    pyro = (t >= 0) & (t < 3.0)
    flags = np.where(pyro, SF_PYRO | SF_CONT, SF_CONT).astype(np.int64)
    flags[t >= 3.0] = 0                          # igniter burnt through

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
    ap = argparse.ArgumentParser(description="Build a synthetic SFDUMP file")
    ap.add_argument("out", nargs="?", default="test_dump.txt")
    ap.add_argument("--count", type=int, default=2, help="how many burns to fake")
    ap.add_argument("--rate", type=float, default=80.0, help="sample rate in Hz")
    ap.add_argument("--cut", action="store_true",
                    help="cut the second burn off mid-flight, as a power loss would")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    np.random.seed(args.seed)
    lines = ["#BEGIN SFDUMP v2",
             '#INFO {"fw":"2.0.0","boots":7,"resumes":0,"burns":%d,"slots":6,'
             '"pages_per_slot":256,"samples_per_page":29,"cal_counts_per_n":%.6f,'
             '"tare":%d,"log_ok":1,"log_err":"","preroll_ms":%d,'
             '"recording_ms":%d,"ignition_ms":3000,"countdown_ms":15000,"state":1}'
             % (args.count, CAL, TARE, int(PREROLL_S * 1000), int(RECORD_S * 1000))]

    for i in range(args.count):
        pages = build(burn_id=i + 1,
                      delay=0.180 + 0.05 * i,
                      peak=180.0 - 15.0 * i,
                      burn=1.6 + 0.2 * i,
                      fuel_g=42.0 - 3.0 * i,
                      rate=args.rate,
                      cut_at=1.9 if (args.cut and i == 1) else None)
        lines.append(f"#SLOT {i} pages={len(pages)}")
        lines += [base64.b64encode(p).decode() for p in pages]
        lines.append(f"#ENDSLOT {i}")

    lines.append("#END SFDUMP")
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Written: {args.out} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
