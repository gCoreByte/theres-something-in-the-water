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
from .device import WIFI_SSID, Device, SerialSource, SimulatedSource, find_port
from .recording import MeasurementRecorder
from .server import SERVICE_TYPE, UploadServer
from .spectrum import CHANNELS, FRAME_MODES, MODE_LABELS, WAVELENGTHS
from .ui import Session

PROJECT_DIR = Path(__file__).resolve().parent.parent
CALIBRATION_SETTLE = 2
MEASURE_SETTLE = 1
DEFAULT_RUNS = 10
DEFAULT_FRAMES = 60
CANCELLED = object()
CALIBRATION_INTRO = (
    "Calibration has {steps} steps of {frames} frames each. Every frame is one LEDs-off reading and one reading "
    "per lighting mode, so room light is subtracted automatically and no dark step is needed.\n"
    "Keep the sensor and glass in the same position for the whole session."
)
SKIP_PROMPT = "Press Enter to start this step, or type s and press Enter to skip it"
NO_CALIBRATION_WARNING = (
    "[yellow]No calibration found, so only counts will be saved. "
    "Run Calibrate first to also save reflectance.[/]"
)
PORT_BUSY_HINT = (
    "Close any other program that uses the port (Arduino Serial Monitor, PlatformIO monitor) and try again."
)
SERVER_STARTED = (
    "[dim]Listening for WiFi readings on port {port}, advertised as {service} so the ESP finds this laptop "
    "on its own when both are on '{ssid}'.[/]"
)
WIFI_ONLY = "[yellow]No ESP found on USB, so the app listens for it over WiFi instead.[/]"
NO_ADVERTISER = (
    "[yellow]Can't advertise the server: install dns-sd (macOS, or Windows with Bonjour) or avahi "
    "(Linux). The ESP won't find this laptop until then.[/]"
)
SERVER_PORT_BUSY = "[red]Could not listen on port {port}: {error}. Pass another port with --http-port.[/]"
PORT_OPENED = (
    "[dim]Opened {port}. Opening the port restarts the ESP. It tries WiFi '{ssid}' for up to 10 s, "
    "then starts measuring with or without it.[/]"
)
MENU_OPTIONS = (
    ("1", "[bold]Calibrate[/]  record the white paper and clean tap water references"),
    ("2", "[bold]Measure[/]    record repeated runs to measurements/<timestamp>-<type>/run-NN.csv"),
    ("3", "[bold]Monitor[/]    show live values without saving"),
    ("q", "Quit"),
)

console = Console()


