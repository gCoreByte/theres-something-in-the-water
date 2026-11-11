import argparse
import itertools
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from rich.console import Console
from rich.table import Table

from plot_measurements import (
    METRICS, SERIES, SURFACE, TEXT, TEXT_SECONDARY, X, grid_axes, has_metric, hide_unused, load_measurement, measurement_folders,
    modes_in, save, style_axes,
)
from tsitw.calibration import Calibration
from tsitw.recording import slugify
from tsitw.spectrum import CHANNELS, MODE_LABELS, MODES, OFF

PROJECT_DIR = Path(__file__).resolve().parent
CLEAN_WATER = "clean-water"
NOISE_FLOOR = 0.05
WEAK_SIGNAL = 0.05
SCORE_NOTE = (
    "[dim]Score is the noise-weighted squared misfit per degree of freedom (lower is better; ~1 means the fit is "
    "as good as the noise allows). Weights compare the mix to each material measured alone; they are not mass "
    "fractions.[/]"
)
FIT_LINE = {"linewidth": 2, "marker": "o", "markersize": 5, "markeredgecolor": SURFACE, "markeredgewidth": 1.5}

console = Console()


@dataclass
class Problem:

    mix: dict
    background: dict
    modes: list
    ylabel: str
    target: np.ndarray
    components: dict
    sigma: np.ndarray
    usable: np.ndarray


def measurement_type(folder):
    meta = folder / "meta.json"
    return json.loads(meta.read_text())["type"] if meta.exists() else folder.name[16:]


def find_folder(folders, query):
    exact = [f for f in folders if query in (f.name, f.name[16:], measurement_type(f))]
    matches = exact or [f for f in folders if query.lower() in f.name.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"No measurement matches '{query}'.")
    names = "\n  ".join(f.name for f in matches)
    sys.exit(f"'{query}' matches several measurements; use a longer name:\n  {names}")


def select_folders(args):
    folders = measurement_folders(args.data_dir)
    mix = find_folder(folders, args.mix)
    background = None if args.background == CLEAN_WATER else find_folder(folders, args.background)
    if args.materials:
        materials = [find_folder(folders, name) for name in args.materials]
    else:
        materials = [f for f in folders if f not in (mix, background) and "mix" not in f.name]
    if not materials:
        sys.exit("No pure measurements to fit with. Pass them with --materials.")
    return mix, background, materials


def shared_modes(folders, requested):
    shared = [mode for mode in MODES if all(mode in modes_in(folder) for folder in folders)]
    modes = [mode for mode in shared if not requested or mode in requested]
    if not modes:
        sys.exit("The chosen measurements share none of the requested lighting modes.")
    return modes


def load(folder, prefix, modes):
    parts = [load_measurement(folder, prefix, mode) for mode in modes]
    if not all(parts):
        sys.exit(f"{folder.name} has no {prefix or 'raw'} values.")
    return {
        "label": parts[0]["label"],
        "mean": np.concatenate([part["mean"] for part in parts]),
        "std": np.concatenate([part["std"] for part in parts]),
        "n": min(part["n"] for part in parts),
    }


def load_clean_water(folder, prefix, modes):
    calibration = (folder / "calibration.json").exists() and Calibration.load(folder)
    if not calibration or not calibration.has("clean_water"):
        sys.exit(f"{folder.name} has no clean water calibration. Pass a measurement with --background.")
    water = calibration.steps["clean_water"]
    means, stds = [], []
    for mode in modes:
        for channel in CHANNELS:
            mean, std = water["modes"][mode]["mean"][channel], water["modes"][mode]["std"][channel]
            mean, std = (math.nan, math.nan) if mean is None else (mean, std)
            if prefix == "":
                mean += water["modes"][OFF]["mean"][channel] or 0
            elif prefix in ("refl_", "excess_"):
                white = calibration.reference("white_ref", mode, channel) if calibration.has("white_ref") else None
                mean, std = (math.nan, math.nan) if white is None else (mean / white, std / white)
                if prefix == "excess_":
                    mean = 0.0 if math.isfinite(mean) else mean
            means.append(mean)
            stds.append(std)
    meta = folder / "meta.json"
    name = json.loads(meta.read_text()).get("calibration") if meta.exists() else None
    label = f"clean water (calibration {name})" if name else "clean water (calibration)"
    return {"label": label, "mean": np.array(means), "std": np.array(stds), "n": water["frames"]}


