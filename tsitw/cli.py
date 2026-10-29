import argparse
import sys
from pathlib import Path

import serial
from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from .calibration import STEPS, Calibration, save_calibration
from .device import WIFI_SSID, Device, ReplaySource, SerialSource, find_port
from .recording import MeasurementRecorder
from .spectrum import CHANNELS, WAVELENGTHS
from .ui import Session

PROJECT_DIR = Path(__file__).resolve().parent.parent
CALIBRATION_SETTLE = 2
MEASURE_SETTLE = 1
CANCELLED = object()
CALIBRATION_INTRO = (
    "Calibration has {steps} steps of {samples} samples each (about one sample per second).\n"
    "Keep the sensor, container and room lighting in the same position for the whole session."
)
SKIP_PROMPT = "Press Enter to start this step, or type s and press Enter to skip it"
NO_CALIBRATION_WARNING = (
    "[yellow]No calibration found, so only raw values will be saved. "
    "Run Calibrate first to also save reflectance.[/]"
)
PORT_BUSY_HINT = (
    "Close any other program that uses the port (Arduino Serial Monitor, PlatformIO monitor) and try again."
)
PORT_OPENED = (
    "[dim]Opened {port}. Opening the port restarts the ESP; "
    "it starts measuring once it joins WiFi '{ssid}'.[/]"
)
MENU_OPTIONS = (
    ("1", "[bold]Calibrate[/]  record dark, white reference, empty container and clean water"),
    ("2", "[bold]Measure[/]    record a labelled measurement to measurements/<timestamp>-<type>/"),
    ("3", "[bold]Monitor[/]    show live values without saving"),
    ("q", "Quit"),
)

console = Console()


