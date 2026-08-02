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


@dataclass
class Result:
    burn_id: int = 0
    slot: int = 0

    # --- baseline and noise ---
    baseline_n: float = 0.0
    baseline_noise_rms: float = 0.0
    baseline_drift_n_per_s: float = 0.0
    baseline_samples: int = 0
    tail_n: float = 0.0

    # --- ignition timing ---
    t_first_motion: float = float("nan")
    t_ignition_detect: float = float("nan")   # 5 % of peak
    t_peak: float = float("nan")
    rise_10_90: float = float("nan")
    detection_threshold_n: float = 0.0
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
        r.baseline_n = float(pre["thrust"].mean())
        r.baseline_noise_rms = float(pre["thrust"].std(ddof=1))
        r.baseline_samples = len(pre)
        if len(pre) >= 20:
            slope = np.polyfit(pre["t"].to_numpy(), pre["thrust"].to_numpy(), 1)[0]
            r.baseline_drift_n_per_s = float(slope)
    else:
        pre0 = df[df["t"] < 0]
        r.baseline_n = float(pre0["thrust"].mean()) if len(pre0) else float(f[0])
        r.baseline_noise_rms = float(pre0["thrust"].std(ddof=1)) if len(pre0) > 1 else 0.0
        r.baseline_samples = len(pre0)
        r.warnings.append("very short pre-roll - the baseline is uncertain")

    post = df[df["t"] >= 0]
    if post.empty:
        r.warnings.append("no data after T0")
        return r

    tp = post["t"].to_numpy(dtype=float)
    fp = post["thrust"].to_numpy(dtype=float)
    net = fp - r.baseline_n

    r.peak_thrust = float(np.max(net))
    if r.peak_thrust <= max(5 * r.baseline_noise_rms, 0.1):
        r.warnings.append("no thrust found above the noise floor - did the motor light?")
        r.curve = pd.DataFrame({"t": tp, "net": net})
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
    above = net >= thr
    run = np.convolve(above.astype(int), np.ones(3, dtype=int), mode="valid")
    hit = np.nonzero(run == 3)[0]
    if hit.size:
        i0 = int(hit[0])
        r.t_first_motion = _first_time_above(tp, net, thr, start_idx=max(0, i0 - 2))
    else:
        r.t_first_motion = _first_time_above(tp, net, thr)

    # ---- rise and decay thresholds ---------------------------------------
    for pct in PCT_STEPS:
        lvl = r.peak_thrust * pct / 100.0
        r.pct_times[pct] = _first_time_above(tp, net, lvl)
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
    mask = (tp >= 0) & (tp <= t5_fall)
    tb = tp[mask]
    fb = fp[mask]
    if tb.size < 3:
        r.warnings.append("too few samples inside the burn window")
        r.curve = pd.DataFrame({"t": tp, "net": net})
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
    r.curve = pd.DataFrame({
        "t": tp,
        "thrust": fp,
        "net": net,
        "baseline": np.interp(tp, tb, baseline_curve, left=r.baseline_n, right=r.tail_n),
    })

    # ---- sanity checks ------------------------------------------------------
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
        ("Baseline drift", fmt(r.baseline_drift_n_per_s, " N/s", 4)),
        ("Largest gap between samples", fmt(r.max_gap_s * 1000.0, " ms", 1)),
    ]