def warn_mixed_calibrations(folders):
    names = {}
    for folder in folders:
        meta = folder / "meta.json"
        names[folder.name] = json.loads(meta.read_text()).get("calibration") if meta.exists() else None
    if len(set(names.values())) > 1:
        listing = ", ".join(f"{folder} ({name or 'none'})" for folder, name in names.items())
        console.print(f"[yellow]The measurements use different calibrations: {listing}[/]")


def build_problem(args):
    mix_folder, background_folder, material_folders = select_folders(args)
    folders = [mix_folder] + ([background_folder] if background_folder else []) + material_folders
    warn_mixed_calibrations(folders)
    modes = shared_modes(folders, args.modes)
    metric = args.metric
    if metric == "auto":
        metric = "net" if all(has_metric(f, "net_") for f in folders) else "raw"
    prefix, ylabel = METRICS[metric]
    mix = load(mix_folder, prefix, modes)
    if background_folder:
        background = load(background_folder, prefix, modes)
    else:
        background = load_clean_water(mix_folder, prefix, modes)
    baseline = background["mean"]
    components = {m["label"]: m["mean"] - baseline for m in (load(f, prefix, modes) for f in material_folders)}
    target = mix["mean"] - baseline
    usable = np.isfinite(target) & np.all([np.isfinite(c) for c in components.values()], axis=0)
    sigma = np.sqrt(mix["std"] ** 2 / mix["n"] + background["std"] ** 2 / background["n"])
    sigma = np.maximum(sigma, np.nanmax(sigma[usable]) * NOISE_FLOOR)
    return Problem(mix, background, modes, ylabel, target, components, sigma, usable)


def fit_model(problem, combo):
    usable = problem.usable
    target, weights = problem.target[usable], 1 / problem.sigma[usable]
    basis = np.column_stack([problem.components[name][usable] for name in combo])
    coef, *_ = np.linalg.lstsq(basis * weights[:, None], target * weights, rcond=None)
    if (coef <= 0).any():
        return None
    residual = target - basis @ coef
    return {
        "combo": combo,
        "coef": coef,
        "score": float(np.sum((residual * weights) ** 2)) / (len(target) - len(combo)),
        "r2": 1 - float(np.sum(residual ** 2)) / float(np.sum((target - target.mean()) ** 2)),
        "rmse": float(np.sqrt(np.mean(residual ** 2))),
    }


def fit_models(problem, max_components):
    names = list(problem.components)
    sizes = range(1, min(max_components, len(names)) + 1)
    combos = (combo for size in sizes for combo in itertools.combinations(names, size))
    results = [result for result in (fit_model(problem, combo) for combo in combos) if result]
    if not results:
        sys.exit("No model fits the mix with positive weights.")
    return sorted(results, key=lambda r: r["score"])


def describe(result):
    return " + ".join(f"{a:.2f} × {name}" for a, name in zip(result["coef"], result["combo"]))


