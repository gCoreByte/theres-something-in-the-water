# There's something in the water

Terminal app for measuring water with the AS7265x spectral triad on a SparkFun ESP32 Thing Plus C running the
firmware in `firmware/`.

## Firmware

The firmware takes frames back to back. Each frame is five readings: one with the LEDs off, then one each with
all LEDs, UV only, IR only and white only. Every reading is printed over USB serial (115200 baud) as one line:

```
Frame 12 Time 48211 [UV] Temps (NIR:31.0, VIS:30.0, UV:29.0)°C: 12.34,...,5.67
```

Saturated channels are printed as `-1`. On boot the ESP tries WiFi `the-water` (password `something-in-the-water`)
for 10 s. If it can't join, it prints a warning and measures without WiFi.

While connected, it also sends every reading over HTTP to the app on the laptop (see [Over WiFi](#over-wifi)).
It finds the laptop with mDNS, so you don't need to know the laptop's IP. It looks every 10 s until it finds the
server, and looks again after 5 failed uploads in a row.

Build and flash it with [PlatformIO](https://platformio.org/):

```
cd firmware
pio run -t upload
```

## Run

```
./run
```

The first run creates `.venv/` and installs the requirements. The menu offers:

1. **Calibrate**: records the white paper and clean tap water references (30 frames each by default) into
   `calibrations/<timestamp>/`. No dark step is needed, because every frame carries its own LEDs-off reading.
2. **Measure**: asks for a measurement type, the number of runs (default 10) and frames per run (default 60), then
   saves each run to its own file in `measurements/<timestamp>-<type>/`.
3. **Monitor**: shows live values without saving.

`./run` reads the ESP over USB when it's plugged in. Otherwise it starts the WiFi upload server and reads the ESP
over WiFi instead (see [Over WiFi](#over-wifi)). Opening the serial port restarts the ESP.

Commands can also be run directly:

```
./run monitor
./run calibrate --cal-frames 60
./run measure --type tap_water --runs 10 --frames 60
./run measure --type clay_1_5g --pause      # wait for Enter between runs, for example to re-stir
./run --simulate monitor                    # simulated sensor, no ESP needed
```

## Over WiFi

```
./server
```

`./run` starts the WiFi upload server when no ESP is plugged in over USB. `./server` always uses WiFi and never
opens USB, so it doesn't restart a plugged-in ESP. The server listens on port 8080 (`--http-port` to change it) for
`GET /upload?frame=…&timestamp=…&combination=…&t_nir=…&t_vis=…&t_uv=…&Aval=…&Lval=…` and advertises itself as
`_tsitw-upload._tcp` with `dns-sd` (macOS, or Windows with Bonjour) or `avahi-publish-service` (Linux). Join the
laptop to the `the-water` network first. The first time, macOS may ask whether Python can accept incoming
connections; click Allow.

```
./server measure --type tap_water
./server --http-port 9000 monitor
```

## Plot

```
./plot                         # every measurement
./plot tap turbid              # only folders whose name contains "tap" or "turbid"
./plot --metric excess --modes UV WHITE --show
```

Each measurement folder gets a `spectrum.png` with one panel per lighting mode. `plots/<timestamp>-<metric>/` gets
`<mode>-individual.png` (one panel per measurement) and `<mode>-comparison.png` (all on one plot) for every
lighting mode. With several runs, the spread shown is the standard deviation between run means; with one run, it
is the spread of the readings. The metric defaults to reflectance when every measurement has it, otherwise raw counts.

Legends show each measurement's type. To show a nicer name, add it to `labels.json` (used by `./plot` and
`./unmix`):

```json
{"blue-textile-7-2g": "Blue textile dye (7.2 g)"}
```

## Unmix

```
./unmix clay-05g-blue-textile-10-2g
./unmix clay-05g-blue-textile-10-2g --materials clay-05g blue-textile-7-2g red-silk-5.4g --modes UV WHITE
```

Subtracts the background and fits the mix as a positive weighted sum of the pure measurements, trying every
combination of up to `--max-components` materials. The background defaults to the clean tap water step of the
mix's calibration; pass `--background <measurement>` to subtract a measurement instead. All lighting modes the
measurements share are fitted together. By default it tries every measurement except the mix, the background
and other mixes, and fits net counts (raw counts for measurements recorded before the LEDs-off reading existed).
Models are ranked by noise-weighted misfit (lower is better). The best fit is saved to
`plots/<timestamp>-unmix-<mix>/fit.png`. Weights compare the mix to each material measured alone; they are not
mass fractions.

## Output

Each measurement folder contains:

- `run-01.csv` … `run-NN.csv`: one row per frame and lighting mode (`mode` is `WHITE_IR_UV`, `UV`, `IR` or
  `WHITE`), with
  - `A`–`L`: raw readings (410–940 nm) and `off_*`: the LEDs-off reading from the same frame
  - `net_*` = raw − off, which removes room light and sensor offset
  - `refl_*` = net / white paper net, for the same lighting mode (white paper = 1)
  - `excess_*` = (net − clean water net) / white paper net: how much the sample reflects beyond clean water

  `refl_*` is left empty for channels where the white paper reading is too weak to divide by, for example the
  near-infrared channels under UV light.
- `meta.json`: type, start/end time, planned runs, frames per run, and each run's file, frame count and times
- `calibration.json`: a copy of the calibration used

Measurements recorded before the custom firmware have a single `samples.csv` in the old format. `./plot` and
`./unmix` still read them.
