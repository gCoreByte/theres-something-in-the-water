import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .recording import write_samples
from .spectrum import CHANNELS, SATURATED, channel_stats


ZERO_DARK = dict.fromkeys(CHANNELS, 0.0)


@dataclass(frozen=True)
class CalibrationStep:

    name: str
    title: str
    instruction: str


STEPS = (
    CalibrationStep(
        "dark",
        "Dark",
        "Cover the sensor completely (e.g. with a black cap or cloth) so no outside light reaches it.\n"
        "If you cannot block all light, skip this step.",
    ),
    CalibrationStep(
        "white_ref",
        "White reference",
        "Place the white reference (PTFE or matte white paper) at the distance you will measure at.",
    ),
    CalibrationStep(
        "empty_container",
        "Empty container",
        "Put the empty, clean container in the measuring position.",
    ),
    CalibrationStep(
        "clean_water",
        "Clean water",
        "Fill the container with clean water and put it back in exactly the same position.",
    ),
)


@dataclass
class Calibration:

    path: Path
    created_at: str
    steps: dict
    references: dict

    def has(self, *names):
        return all(name in self.references for name in names)

    def reflectance(self, values):
        if not self.has("white_ref"):
            return None
        return self._normalise(values, self.references["white_ref"])

    def relative_to_water(self, values):
        if not self.has("clean_water"):
            return None
        return self._normalise(values, self.references["clean_water"])

    def _normalise(self, values, reference):
        dark = self.references.get("dark", ZERO_DARK)
        result = {}
        for channel in CHANNELS:
            value, low, high = values[channel], dark[channel], reference[channel]
            if value == SATURATED or low is None or high is None or high - low <= 0:
                result[channel] = None
            else:
                result[channel] = (value - low) / (high - low)
        return result

    @classmethod
    def load(cls, path):
        data = json.loads((path / "calibration.json").read_text())
        references = {name: {c: step["mean"][c] for c in CHANNELS} for name, step in data["steps"].items()}
        return cls(path=path, created_at=data["created_at"], steps=data["steps"], references=references)

    @classmethod
    def latest(cls, root):
        candidates = sorted(p for p in root.glob("*") if (p / "calibration.json").exists())
        return cls.load(candidates[-1]) if candidates else None


def save_calibration(root, results, samples_per_step, previous=None):
    created_at = datetime.now()
    path = root / created_at.strftime("%Y%m%d-%H%M%S")
    path.mkdir(parents=True)
    steps = {}
    for step in STEPS:
        if step.name in results:
            write_samples(path / f"{step.name}.csv", results[step.name])
            steps[step.name] = measured_step(results[step.name])
        elif previous and previous.has(step.name):
            steps[step.name] = reused_step(previous, step.name)
    data = {
        "created_at": created_at.isoformat(timespec="seconds"), "samples_per_step": samples_per_step, "steps": steps,
    }
    (path / "calibration.json").write_text(json.dumps(data, indent=2))
    return Calibration.load(path)


def measured_step(samples):
    stats = channel_stats(samples)
    return {
        "mean": {c: stats[c]["mean"] for c in CHANNELS},
        "std": {c: stats[c]["std"] for c in CHANNELS},
        "n": len(samples),
        "combination": samples[0].combination if samples else None,
    }


def reused_step(previous, name):
    step = previous.steps[name]
    return {**step, "reused_from": step.get("reused_from", previous.path.name)}
