"""
Evaluation of a single burn: ignition timing, thrust curve, impulse, Isp.

Time base: t = 0 is the moment the firmware energised the pyro channel.
Samples before zero are the pre-roll used to establish the baseline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from sf_protocol import G0, Burn

# fractions of peak thrust that get a reported rise and decay time
PCT_STEPS = (5, 10, 25, 50, 75, 90, 95, 100)

# An igniter can give the load cell a kick of its own - the charge going off,
# the leads twitching - a good second before the grain itself catches. It is
# real thrust, so no threshold rejects it, and taking it as the start of the
# burn puts the ignition delay a second early and stretches the rise time
# across the dead space in between. A blip is treated as the igniter, not the
# burn, when all three of these hold: it is small, it is brief, and the stand
# went quiet again afterwards.
IGNITER_SPIKE_MAX_FRAC = 0.25   # at most this much of peak thrust
IGNITER_SPIKE_MAX_S    = 0.50   # lasting no longer than this
IGNITER_SPIKE_GAP_S    = 0.20   # with at least this much quiet before the burn


@dataclass
class Result:
    burn_id: int = 0
    slot: int = 0

    # --- baseline and noise ---
    baseline_n: float = 0.0
    baseline_noise_rms: float = 0.0
    baseline_drift_n_per_s: float = 0.0
    baseline_samples: int = 0
    baseline_rejected: int = 0    # pre-roll samples excluded as not-at-rest
    baseline_unstable: bool = False
    tail_n: float = 0.0

    # Where the integration window opens. Normally T0, but pulled back when
    # the motor was already producing thrust before the fire command.
    t_analysis_start: float = 0.0
    thrust_before_t0: bool = False

    # --- ignition timing ---
    t_first_motion: float = float("nan")
    t_ignition_detect: float = float("nan")   # 5 % of peak
    t_peak: float = float("nan")
    rise_10_90: float = float("nan")
    detection_threshold_n: float = 0.0
    t_igniter_spike: float = float("nan")   # blip before the burn, if any
    igniter_spike_n: float = 0.0
    t_pyro_off: float = float("nan")
    pyro_on_duration: float = float("nan")
    pct_times: dict = field(default_factory=dict)
    decay_times: dict = field(default_factory=dict)

    # --- thrust curve ---
    peak_thrust: float = 0.0
    avg_thrust: float = 0.0
    avg_thrust_full: float = 0.0
    burn_time: float = 0.0        # T0 -> decay through 5 % of peak
    action_time: float = 0.0      # 5 % rise -> 5 % decay
    thrust_centroid: float = float("nan")
    t_half_impulse: float = float("nan")
    peak_to_avg: float = float("nan")
    max_slope_n_per_s: float = 0.0

    # --- impulse and efficiency ---
    total_impulse: float = 0.0
    impulse_full_window: float = 0.0
    prop_mass_kg: float = 0.0
    prop_mass_source: str = "n/a"
    isp: float = float("nan")
    c_eff: float = float("nan")   # effective exhaust velocity [m/s]
    motor_class: str = "?"
    motor_designation: str = "?"
    class_fill_pct: float = float("nan")

    # --- data quality ---
    sample_rate_hz: float = 0.0
    sample_rate_std: float = 0.0
    max_gap_s: float = 0.0
    n_samples: int = 0
    warnings: list = field(default_factory=list)

    # --- helper series for the plots ---
    curve: pd.DataFrame = field(default_factory=pd.DataFrame)


def _trap(y: np.ndarray, x: np.ndarray) -> float:
    """np.trapezoid on NumPy 2, np.trapz on older releases."""
    fn = getattr(np, "trapezoid", None) or np.trapz
    return float(fn(y, x))


def _first_time_above(t: np.ndarray, y: np.ndarray, level: float,
                      start_idx: int = 0) -> float:
    """Linearly interpolated time of the first crossing above a level."""
    idx = np.nonzero(y[start_idx:] >= level)[0]
    if idx.size == 0:
        return float("nan")
    i = idx[0] + start_idx
    if i == 0:
        return float(t[0])
    y0, y1 = y[i - 1], y[i]
    if y1 == y0:
        return float(t[i])
    frac = (level - y0) / (y1 - y0)
    return float(t[i - 1] + frac * (t[i] - t[i - 1]))


def _runs(mask: np.ndarray, min_len: int = 3) -> list[tuple[int, int]]:
    """Contiguous [start, end) stretches where mask is true, min_len or longer.

    Requiring a few samples in a row is what stops a single noisy sample
    being mistaken for the motor doing something.
    """
    if mask.size == 0:
        return []
    edges = np.diff(mask.astype(np.int8))
    starts = list(np.nonzero(edges == 1)[0] + 1)
    ends = list(np.nonzero(edges == -1)[0] + 1)
    if mask[0]:
        starts.insert(0, 0)
    if mask[-1]:
        ends.append(mask.size)
    return [(a, b) for a, b in zip(starts, ends) if b - a >= min_len]


def _main_burn_start(t: np.ndarray, net: np.ndarray, thr: float,
                     i_peak: int, peak: float) -> tuple[int, int | None]:
    """Index where the burn proper starts, and the igniter blip before it.

    Walks back from the run of samples containing the peak and keeps
    absorbing earlier runs as part of the same event, stopping at the first
    one that looks like the igniter firing on its own: small, brief, and
    followed by a quiet stretch. Returns (burn start index, index of the
    peak of the rejected blip or None).
    """
    runs = _runs(net >= thr, min_len=3)
    if not runs:
        return 0, None

    main = next((k for k, (a, b) in enumerate(runs) if a <= i_peak < b), 0)
    start = main
    for k in range(main - 1, -1, -1):
        a, b = runs[k]
        gap = float(t[runs[start][0]] - t[b - 1])
        amp = float(np.max(net[a:b]))
        dur = float(t[b - 1] - t[a])
        if (amp <= IGNITER_SPIKE_MAX_FRAC * peak
                and dur <= IGNITER_SPIKE_MAX_S
                and gap >= IGNITER_SPIKE_GAP_S):
            return runs[start][0], a + int(np.argmax(net[a:b]))
        start = k
    return runs[start][0], None


def _curve_frame(t: np.ndarray, thrust: np.ndarray, baseline: np.ndarray) -> pd.DataFrame:
    """The plotting series, always with the same four columns.

    Every `return` out of analyze() goes through here. The early-out paths
    used to build this frame with only some of the columns, and the chart
    code - which reads `curve["baseline"]` unconditionally - then died with
    a KeyError on the first odd burn and took the whole batch down with it.
    """
    return pd.DataFrame({
        "t": t,
        "thrust": thrust,
        "net": thrust - baseline,
        "baseline": baseline,
    })


def _robust_baseline(f: np.ndarray) -> tuple[float, float, int]:
    """Resting level and noise from a pre-roll that may not be quiet.

    The pre-roll is *supposed* to be the motor sitting still, but it is not
    always: a motor that lights before the command, someone steadying the
    stand, a cable still being dressed - any of that lands in the window.
    A plain mean/std then reports a resting level metres from the truth and
    a noise figure tens of times too large, and everything downstream (the
    ignition threshold, the "did it light?" guard) is derived from those
    two numbers. One real burn on this stand lit ~1 s before T0 and was
    thrown away entirely as "no thrust above the noise floor" because of it.

    So: centre on the median and measure spread with the MAD, both of which
    survive a contaminated minority, then drop the samples that sit far from
    that centre and recompute on what is left.

    Returns (level, noise as a standard deviation, samples rejected).
    """
    med = float(np.median(f))
    mad = float(np.median(np.abs(f - med)))
    sigma = 1.4826 * mad          # MAD -> sigma, for normally distributed noise
    if not (sigma > 0):
        # Perfectly flat or heavily quantised: fall back to the plain spread.
        sigma = float(np.std(f))
    if not (sigma > 0):
        return med, 0.0, 0

    keep = np.abs(f - med) <= 4.0 * sigma
    # Refuse to throw away most of the window - if that much of it is
    # "outlying" the assumption behind this whole routine is wrong, and a
    # noisy honest answer beats a confident one from five samples.
    if keep.sum() < max(5, int(0.2 * f.size)):
        keep = np.ones(f.size, dtype=bool)

    kept = f[keep]
    level = float(np.mean(kept))
    noise = float(np.std(kept, ddof=1)) if kept.size > 1 else 0.0
    return level, noise, int((~keep).sum())


def _last_time_above(t: np.ndarray, y: np.ndarray, level: float) -> float:
    idx = np.nonzero(y >= level)[0]
    if idx.size == 0:
        return float("nan")
    i = idx[-1]
    if i + 1 >= len(t):
        return float(t[i])
    y0, y1 = y[i], y[i + 1]
    if y1 == y0:
        return float(t[i])
    frac = (level - y0) / (y1 - y0)
    return float(t[i] + frac * (t[i + 1] - t[i]))


def motor_class(total_impulse: float) -> tuple[str, float]:
    """NAR/TRA letter class and how far into that class the motor sits."""
    if total_impulse <= 0:
        return "?", float("nan")
    if total_impulse < 0.3125:
        return "<1/8A", float("nan")
    if total_impulse < 1.25:
        for lo, name in ((0.625, "1/4A"), (0.3125, "1/8A")):
            if total_impulse >= lo:
                return name, (total_impulse - lo) / lo * 100.0
        return "1/8A", float("nan")
    idx = max(0, math.ceil(math.log2(total_impulse / 2.5)))
    letter = chr(ord("A") + idx)
    upper = 2.5 * (2 ** idx)
    lower = upper / 2.0
    return letter, (total_impulse - lower) / (upper - lower) * 100.0


def analyze(burn: Burn, prop_mass_g: float | None = None,
            detect_sigma: float = 6.0, detect_floor_pct: float = 1.0) -> Result:
    r = Result(burn_id=burn.burn_id, slot=burn.slot)
    df = burn.df
    if df.empty:
        r.warnings.append("the burn contains no samples")
        return r

    t = df["t"].to_numpy(dtype=float)
    f = df["thrust"].to_numpy(dtype=float)
    r.n_samples = len(t)

    # ---- sample rate -----------------------------------------------------
    dt = np.diff(t)
    if dt.size:
        good = dt[dt > 0]
        if good.size:
            nominal = float(np.median(good))
            r.sample_rate_hz = 1.0 / nominal
            r.sample_rate_std = float(np.std(1.0 / good))
            r.max_gap_s = float(np.max(good))
            if r.max_gap_s > 3 * nominal:
                r.warnings.append(
                    f"largest gap between samples {r.max_gap_s*1000:.0f} ms "
                    f"(nominal {nominal*1000:.1f} ms)")

    # ---- baseline from the pre-roll --------------------------------------
    # The last 100 ms before T0 are skipped: by then the pyro relay is
    # about to click and people are usually still moving around the stand.
    pre = df[df["t"] < -0.10]
    if len(pre) >= 5:
        pre_f = pre["thrust"].to_numpy(dtype=float)
        r.baseline_n, r.baseline_noise_rms, r.baseline_rejected = _robust_baseline(pre_f)
        r.baseline_samples = len(pre)
        kept = np.abs(pre_f - r.baseline_n) <= max(5 * r.baseline_noise_rms, 1e-9)
        if kept.sum() >= 20:
            slope = np.polyfit(pre["t"].to_numpy()[kept], pre_f[kept], 1)[0]
            r.baseline_drift_n_per_s = float(slope)
    else:
        pre0 = df[df["t"] < 0]
        r.baseline_n = float(pre0["thrust"].mean()) if len(pre0) else float(f[0])
        r.baseline_noise_rms = float(pre0["thrust"].std(ddof=1)) if len(pre0) > 1 else 0.0
        r.baseline_samples = len(pre0)
        r.warnings.append("very short pre-roll - the baseline is uncertain")

    # ---- is the resting level believable at all? ---------------------------
    # Thrust only ever pushes one way, so a pre-roll that is genuinely at
    # rest scatters symmetrically about the baseline. A cluster sitting well
    # BELOW it means the estimate got dragged upwards - the median only
    # survives contamination up to half the window, and a motor burning
    # through most of the pre-roll takes it past that. Nothing in the record
    # can resolve which level is the real one, so this is reported rather
    # than guessed at.
    if len(pre) >= 30 and r.baseline_noise_rms > 0:
        below = int(np.sum(pre_f < r.baseline_n - max(6 * r.baseline_noise_rms, 0.2)))
        if below > 0.05 * len(pre):
            r.baseline_unstable = True

    # ---- was the motor already running before the command? ----------------
    # If a slice of the pre-roll sits well clear of the resting level, thrust
    # started before T0. The record is still perfectly good - it just cannot
    # be read as "everything happens after the command", so the integration
    # window opens where the thrust actually starts instead of at T0.
    detect_level = max(detect_sigma * r.baseline_noise_rms,
                       0.02 * float(np.max(f) - r.baseline_n), 0.05)
    early = pre[pre["thrust"] - r.baseline_n >= detect_level] if len(pre) >= 5 else pre[:0]
    if len(early) >= 3:
        r.thrust_before_t0 = True
        r.t_analysis_start = float(early["t"].iloc[0])
        r.warnings.append(
            f"thrust was already present {abs(r.t_analysis_start):.2f} s BEFORE the fire "
            "command - the motor lit early, so every time measured from T0 (ignition "
            "delay, rise times) is meaningless here; impulse and peak are still valid")

    if r.baseline_unstable and not r.thrust_before_t0:
        r.warnings.append(
            "the stand was never at rest during the pre-roll - the resting level "
            f"({r.baseline_n:.2f} N) is a guess and every figure below is suspect. "
            "A motor already burning through the whole pre-roll looks like this.")

    post = df[df["t"] >= r.t_analysis_start]
    if post.empty:
        r.warnings.append("no data after T0")
        return r

    tp = post["t"].to_numpy(dtype=float)
    fp = post["thrust"].to_numpy(dtype=float)
    net = fp - r.baseline_n

    r.peak_thrust = float(np.max(net))
    if r.peak_thrust <= max(5 * r.baseline_noise_rms, 0.1):
        r.warnings.append("no thrust found above the noise floor - did the motor light?")
        r.curve = _curve_frame(tp, fp, np.full(tp.size, r.baseline_n))
        return r

    i_peak = int(np.argmax(net))
    r.t_peak = float(tp[i_peak])

    # ---- tail: what the cell reads once the motor is done -----------------
    # Taken from the end of the record, but only after thrust has dropped
    # below 2 % of peak and then only 250 ms later still.
    #
    # If the record ended before the motor finished, no such quiet stretch
    # exists. In that case the mass-loss correction is switched off
    # entirely: taking "rest" samples from the middle of the burn would
    # corrupt both the impulse and the derived propellant mass.
    t_decay2 = _last_time_above(tp, net, 0.02 * r.peak_thrust)
    tail_valid = False
    if not math.isnan(t_decay2):
        tail = post[post["t"] > t_decay2 + 0.25]
        if len(tail) >= 5 and float(tail["t"].iloc[-1] - tail["t"].iloc[0]) >= 0.2:
            tail_net = float(tail["thrust"].mean()) - r.baseline_n
            if abs(tail_net) < 0.05 * r.peak_thrust:
                r.tail_n = float(tail["thrust"].mean())
                tail_valid = True
    if not tail_valid:
        r.tail_n = r.baseline_n
        r.warnings.append("the record does not end at rest - mass-loss correction "
                          "is off and propellant mass cannot be read from the cell")

    # ---- first motion detection ------------------------------------------
    # Threshold = max(N sigma of noise, a fraction of peak). Three samples
    # in a row must clear it, otherwise a single spike would move the
    # measured ignition delay.
    thr = max(detect_sigma * r.baseline_noise_rms,
              detect_floor_pct / 100.0 * r.peak_thrust)
    r.detection_threshold_n = float(thr)
    # Where the burn proper begins - which is not necessarily the first
    # thing that moved the cell. See _main_burn_start().
    i_burn, i_spike = _main_burn_start(tp, net, thr, i_peak, r.peak_thrust)
    if i_spike is not None:
        r.t_igniter_spike = float(tp[i_spike])
        r.igniter_spike_n = float(net[i_spike])

    r.t_first_motion = _first_time_above(tp, net, thr, start_idx=i_burn)

    # ---- rise and decay thresholds ---------------------------------------
    # Measured from the start of the burn, so an igniter blip cannot claim
    # the 5 % and 10 % crossings and stretch the rise time across the pause
    # between it and the grain lighting.
    for pct in PCT_STEPS:
        lvl = r.peak_thrust * pct / 100.0
        r.pct_times[pct] = _first_time_above(tp, net, lvl, start_idx=i_burn)
        r.decay_times[pct] = _last_time_above(tp, net, lvl)

    t5_rise = r.pct_times.get(5, float("nan"))
    t5_fall = r.decay_times.get(5, float("nan"))
    r.t_ignition_detect = t5_rise
    if not math.isnan(r.pct_times.get(10, float("nan"))) and \
       not math.isnan(r.pct_times.get(90, float("nan"))):
        r.rise_10_90 = r.pct_times[90] - r.pct_times[10]

    # If the very last sample is still above 5 % of peak, the motor was
    # still burning when the record ended and every duration is a floor.
    if math.isnan(t5_fall) or net[-1] >= 0.05 * r.peak_thrust:
        t5_fall = float(tp[-1])
        r.warnings.append("thrust never fell below 5 % of peak - the motor was still "
                          "burning at the end, burn time and impulse are lower bounds")
    r.burn_time = float(t5_fall)
    r.action_time = float(t5_fall - t5_rise) if not math.isnan(t5_rise) else float(t5_fall)

    # ---- pyro channel -----------------------------------------------------
    pyro = post[post["pyro"]]
    if len(pyro):
        r.t_pyro_off = float(pyro["t"].iloc[-1])
        r.pyro_on_duration = r.t_pyro_off - float(pyro["t"].iloc[0])

    # ---- mass-loss correction ---------------------------------------------
    # The motor sits on the load cell, so as propellant leaves, the resting
    # reading drops. The shift between T0 and burnout is interpolated
    # against the impulse already delivered rather than linearly in time -
    # propellant does not leave at a constant rate.
    mask = (tp >= r.t_analysis_start) & (tp <= t5_fall)
    tb = tp[mask]
    fb = fp[mask]
    if tb.size < 3:
        r.warnings.append("too few samples inside the burn window")
        r.curve = _curve_frame(tp, fp, np.full(tp.size, r.baseline_n))
        return r

    rough = np.maximum(fb - r.baseline_n, 0.0)
    cum = np.concatenate([[0.0], np.cumsum(0.5 * (rough[1:] + rough[:-1]) * np.diff(tb))])
    frac = cum / cum[-1] if cum[-1] > 0 else np.linspace(0, 1, tb.size)
    baseline_curve = r.baseline_n + (r.tail_n - r.baseline_n) * frac

    net_b = np.maximum(fb - baseline_curve, 0.0)
    r.total_impulse = _trap(net_b, tb)

    net_full = np.maximum(fp - np.interp(tp, tb, baseline_curve,
                                         left=r.baseline_n, right=r.tail_n), 0.0)
    r.impulse_full_window = _trap(net_full, tp)

    if r.action_time > 0:
        r.avg_thrust = r.total_impulse / r.action_time
    if t5_fall > 0:
        r.avg_thrust_full = r.total_impulse / t5_fall
    if r.avg_thrust > 0:
        r.peak_to_avg = r.peak_thrust / r.avg_thrust

    # centroid of the curve and the half-impulse crossing
    cum_b = np.concatenate([[0.0], np.cumsum(0.5 * (net_b[1:] + net_b[:-1]) * np.diff(tb))])
    if cum_b[-1] > 0:
        r.thrust_centroid = _trap(net_b * tb, tb) / cum_b[-1]
        r.t_half_impulse = float(np.interp(0.5 * cum_b[-1], cum_b, tb))

    if tb.size > 3:
        r.max_slope_n_per_s = float(np.max(np.gradient(net_b, tb)))

    # ---- propellant mass and Isp -------------------------------------------
    if prop_mass_g is not None and prop_mass_g > 0:
        r.prop_mass_kg = prop_mass_g / 1000.0
        r.prop_mass_source = "entered by the operator"
    else:
        delta_n = r.baseline_n - r.tail_n
        r.prop_mass_kg = delta_n / G0
        r.prop_mass_source = "from the shift in the load cell reading"
        if delta_n <= 0:
            r.prop_mass_kg = 0.0
            if tail_valid:
                r.warnings.append("the resting reading did not drop - mass loss cannot "
                                  "be measured, enter the propellant mass manually")
            else:
                r.warnings.append("enter the propellant mass manually - it cannot be "
                                  "derived from this record")
        else:
            noise_mass = 3 * r.baseline_noise_rms / G0
            if r.prop_mass_kg < noise_mass:
                r.warnings.append("the measured mass loss is down at the noise level - "
                                  "treat Isp as indicative only")

    if r.prop_mass_kg > 0 and r.total_impulse > 0:
        r.isp = r.total_impulse / (r.prop_mass_kg * G0)
        r.c_eff = r.total_impulse / r.prop_mass_kg

    cls, fill = motor_class(r.total_impulse)
    r.motor_class = cls
    r.class_fill_pct = fill
    if r.avg_thrust > 0:
        r.motor_designation = f"{cls}{r.avg_thrust:.0f}"

    # ---- series for the plots ----------------------------------------------
    r.curve = _curve_frame(
        tp, fp, np.interp(tp, tb, baseline_curve, left=r.baseline_n, right=r.tail_n))

    # ---- sanity checks ------------------------------------------------------
    if r.baseline_rejected:
        pct = 100.0 * r.baseline_rejected / max(1, r.baseline_samples)
        r.warnings.append(
            f"{r.baseline_rejected} of {r.baseline_samples} pre-roll samples ({pct:.0f} %) "
            "were not at rest and were left out of the baseline")
    if not math.isnan(r.t_igniter_spike):
        r.warnings.append(
            f"a {r.igniter_spike_n:.1f} N blip at {r.t_igniter_spike*1000:.0f} ms "
            f"({r.igniter_spike_n / r.peak_thrust * 100:.0f} % of peak) was read as the "
            f"igniter firing, not the grain lighting - the burn is timed from "
            f"{r.t_first_motion*1000:.0f} ms instead. Check the ignition close-up if "
            "that looks wrong")
    if not math.isnan(r.t_first_motion) and r.t_first_motion > 2.0:
        r.warnings.append(f"very long ignition delay ({r.t_first_motion:.2f} s) - "
                          "check the igniter and the pyrogen")
    if burn.df["sat"].any():
        r.warnings.append("the load cell saturated - peak thrust is understated")
    if r.sample_rate_hz and r.sample_rate_hz < 40:
        r.warnings.append(f"only {r.sample_rate_hz:.0f} Hz sampling - tie the HX711 "
                          "RATE pin to VCC for 80 SPS")
    return r


def summary_rows(burn: Burn, r: Result) -> list[tuple[str, str]]:
    """(label, value) pairs for the console, the CSV and the spreadsheet."""
    def fmt(v, unit="", dec=3):
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            return "-"
        return f"{v:.{dec}f}{unit}"

    return [
        ("Burn ID", str(r.burn_id)),
        ("Memory slot", str(r.slot)),
        ("Firmware", burn.fw_version),
        ("Samples", str(r.n_samples)),
        ("Sample rate", fmt(r.sample_rate_hz, " Hz", 1)),
        ("", ""),
        ("- IGNITION TIMING -", ""),
        ("T0 (fire command)", "0.000 s"),
        ("Detection threshold", fmt(r.detection_threshold_n, " N")),
        ("Igniter blip (ignored)", fmt(r.t_igniter_spike, " s")),
        ("Igniter blip size", fmt(r.igniter_spike_n, " N", 2) if r.igniter_spike_n else "-"),
        ("First motion (ignition delay)", fmt(r.t_first_motion, " s")),
        ("Reached 5 % of peak", fmt(r.pct_times.get(5), " s")),
        ("Reached 10 % of peak", fmt(r.pct_times.get(10), " s")),
        ("Reached 50 % of peak", fmt(r.pct_times.get(50), " s")),
        ("Reached 90 % of peak", fmt(r.pct_times.get(90), " s")),
        ("Time to peak", fmt(r.t_peak, " s")),
        ("Rise time 10 -> 90 %", fmt(r.rise_10_90, " s")),
        ("Steepest rise", fmt(r.max_slope_n_per_s, " N/s", 0)),
        ("Pyro channel opened at", fmt(r.t_pyro_off, " s")),
        ("", ""),
        ("- THRUST CURVE -", ""),
        ("Peak thrust", fmt(r.peak_thrust, " N", 2)),
        ("Average thrust (action time)", fmt(r.avg_thrust, " N", 2)),
        ("Average thrust (from T0)", fmt(r.avg_thrust_full, " N", 2)),
        ("Peak to average ratio", fmt(r.peak_to_avg, "", 2)),
        ("Burn time (T0 -> 5 %)", fmt(r.burn_time, " s")),
        ("Action time (5 % -> 5 %)", fmt(r.action_time, " s")),
        ("Thrust centroid", fmt(r.thrust_centroid, " s")),
        ("Half impulse delivered at", fmt(r.t_half_impulse, " s")),
        ("", ""),
        ("- IMPULSE AND EFFICIENCY -", ""),
        ("Total impulse", fmt(r.total_impulse, " N.s", 2)),
        ("Impulse over the whole window", fmt(r.impulse_full_window, " N.s", 2)),
        ("Motor class", r.motor_class),
        ("Position within class", fmt(r.class_fill_pct, " %", 0)),
        ("Motor designation", r.motor_designation),
        ("Propellant mass", fmt(r.prop_mass_kg * 1000.0, " g", 1)),
        ("Mass source", r.prop_mass_source),
        ("Specific impulse Isp", fmt(r.isp, " s", 1)),
        ("Effective exhaust velocity", fmt(r.c_eff, " m/s", 0)),
        ("", ""),
        ("- MEASUREMENT QUALITY -", ""),
        ("Resting reading before the burn", fmt(r.baseline_n, " N", 3)),
        ("Resting reading after the burn", fmt(r.tail_n, " N", 3)),
        ("Baseline noise (RMS)", fmt(r.baseline_noise_rms, " N", 4)),
        ("Pre-roll samples not at rest", f"{r.baseline_rejected} of {r.baseline_samples}"),
        ("Integration window opens at", fmt(r.t_analysis_start, " s")),
        ("Baseline drift", fmt(r.baseline_drift_n_per_s, " N/s", 4)),
        ("Largest gap between samples", fmt(r.max_gap_s * 1000.0, " ms", 1)),
    ]
