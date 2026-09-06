import numpy as np
import pytest

from hantek6254bc.gui import format_rate, format_time, waveform_measurements


def test_scope_control_labels():
    assert format_rate(2_500_000) == "2.5 MS/s"
    assert format_rate(25_000) == "25 kS/s"
    assert format_time(0.0002) == "200 µs"
    assert format_time(0.02) == "20 ms"


def test_waveform_measurements_finds_sine_amplitude_and_frequency():
    time_s = np.arange(10_000, dtype=float) / 1_000_000
    volts = 0.5 * np.sin(2 * np.pi * 1_000 * time_s)
    vpp, frequency = waveform_measurements(time_s, volts, vdiv=0.2)
    assert vpp == pytest.approx(1.0, rel=1e-4)
    assert frequency == pytest.approx(1_000, rel=1e-4)


def test_waveform_measurements_suppresses_frequency_for_flat_trace():
    time_s = np.arange(100, dtype=float) / 1_000
    vpp, frequency = waveform_measurements(time_s, np.zeros(100), vdiv=1.0)
    assert vpp == 0.0
    assert frequency is None
