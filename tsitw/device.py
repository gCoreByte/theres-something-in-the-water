import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import serial
from serial.tools import list_ports

from .spectrum import CHANNELS, SATURATED

WIFI_SSID = "727pp"
CH340_VID = 0x1A86
SAMPLE_LINE = re.compile(
    r"Time (\d+) \[(\w+)\] Temps \(NIR:([-\d.]+), VIS:([-\d.]+), UV:([-\d.]+)\)\S*: ([-\d,]+)"
)
IP_LINE = re.compile(r"IP Address: (\S+)")
LOG_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+ ")
STATUS_MARKERS = (
    ("rst:", "ESP restarted"),
    ("Starting setup", "Booting"),
    ("Connecting", f"Connecting to WiFi '{WIFI_SSID}'"),
    ("Sensor not connected", "Spectral sensor not found"),
    ("Starting data collection", "Starting measurements"),
)


@dataclass
class Sample:

    host_time: datetime
    esp_ms: int
    combination: str
    t_nir: float
    t_vis: float
    t_uv: float
    values: dict

    @property
    def saturated(self):
        return any(v == SATURATED for v in self.values.values())


def parse_sample(line):
    match = SAMPLE_LINE.search(line)
    if not match:
        return None
    values = match.group(6).split(",")
    if len(values) != len(CHANNELS):
        return None
    return Sample(
        host_time=datetime.now(),
        esp_ms=int(match.group(1)),
        combination=match.group(2),
        t_nir=float(match.group(3)),
        t_vis=float(match.group(4)),
        t_uv=float(match.group(5)),
        values=dict(zip(CHANNELS, (int(v) for v in values))),
    )


def find_port():
    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    for port in ports:
        if port.vid == CH340_VID:
            return port.device
    for port in ports:
        if "usbserial" in port.device or "usbmodem" in port.device or "ttyUSB" in port.device:
            return port.device
    return None


class SerialSource:

    def __init__(self, port, baudrate=115200):
        self.description = port
        self._serial = serial.Serial()
        self._serial.port = port
        self._serial.baudrate = baudrate
        self._serial.timeout = 0.2
        self._serial.dtr = False
        self._serial.rts = False
        self._serial.open()

    def lines(self):
        buffer = b""
        while True:
            buffer += self._serial.read(4096)
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield line.decode("utf-8", "replace").rstrip("\r")


class ReplaySource:

    def __init__(self, path, interval=1.0):
        self.description = f"replay of {path}"
        self._path = path
        self._interval = interval

    def lines(self):
        while True:
            with open(self._path, encoding="utf-8") as log:
                for line in log:
                    line = LOG_PREFIX.sub("", line.rstrip("\n"))
                    if SAMPLE_LINE.search(line):
                        time.sleep(self._interval)
                    yield line


class Device:

    def __init__(self, source):
        self.description = source.description
        self.status = "Waiting for ESP output"
        self.ip = None
        self.last_sample = None
        self._samples = queue.Queue()
        threading.Thread(target=self._run, args=(source,), daemon=True).start()

    def drain(self):
        while not self._samples.empty():
            self._samples.get_nowait()

    def next_sample(self, timeout):
        try:
            return self._samples.get(timeout=timeout)
        except queue.Empty:
            return None

    def _run(self, source):
        try:
            for line in source.lines():
                self._handle(line)
        except (serial.SerialException, OSError) as exc:
            self.status = f"Serial error: {exc}"

    def _handle(self, line):
        sample = parse_sample(line)
        if sample:
            self.status = "Measuring"
            self.last_sample = sample
            self._samples.put(sample)
            return
        ip = IP_LINE.search(line)
        if ip:
            self.ip = ip.group(1)
            self.status = "WiFi connected"
            return
        for marker, status in STATUS_MARKERS:
            if marker in line:
                self.status = status
                return
