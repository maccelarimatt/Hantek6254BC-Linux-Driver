import numpy as np
import pytest

from hantek6254bc.device import Hantek6254BC, RATE_TABLE, ScopeConfig


class FakeTransport:
    def __init__(self):
        self.commands = []
        self.reads = []
        self.closed = False

    def control_out(self, request, data, value=0):
        return len(data)

    def control_in(self, request, length, value=0):
        return bytes((0,)) + bytes(length - 1)

    def write(self, data):
        self.commands.append(bytes(data))

    def read(self, length, timeout_ms=2000):
        if self.reads:
            data = self.reads.pop(0)
            assert len(data) == length
            return data
        return bytes(length)

    def settle(self):
        pass

    def close(self):
        self.closed = True


def test_config_validation():
    with pytest.raises(ValueError):
        ScopeConfig(sample_rate=123)
    with pytest.raises(ValueError):
        ScopeConfig(depth=1000)
    with pytest.raises(ValueError):
        ScopeConfig(volts_per_div=(1.0,))


@pytest.mark.parametrize("rate", RATE_TABLE)
@pytest.mark.parametrize("depth", (4096, 8192, 16384))
def test_all_rate_and_depth_commands_fit(rate, depth):
    transport = FakeTransport()
    scope = Hantek6254BC(transport, ScopeConfig(sample_rate=rate, depth=depth))
    scope._configure_sample_rate()
    timing = transport.commands[-1]
    assert len(timing) == 14
    assert timing[:2] == b"\x10\x00"


def test_capture_deinterleaves_and_scales(monkeypatch):
    config = ScopeConfig(depth=4096, volts_per_div=(1.0, 0.5, 0.2, 0.1))
    transport = FakeTransport()
    # Wire values corresponding to normalized center codes 128, 128, 129, 128.
    raw_row = bytes((128, 128, 129, 128))
    ready = bytes((2,)) + bytes(511)
    frame = raw_row * config.depth
    transport.reads = [ready, bytes(512)] + [frame[i:i + 512] for i in range(0, len(frame), 512)]
    scope = Hantek6254BC(transport, config)
    scope._initialized = True
    result = scope.capture()
    assert result.raw.shape == (4, config.depth)
    assert result.volts.shape == (4, config.depth)
    # Conversion is quantized because the hardware transmits only 200 ADC codes.
    assert np.max(np.abs(result.volts)) < 0.03
    assert result.time_s[-1] == pytest.approx((config.depth - 1) / config.sample_rate)


def test_capture_polls_until_fpga_is_ready(monkeypatch):
    monkeypatch.setattr("hantek6254bc.device.time.sleep", lambda _seconds: None)
    config = ScopeConfig(depth=4096)
    transport = FakeTransport()
    waiting = bytes(512)
    ready = bytes((2,)) + bytes(511)
    frame = bytes((128, 128, 129, 128)) * config.depth
    transport.reads = [waiting, waiting, ready, bytes(512)] + [
        frame[i:i + 512] for i in range(0, len(frame), 512)
    ]
    scope = Hantek6254BC(transport, config)
    scope._initialized = True
    result = scope.capture()
    assert result.raw.shape == (4, 4096)
    assert transport.commands.count(b"\x06\x00") == 3


def test_capture_offset_uses_trigger_phase_and_pretrigger_depth():
    scope = Hantek6254BC(FakeTransport(), ScopeConfig(sample_rate=2_500_000, depth=4096))
    scope.fpga_version = 0xE003
    trigger = bytes((0xC8, 0x2D, 0xF0, 0x4D, 0x07))
    assert scope._capture_offset(trigger) == 0x0DD8


def test_capture_offset_applies_high_rate_fpga_clock_correction():
    scope = Hantek6254BC(FakeTransport(), ScopeConfig(sample_rate=250_000_000, depth=4096))
    scope.fpga_version = 0xE003
    trigger = bytes((0, 0, 0, 0x40, 0x07))
    assert scope._capture_offset(trigger) == (0x4000 - 0x4000 - 24 - 57 * 4) % 65536


def test_empty_capture_packet_is_rejected():
    config = ScopeConfig(depth=4096)
    transport = FakeTransport()
    ready = bytes((2,)) + bytes(511)
    transport.reads = [ready, bytes(512), bytes(512)]
    scope = Hantek6254BC(transport, config)
    scope._initialized = True
    with pytest.raises(Exception, match="empty data packet"):
        scope.capture()


def test_channel_gain_command():
    transport = FakeTransport()
    config = ScopeConfig(volts_per_div=(0.002, 0.2, 2.0, 10.0))
    scope = Hantek6254BC(transport, config)
    scope._configure_channels()
    assert transport.commands[0] == bytes((8, 0, 0x2E, 0x36, 0x56, 0x56, 1, 0))
    assert transport.commands[-1][3:5] == bytes((0x5D, 0x05))


def test_trigger_voltage_and_falling_edge_commands():
    transport = FakeTransport()
    config = ScopeConfig(
        volts_per_div=(0.2,) * 4,
        trigger_level=0.1,
        trigger_slope="falling",
    )
    scope = Hantek6254BC(transport, config)
    scope.configure()
    trigger_command = next(command for command in transport.commands if command[:2] == b"\x07\x00")
    # 0.1 V at 0.2 V/div is half a division above the ADC midpoint.
    assert trigger_command[2:6] == bytes((145, 145, 137, 137))
    assert next(command for command in transport.commands if command[:2] == b"\x11\x00")[3] == 1


def test_unit_position_and_scale_calibration_commands():
    transport = FakeTransport()
    scope = Hantek6254BC(transport, ScopeConfig(volts_per_div=(0.2,) * 4))
    positions = [0] * 577
    scales = [1024] * 289
    for channel, (low, high, coefficient) in enumerate(
        ((13991, 43407, 1016), (14158, 43373, 1017),
         (13962, 43299, 1020), (13653, 43168, 1029))
    ):
        positions[channel * 144 + 100] = low
        positions[channel * 144 + 101] = high
        scales[channel * 72 + 44] = coefficient
    positions[-1] = 0xFBCF
    scales[-1] = 0xFACF
    scope._position_calibration = tuple(positions)
    scope._scale_calibration = tuple(scales)
    scope._configure_channel_positions()
    scope._configure_scale_calibration()
    assert [int.from_bytes(command[2:4], "little") for command in transport.commands[:4]] == [
        28710, 28777, 28642, 28422
    ]
    assert transport.commands[4][:10] == bytes.fromhex("1e00f803f903fc030504")
    assert len(transport.commands[4]) == 26
