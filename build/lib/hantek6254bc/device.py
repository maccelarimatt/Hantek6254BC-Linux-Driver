from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from .errors import ProtocolError
from .transport import UsbTransport

SUPPORTED_DEPTHS = (4096, 8192, 16384)
SUPPORTED_VDIV = (0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0)

# divider command, timing template bytes, timer base, correction A, correction B
RATE_TABLE = {
    25_000:       (b"\x87\x13\x00\x00", 0x3C, 0x40, 0x27100, 1953, 156),
    50_000:       (b"\xc3\x09\x00\x00", 0xAC, 0x20, 0x13880, 976, 78),
    125_000:      (b"\xe7\x03\x00\x00", 0xBC, 0x40, 0x07D00, 390, 31),
    250_000:      (b"\xf3\x01\x00\x00", 0x6C, 0xA0, 0x03E80, 195, 15),
    500_000:      (b"\xf9\x00\x00\x00", 0xC4, 0xD0, 0x01F40, 97, 7),
    1_250_000:    (b"\x63\x00\x00\x00", 0x2C, 0x20, 0x00C80, 39, 3),
    2_500_000:    (b"\x31\x00\x00\x00", 0xA4, 0x90, 0x00640, 19, 1),
    5_000_000:    (b"\x18\x00\x00\x00", 0xE0, 0xC8, 0x00320, 9, 0),
    12_500_000:   (b"\x09\x00\x00\x00", 0x04, 0x50, 0x00140, 4, 0),
    25_000_000:   (b"\x04\x00\x00\x00", 0x10, 0x28, 0x000A0, 2, 0),
    50_000_000:   (b"\x01\x00\x00\x00", 0x16, 0x14, 0x00050, 1, 0),
    125_000_000:  (b"\x00\x00\x00\x00", 0x80, 0x08, 0x00020, 0, 0),
    250_000_000:  (b"\x00\x00\x00\x00", 0x4E, 0x04, 0x00010, 0, 0),
}


