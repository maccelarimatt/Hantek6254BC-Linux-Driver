"""ROLL-mode streaming against a simulated FPGA that writes its circular memory in real time."""
import numpy as np
import pytest

from hantek6254bc import device
from hantek6254bc.device import Hantek6254BC, ProtocolError, ScopeConfig

RATE = 25_000
RAM = device.RAM_BYTES


def truth(stream_bytes):
    """Wire code written at each absolute stream byte: a distinct slow ramp per channel."""
    frame, channel = np.divmod(np.asarray(stream_bytes), 4)
    return (28 + (frame * (channel + 1) + 37 * channel) % 201).astype(np.uint8)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time_ns(self):
        return int(self.now * 1e9)

    def sleep(self, seconds):
        self.now += max(0.0, seconds)


class RollTransport:
    """Scope whose ROLL acquisition advances with the virtual clock; commands take 1 ms."""

    def __init__(self, clock, start_address=0x1234, misaligned=False):
        self.clock, self.queue = clock, []
        self.start_address, self.misaligned = start_address, misaligned
        self.started_at = None
        self.read_address = 0
        self.base = None  # Stream bytes already written when the driver first read the pointer

    def written(self):  # Stream bytes written since ROLL start, whole frames only
        return int((self.clock.now - self.started_at) * 4 * RATE) // 4 * 4

    def control_out(self, request, data, value=0):
        return len(data)

    def control_in(self, request, length, value=0):
        return bytes(length)

    def settle(self):
        self.clock.now += 0.001

    def write(self, data):
        command = data[0]
        if command == 0x03:
            assert data[2] == device.ROLL_MODE
            self.started_at = self.clock.now
        elif command == 0x0D:
            if self.base is None:
                self.base = self.written()
            address = (self.start_address + self.written() + (2 if self.misaligned else 0)) % RAM
            self.queue.append(bytes((0x78, 0x89, address & 0xFF, address >> 8, 7)) + bytes(507))
        elif command == 0x0E:
            self.read_address = data[2] | data[3] << 8
        elif command == 0x05:
            head = self.written()
            for packet in range(data[3]):
                address = (self.read_address + 512 * packet + np.arange(512)) % RAM
                # Most recent stream byte stored at each address.
                stream = head - 1 - ((self.start_address + head - 1 - address) % RAM)
                self.queue.append(truth(stream).tobytes())
                self.clock.now += 0.0002  # USB time per packet

    def read(self, length, timeout_ms=2000):
        data = self.queue.pop(0)
        assert len(data) == length
        return data

    def close(self):
        pass


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(device.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(device.time, "time_ns", clock.time_ns)
    monkeypatch.setattr(device.time, "sleep", clock.sleep)
    return clock


def open_scope(transport):
    scope = Hantek6254BC(transport, ScopeConfig(sample_rate=RATE))
    scope._initialized = True
    return scope


def frames_of(chunk):
    return chunk.first_frame + np.arange(chunk.raw.shape[1])


def expected_raw(transport, frames):
    """The driver numbers frames from its first pointer read, the FPGA from ROLL start."""
    stream = transport.base + frames[:, None] * 4 + np.arange(4)
    return truth(stream).T


def test_stream_is_contiguous_and_matches_memory(clock):
    transport = RollTransport(clock)
    stream = open_scope(transport).stream(poll_interval=0.02)
    chunks = [next(stream) for _ in range(200)]  # About 4 s, several trips round the memory
    stream.close()
    assert chunks[0].first_frame == 0
    for previous, chunk in zip(chunks, chunks[1:]):
        assert chunk.first_frame == previous.first_frame + previous.raw.shape[1]
        assert chunk.lost_frames == 0
    frames = np.concatenate([frames_of(c) for c in chunks])
    raw = np.concatenate([c.raw for c in chunks], axis=1)
    assert np.array_equal(raw, expected_raw(transport, frames))
    assert frames[-1] > 2 * RAM // 4
    last = chunks[-1]
    assert last.host_frame >= last.first_frame + last.raw.shape[1]
    assert max(c.backlog_bytes for c in chunks) < 8192


def test_slow_consumer_reports_exact_loss_and_resumes(clock):
    transport = RollTransport(clock)
    stream = open_scope(transport).stream(poll_interval=0.02)
    before = [next(stream) for _ in range(20)]
    clock.now += 2.3  # Consumer stalls for longer than three trips round the memory
    after = [next(stream) for _ in range(20)]
    stream.close()
    end = before[-1].first_frame + before[-1].raw.shape[1]
    gap = after[0]
    assert gap.lost_frames > 0
    assert gap.first_frame == end + gap.lost_frames  # The loss is counted exactly
    assert gap.backlog_bytes == RAM
    assert all(c.lost_frames == 0 for c in after[1:])
    for chunk in before + after:  # Data after the gap is still the right data
        assert np.array_equal(chunk.raw, expected_raw(transport, frames_of(chunk)))


def test_misaligned_pointer_is_rejected(clock):
    stream = open_scope(RollTransport(clock, misaligned=True)).stream()
    with pytest.raises(ProtocolError, match="frame boundary"):
        next(stream)


def test_stream_requires_supported_rate():
    scope = Hantek6254BC(RollTransport(Clock()), ScopeConfig(sample_rate=125_000))
    scope._initialized = True
    with pytest.raises(ValueError, match="sample rates up to"):
        next(scope.stream())
