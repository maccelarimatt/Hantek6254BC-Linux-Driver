from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys
import time

from .device import Hantek6254BC, RATE_TABLE, STREAM_MAX_RATE, SUPPORTED_DEPTHS, ScopeConfig, wire_to_volts
from .errors import HantekError
from .transport import UsbTransport


def _number(value: str) -> int:
    suffixes = {"k": 1_000, "m": 1_000_000}
    text = value.strip().lower().replace("s/s", "").replace("sa/s", "")
    multiplier = suffixes.get(text[-1:], 1)
    if multiplier != 1:
        text = text[:-1]
    return int(float(text) * multiplier)


def _vdiv(value: str) -> tuple[float, float, float, float]:
    values = tuple(float(item) for item in value.split(","))
    if len(values) == 1:
        values *= 4
    if len(values) != 4:
        raise argparse.ArgumentTypeError("use one value or four comma-separated values")
    return values  # validated by ScopeConfig


def _add_usb_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bus", type=int, help="USB bus number (normally omit; e.g. 1)")
    parser.add_argument("--device", type=int, help="USB device address (normally omit; e.g. 8)")


def _add_capture_args(parser: argparse.ArgumentParser) -> None:
    _add_usb_args(parser)
    parser.add_argument("--rate", type=_number, default=2_500_000, choices=RATE_TABLE, help="sample rate")
    parser.add_argument("--depth", type=int, default=4096, choices=SUPPORTED_DEPTHS, help="samples per channel")
    parser.add_argument("--vdiv", type=_vdiv, default=(1.0,) * 4, help="V/div: one value or CH1,CH2,CH3,CH4")
    parser.add_argument("--trigger-source", type=int, choices=(1, 2, 3, 4), default=1)
    parser.add_argument("--trigger-level", type=float, default=0.0, help="trigger voltage")
    parser.add_argument("--slope", choices=("rising", "falling"), default="rising")
    parser.add_argument("--sweep", choices=("auto", "normal", "single"), default="auto")


def _config(args) -> ScopeConfig:
    return ScopeConfig(
        sample_rate=args.rate,
        depth=args.depth,
        volts_per_div=args.vdiv,
        trigger_source=args.trigger_source - 1,
        trigger_level=args.trigger_level,
        trigger_slope=args.slope,
        trigger_sweep=args.sweep,
    )


def _write_csv(path: Path, capture) -> None:
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("time_s", "ch1_v", "ch2_v", "ch3_v", "ch4_v"))
        writer.writerows(zip(capture.time_s, *capture.volts))


def doctor(args) -> int:
    transport = UsbTransport.open(bus=args.bus, address=args.device)
    try:
        print(f"PASS: found 04b5:6cde at Bus {transport.bus:03d} Device {transport.address:03d}")
        print("PASS: claimed interface 0 (bulk OUT 0x02, bulk IN 0x86)")
        print("USB access is ready. Run 'hantek6254bc capture capture.csv'.")
    finally:
        transport.close()
    return 0


def capture_command(args) -> int:
    config = _config(args)
    output = Path(args.output)
    with Hantek6254BC.open(config=config, bus=args.bus, address=args.device) as scope:
        result = scope.capture()
    _write_csv(output, result)
    print(f"Captured {config.depth} samples/channel at {config.sample_rate:,} S/s -> {output}")
    return 0


def stream_command(args) -> int:
    """Record continuously in ROLL mode for a fixed time and save every sample."""
    import numpy as np

    config = ScopeConfig(sample_rate=args.rate, volts_per_div=args.vdiv)
    output = Path(args.output)
    chunks, lost = [], 0
    with Hantek6254BC.open(config=config, bus=args.bus, address=args.device) as scope:
        stream = scope.stream()
        started = time.monotonic()
        try:
            for chunk in stream:
                chunks.append(chunk.raw)
                lost += chunk.lost_frames
                if time.monotonic() - started >= args.seconds:
                    break
        finally:
            stream.close()
    raw = np.concatenate(chunks, axis=1)
    np.savez_compressed(output, raw=raw, volts=wire_to_volts(raw, config.volts_per_div),
                        sample_rate=config.sample_rate, lost_frames=lost)
    print(f"Streamed {raw.shape[1]:,} samples/channel at {config.sample_rate:,} S/s "
          f"({raw.shape[1] / config.sample_rate:.1f} s), {lost} lost -> {output}")
    return 0 if lost == 0 else 1


def live_command(args) -> int:
    config = _config(args)
    from .gui import run_gui
    return run_gui(config=config, bus=args.bus, address=args.device)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hantek6254bc", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    doctor_parser = sub.add_parser("doctor", help="check detection and USB permissions")
    _add_usb_args(doctor_parser)
    doctor_parser.set_defaults(func=doctor)

    capture_parser = sub.add_parser("capture", help="capture one waveform to CSV")
    capture_parser.add_argument("output", nargs="?", default="capture.csv")
    _add_capture_args(capture_parser)
    capture_parser.set_defaults(func=capture_command)

    stream_parser = sub.add_parser("stream", help="record continuously (ROLL mode) to a .npz file")
    stream_parser.add_argument("output", nargs="?", default="stream.npz")
    _add_usb_args(stream_parser)
    stream_parser.add_argument("--seconds", type=float, default=10.0)
    stream_parser.add_argument("--rate", type=_number, default=25_000,
                               choices=[r for r in RATE_TABLE if r <= STREAM_MAX_RATE], help="sample rate")
    stream_parser.add_argument("--vdiv", type=_vdiv, default=(1.0,) * 4, help="V/div: one value or CH1,CH2,CH3,CH4")
    stream_parser.set_defaults(func=stream_command)

    live_parser = sub.add_parser("live", help="display continuously captured waveforms")
    _add_capture_args(live_parser)
    live_parser.set_defaults(func=live_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.func(args)
    except (HantekError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
