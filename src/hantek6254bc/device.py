from __future__ import annotations

from dataclasses import dataclass
import struct
import time
from typing import Iterator, Protocol

import numpy as np
from numpy.typing import NDArray

from .errors import ProtocolError
from .transport import UsbTransport

SUPPORTED_DEPTHS = (4096, 8192, 16384)
CHANNELS = 4
PACKET_BYTES = 512
RAM_BYTES = 65536          # Circular capture memory: 4 interleaved channels x 16384 frames
ROLL_MODE = 0x02           # dsoHTStartCollectData nStartControl bit 1 (bit 0 = auto, bit 2 = single)
STREAM_MAX_RATE = 50_000   # ROLL streaming verified at 25 kS/s; faster rates leave too little headroom
STREAM_GUARD_BYTES = 16384 # Never read closer than this to being overwritten (0.16 s at 25 kS/s)
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


@dataclass(frozen=True)
class StreamChunk:
    """Consecutive frames read from the scope while it records continuously in ROLL mode.

    Frames are counted on the scope's sample clock from the start of the stream, so
    chunk.first_frame + n of one chunk equals the next chunk's first_frame unless
    lost_frames > 0 (the reader fell more than the memory behind and those frames were
    overwritten before they could be read).
    """
    raw: NDArray[np.uint8]   # (4, n) wire codes; n is a multiple of 128 frames
    first_frame: int         # Stream frame index of raw[:, 0]
    lost_frames: int         # Frames lost immediately before this chunk (0 = contiguous)
    host_ns: int             # Host time.time_ns() just after the write pointer was read
    host_frame: int          # Stream frame the scope was about to write at host_ns
    backlog_bytes: int       # Unread bytes in scope memory at that pointer read (65536 = full)
    sample_rate: int


def wire_to_volts(raw: NDArray[np.uint8], volts_per_div) -> NDArray[np.float64]:
    """Convert (4, n) wire codes to volts as the vendor software does."""
    # The FPGA wire format spans codes 28..228. The vendor implementation
    # expands those 200 codes to the nominal 8-bit 0..255 sample range.
    samples = np.floor((raw.astype(float) - 28.0) * (256.0 / 200.0) + 0.5)
    samples = np.clip(samples, 0.0, 255.0)
    scales = np.asarray(volts_per_div, dtype=float)[:, None]
    # The vendor API uses 31 normalized ADC codes per vertical division.
    return (samples - 128.0) / 31.0 * scales


