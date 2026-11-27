# Hantek 6254BC Linux driver

This project provides a userspace USB driver, CSV capture command, and live four-channel display for the Hantek 6004BC family, including the Hantek6254BC. It talks directly to the scope through Linux `libusb`/PyUSB; no kernel module or Windows driver is needed.

The currently connected scope was identified as:

```
Bus 001 Device 008: ID 04b5:6cde ODM DSO Device
USB 2.0, interface 0, bulk OUT 0x02, bulk IN 0x86
```

USB device numbers are temporary and can change whenever the scope is reconnected. The driver therefore selects `04b5:6cde` by default; `--bus 1 --device 8` is only needed when selecting among multiple scopes.

## Install on Pop!_OS / Ubuntu

Install the system prerequisites:

```sh
sudo apt update
sudo apt install python3-venv python3-dev libusb-1.0-0
```

From this directory:

```sh
./install.sh
sudo ./install-udev.sh
```

`install.sh` deliberately rebuilds `.venv` using the `python3` belonging to the
terminal that runs it. It is safe to run again if the virtual environment is
stale or a launcher reports `ModuleNotFoundError`.

Unplug and reconnect the scope after installing the udev rule. Check access:

```sh
.venv/bin/hantek6254bc doctor
```

## Use it

Capture one 4-channel waveform to a CSV file:

```sh
.venv/bin/hantek6254bc capture capture.csv --rate 2.5M --depth 4096 --vdiv 1
```

Open the oscilloscope GUI:

```sh
.venv/bin/hantek6254bc live --rate 2.5M --depth 4096 --vdiv 1,1,0.5,0.2
```

The GUI opens with CH1 visible. Its control panel provides:

- independent Show, volts/div, and vertical-position controls for CH1–CH4;
- time/div and capture-memory controls;
- trigger source, voltage level, rising/falling edge, and Auto/Normal modes;
- Run/Stop, Single acquisition, Apply settings, and Save CSV controls;
- live Vpp and frequency measurements for each visible channel.

Press Space to run or stop acquisition and Ctrl+S to save the most recent
waveform. Channel visibility and vertical position update immediately. Press
**Apply settings** after changing time/div, memory, volts/div, or trigger settings.
The plot toolbar also provides interactive zoom and pan.

Trigger example (channel numbers on the command line are 1 through 4):

```sh
.venv/bin/hantek6254bc capture edge.csv \
  --rate 25M --vdiv 1 --trigger-source 1 --trigger-level 0.5 \
  --slope rising --sweep normal
```

Use `hantek6254bc capture --help` for all options. Supported sample rates are 25 kS/s, 50 kS/s, 125 kS/s, 250 kS/s, 500 kS/s, 1.25 MS/s, 2.5 MS/s, 5 MS/s, 12.5 MS/s, 25 MS/s, 50 MS/s, 125 MS/s, and 250 MS/s. Supported memory depths are 4096, 8192, and 16384 samples per channel.

## Continuous streaming (ROLL mode)

`capture()` records one burst into the scope's 64 KB memory, stops, and reads it out, so there is a gap between bursts. `stream()` instead starts the hardware's ROLL mode (bit 1 of the vendor SDK's `dsoHTStartCollectData` start control, sent as `0x03 0x00 0x02 0x00`). The FPGA then writes all four channels round its memory without stopping, and the driver follows the write pointer (bytes 2-3 of the `0x0D` response) and reads everything behind it while acquisition continues. Every sample is delivered exactly once, in order:

```python
from hantek6254bc import Hantek6254BC, ScopeConfig, wire_to_volts

with Hantek6254BC.open(ScopeConfig(sample_rate=25_000)) as scope:
    for chunk in scope.stream():           # StreamChunk every ~20 ms
        volts = wire_to_volts(chunk.raw, scope.config.volts_per_div)
        ...                                # chunk.first_frame counts samples on the scope clock
```

The memory holds 0.655 s at 25 kS/s, so the consumer must ask for the next chunk well within that; hand chunks to another thread for processing. If it falls further behind, the overwritten samples are counted exactly in `chunk.lost_frames` rather than silently joined. `chunk.host_ns` and `chunk.host_frame` tie the scope's sample clock to host time. Streaming is limited to 25 and 50 kS/s; it was verified on hardware at 25 kS/s by following a 60 Hz sine for 30 s with no lost or repeated samples.

```sh
.venv/bin/hantek6254bc stream stream.npz --seconds 60 --vdiv 1
```

## Current limitations

- Four-channel acquisition mode only. The hardware's faster one- and two-channel interleaved modes are not enabled.
- Voltage conversion uses the scope's unit-specific EEPROM position and gain calibration tables. Verify critical measurements against a known reference because the function-generator load setting and probe attenuation still affect the displayed amplitude.
- Edge triggering is implemented. Pulse/video/alternate trigger modes, AC/GND coupling control, autoset, and self-calibration are not yet exposed.
- This is an independently implemented, reverse-engineered driver. Treat early captures as uncalibrated until checked against the scope's 1 kHz calibration output.

The scope shares electrical ground with the laptop through USB. Never attach the probe ground to a live mains-referenced point.

If the live window cannot connect to the display, run it from the normal Pop!_OS desktop terminal rather than a container, SSH session, or sandboxed IDE terminal.

## Development

```sh
.venv/bin/pip install -e '.[plot,test]'
.venv/bin/pytest
```

Protocol interoperability references include the [Hantek6254BC libusb prototype](https://gitlab.iae.nsk.su/terentevvs/Hantek6254BC), the [Hantek 6004 Android implementation](https://github.com/hackhantek/hantek6004), and the [sigrok descriptor record for this USB identity](https://sigrok.org/wiki/Hantek_6254BD/Info).

## License

MIT. See `LICENSE`.
