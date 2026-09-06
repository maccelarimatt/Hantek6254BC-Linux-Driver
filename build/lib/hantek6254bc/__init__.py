"""Linux userspace driver for Hantek 6004BC-series oscilloscopes."""

from .device import Capture, Hantek6254BC, ScopeConfig
from .errors import DeviceNotFound, HantekError, PermissionDenied, ProtocolError

__all__ = [
    "Capture",
    "DeviceNotFound",
    "Hantek6254BC",
    "HantekError",
    "PermissionDenied",
    "ProtocolError",
    "ScopeConfig",
]

__version__ = "0.1.0"