class Hantek6254BC:
    """Protocol driver for four-channel Hantek 6004BC-family scopes."""

    def __init__(self, transport: Transport, config: ScopeConfig | None = None):
        self.transport = transport
        self.config = config or ScopeConfig()
        self.fpga_version = 0
        self._position_calibration: tuple[int, ...] | None = None
        self._scale_calibration: tuple[int, ...] | None = None
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
        for _ in range(3):
            self._command(b"\x0c\x00")
        version = self.transport.read(self._response_length())
        if len(version) >= 4 and version[:2] == version[2:4]:
            self.fpga_version = version[0] | version[1] << 8
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
        self._read_calibration()
        self.configure()
        self._initialized = True

    def _read_calibration(self) -> None:
        """Load the unit-specific position and gain tables from EEPROM."""
        self._reset_command_pipe()
        position_data = self.transport.control_in(0xA2, 1154, value=0x1600)
        if len(position_data) == 1154:
            positions = struct.unpack("<577H", position_data)
            if positions[-1] == 0xFBCF:
                self._position_calibration = positions

        self._reset_command_pipe()
        scale_data = self.transport.control_in(0xA2, 578, value=0x1B00)
        if len(scale_data) == 578:
            scales = struct.unpack("<289H", scale_data)
            if scales[-1] == 0xFACF:
                self._scale_calibration = scales

    def configure(self) -> None:
        self._configure_sample_rate()
        self._configure_channels()
        ram_code = 0x3C if self.config.sample_rate == 250_000_000 else 0x3D
        self._command(bytes((0x12, 0, ram_code, 0, 0, self.config.trigger_source)))

        self._configure_channel_positions()
        self._configure_scale_calibration()

        source_vdiv = self.config.volts_per_div[self.config.trigger_source]
        normalized_level = round(self.config.trigger_level * 31.0 / source_vdiv + 128.0)
        normalized_level = min(255, max(0, normalized_level))
        trigger_code = int(normalized_level * 200.0 / 256.0 + 28.5)
        trigger_code = min(224, max(32, trigger_code))
        high, low = trigger_code + 4, trigger_code - 4
        trigger_levels = bytes(
            (0x07, 0x00) + (high, high, low, low) * 4 + (trigger_code,) * 8
        )
        self._command(trigger_levels)
        slope = 0 if self.config.trigger_slope == "rising" else 1
        self._command(bytes((0x11, 0, 0, slope, 0, 0)))

    def _position_pwm(self, channel: int, vdiv: float, level: int = 128) -> int | None:
        calibration = self._position_calibration
        if calibration is None:
            return None
        ordinal = SUPPORTED_VDIV.index(vdiv)
        # The analog ranges share EEPROM endpoints; sensitive ranges narrow the
        # calibrated endpoint span by the front-end gain factor.
        lookup_and_divisor = {
            0: (5, 50), 1: (5, 20), 2: (5, 10), 3: (5, 5), 4: (5, 2),
            6: (8, 5), 7: (8, 2), 9: (11, 5), 10: (11, 2),
        }
        lookup, divisor = lookup_and_divisor.get(ordinal, (ordinal, 1))
        index = channel * 144 + lookup * 12 + 4  # +4 selects four-channel mode
        low, high = calibration[index], calibration[index + 1]
        if low in (0, 0xFFFF) or high in (0, 0xFFFF) or high <= low:
            return None
        if divisor != 1:
            midpoint = int((low + high) / 2.0 + 0.5)
            half_span = int((high - midpoint) / divisor + 0.5)
            low, high = midpoint - half_span, midpoint + half_span
        return int((high - low) * level / 255.0 + low) & 0xFFFF

    def _configure_channel_positions(self) -> None:
        fallback = (0x71C2, 0x712C, 0x72AD, 0x7239)
        selectors = (0, 1, 2, 4)
        for channel, (selector, fallback_pwm) in enumerate(zip(selectors, fallback)):
            pwm = self._position_pwm(channel, self.config.volts_per_div[channel])
            if pwm is None:
                pwm = fallback_pwm
            self._command(bytes((selector, 0, pwm & 0xFF, pwm >> 8)))

    def _configure_scale_calibration(self) -> None:
        calibration = self._scale_calibration
        if calibration is None:
            return
        coefficients = []
        corrections = []
        for channel, vdiv in enumerate(self.config.volts_per_div):
            ordinal = SUPPORTED_VDIV.index(vdiv)
            lookup = 5 if ordinal <= 5 else 7 if ordinal <= 8 else 9
            coefficient = calibration[channel * 72 + lookup * 6 + 2]
            if coefficient in (0, 0xFFFF):
                return
            coefficients.append(coefficient)
            wire_level = 128
            correction = int((1.0 - coefficient / 1024.0) * wire_level * 1024.0 + 0.5)
            corrections.append(correction if correction >= 0 else correction + 0x2000000)

        command = bytearray((0x1E, 0))
        for coefficient in coefficients:
            command.extend(coefficient.to_bytes(2, "little"))
        for correction in corrections:
            command.extend(correction.to_bytes(4, "little"))
        self._command(bytes(command))

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

    def _acquisition_ready(self) -> bool:
        """Return the FPGA capture-complete flag from command 0x06."""
        self._command(b"\x06\x00")
        state = self.transport.read(512)
        if len(state) < 2:
            raise ProtocolError("acquisition state response is too short")
        return bool((state[0] | state[1] << 8) & 0x02)

    def _wait_until_ready(self) -> None:
        # The vendor Android driver polls every 3 ms. Allow slow triggered
        # acquisitions enough time while still reporting a disconnected trigger.
        deadline = time.monotonic() + max(2.0, self.config.depth / self.config.sample_rate * 4.0)
        while time.monotonic() < deadline:
            if self._acquisition_ready():
                return
            time.sleep(0.003)
        raise ProtocolError(
            "acquisition did not become ready within the timeout; check the trigger "
            "level/source or use --sweep auto"
        )

    def _read_capture_memory(self, byte_count: int) -> bytes:
        """Read capture RAM one USB max-packet at a time as required by the FPGA."""
        if byte_count % 512:
            raise ProtocolError("capture byte count is not aligned to a USB packet")
        chunks = []
        empty_packet = bytes(512)
        for packet_index in range(byte_count // 512):
            packet = self.transport.read(512, timeout_ms=5000)
            if packet == empty_packet:
                raise ProtocolError(
                    f"scope returned an empty data packet at packet {packet_index + 1}; "
                    "capture memory was not ready"
                )
            chunks.append(packet)
        return b"".join(chunks)

    def _capture_offset(self, trigger: bytes) -> int:
        """Calculate the circular-RAM start address from FPGA trigger metadata."""
        if len(trigger) < 5:
            raise ProtocolError("trigger response is too short")
        trigger_word = trigger[2] | trigger[3] << 8
        channel_count = 4

        # FPGA versions A00A/A00B use a slightly different acquisition clock;
        # all known E-series revisions, including the connected E003, use 230.
        clock_code = 246 if self.fpga_version in (0xA00A, 0xA00B) else 230
        sampling_divisor = 1_000_000_000.0 / self.config.sample_rate
        sample_correction = int(clock_code / sampling_divisor)

        phase = (7 - trigger[4]) & 7
        phase = (phase & 1) - 6
        pretrigger_bytes = channel_count * self.config.depth
        return (
            trigger_word
            - pretrigger_bytes
            + phase * channel_count
            - sample_correction * channel_count
        ) % 65536

    def capture(self) -> Capture:
        if not self._initialized:
            raise ProtocolError("scope has not been initialized")
        self._start()
        self._wait_until_ready()

        self._command(b"\x0d\x00")
        trigger = self.transport.read(512)
        offset = self._capture_offset(trigger)
        self._command(bytes((0x0E, 0, offset & 0xFF, offset >> 8)))

        byte_count = self.config.depth * 4
        packets = byte_count // 512
        self._command(bytes((0x05, 0, 0, packets)))
        frame = self._read_capture_memory(byte_count)
        interleaved = np.frombuffer(frame, dtype=np.uint8)
        raw = interleaved.reshape((-1, 4)).T.copy()
        volts = wire_to_volts(raw, self.config.volts_per_div)
        time_s = np.arange(self.config.depth, dtype=float) / self.config.sample_rate
        return Capture(time_s=time_s, volts=volts, raw=raw, sample_rate=self.config.sample_rate)

    def _roll_pointer(self) -> tuple[int, float]:
        """Byte address the FPGA writes next in ROLL mode, and host monotonic time after reading it."""
        self._command(b"\x0d\x00")
        response = self.transport.read(PACKET_BYTES)
        now = time.monotonic()
        if len(response) < 4:
            raise ProtocolError("write-pointer response is too short")
        address = response[2] | response[3] << 8
        if address % CHANNELS:
            raise ProtocolError(f"write pointer 0x{address:04x} is not on a 4-channel frame boundary")
        return address, now

    def _read_memory(self, address: int, byte_count: int) -> bytes:
        """Read capture memory from any frame-aligned address (reads wrap at 64 KB)."""
        packets = byte_count // PACKET_BYTES
        if byte_count % PACKET_BYTES or not 0 < packets <= 128:
            raise ProtocolError("memory reads must be 1 to 128 whole USB packets")
        address %= RAM_BYTES
        self._command(bytes((0x0E, 0, address & 0xFF, address >> 8)))
        self._command(bytes((0x05, 0, 0, packets)))
        return b"".join(self.transport.read(PACKET_BYTES, timeout_ms=5000) for _ in range(packets))

    def stream(self, poll_interval: float = 0.02) -> Iterator[StreamChunk]:
        """Record continuously in ROLL mode and yield every frame exactly once, in order.

        In ROLL mode the FPGA never stops: it writes all four channels round its 64 KB
        memory (0.655 s at 25 kS/s) while this generator follows the write pointer
        (bytes 2-3 of the 0x0D response) and reads everything behind it. Nothing is
        lost as long as the consumer asks for the next chunk well within that time;
        if it falls further behind, the overwritten frames are reported in
        StreamChunk.lost_frames, never silently joined. Close the generator to stop.
        """
        if not self._initialized:
            raise ProtocolError("scope has not been initialized")
        if self.config.sample_rate > STREAM_MAX_RATE:
            raise ValueError(f"ROLL streaming supports sample rates up to {STREAM_MAX_RATE} S/s")
        rate = CHANNELS * self.config.sample_rate  # Bytes written per second
        self._command(bytes((0x03, 0, ROLL_MODE, 0)))
        time.sleep(0.05)  # As in the hardware verification: let ROLL acquisition settle first
        base, last_t = self._roll_pointer()  # Stream byte 0: the first byte written after this read
        last_address = base
        written = consumed = 0  # Stream bytes written by the FPGA / handed to the caller
        while True:
            loop_start = time.monotonic()
            address, now = self._roll_pointer()
            host_ns = time.time_ns()
            step = (address - last_address) % RAM_BYTES
            expected = (now - last_t) * rate
            # The pointer is only 16 bits; after a stall longer than the memory it has
            # wrapped, and the host clock says how many times.
            step += RAM_BYTES * max(0, round((expected - step) / RAM_BYTES))
            if abs(step - expected) > RAM_BYTES / 4:
                raise ProtocolError(f"write pointer moved {step} bytes in {now - last_t:.3f} s; "
                                    f"expected about {expected:.0f}")
            written += step
            last_address, last_t = address, now
            backlog = written - consumed
            lost = max(0, backlog - (RAM_BYTES - STREAM_GUARD_BYTES))
            consumed += lost
            packets = (written - consumed) // PACKET_BYTES
            if packets:
                data = self._read_memory(base + consumed, packets * PACKET_BYTES)
                raw = np.frombuffer(data, dtype=np.uint8).reshape((-1, CHANNELS)).T.copy()
                yield StreamChunk(raw=raw, first_frame=consumed // CHANNELS, lost_frames=lost // CHANNELS,
                                  host_ns=host_ns, host_frame=written // CHANNELS,
                                  backlog_bytes=min(backlog, RAM_BYTES), sample_rate=self.config.sample_rate)
                consumed += packets * PACKET_BYTES
            time.sleep(max(0.0, poll_interval - (time.monotonic() - loop_start)))

    def close(self) -> None:
        self.transport.close()
        self._initialized = False

    def __enter__(self) -> "Hantek6254BC":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
