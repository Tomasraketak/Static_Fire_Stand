"""
Výstupy: grafy (PNG), surová data pro český Excel (CSV + XLSX) a textový souhrn.
"""
from __future__ import annotations

import math
import os
import re
import sys
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

# Na stroji bez displeje (CI, SSH) je nutný neinteraktivní backend,
# na Windows a macOS necháme matplotlib vybrat, ať funguje --ukaz.
if not (os.environ.get("DISPLAY") or sys.platform.startswith(("win", "darwin"))):
    matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import AutoMinorLocator  # noqa: E402

from sf_analysis import PCT_STEPS, Result, summary_rows  # noqa: E402
from sf_protocol import Burn  # noqa: E402

# --- paleta ------------------------------------------------------------
# Kategorické sloty se přiřazují v pevném pořadí, nikdy se necyklují.
C_THRUST = "#2a78d6"   # slot 1, modrá – naměřený tah
C_BASE = "#eb6834"     # slot 2, oranžová – korekce úbytku hmoty
C_IMPULSE = "#1baf7a"  # slot 3, akvamarín – kumulovaný impuls
C_CRIT = "#d03b3b"     # status critical – pyro / špička
C_MUTED = "#898781"
C_GRID = "#e1e0d9"
C_AXIS = "#c3c2b7"
C_INK = "#0b0b0b"
C_INK2 = "#52514e"
C_SURFACE = "#fcfcfb"