class App:

    def __init__(self, device, data_dir, cal_frames):
        self.device = device
        self.calibrations_dir = data_dir / "calibrations"
        self.measurements_dir = data_dir / "measurements"
        self.cal_frames = cal_frames
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
        intro = CALIBRATION_INTRO.format(steps=len(STEPS), frames=self.cal_frames)
        console.print(Panel(intro, title="Calibrate", border_style="magenta"))
        previous = self.calibration
        results = {}
        for index, step in enumerate(STEPS, 1):
            frames = self._calibration_step(index, step, previous)
            if frames is CANCELLED:
                console.print("[yellow]Calibration cancelled. Nothing was saved.[/]")
                return
            if frames is not None:
                results[step.name] = frames
        if not results:
            console.print("[yellow]All steps were skipped. Nothing was saved.[/]")
            return
        self._save_calibration(results, previous)

    def measure(self, measurement_type=None, runs=None, frames=None, pause=False):
        if not self.calibration:
            console.print(NO_CALIBRATION_WARNING)
        measurement_type, runs, frames = self._ask_measurement(measurement_type, runs, frames)
        recorder = MeasurementRecorder(
            self.measurements_dir, measurement_type, self.calibration, self.device.description, runs, frames,
        )
        completed = all(self._run(recorder, index, pause) for index in range(1, runs + 1))
        recorder.finish(completed)
        console.print(f"[green]Saved {len(recorder.runs)} run(s), {recorder.frames} frames, to {recorder.path}[/]")

    def _run(self, recorder, index, pause):
        if pause and index > 1:
            Prompt.ask(f"Press Enter to start run {index}/{recorder.planned_runs}", default="", show_default=False)
        name = recorder.start_run()
        session = Session(
            f"Measure · {recorder.measurement_type} · run {index}/{recorder.planned_runs}", self.device,
            self.calibration, target=recorder.frames_per_run, settle=MEASURE_SETTLE,
            notes=[("Saving to", str(recorder.path / name))], hint="Press Ctrl+C to stop the measurement.",
        )
        completed = session.run(on_frame=recorder.add)
        recorder.finish_run(completed)
        return completed

    def monitor(self):
        Session("Monitor", self.device, self.calibration).run()

    def _calibration_step(self, index, step, previous):
        title = f"Step {index}/{len(STEPS)}: {step.title}"
        console.print(Panel(step.instruction, title=title, border_style="magenta"))
        answer = Prompt.ask(f"{SKIP_PROMPT} {self._skip_effect(step, previous)}", default="", show_default=False)
        if answer.strip().lower() == "s":
            return None
        session = Session(
            f"Calibrate · {step.title}", self.device, target=self.cal_frames, settle=CALIBRATION_SETTLE,
            hint="Press Ctrl+C to cancel the calibration.",
        )
        return session.frames if session.run() else CANCELLED

    def _save_calibration(self, results, previous):
        self.calibration = save_calibration(self.calibrations_dir, results, self.cal_frames, previous)
        for name in self.calibration.steps:
            console.print(self._calibration_table(self.calibration, name))
        console.print(f"[green]Calibration saved to {self.calibration.path}[/]")
        if not self.calibration.has("white_ref"):
            console.print("[yellow]Reflectance needs the white reference step.[/]")

    @staticmethod
    def _ask_measurement(measurement_type, runs, frames):
        while not measurement_type:
            measurement_type = Prompt.ask("Measurement type (for example tap_water or turbid_1)").strip()
        while runs is None or runs < 1:
            runs = IntPrompt.ask("Number of runs", default=DEFAULT_RUNS)
        while frames is None or frames < 1:
            frames = IntPrompt.ask("Frames per run", default=DEFAULT_FRAMES)
        return measurement_type, runs, frames

    def _menu_panel(self):
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim")
        grid.add_column()
        grid.add_row("Device", f"{self.device.status}  ({self.device.description})")
        if self.calibration:
            steps = ", ".join(self.calibration.steps)
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
        return "(no previous value)"

    @staticmethod
    def _calibration_table(calibration, name):
        step = calibration.steps[name]
        reused = f" [dim](reused from {step['reused_from']})[/]" if step.get("reused_from") else ""
        table = Table(title=f"{name}: mean ± std, counts above LEDs off (Off: raw){reused}", header_style="bold")
        table.add_column("Ch")
        table.add_column("λ nm", justify="right")
        for mode in FRAME_MODES:
            table.add_column(MODE_LABELS[mode], justify="right")
        for channel in CHANNELS:
            cells = []
            for mode in FRAME_MODES:
                mean, std = step["modes"][mode]["mean"][channel], step["modes"][mode]["std"][channel]
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
    parser.add_argument("--simulate", action="store_true", help="use simulated readings instead of the ESP")
    parser.add_argument(
        "--server", action="store_true", help="receive readings over WiFi only, without opening USB (./server)",
    )
    parser.add_argument(
        "--http-port", type=int, default=8080, help="port the WiFi upload server listens on (default: 8080)",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=PROJECT_DIR, help="where calibrations/ and measurements/ are stored",
    )
    parser.add_argument("--cal-frames", type=int, default=30, help="frames per calibration step (default: 30)")
    add_commands(parser)
    return parser.parse_args(argv)


def add_commands(parser):
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("calibrate", help="run the calibration steps")
    measure = commands.add_parser("measure", help="record a measurement")
    measure.add_argument("--type", dest="measurement_type", help="measurement type, used in the folder name")
    measure.add_argument("--runs", type=int, help=f"number of runs, each saved to its own file (default: {DEFAULT_RUNS})")
    measure.add_argument("--frames", type=int, help=f"frames per run (default: {DEFAULT_FRAMES})")
    measure.add_argument("--pause", action="store_true", help="wait for Enter before each run after the first")
    commands.add_parser("monitor", help="show live values without saving")


def open_device(args):
    if args.simulate:
        return Device(SimulatedSource())
    if not args.server:
        source = open_serial(args.port)
        if source:
            return Device(source)
        console.print(WIFI_ONLY)
    return Device(start_server(args.http_port))


def open_serial(port):
    port = port or find_port()
    if not port:
        return None
    try:
        source = SerialSource(port)
    except serial.SerialException as exc:
        console.print(f"[red]Could not open {port}: {exc}[/]")
        console.print(PORT_BUSY_HINT)
        return None
    console.print(PORT_OPENED.format(port=port, ssid=WIFI_SSID))
    return source


def start_server(port):
    try:
        server = UploadServer(port)
    except OSError as exc:
        console.print(SERVER_PORT_BUSY.format(port=port, error=exc.strerror or exc))
        sys.exit(1)
    console.print(SERVER_STARTED.format(port=port, service=SERVICE_TYPE, ssid=WIFI_SSID))
    if not server.advertiser:
        console.print(NO_ADVERTISER)
    return server


def main(argv=None):
    args = parse_args(argv)
    app = App(open_device(args), args.data_dir, args.cal_frames)
    try:
        if args.command == "calibrate":
            app.calibrate()
        elif args.command == "measure":
            app.measure(args.measurement_type, args.runs, args.frames, args.pause)
        elif args.command == "monitor":
            app.monitor()
        else:
            app.menu()
    except (KeyboardInterrupt, EOFError):
        console.print()
