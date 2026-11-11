import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import datetime
from functools import cache
from pathlib import Path
from statistics import fmean, pstdev, stdev

import matplotlib.pyplot as plt

from tsitw.spectrum import CHANNELS, MODE_LABELS, MODES, WAVELENGTHS

PROJECT_DIR = Path(__file__).resolve().parent
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
LINESTYLES = ("-", "--", ":", "-.")
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"
METRICS = {
    "raw": ("", "Raw counts"),
    "net": ("net_", "Counts above the LEDs-off reading"),
    "refl": ("refl_", "Reflectance (white paper = 1)"),
    "excess": ("excess_", "Reflectance above clean water"),
}
DIRECT_LABEL_LIMIT = 4
X = [WAVELENGTHS[c] for c in CHANNELS]


def data_files(folder):
    runs = sorted(folder.glob("run-*.csv"))
    legacy = folder / "samples.csv"
    return runs or ([legacy] if legacy.exists() else [])


def measurement_folders(data_dir):
    return sorted(p for p in (data_dir / "measurements").glob("*") if data_files(p))


def read_runs(folder):
    runs = []
    for path in data_files(folder):
        with open(path, newline="") as fh:
            runs.append(list(csv.DictReader(fh)))
    return runs


def row_mode(row):
    return row.get("mode") or row.get("combination")


def has_metric(folder, prefix):
    with open(data_files(folder)[0], newline="") as fh:
        return f"{prefix}{CHANNELS[0]}" in next(csv.reader(fh), [])


def modes_in(folder):
    present = {row_mode(row) for run in read_runs(folder) for row in run}
    return [mode for mode in MODES if mode in present]


def label_of(folder):
    meta = folder / "meta.json"
    measurement_type = json.loads(meta.read_text())["type"] if meta.exists() else folder.name[16:]
    return custom_labels(folder.parent.parent).get(measurement_type, measurement_type)


@cache
def custom_labels(data_dir):
    path = data_dir / "labels.json"
    return json.loads(path.read_text()) if path.exists() else {}


def load_measurement(folder, prefix, mode):
    if not has_metric(folder, prefix):
        return None
    runs = [[row for row in run if row_mode(row) == mode] for run in read_runs(folder)]
    runs = [run for run in runs if run]
    if not runs:
        return None
    means, stds = [], []
    for channel in CHANNELS:
        mean, std = channel_summary([run_values(run, prefix + channel, prefix) for run in runs])
        means.append(mean)
        stds.append(std)
    return {
        "folder": folder, "label": label_of(folder), "started": folder.name[:15], "mode": mode,
        "mean": means, "std": stds, "runs": len(runs), "readings": sum(len(run) for run in runs),
        "n": len(runs) if len(runs) > 1 else sum(len(run) for run in runs),
    }


def run_values(run, column, prefix):
    missing = ("", "-1", "-1.0") if prefix == "" else ("",)
    return [float(row[column]) for row in run if row[column] not in missing]


def channel_summary(runs):
    runs = [values for values in runs if values]
    if not runs:
        return math.nan, math.nan
    if len(runs) == 1:
        return fmean(runs[0]), pstdev(runs[0])
    run_means = [fmean(values) for values in runs]
    return fmean(run_means), stdev(run_means)


def describe_n(measurement):
    if measurement["runs"] > 1:
        return f"{measurement['runs']} runs, ± std between runs"
    return f"{measurement['readings']} readings, ± std"


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


