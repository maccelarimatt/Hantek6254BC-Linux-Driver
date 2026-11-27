"""Linux userspace driver for Hantek 6004BC-series oscilloscopes."""

from .device import Capture, Hantek6254BC, ScopeConfig, StreamChunk, wire_to_volts
from .errors import DeviceNotFound, HantekError, PermissionDenied, ProtocolError

__all__ = [
    "Capture",
    "DeviceNotFound",
    "Hantek6254BC",
    "HantekError",
    "PermissionDenied",
    "ProtocolError",
    "ScopeConfig",
    "StreamChunk",
    "wire_to_volts",
]

__version__ = "0.1.0"

