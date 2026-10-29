import csv
import json
import re
import shutil
from datetime import datetime

from .spectrum import CHANNELS, channel_stats

BASE_COLUMNS = ["host_time", "esp_ms", "combination", "t_nir", "t_vis", "t_uv"] + list(CHANNELS)


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "measurement"


def sample_row(sample):
    return [
        sample.host_time.isoformat(timespec="milliseconds"),
        sample.esp_ms,
        sample.combination,
        sample.t_nir,
        sample.t_vis,
        sample.t_uv,
    ] + [sample.values[c] for c in CHANNELS]


def write_samples(path, samples):
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(BASE_COLUMNS)
        writer.writerows(sample_row(s) for s in samples)


def _rounded(values):
    return ["" if v is None else round(v, 5) for v in (values[c] for c in CHANNELS)]


class MeasurementRecorder:

    def __init__(self, root, measurement_type, calibration, source):
        self.started_at = datetime.now()
        self.measurement_type = measurement_type
        self.calibration = calibration
        self.source = source
        self.path = root / f"{self.started_at.strftime('%Y%m%d-%H%M%S')}-{slugify(measurement_type)}"
        self.path.mkdir(parents=True)
        self.samples = []
        self._fh = open(self.path / "samples.csv", "w", newline="", buffering=1)
        self._writer = csv.writer(self._fh)
        self._writer.writerow(BASE_COLUMNS + self._derived_columns())
        if calibration:
            shutil.copy(calibration.path / "calibration.json", self.path / "calibration.json")

    def add(self, sample):
        self.samples.append(sample)
        row = sample_row(sample)
        if self.calibration and self.calibration.has("white_ref"):
            row += _rounded(self.calibration.reflectance(sample.values))
        if self.calibration and self.calibration.has("clean_water"):
            row += _rounded(self.calibration.relative_to_water(sample.values))
        self._writer.writerow(row)

    def finish(self, completed):
        self._fh.close()
        stats = channel_stats(self.samples)
        meta = {
            "type": self.measurement_type,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "completed": completed,
            "samples": len(self.samples),
            "source": self.source,
            "combination": self.samples[0].combination if self.samples else None,
            "calibration": str(self.calibration.path) if self.calibration else None,
            "mean": {c: stats[c]["mean"] for c in CHANNELS},
            "std": {c: stats[c]["std"] for c in CHANNELS},
        }
        (self.path / "meta.json").write_text(json.dumps(meta, indent=2))

    def _derived_columns(self):
        columns = []
        if self.calibration and self.calibration.has("white_ref"):
            columns += [f"refl_{c}" for c in CHANNELS]
        if self.calibration and self.calibration.has("clean_water"):
            columns += [f"vs_water_{c}" for c in CHANNELS]
        return columns
