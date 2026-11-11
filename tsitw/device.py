import queue
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import serial
from serial.tools import list_ports

from .spectrum import CHANNELS, FRAME_MODES, MODES, OFF, SATURATED, WAVELENGTHS

WIFI_SSID = "the-water"
CH340_VID = 0x1A86
READING_LINE = re.compile(
    r"Frame (\d+) Time (\d+) \[(\w+)\] Temps \(NIR:([-\d.]+), VIS:([-\d.]+), UV:([-\d.]+)\)\S*: ([-\d.,]+)"
)
IP_LINE = re.compile(r"IP Address: (\S+)")
UPLOAD_LINE = re.compile(r"Upload server found: (\S+)")
STATUS_MARKERS = (
    ("rst:", "ESP restarted"),
    ("Starting setup", "Booting"),
    ("Connecting to WiFi", f"Connecting to WiFi '{WIFI_SSID}'"),
    ("not found. Measuring without upload", f"WiFi '{WIFI_SSID}' not found, measuring without upload"),
    ("Could not join WiFi", f"Could not join WiFi '{WIFI_SSID}', measuring without upload"),
    ("Sensor not connected", "Spectral sensor not found"),
    ("No upload server found", "No upload server found, run ./server on the laptop"),
    ("Upload server not answering", "Upload server not answering, looking for it again"),
    ("Starting data collection", "Starting measurements"),
)


@dataclass
class Reading:

    host_time: datetime
    esp_ms: int
    mode: str
    t_nir: float
    t_vis: float
    t_uv: float
    values: dict

    @property
    def saturated(self):
        return any(v == SATURATED for v in self.values.values())


@dataclass
class Frame:

    number: int
    readings: dict

    @property
    def complete(self):
        return all(mode in self.readings for mode in FRAME_MODES)

    @property
    def saturated(self):
        return any(r.saturated for r in self.readings.values())

    def net(self, mode):
        lit, off = self.readings[mode].values, self.readings[OFF].values
        return {c: None if SATURATED in (lit[c], off[c]) else lit[c] - off[c] for c in CHANNELS}


def parse_reading(line):
    match = READING_LINE.search(line)
    if not match:
        return None
    values = match.group(7).split(",")
    if len(values) != len(CHANNELS):
        return None
    reading = Reading(
        host_time=datetime.now(),
        esp_ms=int(match.group(2)),
        mode=match.group(3),
        t_nir=float(match.group(4)),
        t_vis=float(match.group(5)),
        t_uv=float(match.group(6)),
        values=dict(zip(CHANNELS, (float(v) for v in values))),
    )
    return int(match.group(1)), reading


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


class SimulatedSource:

    LIGHT = {
        "WHITE": lambda nm: 900 if 430 <= nm <= 700 else 120,
        "IR": lambda nm: 700 if nm >= 700 else 15,
        "UV": lambda nm: 500 if nm <= 435 else 5,
    }

    def __init__(self, interval=0.1):
        self.description = "simulated sensor"
        self._interval = interval

    def lines(self):
        yield "Starting setup..."
        yield f"[Warning] WiFi '{WIFI_SSID}' not found. Measuring without upload."
        yield "Starting data collection..."
        frame = 0
        while True:
            frame += 1
            for mode in FRAME_MODES:
                time.sleep(self._interval)
                yield self._line(frame, mode)

    def _line(self, frame, mode):
        lights = () if mode == OFF else (("WHITE", "IR", "UV") if mode == MODES[0] else (mode,))
        values = []
        for channel in CHANNELS:
            nm = WAVELENGTHS[channel]
            signal = 40 + sum(self.LIGHT[light](nm) for light in lights) * 0.05
            values.append(f"{max(signal + random.gauss(0, 1), 0):.2f}")
        ms = int(time.monotonic() * 1000)
        return f"Frame {frame} Time {ms} [{mode}] Temps (NIR:31.0, VIS:30.0, UV:29.0)°C: {','.join(values)}"


class Device:

    def __init__(self, source):
        self.description = source.description
        self.status = "Waiting for ESP output"
        self.ip = None
        self.upload_url = None
        self.last_frame = None
        self._pending = None
        self._frames = queue.Queue()
        threading.Thread(target=self._run, args=(source,), daemon=True).start()

    def drain(self):
        while not self._frames.empty():
            self._frames.get_nowait()

    def next_frame(self, timeout):
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def _run(self, source):
        try:
            for line in source.lines():
                self._handle(line)
        except (serial.SerialException, OSError) as exc:
            self.status = f"Serial error: {exc}"

    def _handle(self, line):
        parsed = parse_reading(line)
        if parsed:
            self.status = "Measuring"
            self._add(*parsed)
            return
        ip = IP_LINE.search(line)
        if ip:
            self.ip = ip.group(1)
            self.status = "WiFi connected"
            return
        upload = UPLOAD_LINE.search(line)
        if upload:
            self.upload_url = upload.group(1)
            return
        for marker, status in STATUS_MARKERS:
            if marker in line:
                if "upload server" in status.lower():
                    self.upload_url = None
                self.status = status
                return

    def _add(self, number, reading):
        if reading.mode not in FRAME_MODES:
            return
        if self._pending is None or self._pending.number != number:
            self._pending = Frame(number, {})
        self._pending.readings[reading.mode] = reading
        if self._pending.complete:
            self.last_frame = self._pending
            self._frames.put(self._pending)
            self._pending = None
