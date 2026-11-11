import time
from collections import deque

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .spectrum import CHANNELS, COLORS, MODE_LABELS, MODES, OFF, SATURATED, WAVELENGTHS

BAR_WIDTH = 20
BLOCKS = " ▏▎▍▌▋▊▉"
SPARKS = "▁▂▃▄▅▆▇█"
HISTORY = 60


def bar(value, maximum, color):
    if value is None or value <= 0 or maximum <= 0:
        return Text("")
    full, part = divmod(round(min(value / maximum, 1) * BAR_WIDTH * 8), 8)
    return Text("█" * full + (BLOCKS[part] if part else ""), style=color)


def sparkline(values):
    if not values:
        return Text("waiting for frames", style="dim")
    low, high = min(values), max(values)
    span = (high - low) or 1
    return Text("".join(SPARKS[round((v - low) / span * (len(SPARKS) - 1))] for v in values), style="cyan")


def number(value, digits):
    if value is None:
        return "–"
    return "SAT" if value == SATURATED else f"{value:.{digits}f}"


def elapsed(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"


class Session:

    def __init__(self, title, device, calibration=None, target=None, settle=0, notes=(), hint="Press Ctrl+C to stop."):
        self.title = title
        self.device = device
        self.calibration = calibration
        self.target = target
        self.settling = settle
        self.notes = notes
        self.hint = hint
        self.frames = []
        self.latest = None
        self.history = deque(maxlen=HISTORY)
        self.started = time.monotonic()

    def run(self, on_frame=None):
        self.device.drain()
        self.started = time.monotonic()
        with Live(self.render(), refresh_per_second=8) as live:
            try:
                while self.target is None or len(self.frames) < self.target:
                    frame = self.device.next_frame(timeout=0.1)
                    if frame:
                        self._accept(frame, on_frame)
                    live.update(self.render())
            except KeyboardInterrupt:
                live.update(self.render())
                return False
        return True

    def _accept(self, frame, on_frame):
        self.latest = frame
        if self.settling:
            self.settling -= 1
            return
        self.frames.append(frame)
        self.history.append(sum(v for v in frame.net(MODES[0]).values() if v is not None))
        if on_frame:
            on_frame(frame)

    def render(self):
        return Panel(
            Group(self._header(), Text(""), self._table(), Text(""), self._footer()),
            title=f"[bold]{self.title}[/]",
            border_style="blue",
            expand=False,
        )

    def _header(self):
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim")
        grid.add_column()
        grid.add_row("Device", self._status())
        grid.add_row("Source", self.device.description)
        if self.latest:
            s = self.latest.readings[OFF]
            grid.add_row("Temps", f"NIR {s.t_nir:.1f}°C   VIS {s.t_vis:.1f}°C   UV {s.t_uv:.1f}°C")
        grid.add_row("Values", self._values_label())
        grid.add_row("Frames", self._progress())
        for label, value in self._warnings() + list(self.notes):
            grid.add_row(label, value)
        return grid

    def _status(self):
        style = "green" if self.device.status == "Measuring" else "yellow"
        status = Text(self.device.status, style=style)
        if self.device.ip:
            status.append(f"  ESP {self.device.ip}", style="dim")
        if self.device.upload_url:
            status.append(f"  uploading to {self.device.upload_url}", style="dim")
        return status

    def _values_label(self):
        if self.calibration and self.calibration.has("white_ref"):
            return "reflectance per lighting mode (white paper = 1); Off is raw counts"
        return "counts above the LEDs-off reading; Off is raw counts"

    def _progress(self):
        progress = f"{len(self.frames)}" + (f" / {self.target}" if self.target else "")
        return progress + f"   elapsed {elapsed(time.monotonic() - self.started)}"

    def _warnings(self):
        warnings = []
        if self.settling:
            warnings.append(("", Text(f"Settling: discarding the next {self.settling} frame(s)", style="yellow")))
        if self.latest and self.latest.saturated:
            warnings.append(("", Text("Saturated channels detected: move the sample further away.", style="bold red")))
        return warnings

    def _table(self):
        table = Table(expand=False, header_style="bold", box=None, padding=(0, 1))
        table.add_column("Ch")
        table.add_column("λ nm", justify="right")
        table.add_column(MODE_LABELS[OFF], justify="right")
        for mode in MODES:
            table.add_column(MODE_LABELS[mode], justify="right")
        table.add_column(f"{MODE_LABELS[MODES[0]]} (net)", width=BAR_WIDTH, no_wrap=True)
        values = self._values()
        nets = self.latest.net(MODES[0]) if self.latest else {}
        maximum = max((v for v in nets.values() if v is not None), default=0)
        for channel in CHANNELS:
            off = self.latest.readings[OFF].values[channel] if self.latest else None
            cells = [number(values[mode][channel], self._digits) if values else "–" for mode in MODES]
            table.add_row(
                Text(channel, style=f"bold {COLORS[channel]}"), str(WAVELENGTHS[channel]), number(off, 1), *cells,
                bar(nets.get(channel), maximum, COLORS[channel]),
            )
        return table

    def _values(self):
        if not self.latest:
            return None
        if self.calibration and self.calibration.has("white_ref"):
            return self.calibration.reflectance(self.latest)
        return {mode: self.latest.net(mode) for mode in MODES}

    @property
    def _digits(self):
        return 3 if self.calibration and self.calibration.has("white_ref") else 1

    def _footer(self):
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim")
        grid.add_column()
        grid.add_row("Total signal", sparkline(list(self.history)))
        grid.add_row("", Text(self.hint, style="dim"))
        return grid
