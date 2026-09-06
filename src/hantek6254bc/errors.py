class HantekError(RuntimeError):
    """Base exception for scope errors."""


class DeviceNotFound(HantekError):
    """No matching USB scope was found."""


class PermissionDenied(HantekError):
    """The scope exists but the current user cannot open it."""


class ProtocolError(HantekError):
    """The scope returned an incomplete or invalid protocol response."""