class App:

    def __init__(self, device, data_dir, cal_samples):
        self.device = device
        self.calibrations_dir = data_dir / "calibrations"
        self.measurements_dir = data_dir / "measurements"
        self.cal_samples = cal_samples
        self.calibration = Calibration.latest(self.calibrations_dir)

    def menu(self):
        actions = {"1": self.calibrate, "2": self.measure, "3": self.monitor}
        while True:
            console.print(self._menu_panel())
            choice = Prompt.ask("Choose", choices=["1", "2", "3", "q"], default="3")
            if choice == "q":
                return
            actions[choice]()

    def calibrate(self):
        intro = CALIBRATION_INTRO.format(steps=len(STEPS), samples=self.cal_samples)
        console.print(Panel(intro, title="Calibrate", border_style="magenta"))
        previous = self.calibration
        results = {}
        for index, step in enumerate(STEPS, 1):
            samples = self._calibration_step(index, step, previous)
            if samples is CANCELLED:
                console.print("[yellow]Calibration cancelled. Nothing was saved.[/]")
                return
            if samples is not None:
                results[step.name] = samples
        if not results:
            console.print("[yellow]All steps were skipped. Nothing was saved.[/]")
            return
        self._save_calibration(results, previous)

    def measure(self, measurement_type=None, samples=None):
        if not self.calibration:
            console.print(NO_CALIBRATION_WARNING)
        measurement_type, samples = self._ask_measurement(measurement_type, samples)
        recorder = MeasurementRecorder(
            self.measurements_dir, measurement_type, self.calibration, self.device.description,
        )
        session = Session(
            f"Measure · {measurement_type}", self.device, self.calibration, target=samples or None,
            settle=MEASURE_SETTLE, notes=[("Saving to", str(recorder.path))],
        )
        completed = session.run(on_sample=recorder.add) or not samples
        recorder.finish(completed)
        console.print(f"[green]Saved {len(recorder.samples)} samples to {recorder.path}[/]")

    def monitor(self):
        Session("Monitor", self.device, self.calibration).run()

    def _calibration_step(self, index, step, previous):
        title = f"Step {index}/{len(STEPS)}: {step.title}"
        console.print(Panel(step.instruction, title=title, border_style="magenta"))
        answer = Prompt.ask(f"{SKIP_PROMPT} {self._skip_effect(step, previous)}", default="", show_default=False)
        if answer.strip().lower() == "s":
            return None
        session = Session(
            f"Calibrate · {step.title}", self.device, target=self.cal_samples, settle=CALIBRATION_SETTLE,
            hint="Press Ctrl+C to cancel the calibration.",
        )
        return session.samples if session.run() else CANCELLED

    def _save_calibration(self, results, previous):
        self.calibration = save_calibration(self.calibrations_dir, results, self.cal_samples, previous)
        console.print(self._calibration_table(self.calibration))
        console.print(f"[green]Calibration saved to {self.calibration.path}[/]")
        if not self.calibration.has("white_ref"):
            console.print("[yellow]Reflectance needs the white reference step.[/]")

    @staticmethod
    def _ask_measurement(measurement_type, samples):
        while not measurement_type:
            measurement_type = Prompt.ask("Measurement type (for example tap_water or turbid_1)").strip()
        while samples is None or samples < 0:
            samples = IntPrompt.ask("Number of samples, or 0 to record until you press Ctrl+C", default=60)
        return measurement_type, samples

    def _menu_panel(self):
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim")
        grid.add_column()
        grid.add_row("Device", f"{self.device.status}  ({self.device.description})")
        if self.calibration:
            steps = ", ".join(self.calibration.references)
            grid.add_row("Calibration", f"{self.calibration.created_at}  [dim]{steps}[/]")
        else:
            grid.add_row("Calibration", Text("none yet: run Calibrate first", style="yellow"))
        grid.add_row("", "")
        for key, text in MENU_OPTIONS:
            grid.add_row(f"[bold cyan]{key}[/]", text)
        return Panel(grid, title="[bold]There's something in the water[/]", border_style="blue")

    @staticmethod
    def _skip_effect(step, previous):
        if previous and previous.has(step.name):
            source = previous.steps[step.name].get("reused_from", previous.path.name)
            return f"and reuse the value from {source}"
        if step.name == "dark":
            return "(no previous value, so dark is taken as zero)"
        return "(no previous value)"

    @staticmethod
    def _calibration_table(calibration):
        table = Table(title="Calibration means (± std)", header_style="bold")
        table.add_column("Ch")
        table.add_column("λ nm", justify="right")
        for name, step in calibration.steps.items():
            reused = step.get("reused_from")
            table.add_column(f"{name}\n[dim]reused {reused}[/]" if reused else name, justify="right")
        for channel in CHANNELS:
            cells = []
            for step in calibration.steps.values():
                mean, std = step["mean"][channel], step["std"][channel]
                cells.append("SAT" if mean is None else f"{mean:.1f} ± {std:.1f}")
            table.add_row(channel, str(WAVELENGTHS[channel]), *cells)
        return table


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="tsitw",
        description=(
            "Measure water with the AS7265x spectral triad on the ESP32. Run without a command to open the menu."
        ),
    )
    parser.add_argument("--port", help="serial port of the ESP (default: auto-detect)")
    parser.add_argument("--replay", type=Path, help="replay a recorded serial log instead of reading the ESP")
    parser.add_argument(
        "--data-dir", type=Path, default=PROJECT_DIR, help="where calibrations/ and measurements/ are stored",
    )
    parser.add_argument("--cal-samples", type=int, default=30, help="samples per calibration step (default: 30)")
    add_commands(parser)
    return parser.parse_args(argv)


def add_commands(parser):
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("calibrate", help="run the calibration steps")
    measure = commands.add_parser("measure", help="record a measurement")
    measure.add_argument("--type", dest="measurement_type", help="measurement type, used in the folder name")
    measure.add_argument("--samples", type=int, help="number of samples, 0 records until Ctrl+C")
    commands.add_parser("monitor", help="show live values without saving")


def open_device(args):
    if args.replay:
        return Device(ReplaySource(args.replay))
    port = args.port or find_port()
    if not port:
        console.print("[red]No ESP serial port found. Plug in the ESP, or pass the port with --port.[/]")
        sys.exit(1)
    try:
        source = SerialSource(port)
    except serial.SerialException as exc:
        console.print(f"[red]Could not open {port}: {exc}[/]")
        console.print(PORT_BUSY_HINT)
        sys.exit(1)
    console.print(PORT_OPENED.format(port=port, ssid=WIFI_SSID))
    return Device(source)


def main(argv=None):
    args = parse_args(argv)
    app = App(open_device(args), args.data_dir, args.cal_samples)
    try:
        if args.command == "calibrate":
            app.calibrate()
        elif args.command == "measure":
            app.measure(args.measurement_type, args.samples)
        elif args.command == "monitor":
            app.monitor()
        else:
            app.menu()
    except (KeyboardInterrupt, EOFError):
        console.print()
