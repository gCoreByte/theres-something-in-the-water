# There's something in the water

Terminal app for measuring water with the AS7265x spectral triad on the ESP32 running the
`plasticdebris` firmware (WHITE_IR_UV lighting, one sample per second over USB serial).

## Run

```
./run
```

The first run creates `.venv/` and installs the requirements. The menu offers:

1. **Calibrate**: records dark, white reference, empty container and clean water references
   (30 samples each by default) into `calibrations/<timestamp>/`.
2. **Measure**: asks for a measurement type and a sample count, then saves to
   `measurements/<timestamp>-<type>/`.
3. **Monitor**: shows live values without saving.

Opening the serial port restarts the ESP. Keep the WiFi hotspot `727pp` (password `parool123`)
on, because the firmware waits for WiFi before it starts measuring.

Commands can also be run directly:

```
./run monitor
./run calibrate --cal-samples 60
./run measure --type tap_water --samples 120
./run --replay path/to/serial.log monitor
```

## Plot

```
./plot                       # every measurement
./plot tap turbid            # only folders whose name contains "tap" or "turbid"
./plot --metric raw --show   # raw counts, and open the plots in windows
```

Each measurement folder gets a `spectrum.png`, and `plots/<timestamp>-<metric>/` gets
`individual.png` (one panel per measurement, shared axes) and `comparison.png` (all on one plot).
The metric defaults to reflectance when every measurement has it, otherwise raw counts.

## Unmix

```
./unmix clay-textile-red-mix
./unmix clay-textile-red-mix --materials textile-red clay-1-5g micro-04g --metric refl
```

Subtracts the background (`--background`, default `tap-water`) and fits the mix as a positive
weighted sum of the pure measurements, trying every combination of up to `--max-components`
materials. By default it tries every measurement except the mix, the background and other mixes.
Models are ranked by noise-weighted misfit (lower is better). The best fit is saved to
`plots/<timestamp>-unmix-<mix>/fit.png`. Weights compare the mix to each material measured
alone; they are not mass fractions.

## Output

Each measurement folder contains:

- `samples.csv`: raw counts per channel (A–L, 410–940 nm), chip temperatures, and, once calibrated,
  `refl_*` = (raw − dark) / (white_ref − dark) and `vs_water_*` = (raw − dark) / (clean_water − dark).
  A skipped calibration step reuses its value from the most recent calibration. With no previous
  dark, dark is taken as zero.
- `meta.json`: type, start/end time, sample count, per-channel mean and std
- `calibration.json`: a copy of the calibration used
