"""
Vyhodnocení jednoho zážehu: časování zapálení, křivka tahu, impuls, Isp.

Konvence časové osy: t = 0 je okamžik, kdy firmware sepnul pyro kanál.
Vzorky před nulou jsou předzáznam (baseline).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from sf_protocol import G0, Burn

# procenta špičky, ke kterým se hlásí čas náběhu i doběhu
PCT_STEPS = (5, 10, 25, 50, 75, 90, 95, 100)


@dataclass
class Result:
    burn_id: int = 0
    slot: int = 0

    # --- baseline a šum ---
    baseline_n: float = 0.0
    baseline_noise_rms: float = 0.0
    baseline_drift_n_per_s: float = 0.0
    baseline_samples: int = 0
    tail_n: float = 0.0

    # --- časování zapálení ---
    t_first_motion: float = float("nan")
    t_ignition_detect: float = float("nan")   # 5 % špičky
    t_peak: float = float("nan")
    rise_10_90: float = float("nan")
    detection_threshold_n: float = 0.0
    t_pyro_off: float = float("nan")
    pyro_on_duration: float = float("nan")
    pct_times: dict = field(default_factory=dict)
    decay_times: dict = field(default_factory=dict)

    # --- křivka tahu ---
    peak_thrust: float = 0.0
    avg_thrust: float = 0.0
    avg_thrust_full: float = 0.0
    burn_time: float = 0.0        # T0 → pokles pod 5 % špičky
    action_time: float = 0.0      # 5 % náběh → 5 % doběh
    thrust_centroid: float = float("nan")
    t_half_impulse: float = float("nan")
    peak_to_avg: float = float("nan")
    max_slope_n_per_s: float = 0.0

    # --- impuls a Isp ---
    total_impulse: float = 0.0
    impulse_full_window: float = 0.0
    prop_mass_kg: float = 0.0
    prop_mass_source: str = "n/a"
    isp: float = float("nan")
    c_star_eff: float = float("nan")   # efektivní výtoková rychlost [m/s]
    motor_class: str = "?"
    motor_designation: str = "?"
    class_fill_pct: float = float("nan")

    # --- kvalita dat ---
    sample_rate_hz: float = 0.0
    sample_rate_std: float = 0.0
    max_gap_s: float = 0.0
    n_samples: int = 0
    warnings: list = field(default_factory=list)

    # --- pomocné řady pro grafy ---
    curve: pd.DataFrame = field(default_factory=pd.DataFrame)


def _trap(y: np.ndarray, x: np.ndarray) -> float:
    """np.trapezoid na NumPy 2, np.trapz na starších verzích."""
    fn = getattr(np, "trapezoid", None) or np.trapz
    return float(fn(y, x))


def _first_time_above(t: np.ndarray, y: np.ndarray, level: float,
                      start_idx: int = 0) -> float:
    """Lineárně interpolovaný čas prvního překročení úrovně."""
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
    """Písmenná třída podle NAR/TRA a naplnění třídy v procentech."""
    if total_impulse <= 0:
        return "?", float("nan")
    if total_impulse < 0.3125:
        return "<1/8A", float("nan")
    if total_impulse < 1.25:
        frac = {0.3125: "1/8A", 0.625: "1/4A"}
        for lo, name in sorted(frac.items(), reverse=True):
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
        r.warnings.append("zážeh neobsahuje žádné vzorky")
        return r

    t = df["t"].to_numpy(dtype=float)
    f = df["thrust"].to_numpy(dtype=float)
    r.n_samples = len(t)

    # ---- vzorkovací frekvence -------------------------------------------
    dt = np.diff(t)
    if dt.size:
        good = dt[dt > 0]
        if good.size:
            r.sample_rate_hz = float(1.0 / np.median(good))
            r.sample_rate_std = float(np.std(1.0 / good))
            r.max_gap_s = float(np.max(good))
            nominal = float(np.median(good))
            if r.max_gap_s > 3 * nominal:
                r.warnings.append(
                    f"největší mezera mezi vzorky {r.max_gap_s*1000:.0f} ms "
                    f"(nominál {nominal*1000:.1f} ms)")

    # ---- baseline z předzáznamu -----------------------------------------
    # Posledních 100 ms před T0 se vynechá: v té chvíli už bývá na stendu
    # slyšet obsluha, relé a vlastní sepnutí pyro okruhu.
    pre = df[(df["t"] < -0.10)]
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
        r.warnings.append("krátký předzáznam – baseline je nejistá")

    post = df[df["t"] >= 0]
    if post.empty:
        r.warnings.append("žádná data po T0")
        return r

    tp = post["t"].to_numpy(dtype=float)
    fp = post["thrust"].to_numpy(dtype=float)
    net = fp - r.baseline_n

    r.peak_thrust = float(np.max(net))
    if r.peak_thrust <= max(5 * r.baseline_noise_rms, 0.1):
        r.warnings.append("nenalezen žádný tah nad úrovní šumu – motor nezahořel?")
        r.curve = pd.DataFrame({"t": tp, "net": net})
        return r

    i_peak = int(np.argmax(net))
    r.t_peak = float(tp[i_peak])

    # ---- ocas: klidová hodnota po dohoření --------------------------------
    # Bere se konec záznamu, ale až po poklesu pod 2 % špičky a s odstupem
    # 250 ms, aby se do něj nedostal doznívající tah.
    #
    # Pokud záznam skončil dřív, než motor dohořel (useknutý zážeh), žádný
    # takový klidový úsek neexistuje. V tom případě se korekce úbytku
    # hmoty vypne úplně – vzít místo klidu vzorky z půlky hoření by
    # zkreslilo jak impuls, tak dopočtenou hmotnost paliva.
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
        r.warnings.append("záznam nekončí v klidu – korekce úbytku hmoty je vypnutá "
                          "a hmotnost paliva nelze z váhy odečíst")

    # ---- detekce prvního pohybu ------------------------------------------
    # Práh = max(N sigma šumu, procento špičky). Musí být překročen třemi
    # vzorky po sobě, jinak jeden zákmit posune celé měření zpoždění.
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

    # ---- prahy náběhu a doběhu -------------------------------------------
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

    # Když tah v posledním vzorku pořád přesahuje 5 % špičky, motor při
    # konci záznamu ještě hořel a doba hoření je jen dolní odhad.
    if math.isnan(t5_fall) or net[-1] >= 0.05 * r.peak_thrust:
        t5_fall = float(tp[-1])
        r.warnings.append("tah na konci záznamu neklesl pod 5 % špičky – motor ještě "
                          "hořel, doba hoření i impuls jsou jen dolní odhad")
    r.burn_time = float(t5_fall)
    r.action_time = float(t5_fall - t5_rise) if not math.isnan(t5_rise) else float(t5_fall)

    # ---- pyro kanál -------------------------------------------------------
    pyro = post[post["pyro"]]
    if len(pyro):
        r.t_pyro_off = float(pyro["t"].iloc[-1])
        r.pyro_on_duration = r.t_pyro_off - float(pyro["t"].iloc[0])

    # ---- korekce úbytku hmoty --------------------------------------------
    # Motor stojí na tenzometru, takže jak ubývá palivo, klesá i klidová
    # hodnota. Mezi T0 a koncem hoření se posun interpoluje lineárně
    # s vahou spáleného impulsu – to je blíž realitě než přímka v čase,
    # protože palivo neubývá rovnoměrně.
    mask = (tp >= 0) & (tp <= t5_fall)
    tb = tp[mask]
    fb = fp[mask]
    if tb.size < 3:
        r.warnings.append("příliš málo vzorků v okně hoření")
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

    # těžiště křivky a čas poloviny impulsu
    cum_b = np.concatenate([[0.0], np.cumsum(0.5 * (net_b[1:] + net_b[:-1]) * np.diff(tb))])
    if cum_b[-1] > 0:
        r.thrust_centroid = _trap(net_b * tb, tb) / cum_b[-1]
        r.t_half_impulse = float(np.interp(0.5 * cum_b[-1], cum_b, tb))

    if tb.size > 3:
        d = np.gradient(net_b, tb)
        r.max_slope_n_per_s = float(np.max(d))

    # ---- hmotnost paliva a Isp -------------------------------------------
    if prop_mass_g is not None and prop_mass_g > 0:
        r.prop_mass_kg = prop_mass_g / 1000.0
        r.prop_mass_source = "zadáno uživatelem"
    else:
        delta_n = r.baseline_n - r.tail_n
        r.prop_mass_kg = delta_n / G0
        r.prop_mass_source = "z posunu klidové hodnoty tenzometru"
        if delta_n <= 0:
            r.prop_mass_kg = 0.0
            if tail_valid:
                r.warnings.append("klidová hodnota po zážehu neklesla – úbytek hmoty "
                                  "nelze změřit, zadej hmotnost paliva ručně (--palivo)")
            else:
                r.warnings.append("hmotnost paliva zadej ručně přes --palivo, "
                                  "z tohoto záznamu ji spočítat nelze")
        else:
            noise_mass = 3 * r.baseline_noise_rms / G0
            if r.prop_mass_kg < noise_mass:
                r.warnings.append("změřený úbytek hmoty je na úrovni šumu – "
                                  "Isp ber jen orientačně")

    if r.prop_mass_kg > 0 and r.total_impulse > 0:
        r.isp = r.total_impulse / (r.prop_mass_kg * G0)
        r.c_star_eff = r.total_impulse / r.prop_mass_kg

    cls, fill = motor_class(r.total_impulse)
    r.motor_class = cls
    r.class_fill_pct = fill
    if r.avg_thrust > 0:
        r.motor_designation = f"{cls}{r.avg_thrust:.0f}"

    # ---- řada pro grafy ---------------------------------------------------
    r.curve = pd.DataFrame({
        "t": tp,
        "thrust": fp,
        "net": net,
        "baseline": np.interp(tp, tb, baseline_curve, left=r.baseline_n, right=r.tail_n),
    })

    # ---- kontroly ---------------------------------------------------------
    if not math.isnan(r.t_first_motion) and r.t_first_motion > 2.0:
        r.warnings.append(f"velmi dlouhé zpoždění zapálení ({r.t_first_motion:.2f} s) – "
                          "zkontroluj palník a zápalnou směs")
    if burn.df["sat"].any():
        r.warnings.append("tenzometr byl v saturaci – špičkový tah je podhodnocený")
    if r.sample_rate_hz and r.sample_rate_hz < 40:
        r.warnings.append(f"vzorkování jen {r.sample_rate_hz:.0f} Hz – "
                          "propoj RATE pin HX711 na VCC pro 80 SPS")
    return r


def summary_rows(burn: Burn, r: Result) -> list[tuple[str, str]]:
    """Dvojice (popis, hodnota) pro výpis do konzole i do Excelu."""
    def fmt(v, unit="", dec=3):
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            return "—"
        return f"{v:.{dec}f}{unit}"

    rows = [
        ("Zážeh ID", str(r.burn_id)),
        ("Slot v paměti", str(r.slot)),
        ("Firmware", burn.fw_version),
        ("Počet vzorků", str(r.n_samples)),
        ("Vzorkovací frekvence", fmt(r.sample_rate_hz, " Hz", 1)),
        ("", ""),
        ("— ČASOVÁNÍ ZAPÁLENÍ —", ""),
        ("T0 (povel k zapálení)", "0.000 s"),
        ("Práh detekce", fmt(r.detection_threshold_n, " N")),
        ("První pohyb (zpoždění zapálení)", fmt(r.t_first_motion, " s")),
        ("Dosažení 5 % špičky", fmt(r.pct_times.get(5), " s")),
        ("Dosažení 10 % špičky", fmt(r.pct_times.get(10), " s")),
        ("Dosažení 50 % špičky", fmt(r.pct_times.get(50), " s")),
        ("Dosažení 90 % špičky", fmt(r.pct_times.get(90), " s")),
        ("Čas špičky", fmt(r.t_peak, " s")),
        ("Náběh 10 → 90 %", fmt(r.rise_10_90, " s")),
        ("Max. strmost náběhu", fmt(r.max_slope_n_per_s, " N/s", 0)),
        ("Pyro kanál vypnut v", fmt(r.t_pyro_off, " s")),
        ("", ""),
        ("— KŘIVKA TAHU —", ""),
        ("Špičkový tah", fmt(r.peak_thrust, " N", 2)),
        ("Průměrný tah (action time)", fmt(r.avg_thrust, " N", 2)),
        ("Průměrný tah (od T0)", fmt(r.avg_thrust_full, " N", 2)),
        ("Poměr špička/průměr", fmt(r.peak_to_avg, "", 2)),
        ("Doba hoření (T0 → 5 %)", fmt(r.burn_time, " s")),
        ("Action time (5 % → 5 %)", fmt(r.action_time, " s")),
        ("Těžiště křivky tahu", fmt(r.thrust_centroid, " s")),
        ("Čas poloviny impulsu", fmt(r.t_half_impulse, " s")),
        ("", ""),
        ("— IMPULS A ÚČINNOST —", ""),
        ("Celkový impuls", fmt(r.total_impulse, " N·s", 2)),
        ("Impuls v celém okně", fmt(r.impulse_full_window, " N·s", 2)),
        ("Třída motoru", r.motor_class),
        ("Naplnění třídy", fmt(r.class_fill_pct, " %", 0)),
        ("Označení motoru", r.motor_designation),
        ("Hmotnost paliva", fmt(r.prop_mass_kg * 1000.0, " g", 1)),
        ("Zdroj hmotnosti", r.prop_mass_source),
        ("Specifický impuls Isp", fmt(r.isp, " s", 1)),
        ("Efektivní výtoková rychlost", fmt(r.c_star_eff, " m/s", 0)),
        ("", ""),
        ("— KVALITA MĚŘENÍ —", ""),
        ("Klidová hodnota před zážehem", fmt(r.baseline_n, " N", 3)),
        ("Klidová hodnota po zážehu", fmt(r.tail_n, " N", 3)),
        ("Šum baseline (RMS)", fmt(r.baseline_noise_rms, " N", 4)),
        ("Drift baseline", fmt(r.baseline_drift_n_per_s, " N/s", 4)),
        ("Největší mezera mezi vzorky", fmt(r.max_gap_s * 1000.0, " ms", 1)),
    ]
    return rows
