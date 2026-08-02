"""
Downloading and decoding data from the Static Fire Stand.

This module only handles transport and unpacking of the binary format.
All the maths lives in sf_analysis.py, all the drawing in sf_report.py.

Wire format (serial command 'p'):

    #BEGIN SFDUMP v2
    #INFO {...json...}
    #SLOT 0 pages=137
    <base64 line = one 256 B page>
    ...
    #ENDSLOT 0
    #END SFDUMP

Every page carries its own CRC32, so a corrupted line is dropped and the
rest of the burn stays usable.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

PAGE_SIZE = 256
SAMPLES_PER_PAGE = 29

MAGIC_HEADER = 0x31484653  # "SFH1"
MAGIC_DATA = 0x31504653    # "SFP1"
MAGIC_FOOTER = 0x31464653  # "SFF1"

# per-sample flag bits
SF_PYRO = 0x01
SF_CONT = 0x02
SF_RBF = 0x04
SF_SAT = 0x08

# per-page flag bits
PF_RESUME = 0x01
PF_GAP = 0x02

_HDR_FMT = "<5Ifi5I4I"
_FOOT_FMT = "<5Ii4I6I"
_DATA_HDR_FMT = "<IIHHII"

G0 = 9.80665


# ---------------------------------------------------------------------
#  CRC
# ---------------------------------------------------------------------
def _crc_ok(page: bytes, crc_offset: int) -> bool:
    stored = struct.unpack_from("<I", page, crc_offset)[0]
    blanked = page[:crc_offset] + b"\x00\x00\x00\x00" + page[crc_offset + 4:]
    return binascii.crc32(blanked) & 0xFFFFFFFF == stored


# ---------------------------------------------------------------------
#  Data classes
# ---------------------------------------------------------------------
@dataclass
class BurnQuality:
    pages_seen: int = 0
    pages_bad_crc: int = 0
    pages_unknown: int = 0
    has_footer: bool = False
    clean_finish: bool = False
    resume_events: int = 0
    gap_events: int = 0
    saturated_samples: int = 0
    duplicate_samples: int = 0
    nonmonotonic_samples: int = 0

    def problems(self) -> list[str]:
        p = []
        if self.pages_bad_crc:
            p.append(f"{self.pages_bad_crc} page(s) failed CRC and were dropped")
        if not self.has_footer:
            p.append("no closing page - the record was cut short (reset / power loss)")
        elif not self.clean_finish:
            p.append("the record was closed as incomplete")
        if self.resume_events:
            p.append(f"the stand restarted and resumed recording {self.resume_events}x")
        if self.gap_events:
            p.append(f"{self.gap_events} hole(s) of unknown length in the timeline")
        if self.saturated_samples:
            p.append(f"{self.saturated_samples} samples hit the ADC rail - load cell overloaded")
        if self.nonmonotonic_samples:
            p.append(f"{self.nonmonotonic_samples} samples arrived out of time order")
        return p


@dataclass
class Burn:
    slot: int
    burn_id: int = 0
    cal_counts_per_n: float = 1.0
    tare_offset: int = 0
    boot_count: int = 0
    fw_version: str = "?"
    preroll_ms: int = 0
    recording_ms: int = 0
    ignition_ms: int = 0
    countdown_ms: int = 0
    t_pyro_off_us: int = 0
    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    quality: BurnQuality = field(default_factory=BurnQuality)

    @property
    def n_samples(self) -> int:
        return len(self.df)

    @property
    def duration_s(self) -> float:
        if self.df.empty:
            return 0.0
        return float(self.df["t"].iloc[-1] - self.df["t"].iloc[0])


# ---------------------------------------------------------------------
#  Page decoding
# ---------------------------------------------------------------------
def decode_slot(pages: list[bytes], slot: int) -> Burn | None:
    """Reassemble one burn from the pages collected for a slot."""
    burn = Burn(slot=slot)
    q = burn.quality
    rows_t: list[np.ndarray] = []
    rows_raw: list[np.ndarray] = []
    rows_flags: list[np.ndarray] = []

    for page in pages:
        q.pages_seen += 1
        if len(page) != PAGE_SIZE:
            q.pages_unknown += 1
            continue
        magic = struct.unpack_from("<I", page, 0)[0]

        if magic == MAGIC_HEADER:
            if not _crc_ok(page, 8):
                q.pages_bad_crc += 1
                continue
            (_, burn_id, _crc, boot, fwv, cal, tare,
             preroll, recording, ignition, countdown, _t0u,
             _r0, _r1, _r2, _r3) = struct.unpack_from(_HDR_FMT, page, 0)
            burn.burn_id = burn_id
            burn.boot_count = boot
            burn.fw_version = f"{(fwv >> 16) & 0xFF}.{(fwv >> 8) & 0xFF}.{fwv & 0xFF}"
            burn.cal_counts_per_n = cal if cal != 0.0 else 1.0
            burn.tare_offset = tare
            burn.preroll_ms = preroll
            burn.recording_ms = recording
            burn.ignition_ms = ignition
            burn.countdown_ms = countdown

        elif magic == MAGIC_DATA:
            if not _crc_ok(page, 16):
                q.pages_bad_crc += 1
                continue
            _m, _bid, _pidx, n, flags, _crc = struct.unpack_from(_DATA_HDR_FMT, page, 0)
            if flags & PF_RESUME:
                q.resume_events += 1
            if flags & PF_GAP:
                q.gap_events += 1
            n = min(n, SAMPLES_PER_PAGE)
            if n == 0:
                continue
            block = np.frombuffer(page, dtype=np.dtype([("t", "<i4"), ("p", "<u4")]),
                                  count=n, offset=20)
            packed = block["p"].astype(np.uint32)
            raw = (packed & 0x00FFFFFF).astype(np.int64)
            raw = np.where(raw & 0x800000, raw - 0x1000000, raw)
            rows_t.append(block["t"].astype(np.int64))
            rows_raw.append(raw)
            rows_flags.append((packed >> 24).astype(np.uint8))

        elif magic == MAGIC_FOOTER:
            if not _crc_ok(page, 8):
                q.pages_bad_crc += 1
                continue
            vals = struct.unpack_from(_FOOT_FMT, page, 0)
            burn.t_pyro_off_us = vals[7]
            q.has_footer = True
            q.clean_finish = bool(vals[9])

        else:
            q.pages_unknown += 1

    if not rows_t:
        return None

    t_us = np.concatenate(rows_t)
    raw = np.concatenate(rows_raw)
    flags = np.concatenate(rows_flags)

    order = np.argsort(t_us, kind="stable")
    q.nonmonotonic_samples = int(np.sum(np.diff(t_us) < 0))
    t_us, raw, flags = t_us[order], raw[order], flags[order]

    keep = np.ones(len(t_us), dtype=bool)
    keep[1:] = np.diff(t_us) != 0
    q.duplicate_samples = int(np.sum(~keep))
    t_us, raw, flags = t_us[keep], raw[keep], flags[keep]

    q.saturated_samples = int(np.sum((flags & SF_SAT) != 0))

    cal = burn.cal_counts_per_n or 1.0
    df = pd.DataFrame({
        "t": t_us / 1e6,
        "t_ms": t_us / 1e3,
        "raw": raw,
        "thrust": (raw - burn.tare_offset) / cal,
        "pyro": (flags & SF_PYRO) != 0,
        "cont": (flags & SF_CONT) != 0,
        "rbf": (flags & SF_RBF) != 0,
        "sat": (flags & SF_SAT) != 0,
    })
    burn.df = df.reset_index(drop=True)
    return burn


# ---------------------------------------------------------------------
#  Parsing the text dump
# ---------------------------------------------------------------------
def parse_dump(text: str) -> tuple[dict, list[Burn]]:
    info: dict = {}
    burns: list[Burn] = []
    slot = None
    pages: list[bytes] = []
    bad_lines = 0

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#INFO"):
            try:
                info = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                pass
        elif line.startswith("#SLOT"):
            m = re.match(r"#SLOT\s+(\d+)", line)
            slot = int(m.group(1)) if m else None
            pages = []
        elif line.startswith("#ENDSLOT"):
            if slot is not None:
                b = decode_slot(pages, slot)
                if b is not None:
                    b.quality.pages_unknown += bad_lines
                    burns.append(b)
            slot, pages, bad_lines = None, [], 0
        elif line.startswith("#"):
            continue
        elif slot is not None:
            try:
                pages.append(base64.b64decode(line, validate=True))
            except (binascii.Error, ValueError):
                bad_lines += 1

    burns.sort(key=lambda b: b.burn_id)
    return info, burns


# ---------------------------------------------------------------------
#  Importing old data (V0 firmware, CSV over the serial line)
# ---------------------------------------------------------------------
def load_legacy_csv(path: str | Path, burn_id: int = 0) -> Burn:
    """Read a CSV produced by the original firmware.

    Columns: Time(ms), Thrust(N), Pyro_Active. Handles both the Czech
    flavour (`;` with a decimal comma) and the plain one (`,` with a
    decimal point). Thrust in those files is already in newtons; the raw
    ADC counts are not recorded, so recalibration is not possible.
    """
    import io

    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        text = fh.read()
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    sep = ";" if first.count(";") >= first.count(",") else ","
    dec = "," if sep == ";" else "."
    df = pd.read_csv(io.StringIO(text), sep=sep, decimal=dec)
    df.columns = [c.strip() for c in df.columns]

    col_t = next((c for c in df.columns if c.lower().startswith("time(ms")), None)
    col_f = next((c for c in df.columns if c.lower().startswith("thrust")), None)
    col_p = next((c for c in df.columns if c.lower().startswith("pyro")), None)
    if col_t is None or col_f is None:
        raise ValueError(f"{path}: missing the Time(ms) and Thrust(N) columns")

    t_ms = pd.to_numeric(df[col_t], errors="coerce")
    thrust = pd.to_numeric(df[col_f], errors="coerce")
    ok = t_ms.notna() & thrust.notna()
    t_ms, thrust = t_ms[ok].to_numpy(float), thrust[ok].to_numpy(float)
    pyro = (pd.to_numeric(df[col_p], errors="coerce").fillna(0)[ok].to_numpy() > 0) \
        if col_p else np.zeros(len(t_ms), dtype=bool)

    order = np.argsort(t_ms, kind="stable")
    t_ms, thrust, pyro = t_ms[order], thrust[order], pyro[order]

    burn = Burn(slot=-1, burn_id=burn_id, cal_counts_per_n=1.0, tare_offset=0,
                fw_version="V0 (CSV import)")
    burn.df = pd.DataFrame({
        "t": t_ms / 1000.0,
        "t_ms": t_ms,
        "raw": np.zeros(len(t_ms), dtype=np.int64),
        "thrust": thrust,
        "pyro": pyro,
        "cont": np.zeros(len(t_ms), dtype=bool),
        "rbf": np.zeros(len(t_ms), dtype=bool),
        "sat": np.zeros(len(t_ms), dtype=bool),
    }).reset_index(drop=True)
    q = burn.quality
    q.has_footer = True
    q.clean_finish = True
    if burn.burn_id == 0:
        m = re.search(r"(\d+)", os.path.basename(str(path)))
        burn.burn_id = int(m.group(1)) if m else 0
    return burn


# ---------------------------------------------------------------------
#  Serial communication
# ---------------------------------------------------------------------
def list_ports() -> list[tuple[str, str]]:
    """All serial ports as (device, description), Pico first."""
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    out = []
    for p in lp.comports():
        pico = (p.vid == 0x2E8A)
        label = f"{p.description}{'  [Raspberry Pi Pico]' if pico else ''}"
        out.append((p.device, label, pico))
    out.sort(key=lambda r: (not r[2], r[0]))
    return [(d, la) for d, la, _ in out]


def find_port() -> str | None:
    """Pick the Pico automatically by its USB vendor id (0x2E8A)."""
    ports = list_ports()
    for dev, label in ports:
        if "Raspberry Pi Pico" in label:
            return dev
    for dev, _ in ports:
        if "ACM" in dev or "usbmodem" in dev:
            return dev
    return ports[0][0] if ports else None


class Stand:
    """Thin wrapper around the stand's serial console."""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 3.0):
        import serial  # imported lazily so offline analysis needs no pyserial
        self.ser = serial.Serial(port, baud, timeout=timeout)
        self.port = port
        time.sleep(2.0)          # USB CDC needs a moment to settle
        self.ser.reset_input_buffer()

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass

    def __enter__(self) -> "Stand":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def command(self, cmd: str) -> None:
        self.ser.write((cmd + "\n").encode())
        self.ser.flush()

    def read_lines_until(self, sentinel: str, overall_timeout: float = 180.0,
                         progress: bool = True) -> str:
        buf: list[str] = []
        t0 = time.time()
        last_report = 0.0
        while True:
            raw = self.ser.readline()
            if not raw:
                if time.time() - t0 > overall_timeout:
                    break
                if buf:            # silence after data means we are done
                    break
                continue
            buf.append(raw.decode("utf-8", errors="replace"))
            if progress and time.time() - last_report > 0.5:
                last_report = time.time()
                print(f"  {len(buf)} lines received...")
            if sentinel in buf[-1]:
                break
            if time.time() - t0 > overall_timeout:
                break
        return "".join(buf)

    def info(self) -> dict:
        self.ser.reset_input_buffer()
        self.command("i")
        deadline = time.time() + 5
        while time.time() < deadline:
            line = self.ser.readline().decode("utf-8", errors="replace").strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return {}

    def download(self, slot: int | None = None, retries: int = 2) -> str:
        text = ""
        for attempt in range(retries + 1):
            self.ser.reset_input_buffer()
            self.command("p" if slot is None else f"p {slot}")
            text = self.read_lines_until("#END SFDUMP")
            if "#BEGIN SFDUMP" in text and "#END SFDUMP" in text:
                return text
            if attempt < retries:
                print(f"  incomplete transfer, retrying ({attempt + 1}/{retries})...")
                time.sleep(1.0)
        return text

    def erase(self) -> None:
        self.command("d")
        time.sleep(3.0)

    def tare(self) -> None:
        self.command("t")
        time.sleep(2.0)

    def calibrate_grams(self, grams: float) -> None:
        self.command(f"calg {grams}")
        time.sleep(3.0)

    def drain(self, seconds: float = 1.5) -> str:
        """Collect whatever the stand printed, e.g. after a command."""
        out, deadline = [], time.time() + seconds
        while time.time() < deadline:
            line = self.ser.readline()
            if line:
                out.append(line.decode("utf-8", errors="replace"))
        return "".join(out)