SERIES = [C_THRUST, C_BASE, C_IMPULSE, "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


def _style(ax, xlabel: str, ylabel: str, title: str | None = None) -> None:
    ax.set_facecolor(C_SURFACE)
    ax.grid(True, color=C_GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(C_AXIS)
        ax.spines[side].set_linewidth(1.0)
    ax.tick_params(colors=C_MUTED, labelsize=9, length=3)
    ax.xaxis.set_minor_locator(AutoMinorLocator(2))
    ax.set_xlabel(xlabel, color=C_INK2, fontsize=10)
    ax.set_ylabel(ylabel, color=C_INK2, fontsize=10)
    if title:
        ax.set_title(title, color=C_INK, fontsize=12, fontweight="600", loc="left", pad=10)


def _ok(v) -> bool:
    return v is not None and isinstance(v, float) and not math.isnan(v)


# ---------------------------------------------------------------------
#  Hlavní list s grafy
# ---------------------------------------------------------------------
def plot_burn(burn: Burn, r: Result, outdir: Path, show: bool = False) -> Path:
    df = burn.df
    fig = plt.figure(figsize=(13, 11), facecolor=C_SURFACE)
    gs = fig.add_gridspec(3, 2, height_ratios=[1.5, 1, 1], hspace=0.42, wspace=0.22)

    fig.suptitle(f"Statický zážeh #{r.burn_id}   ·   {r.motor_designation}   ·   "
                 f"{r.total_impulse:.1f} N·s",
                 color=C_INK, fontsize=17, fontweight="700", x=0.045, ha="left", y=0.985)

    # Záznam je typicky mnohem delší než hoření. Grafy se proto oříznou
    # kolem zajímavé části, jinak by křivka byla čárka na kraji obrázku.
    t_min = float(df["t"].min())
    t_max = float(df["t"].max())
    t_view = min(t_max, max(r.burn_time * 1.6, r.burn_time + 2.0)) if r.burn_time > 0 else t_max
    clipped = t_view < t_max - 0.5

    # ---- 1) celá křivka tahu -----------------------------------------
    ax = fig.add_subplot(gs[0, :])
    _style(ax, "Čas od povelu k zapálení [s]", "Tah [N]",
           "Křivka tahu" + (f"  (záznam pokračuje do {t_max:.0f} s)" if clipped else ""))

    if not r.curve.empty:
        span = df.loc[df["t"] <= t_view, "thrust"]
        lo, hi = float(span.min()), float(span.max())
        pad = max(0.05 * (hi - lo), 0.5)

        pyro = df[df["pyro"]]
        if len(pyro):
            ax.axvspan(float(pyro["t"].iloc[0]), float(pyro["t"].iloc[-1]),
                       color=C_CRIT, alpha=0.10, zorder=1,
                       label=f"pyro sepnuto ({float(pyro['t'].iloc[-1]):.2f} s)")

        if r.action_time > 0 and _ok(r.pct_times.get(5)):
            ax.axvspan(r.pct_times[5], r.burn_time, color=C_THRUST, alpha=0.07, zorder=1,
                       label=f"action time ({r.action_time:.2f} s)")

        ax.plot(df["t"], df["thrust"], color=C_THRUST, linewidth=2.0, zorder=4,
                label="naměřený tah")
        ax.plot(r.curve["t"], r.curve["baseline"], color=C_BASE, linewidth=2.0,
                linestyle=(0, (4, 3)), zorder=5, label="korekce úbytku hmoty")

        ax.axvline(0, color=C_MUTED, linewidth=1.2, linestyle=(0, (2, 3)), zorder=3)
        ax.annotate("T0", xy=(0, lo - pad), xytext=(4, 6), textcoords="offset points",
                    color=C_MUTED, fontsize=9, va="bottom")

        if _ok(r.t_peak):
            ax.plot([r.t_peak], [r.baseline_n + r.peak_thrust], "o", markersize=9,
                    color=C_CRIT, markeredgecolor=C_SURFACE, markeredgewidth=2, zorder=6)
            ax.annotate(f"špička {r.peak_thrust:.1f} N  @ {r.t_peak:.2f} s",
                        xy=(r.t_peak, r.baseline_n + r.peak_thrust),
                        xytext=(12, 2), textcoords="offset points",
                        color=C_INK, fontsize=10, fontweight="600", va="center")
        if _ok(r.t_first_motion):
            ax.plot([r.t_first_motion], [r.baseline_n + r.detection_threshold_n], "o",
                    markersize=8, color=C_IMPULSE, markeredgecolor=C_SURFACE,
                    markeredgewidth=2, zorder=6)
            ax.annotate(f"první pohyb  {r.t_first_motion*1000:.0f} ms",
                        xy=(r.t_first_motion, r.baseline_n + r.detection_threshold_n),
                        xytext=(10, 12), textcoords="offset points",
                        color=C_INK2, fontsize=9)

        ax.set_xlim(t_min, t_view)
        ax.set_ylim(lo - pad, hi + pad * 2.2)
        leg = ax.legend(loc="upper right", frameon=False, fontsize=9, labelcolor=C_INK2)
        for txt in leg.get_texts():
            txt.set_color(C_INK2)

    # ---- 2) detail zapálení -------------------------------------------
    ax = fig.add_subplot(gs[1, 0])
    _style(ax, "Čas od povelu [s]", "Tah nad klidem [N]", "Detail zapálení")
    zoom_hi = max(0.6, (r.t_peak * 1.7) if _ok(r.t_peak) else 0.6)
    z = df[(df["t"] >= -0.25) & (df["t"] <= zoom_hi)]
    if len(z):
        ax.plot(z["t"], z["thrust"] - r.baseline_n, color=C_THRUST, linewidth=2.0,
                marker="o", markersize=3.2, markeredgewidth=0, zorder=4)
        ax.axhline(r.detection_threshold_n, color=C_MUTED, linewidth=1.0,
                   linestyle=(0, (2, 3)), zorder=3)
        ax.axvline(0, color=C_CRIT, linewidth=1.4, zorder=3)
        ax.annotate("povel", xy=(0, ax.get_ylim()[0]), xytext=(4, 6),
                    textcoords="offset points", color=C_CRIT, fontsize=9, va="bottom")
        for pct in (10, 50, 90):
            tv = r.pct_times.get(pct)
            if _ok(tv) and tv <= zoom_hi:
                ax.axvline(tv, color=C_MUTED, linewidth=0.9, linestyle=(0, (1, 3)), zorder=2)
        if _ok(r.t_first_motion):
            ax.axvline(r.t_first_motion, color=C_IMPULSE, linewidth=1.6, zorder=3)

        # Prahy leží pár desítek milisekund od sebe, popisky u čar by se
        # překrývaly – jdou proto do jednoho bloku v rohu.
        lines = []
        if _ok(r.t_first_motion):
            lines.append(f"první pohyb   {r.t_first_motion*1000:7.0f} ms")
        for pct in (10, 50, 90):
            tv = r.pct_times.get(pct)
            if _ok(tv):
                lines.append(f"{pct:>3} % špičky {tv*1000:7.0f} ms")
        if _ok(r.rise_10_90):
            lines.append(f"náběh 10–90 % {r.rise_10_90*1000:6.0f} ms")
        # Blok se posadí na tu stranu, kde není křivka: při dlouhém
        # zpoždění zapálení je náběh vpravo, při krátkém vlevo.
        x0, x1 = -0.25, zoom_hi
        peak_frac = ((r.t_peak - x0) / (x1 - x0)) if _ok(r.t_peak) else 0.5
        if peak_frac > 0.5:
            bx, ha = 0.025, "left"
        else:
            bx, ha = 0.975, "right"
        ax.text(bx, 0.97, "\n".join(lines), transform=ax.transAxes,
                ha=ha, va="top", fontsize=9, color=C_INK2, family="monospace",
                bbox=dict(boxstyle="round,pad=0.5", facecolor=C_SURFACE,
                          edgecolor=C_GRID))

    # ---- 3) kumulovaný impuls -----------------------------------------
    ax = fig.add_subplot(gs[1, 1])
    _style(ax, "Čas od povelu [s]", "Impuls [N·s]", "Kumulovaný impuls")
    if not r.curve.empty:
        c = r.curve
        net = np.maximum(c["thrust"].to_numpy() - c["baseline"].to_numpy(), 0.0)
        t = c["t"].to_numpy()
        cum = np.concatenate([[0.0], np.cumsum(0.5 * (net[1:] + net[:-1]) * np.diff(t))])
        ax.plot(t, cum, color=C_IMPULSE, linewidth=2.0, zorder=4)
        ax.fill_between(t, 0, cum, color=C_IMPULSE, alpha=0.10, zorder=2)
        ax.set_xlim(0, t_view)
        if _ok(r.t_half_impulse):
            ax.plot([r.t_half_impulse], [np.interp(r.t_half_impulse, t, cum)], "o",
                    markersize=8, color=C_INK, markeredgecolor=C_SURFACE,
                    markeredgewidth=2, zorder=5)
            ax.annotate(f"50 % impulsu\n{r.t_half_impulse:.2f} s",
                        xy=(r.t_half_impulse, np.interp(r.t_half_impulse, t, cum)),
                        xytext=(8, -22), textcoords="offset points",
                        color=C_INK2, fontsize=9)
        ax.annotate(f"celkem {r.total_impulse:.1f} N·s", xy=(t_view, cum[-1]),
                    xytext=(-6, -14), textcoords="offset points", ha="right",
                    color=C_INK, fontsize=10, fontweight="600")

    # ---- 4) prahy náběhu ----------------------------------------------
    ax = fig.add_subplot(gs[2, 0])
    _style(ax, "Čas od povelu [ms]", "Podíl špičkového tahu [%]", "Náběh tahu")
    pcts = [p for p in PCT_STEPS if _ok(r.pct_times.get(p))]
    if pcts:
        xs = [r.pct_times[p] * 1000.0 for p in pcts]
        ax.plot(xs, pcts, color=C_THRUST, linewidth=2.0, marker="o", markersize=7,
                markeredgecolor=C_SURFACE, markeredgewidth=2, zorder=4)
        # popisky jen u vybraných bodů, jinak by se slily do sebe
        for x, p in zip(xs, pcts):
            if p in (5, 25, 75, 100):
                ax.annotate(f"{x:.0f} ms", xy=(x, p), xytext=(8, -3),
                            textcoords="offset points", color=C_INK2, fontsize=9)
        ax.set_ylim(0, 108)
        ax.set_xlim(min(xs) - 0.14 * (max(xs) - min(xs) + 1),
                    max(xs) + 0.30 * (max(xs) - min(xs) + 1))

    # ---- 5) kvalita dat -----------------------------------------------
    ax = fig.add_subplot(gs[2, 1])
    _style(ax, "Čas od povelu [s]", "Interval mezi vzorky [ms]", "Kvalita záznamu")
    if len(df) > 2:
        dt = np.diff(df["t"].to_numpy()) * 1000.0
        ax.plot(df["t"].to_numpy()[1:], dt, color=C_MUTED, linewidth=1.0, zorder=3)
        nominal = float(np.median(dt))
        ax.axhline(nominal, color=C_THRUST, linewidth=1.6, zorder=4)
        ax.annotate(f"medián {nominal:.1f} ms  ({1000/nominal:.0f} Hz)",
                    xy=(df["t"].iloc[-1], nominal), xytext=(-6, 6),
                    textcoords="offset points", ha="right", color=C_INK2, fontsize=9)
        ax.set_ylim(0, max(nominal * 4, float(np.max(dt)) * 1.1))

    # ---- varování patičkou --------------------------------------------
    notes = list(r.warnings) + burn.quality.problems()
    if notes:
        fig.text(0.045, 0.012, "⚠  " + "   ·   ".join(notes[:4]),
                 color=C_CRIT, fontsize=9, ha="left", va="bottom")

    fig.subplots_adjust(top=0.905, bottom=0.075, left=0.065, right=0.975)
    path = outdir / f"zazeh_{r.burn_id:03d}_grafy.png"
    fig.savefig(path, dpi=160, facecolor=C_SURFACE)
    if show:
        plt.show()
    plt.close(fig)
    return path


def plot_comparison(pairs: list[tuple[Burn, Result]], outdir: Path) -> Path | None:
    """Přeložené křivky všech stažených zážehů přes sebe."""
    pairs = [(b, r) for b, r in pairs if not r.curve.empty]
    if len(pairs) < 2:
        return None
    fig, ax = plt.subplots(figsize=(11, 6), facecolor=C_SURFACE)
    _style(ax, "Čas od povelu k zapálení [s]", "Tah nad klidem [N]",
           "Porovnání zážehů")
    for i, (b, r) in enumerate(pairs[:8]):
        color = SERIES[i % len(SERIES)]
        ax.plot(r.curve["t"], r.curve["thrust"] - r.curve["baseline"],
                color=color, linewidth=2.0, zorder=4,
                label=f"#{r.burn_id} · {r.motor_designation} · {r.total_impulse:.1f} N·s")
    leg = ax.legend(loc="upper right", frameon=False, fontsize=9)
    for txt in leg.get_texts():
        txt.set_color(C_INK2)
    fig.tight_layout()
    path = outdir / "porovnani_zazehu.png"
    fig.savefig(path, dpi=160, facecolor=C_SURFACE)
    plt.close(fig)
    return path


# ---------------------------------------------------------------------
#  Export dat
# ---------------------------------------------------------------------
CZ_COLUMNS = {
    "t": "Čas [s]",
    "t_ms": "Čas [ms]",
    "thrust": "Tah [N]",
    "net": "Tah nad klidem [N]",
    "raw": "Surová hodnota ADC",
    "pyro": "Pyro sepnuto",
    "cont": "Kontinuita",
    "sat": "Saturace ADC",
}


def _export_frame(burn: Burn, r: Result) -> pd.DataFrame:
    df = burn.df.copy()
    df["net"] = df["thrust"] - r.baseline_n
    out = df[["t", "t_ms", "thrust", "net", "raw", "pyro", "cont", "sat"]].copy()
    out["t"] = out["t"].round(6)
    out["t_ms"] = out["t_ms"].round(3)
    for col in ("pyro", "cont", "sat"):
        out[col] = out[col].map({True: "ANO", False: "NE"})
    return out.rename(columns=CZ_COLUMNS)


def export_csv_cz(burn: Burn, r: Result, outdir: Path) -> Path:
    """CSV pro české Excelovské prostředí: středník, desetinná čárka, BOM.

    První řádek `sep=;` říká Excelu, jak soubor rozdělit, nezávisle
    na tom, co má uživatel nastavené v systému.
    """
    out = _export_frame(burn, r)
    # Každý sloupec svůj počet desetinných míst – šest číslic za čárkou
    # u milisekund je jen šum, u sekund je naopak potřeba.
    decimals = {CZ_COLUMNS["t"]: 6, CZ_COLUMNS["t_ms"]: 3,
                CZ_COLUMNS["thrust"]: 4, CZ_COLUMNS["net"]: 4}
    for col, dec in decimals.items():
        if col in out.columns:
            out[col] = out[col].map(lambda v, d=dec: f"{v:.{d}f}".replace(".", ","))

    path = outdir / f"zazeh_{r.burn_id:03d}_data.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("sep=;\n")
        out.to_csv(fh, sep=";", index=False, lineterminator="\r\n")
    return path


def export_summary_csv_cz(rows: list[tuple[str, str]], r: Result, outdir: Path) -> Path:
    path = outdir / f"zazeh_{r.burn_id:03d}_souhrn.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("sep=;\n")
        fh.write("Veličina;Hodnota\r\n")
        for k, v in rows:
            # desetinná tečka na čárku, ale jen mezi číslicemi a jen
            # u hodnot, které jsou opravdu číselné (verze firmwaru ne)
            if k != "Firmware":
                v = re.sub(r"(?<=\d)\.(?=\d)", ",", v)
            fh.write(f"{k};{v}\r\n")
    return path


def export_xlsx(burn: Burn, r: Result, outdir: Path) -> Path | None:
    """Sešit .xlsx: souhrn, surová data a nativní graf.

    Čísla jsou uložená jako čísla, takže Excel je zobrazí podle
    lokálního nastavení – s desetinnou čárkou, bez ručních úprav.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.chart import LineChart, Reference
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return None

    wb = Workbook()

    # --- Souhrn -------------------------------------------------------
    ws = wb.active
    ws.title = "Souhrn"
    ws["A1"] = f"Statický zážeh č. {r.burn_id}"
    ws["A1"].font = Font(size=15, bold=True)
    ws["A2"] = r.motor_designation
    ws["A2"].font = Font(size=12, color="52514E")
    row = 4
    for key, val in summary_rows(burn, r):
        if not key:
            row += 1
            continue
        if key.startswith("—"):
            ws.cell(row=row, column=1, value=key.strip("— ")).font = Font(bold=True, size=11)
            ws.cell(row=row, column=1).fill = PatternFill("solid", fgColor="F0EFEC")
            ws.cell(row=row, column=2).fill = PatternFill("solid", fgColor="F0EFEC")
        else:
            ws.cell(row=row, column=1, value=key)
            ws.cell(row=row, column=2, value=val).alignment = Alignment(horizontal="right")
        row += 1

    notes = list(r.warnings) + burn.quality.problems()
    if notes:
        row += 1
        ws.cell(row=row, column=1, value="UPOZORNĚNÍ").font = Font(bold=True, color="D03B3B")
        for note in notes:
            row += 1
            ws.cell(row=row, column=1, value="• " + note)
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 26

    # --- Data ---------------------------------------------------------
    wsd = wb.create_sheet("Data")
    out = _export_frame(burn, r)
    wsd.append(list(out.columns))
    for cell in wsd[1]:
        cell.font = Font(bold=True)
    for rec in out.itertuples(index=False):
        wsd.append(list(rec))
    for col_idx, name in enumerate(out.columns, start=1):
        letter = get_column_letter(col_idx)
        wsd.column_dimensions[letter].width = max(12, len(name) + 2)
        if "[" in name:
            fmt = "0.000" if "N]" in name or "s]" in name else "0.0"
            for cell in wsd[letter][1:]:
                cell.number_format = fmt
    wsd.freeze_panes = "A2"

    # nativní graf v Excelu, ať se dá s daty pracovat dál
    n = len(out) + 1
    chart = LineChart()
    chart.title = f"Křivka tahu – zážeh {r.burn_id}"
    chart.y_axis.title = "Tah [N]"
    chart.x_axis.title = "Čas [s]"
    chart.height, chart.width = 10, 26
    chart.style = 2
    data = Reference(wsd, min_col=3, min_row=1, max_row=n)
    cats = Reference(wsd, min_col=1, min_row=2, max_row=n)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    for s in chart.series:
        s.smooth = False
    wsd.add_chart(chart, "K2")

    # --- Náběh --------------------------------------------------------
    wsp = wb.create_sheet("Náběh a doběh")
    wsp.append(["Podíl špičky [%]", "Úroveň [N]", "Náběh – čas [s]", "Doběh – čas [s]"])
    for cell in wsp[1]:
        cell.font = Font(bold=True)
    for pct in PCT_STEPS:
        rise = r.pct_times.get(pct)
        fall = r.decay_times.get(pct)
        wsp.append([pct, r.peak_thrust * pct / 100.0,
                    None if not _ok(rise) else rise,
                    None if not _ok(fall) else fall])
    for letter, width, fmt in (("A", 18, "0"), ("B", 14, "0.00"),
                               ("C", 16, "0.0000"), ("D", 16, "0.0000")):
        wsp.column_dimensions[letter].width = width
        for cell in wsp[letter][1:]:
            cell.number_format = fmt

    path = outdir / f"zazeh_{r.burn_id:03d}_data.xlsx"
    wb.save(path)
    return path


def export_overview_csv(pairs: list[tuple[Burn, Result]], outdir: Path) -> Path:
    """Jeden řádek na zážeh – přehled pro porovnávání šarží."""
    recs = []
    for b, r in pairs:
        recs.append({
            "Zážeh": r.burn_id,
            "Označení": r.motor_designation,
            "Třída": r.motor_class,
            "Špičkový tah [N]": r.peak_thrust,
            "Průměrný tah [N]": r.avg_thrust,
            "Celkový impuls [N·s]": r.total_impulse,
            "Doba hoření [s]": r.burn_time,
            "Action time [s]": r.action_time,
            "Zpoždění zapálení [ms]": r.t_first_motion * 1000.0 if _ok(r.t_first_motion) else None,
            "Náběh 10–90 % [ms]": r.rise_10_90 * 1000.0 if _ok(r.rise_10_90) else None,
            "Hmotnost paliva [g]": r.prop_mass_kg * 1000.0,
            "Isp [s]": r.isp if _ok(r.isp) else None,
            "Vzorků": r.n_samples,
            "Frekvence [Hz]": r.sample_rate_hz,
            "Šum baseline [N]": r.baseline_noise_rms,
        })
    df = pd.DataFrame(recs)
    path = outdir / "prehled_zazehu.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("sep=;\n")
        df.to_csv(fh, sep=";", decimal=",", index=False,
                  float_format="%.4f", lineterminator="\r\n")
    return path
