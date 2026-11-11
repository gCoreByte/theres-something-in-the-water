import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .recording import write_frames
from .spectrum import CHANNELS, MODES, OFF, channel_stats

MIN_REFERENCE_SNR = 3
MIN_REFERENCE = 10


@dataclass(frozen=True)
class CalibrationStep:

    name: str
    title: str
    instruction: str


STEPS = (
    CalibrationStep(
        "white_ref",
        "White paper",
        "Hold the matte white paper against the glass in front of the sensor, at the spot and in the way you will "
        "use every time.",
    ),
    CalibrationStep(
        "clean_water",
        "Clean tap water",
        "Fill the clean glass with tap water and put it back in exactly the measuring position.",
    ),
)


@dataclass
class Calibration:

    path: Path
    created_at: str
    steps: dict

    def has(self, *names):
        return all(name in self.steps for name in names)

    def reference(self, name, mode, channel):
        stats = self.steps[name]["modes"][mode]
        mean, std = stats["mean"][channel], stats["std"][channel]
        if mean is None or mean < MIN_REFERENCE or mean <= MIN_REFERENCE_SNR * (std or 0):
            return None
        return mean

    def reflectance(self, frame):
        if not self.has("white_ref"):
            return None
        return {mode: self._reflectance(frame.net(mode), mode) for mode in MODES}

    def excess(self, frame):
        if not self.has("white_ref", "clean_water"):
            return None
        return {mode: self._excess(frame.net(mode), mode) for mode in MODES}

    def _reflectance(self, net, mode):
        result = {}
        for channel in CHANNELS:
            white = self.reference("white_ref", mode, channel)
            result[channel] = None if white is None or net[channel] is None else net[channel] / white
        return result

    def _excess(self, net, mode):
        result = {}
        for channel in CHANNELS:
            white = self.reference("white_ref", mode, channel)
            water = self.steps["clean_water"]["modes"][mode]["mean"][channel]
            missing = white is None or water is None or net[channel] is None
            result[channel] = None if missing else (net[channel] - water) / white
        return result

    @classmethod
    def load(cls, path):
        data = json.loads((path / "calibration.json").read_text())
        if not all("modes" in step for step in data["steps"].values()):
            return None
        return cls(path=path, created_at=data["created_at"], steps=data["steps"])

    @classmethod
    def latest(cls, root):
        candidates = sorted((p for p in root.glob("*") if (p / "calibration.json").exists()), reverse=True)
        for candidate in candidates:
            calibration = cls.load(candidate)
            if calibration:
                return calibration
        return None


def save_calibration(root, results, frames_per_step, previous=None):
    created_at = datetime.now()
    path = root / created_at.strftime("%Y%m%d-%H%M%S")
    path.mkdir(parents=True)
    steps = {}
    for step in STEPS:
        if step.name in results:
            write_frames(path / f"{step.name}.csv", results[step.name])
            steps[step.name] = measured_step(results[step.name])
        elif previous and previous.has(step.name):
            steps[step.name] = reused_step(previous, step.name)
    data = {
        "created_at": created_at.isoformat(timespec="seconds"), "frames_per_step": frames_per_step, "steps": steps,
    }
    (path / "calibration.json").write_text(json.dumps(data, indent=2))
    return Calibration.load(path)


def measured_step(frames):
    modes = {OFF: summary(channel_stats([f.readings[OFF].values for f in frames]))}
    for mode in MODES:
        modes[mode] = summary(channel_stats([f.net(mode) for f in frames]))
    return {"frames": len(frames), "modes": modes}


def summary(stats):
    return {"mean": {c: stats[c]["mean"] for c in CHANNELS}, "std": {c: stats[c]["std"] for c in CHANNELS}}


def reused_step(previous, name):
    step = previous.steps[name]
    return {**step, "reused_from": step.get("reused_from", previous.path.name)}