def results_table(results, limit):
    table = Table(header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("Model for the mix (minus background)")
    table.add_column("Score", justify="right")
    table.add_column("R²", justify="right")
    table.add_column("RMSE", justify="right")
    for rank, r in enumerate(results[:limit], 1):
        table.add_row(
            str(rank), describe(r), f"{r['score']:.1f}", f"{r['r2']:.3f}", f"{r['rmse']:.4g}",
            style="bold green" if rank == 1 else "",
        )
    return table


def report(problem, results, limit):
    if not problem.usable.all():
        features = [f"{MODE_LABELS[m]} {c}" for m in problem.modes for c in CHANNELS]
        dropped = ", ".join(name for name, ok in zip(features, problem.usable) if not ok)
        console.print(f"[yellow]Ignoring channels without values (saturated or unlit): {dropped}[/]")
    console.print(
        f"[bold]Mix:[/] {problem.mix['label']}   [bold]Background:[/] {problem.background['label']}   "
        f"[bold]Values:[/] {problem.ylabel}",
    )
    console.print(f"[bold]Lighting modes fitted together:[/] {', '.join(MODE_LABELS[m] for m in problem.modes)}")
    console.print(f"[bold]Materials tried:[/] {', '.join(problem.components)}\n")
    console.print(results_table(results, limit))
    console.print(f"\n[bold green]Best explanation:[/] {describe(results[0])}")
    if len(results) > 1:
        console.print(f"The next best model scores {results[1]['score'] / results[0]['score']:.1f}× worse.")
    console.print(SCORE_NOTE)
    warn_weak_materials(problem)


def warn_weak_materials(problem):
    norms = {name: np.linalg.norm(c[problem.usable]) for name, c in problem.components.items()}
    strongest = max(norms.values())
    for name, norm in norms.items():
        if norm < WEAK_SIGNAL * strongest:
            console.print(
                f"[yellow]{name} barely differs from {problem.background['label']}, "
                "so the fit cannot tell whether it is in the mix.[/]",
            )


def plot_fit(problem, best, path):
    parts = [(a, name, a * problem.components[name]) for a, name in zip(best["coef"], best["combo"])]
    fit = sum(part for *_, part in parts)
    fig, axes = grid_axes(len(problem.modes), cols=2, sharey=False)
    for index, (ax, mode) in enumerate(zip(axes.flat, problem.modes)):
        block = slice(index * len(CHANNELS), (index + 1) * len(CHANNELS))
        style_axes(ax, "")
        ax.set_xticks(X, [str(nm) for nm in X], rotation=90, fontsize=7)
        ax.tick_params(axis="x", labelbottom=True)
        ax.set_xlabel("Wavelength (nm)", color=TEXT_SECONDARY, fontsize=9)
        ax.plot(X, problem.target[block], color=SERIES[0], label=f"{problem.mix['label']} (measured)", **FIT_LINE)
        ax.plot(X, fit[block], color=SERIES[1], linestyle="--", label="best fit", **FIT_LINE)
        for color_index, (a, name, part) in enumerate(parts, 2):
            color = SERIES[color_index % len(SERIES)]
            ax.plot(X, part[block], color=color, linestyle=":", linewidth=2, label=f"{a:.2f} × {name}")
        ax.set_title(f"{MODE_LABELS[mode]} LEDs", color=TEXT, fontsize=10, loc="left")
    hide_unused(axes, len(problem.modes))
    fig.suptitle(f"{problem.mix['label']} explained as {describe(best)}", color=TEXT, fontsize=12, x=0.01, ha="left")
    fig.supylabel(f"{problem.ylabel}, minus background", color=TEXT_SECONDARY, fontsize=9)
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=2, frameon=False, fontsize=9, labelcolor=TEXT)
    fig.tight_layout(rect=(0, 0.08, 1, 0.97))
    save(fig, path)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Explain a mixture as a weighted sum of pure measurements (linear spectral unmixing).",
    )
    parser.add_argument("mix", help="the mixture measurement (type or part of its folder name)")
    parser.add_argument(
        "--materials", nargs="+",
        help="pure measurements to try (default: all except the mix, the background and other mixes)",
    )
    parser.add_argument(
        "--background", default=CLEAN_WATER,
        help="measurement subtracted from all spectra (default: the clean water step of the mix's calibration)",
    )
    parser.add_argument(
        "--metric", choices=("auto",) + tuple(METRICS), default="auto",
        help="values to fit (default: net counts if every measurement has them, otherwise raw)",
    )
    parser.add_argument(
        "--modes", nargs="+", choices=MODES, help="lighting modes to fit together (default: every shared mode)",
    )
    parser.add_argument("--max-components", type=int, default=3, help="most materials in one model (default: 3)")
    parser.add_argument("--top", type=int, default=10, help="number of models to list (default: 10)")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_DIR, help="folder that contains measurements/")
    return parser.parse_args()


def main():
    args = parse_args()
    problem = build_problem(args)
    results = fit_models(problem, args.max_components)
    report(problem, results, args.top)
    out = args.data_dir / "plots" / f"{datetime.now():%Y%m%d-%H%M%S}-unmix-{slugify(problem.mix['label'])}"
    out.mkdir(parents=True)
    plot_fit(problem, results[0], out / "fit.png")


if __name__ == "__main__":
    main()
