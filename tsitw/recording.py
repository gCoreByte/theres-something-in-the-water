import csv
import json
import re
import shutil
from datetime import datetime

from .spectrum import CHANNELS, MODES, OFF

BASE_COLUMNS = ["frame", "host_time", "esp_ms", "mode", "t_nir", "t_vis", "t_uv"] + list(CHANNELS)
OFF_COLUMNS = [f"off_{c}" for c in CHANNELS]
NET_COLUMNS = [f"net_{c}" for c in CHANNELS]
REFL_COLUMNS = [f"refl_{c}" for c in CHANNELS]
EXCESS_COLUMNS = [f"excess_{c}" for c in CHANNELS]


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "measurement"


def _rounded(values):
    return ["" if v is None else round(v, 5) for v in (values[c] for c in CHANNELS)]


def columns(calibration=None):
    result = BASE_COLUMNS + OFF_COLUMNS + NET_COLUMNS
    if calibration and calibration.has("white_ref"):
        result += REFL_COLUMNS
    if calibration and calibration.has("white_ref", "clean_water"):
        result += EXCESS_COLUMNS
    return result


def frame_rows(frame, calibration=None):
    off = frame.readings[OFF]
    reflectance = calibration.reflectance(frame) if calibration else None
    excess = calibration.excess(frame) if calibration else None
    rows = []
    for mode in MODES:
        reading = frame.readings[mode]
        row = [
            frame.number, reading.host_time.isoformat(timespec="milliseconds"), reading.esp_ms, mode,
            reading.t_nir, reading.t_vis, reading.t_uv,
        ]
        row += [reading.values[c] for c in CHANNELS] + [off.values[c] for c in CHANNELS]
        row += _rounded(frame.net(mode))
        if reflectance:
            row += _rounded(reflectance[mode])
        if excess:
            row += _rounded(excess[mode])
        rows.append(row)
    return rows


def write_frames(path, frames, calibration=None):
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns(calibration))
        for frame in frames:
            writer.writerows(frame_rows(frame, calibration))


class MeasurementRecorder:

    def __init__(self, root, measurement_type, calibration, source, runs, frames_per_run):
        self.started_at = datetime.now()
        self.measurement_type = measurement_type
        self.calibration = calibration
        self.source = source
        self.frames_per_run = frames_per_run
        self.planned_runs = runs
        self.path = root / f"{self.started_at.strftime('%Y%m%d-%H%M%S')}-{slugify(measurement_type)}"
        self.path.mkdir(parents=True)
        self.runs = []
        self._fh = None
        self._writer = None
        if calibration:
            shutil.copy(calibration.path / "calibration.json", self.path / "calibration.json")

    def start_run(self):
        number = len(self.runs) + 1
        run = {"file": f"run-{number:02d}.csv", "started_at": datetime.now().isoformat(timespec="seconds"), "frames": 0}
        self.runs.append(run)
        self._fh = open(self.path / run["file"], "w", newline="", buffering=1)
        self._writer = csv.writer(self._fh)
        self._writer.writerow(columns(self.calibration))
        return run["file"]

    def add(self, frame):
        self._writer.writerows(frame_rows(frame, self.calibration))
        self.runs[-1]["frames"] += 1

    def finish_run(self, completed):
        self._fh.close()
        self.runs[-1]["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self.runs[-1]["completed"] = completed

    def finish(self, completed):
        meta = {
            "type": self.measurement_type,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "completed": completed,
            "planned_runs": self.planned_runs,
            "frames_per_run": self.frames_per_run,
            "modes": list(MODES),
            "source": self.source,
            "calibration": self.calibration.path.name if self.calibration else None,
            "runs": self.runs,
        }
        (self.path / "meta.json").write_text(json.dumps(meta, indent=2))

    @property
    def frames(self):
        return sum(run["frames"] for run in self.runs)