class Transport(Protocol):
    def control_out(self, request: int, data: bytes, value: int = 0) -> int: ...
    def control_in(self, request: int, length: int, value: int = 0) -> bytes: ...
    def write(self, data: bytes) -> None: ...
    def read(self, length: int, timeout_ms: int = 2000) -> bytes: ...
    def settle(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class ScopeConfig:
    sample_rate: int = 2_500_000
    depth: int = 4096
    volts_per_div: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    trigger_source: int = 0
    trigger_level: float = 0.0
    trigger_slope: str = "rising"
    trigger_sweep: str = "auto"

    def __post_init__(self) -> None:
        if self.sample_rate not in RATE_TABLE:
            raise ValueError(f"unsupported sample rate {self.sample_rate}; choose one of {tuple(RATE_TABLE)}")
        if self.depth not in SUPPORTED_DEPTHS:
            raise ValueError(f"unsupported depth {self.depth}; choose one of {SUPPORTED_DEPTHS}")
        if len(self.volts_per_div) != 4 or any(v not in SUPPORTED_VDIV for v in self.volts_per_div):
            raise ValueError(f"volts_per_div must contain four values chosen from {SUPPORTED_VDIV}")
        if self.trigger_source not in range(4):
            raise ValueError("trigger_source must be 0, 1, 2, or 3")
        if self.trigger_slope not in ("rising", "falling"):
            raise ValueError("trigger_slope must be 'rising' or 'falling'")
        if self.trigger_sweep not in ("normal", "auto", "single"):
            raise ValueError("trigger_sweep must be 'normal', 'auto', or 'single'")


@dataclass(frozen=True)
class Capture:
    time_s: NDArray[np.float64]
    volts: NDArray[np.float64]
    raw: NDArray[np.uint8]
    sample_rate: int


class Hantek6254BC:
    """Protocol driver for four-channel Hantek 6004BC-family scopes."""

    def __init__(self, transport: Transport, config: ScopeConfig | None = None):
        self.transport = transport
        self.config = config or ScopeConfig()
        self._initialized = False

    @classmethod
    def open(
        cls, config: ScopeConfig | None = None, bus: int | None = None, address: int | None = None
    ) -> "Hantek6254BC":
        transport = UsbTransport.open(bus=bus, address=address)
        scope = cls(transport, config)
        try:
            scope.initialize()
        except Exception:
            transport.close()
            raise
        return scope

    def _reset_command_pipe(self) -> None:
        self.transport.control_out(0xB3, b"\x0f\x03\x03\x03\x00\x00\x00\x00\x00\x00")
        self.transport.control_in(0xB2, 10)

    def _command(self, data: bytes) -> None:
        self._reset_command_pipe()
        self.transport.write(data)
        self.transport.settle()

    def _response_length(self) -> int:
        status = self.transport.control_in(0xB2, 10)
        if not status:
            raise ProtocolError("empty response-length status")
        return 512 if status[0] else 64

    def initialize(self) -> None:
        # Query FPGA and controller versions before configuring the acquisition path.
        self._command(b"\x0c\x00")
        self._command(b"\x0c\x00")
        self.transport.read(self._response_length())
        self.transport.control_in(0xA2, 71, value=0x1580)
        self._reset_command_pipe()
        self.transport.control_in(0xA2, 8, value=0x15E0)

        for command in (
            b"\x08\x00\x00\x77\x47\x12\x04\x00",
            b"\x08\x00\x00\x03\x00\x33\x04\x00",
            b"\x08\x00\x00\x65\x00\x30\x02\x00",
            b"\x08\x00\x00\x28\xf1\x0f\x02\x00",
            b"\x08\x00\x00\x12\x38\x01\x02\x00",
            b"\x08\x00\x00\x3f\x00\x55\x04\x00",
        ):
            self._command(command)
        self.configure()
        self._initialized = True

    def configure(self) -> None:
        self._configure_sample_rate()
        self._configure_channels()
        ram_code = 0x3C if self.config.sample_rate == 250_000_000 else 0x3D
        self._command(bytes((0x12, 0, ram_code, 0, 0, self.config.trigger_source)))

        # Calibrated mid-screen positions for channels 1..4 (the fourth selector is 4).
        for command in (
            b"\x00\x00\xc2\x71", b"\x01\x00\x2c\x71",
            b"\x02\x00\xad\x72", b"\x04\x00\x39\x72",
        ):
            self._command(command)

        source_vdiv = self.config.volts_per_div[self.config.trigger_source]
        trigger_code = round(self.config.trigger_level * 255.0 / (10.0 * source_vdiv) + 128.0)
        trigger_code = min(228, max(28, trigger_code))
        high, low = trigger_code + 5, trigger_code - 5
        trigger_levels = bytes(
            (0x07, 0x00) + (high, high, low, low) * 4 + (trigger_code,) * 8
        )
        self._command(trigger_levels)
        slope = 0 if self.config.trigger_slope == "rising" else 1
        self._command(bytes((0x11, 0, 0, slope, 0, 0)))

    def _configure_sample_rate(self) -> None:
        for command in (
            b"\x08\x00\x00\x10\x08\x3a\x04\x00",
            b"\x08\x00\x00\x04\x02\x3b\x04\x00",
            b"\x08\x00\x00\x00\x00\x0f\x04\x00",
            b"\x08\x00\x00\x04\x02\x31\x04\x00",
            b"\x08\x00\x00\x00\x00\x2a\x04\x00",
        ):
            self._command(command)

        divider, template, timing_flag, timer_base, correction_a, correction_b = RATE_TABLE[
            self.config.sample_rate
        ]
        self._command(b"\x0f\x00" + divider)
        depth_divisor = 16384 // self.config.depth
        timer_a = timer_base // depth_divisor + correction_a
        timer_b = timer_base // depth_divisor + correction_b
        timing = bytearray((0x10, 0, template, 0, 0, 0, 0, 0, timing_flag, 0, 0, 0, 0, 0))
        timing[3:6] = timer_a.to_bytes(3, "little")
        timing[9:12] = timer_b.to_bytes(3, "little")
        self._command(bytes(timing))

    def _configure_channels(self) -> None:
        attenuator = []
        gain_nibbles = []
        nibble_by_vdiv = {
            0.002: 0xD, 0.005: 0xA, 0.01: 0x7, 0.02: 0x5, 0.05: 0x2, 0.1: 0x0,
            0.2: 0x5, 0.5: 0x2, 1.0: 0x0, 2.0: 0x5, 5.0: 0x2, 10.0: 0x0,
        }
        for vdiv in self.config.volts_per_div:
            attenuator.append(0x2E if vdiv <= 0.1 else 0x36 if vdiv <= 1.0 else 0x56)
            gain_nibbles.append(nibble_by_vdiv[vdiv])
        self._command(bytes((0x08, 0, *attenuator, 0x01, 0x00)))
        self._command(b"\x08\x00\x06\x06\x06\x06\x01\x01")
        for command in (
            b"\x08\x00\x00\x10\x08\x3a\x04\x00",
            b"\x08\x00\x00\x04\x02\x3b\x04\x00",
            b"\x08\x00\x00\x00\x00\x0f\x04\x00",
            b"\x08\x00\x00\x04\x02\x31\x04\x00",
        ):
            self._command(command)
        packed_12 = gain_nibbles[1] << 4 | gain_nibbles[0]
        packed_34 = gain_nibbles[3] << 4 | gain_nibbles[2]
        self._command(bytes((0x08, 0, 0, packed_12, packed_34, 0x2A, 0x04, 0)))

    def _start(self) -> None:
        mode = {"normal": 0x00, "auto": 0x01, "single": 0x04}[self.config.trigger_sweep]
        self._command(bytes((0x03, 0, mode, 0)))

    @staticmethod
    def _trigger_offset(trigger_word: int, trigger_byte: int) -> int:
        aligned = trigger_word
        remainder = aligned & 7
        if remainder:
            aligned += -remainder if remainder < 5 else 8 - remainder
        offset = aligned + (((-trigger_byte - 1) & 1) - 6) * 4
        return offset % 65536

    def capture(self) -> Capture:
        if not self._initialized:
            raise ProtocolError("scope has not been initialized")
        self._start()
        self._command(b"\x06\x00")
        self.transport.read(512)
        time.sleep(self.config.depth / self.config.sample_rate * 1.5 + 0.005)
        self._command(b"\x06\x00")
        self.transport.read(512)

        self._command(b"\x0d\x00")
        trigger = self.transport.read(512)
        if len(trigger) < 4:
            raise ProtocolError("trigger response is too short")
        trigger_word = trigger[2] | trigger[3] << 8
        offset = (self._trigger_offset(trigger_word, trigger[1]) + 29) & 0xFFFF
        self._command(bytes((0x0E, 0, offset & 0xFF, offset >> 8)))

        byte_count = self.config.depth * 4
        packets = byte_count // 512
        self._command(bytes((0x05, 0, 0, packets)))
        interleaved = np.frombuffer(self.transport.read(byte_count, timeout_ms=5000), dtype=np.uint8)
        raw = interleaved.reshape((-1, 4)).T.copy()
        centers = np.array((128.0, 128.0, 129.0, 128.0))[:, None]
        scales = np.asarray(self.config.volts_per_div, dtype=float)[:, None]
        volts = (raw.astype(float) - centers) / 255.0 * 10.0 * scales
        time_s = np.arange(self.config.depth, dtype=float) / self.config.sample_rate
        return Capture(time_s=time_s, volts=volts, raw=raw, sample_rate=self.config.sample_rate)

    def close(self) -> None:
        self.transport.close()
        self._initialized = False

    def __enter__(self) -> "Hantek6254BC":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