def plot_single(by_mode, ylabel):
    fig, axes = grid_axes(len(by_mode), cols=2, sharey=False)
    for ax, measurement in zip(axes.flat, by_mode):
        style_axes(ax, ylabel)
        plot_spectrum(ax, measurement, SERIES[0])
        ax.set_title(f"{MODE_LABELS[measurement['mode']]} LEDs", color=TEXT, fontsize=10, loc="left")
    hide_unused(axes, len(by_mode))
    first = by_mode[0]
    fig.suptitle(f"{first['label']}  ({describe_n(first)})", color=TEXT, fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    save(fig, first["folder"] / "spectrum.png")
    return fig


def plot_grid(measurements, ylabel, path):
    fig, axes = grid_axes(len(measurements))
    for ax, measurement in zip(axes.flat, measurements):
        style_axes(ax, ylabel)
        plot_spectrum(ax, measurement, SERIES[0])
        title = f"{measurement['label']}  ({describe_n(measurement)})"
        ax.set_title(title, color=TEXT, fontsize=10, loc="left")
    hide_unused(axes, len(measurements))
    for ax in axes.flat:
        ax.label_outer()
    mode = MODE_LABELS[measurements[0]["mode"]]
    fig.suptitle(f"Mean spectrum per measurement, {mode} LEDs", color=TEXT, fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    save(fig, path)
    return fig


def grid_axes(count, cols=3, sharey=True):
    cols = min(cols, count)
    rows = math.ceil(count / cols)
    return plt.subplots(
        rows, cols, figsize=(5.2 * cols, 3.6 * rows), sharex=True, sharey=sharey, squeeze=False, facecolor=SURFACE,
    )


def hide_unused(axes, used):
    for ax in list(axes.flat)[used:]:
        ax.set_visible(False)


def plot_comparison(measurements, ylabel, path):
    fig, ax = plt.subplots(figsize=(12, 6), facecolor=SURFACE)
    style_axes(ax, ylabel)
    labelled = len(measurements) <= DIRECT_LABEL_LIMIT
    for index, measurement in enumerate(measurements):
        plot_series(ax, index, measurement, labelled)
    mode = MODE_LABELS[measurements[0]["mode"]]
    ax.set_title(f"Mean spectra compared, {mode} LEDs", color=TEXT, fontsize=12, loc="left")
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
        help="raw, net counts, reflectance or reflectance above clean water (default: reflectance if every "
        "measurement has it)",
    )
    parser.add_argument("--modes", nargs="+", choices=MODES, help="lighting modes to plot (default: all recorded)")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_DIR, help="folder that contains measurements/")
    parser.add_argument("--show", action="store_true", help="open the plots in windows as well as saving them")
    return parser.parse_args()


def find_folders(data_dir, filters):
    folders = measurement_folders(data_dir)
    if filters:
        folders = [f for f in folders if any(s.lower() in f.name.lower() for s in filters)]
    if not folders:
        sys.exit("No measurements found.")
    return folders


def choose_metric(folders, metric):
    if metric != "auto":
        return metric
    return "refl" if all(has_metric(f, "refl_") for f in folders) else "raw"


def load_all(folders, prefix, metric, modes):
    by_mode = {}
    for folder in folders:
        if not has_metric(folder, prefix):
            print(f"Skipping {folder.name}: no {metric} values")
            continue
        for mode in modes_in(folder):
            if not modes or mode in modes:
                by_mode.setdefault(mode, []).append(load_measurement(folder, prefix, mode))
    if not by_mode:
        sys.exit(f"No measurements have {metric} values.")
    for measurements in by_mode.values():
        disambiguate(measurements)
    return {mode: by_mode[mode] for mode in MODES if mode in by_mode}


def write_plots(by_mode, ylabel, out, show):
    by_folder = {}
    for measurements in by_mode.values():
        for measurement in measurements:
            by_folder.setdefault(measurement["folder"], []).append(measurement)
    for measurements in by_folder.values():
        fig = plot_single(measurements, ylabel)
        if not show:
            plt.close(fig)
    for mode, measurements in by_mode.items():
        slug = mode.lower().replace("_", "-")
        figs = (
            plot_grid(measurements, ylabel, out / f"{slug}-individual.png"),
            plot_comparison(measurements, ylabel, out / f"{slug}-comparison.png"),
        )
        if not show:
            for fig in figs:
                plt.close(fig)
    if show:
        plt.show()


def main():
    args = parse_args()
    folders = find_folders(args.data_dir, args.filters)
    metric = choose_metric(folders, args.metric)
    prefix, ylabel = METRICS[metric]
    by_mode = load_all(folders, prefix, metric, args.modes)
    out = args.data_dir / "plots" / f"{datetime.now():%Y%m%d-%H%M%S}-{metric}"
    out.mkdir(parents=True)
    write_plots(by_mode, ylabel, out, args.show)


if __name__ == "__main__":
    main()
