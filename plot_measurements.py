import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import fmean, pstdev

import matplotlib.pyplot as plt

from tsitw.spectrum import CHANNELS, WAVELENGTHS

PROJECT_DIR = Path(__file__).resolve().parent
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
LINESTYLES = ("-", "--", ":", "-.")
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"
METRICS = {
    "raw": ("", "Raw counts"),
    "refl": ("refl_", "Reflectance (relative to white reference)"),
    "vs_water": ("vs_water_", "Signal relative to clean water"),
}
DIRECT_LABEL_LIMIT = 4
X = [WAVELENGTHS[c] for c in CHANNELS]


def read_rows(folder):
    with open(folder / "samples.csv", newline="") as fh:
        return list(csv.DictReader(fh))


def load_measurement(folder, prefix):
    rows = read_rows(folder)
    if not rows or f"{prefix}{CHANNELS[0]}" not in rows[0]:
        return None
    missing = ("", "-1") if prefix == "" else ("",)
    means, stds = [], []
    for channel in CHANNELS:
        values = [float(r[prefix + channel]) for r in rows if r[prefix + channel] not in missing]
        means.append(fmean(values) if values else math.nan)
        stds.append(pstdev(values) if values else math.nan)
    meta = folder / "meta.json"
    label = json.loads(meta.read_text())["type"] if meta.exists() else folder.name[16:]
    return {"folder": folder, "label": label, "started": folder.name[:15], "mean": means, "std": stds, "n": len(rows)}


def disambiguate(measurements):
    counts = Counter(m["label"] for m in measurements)
    for m in measurements:
        if counts[m["label"]] > 1:
            started = datetime.strptime(m["started"], "%Y%m%d-%H%M%S")
            m["label"] = f"{m['label']} ({started:%d.%m %H:%M})"


def style_axes(ax, ylabel):
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=8)
    ax.set_xticks(X, [f"{nm}\n{c}" for c, nm in zip(CHANNELS, X)])
    ax.set_xlabel("Wavelength (nm) / channel", color=TEXT_SECONDARY, fontsize=9)
    ax.set_ylabel(ylabel, color=TEXT_SECONDARY, fontsize=9)


def plot_spectrum(ax, measurement, color, linestyle="-", band=True):
    mean, std = measurement["mean"], measurement["std"]
    if band:
        ax.fill_between(
            X, [m - s for m, s in zip(mean, std)], [m + s for m, s in zip(mean, std)],
            color=color, alpha=0.15, linewidth=0,
        )
    ax.plot(
        X, mean, color=color, linestyle=linestyle, linewidth=2, marker="o", markersize=5,
        markeredgecolor=SURFACE, markeredgewidth=1.5, label=measurement["label"],
    )


def save(fig, path):
    fig.savefig(path, dpi=150, facecolor=SURFACE, bbox_inches="tight")
    print(f"Saved {path}")


def plot_single(measurement, ylabel):
    fig, ax = plt.subplots(figsize=(9, 4.5), facecolor=SURFACE)
    style_axes(ax, ylabel)
    plot_spectrum(ax, measurement, SERIES[0])
    title = f"{measurement['label']}  (n={measurement['n']}, mean ± std)"
    ax.set_title(title, color=TEXT, fontsize=11, loc="left")
    save(fig, measurement["folder"] / "spectrum.png")
    return fig


def plot_grid(measurements, ylabel, path):
    fig, axes = grid_axes(len(measurements))
    for ax, measurement in zip(axes.flat, measurements):
        style_axes(ax, ylabel)
        plot_spectrum(ax, measurement, SERIES[0])
        ax.set_title(f"{measurement['label']}  (n={measurement['n']})", color=TEXT, fontsize=10, loc="left")
    for ax in list(axes.flat)[len(measurements):]:
        ax.set_visible(False)
    for ax in axes.flat:
        ax.label_outer()
    fig.suptitle("Mean spectrum per measurement (± std)", color=TEXT, fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    save(fig, path)
    return fig


def grid_axes(count):
    cols = min(3, count)
    rows = math.ceil(count / cols)
    return plt.subplots(
        rows, cols, figsize=(5.2 * cols, 3.6 * rows), sharex=True, sharey=True, squeeze=False, facecolor=SURFACE,
    )


def plot_comparison(measurements, ylabel, path):
    fig, ax = plt.subplots(figsize=(12, 6), facecolor=SURFACE)
    style_axes(ax, ylabel)
    labelled = len(measurements) <= DIRECT_LABEL_LIMIT
    for index, measurement in enumerate(measurements):
        plot_series(ax, index, measurement, labelled)
    ax.set_title("Mean spectra compared", color=TEXT, fontsize=12, loc="left")
    ax.legend(
        loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=min(4, len(measurements)),
        frameon=False, fontsize=9, labelcolor=TEXT,
    )
    save(fig, path)
    return fig


def plot_series(ax, index, measurement, labelled):
    color = SERIES[index % len(SERIES)]
    linestyle = LINESTYLES[(index // len(SERIES)) % len(LINESTYLES)]
    plot_spectrum(ax, measurement, color, linestyle, band=labelled)
    if labelled:
        ax.annotate(
            measurement["label"], (X[-1], measurement["mean"][-1]), xytext=(8, 0),
            textcoords="offset points", va="center", fontsize=9, color=TEXT,
        )


def parse_args():
    parser = argparse.ArgumentParser(description="Plot the mean spectrum of each measurement, separately and compared.")
    parser.add_argument("filters", nargs="*", help="only plot measurement folders whose name contains one of these")
    parser.add_argument(
        "--metric", choices=("auto",) + tuple(METRICS), default="auto",
        help="raw counts, reflectance or relative to clean water (default: reflectance if every measurement has it)",
    )
    parser.add_argument("--data-dir", type=Path, default=PROJECT_DIR, help="folder that contains measurements/")
    parser.add_argument("--show", action="store_true", help="open the plots in windows as well as saving them")
    return parser.parse_args()


def find_folders(data_dir, filters):
    folders = sorted(p for p in (data_dir / "measurements").glob("*") if (p / "samples.csv").exists())
    if filters:
        folders = [f for f in folders if any(s.lower() in f.name.lower() for s in filters)]
    if not folders:
        sys.exit("No measurements found.")
    return folders


def choose_metric(folders, metric):
    if metric != "auto":
        return metric
    return "refl" if all(load_measurement(f, "refl_") for f in folders) else "raw"


def load_all(folders, prefix, metric):
    measurements = []
    for folder in folders:
        measurement = load_measurement(folder, prefix)
        if measurement:
            measurements.append(measurement)
        else:
            print(f"Skipping {folder.name}: no {metric} values")
    if not measurements:
        sys.exit(f"No measurements have {metric} values.")
    return measurements


def write_plots(measurements, ylabel, out, show):
    for measurement in measurements:
        fig = plot_single(measurement, ylabel)
        if not show:
            plt.close(fig)
    plot_grid(measurements, ylabel, out / "individual.png")
    plot_comparison(measurements, ylabel, out / "comparison.png")
    if show:
        plt.show()


def main():
    args = parse_args()
    folders = find_folders(args.data_dir, args.filters)
    metric = choose_metric(folders, args.metric)
    prefix, ylabel = METRICS[metric]
    measurements = load_all(folders, prefix, metric)
    disambiguate(measurements)
    out = args.data_dir / "plots" / f"{datetime.now():%Y%m%d-%H%M%S}-{metric}"
    out.mkdir(parents=True)
    write_plots(measurements, ylabel, out, args.show)


if __name__ == "__main__":
    main()
