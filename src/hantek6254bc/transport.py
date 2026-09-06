from __future__ import annotations

import errno
import time
from typing import Iterable

from .errors import DeviceNotFound, PermissionDenied, ProtocolError

USB_VENDOR_ID = 0x04B5
USB_PRODUCT_ID = 0x6CDE
INTERFACE = 0
ENDPOINT_OUT = 0x02
ENDPOINT_IN = 0x86


class UsbTransport:
    """Small PyUSB transport for the vendor-specific Hantek interface."""

    def __init__(self, device, usb_core, usb_util):
        self.device = device
        self._usb_core = usb_core
        self._usb_util = usb_util
        self._claimed = False
        self._detached_kernel_driver = False

    @classmethod
    def open(cls, bus: int | None = None, address: int | None = None) -> "UsbTransport":
        try:
            import usb.core
            import usb.util
        except ImportError as exc:
            raise RuntimeError(
                "PyUSB is not installed. Run ./install.sh or install this package first."
            ) from exc

        matches: Iterable = usb.core.find(
            find_all=True, idVendor=USB_VENDOR_ID, idProduct=USB_PRODUCT_ID
        ) or []
        devices = [
            dev
            for dev in matches
            if (bus is None or getattr(dev, "bus", None) == bus)
            and (address is None or getattr(dev, "address", None) == address)
        ]
        if not devices:
            where = ""
            if bus is not None or address is not None:
                where = f" at bus {bus!s}, address {address!s}"
            raise DeviceNotFound(
                f"Hantek 6254BC USB device 04b5:6cde not found{where}. "
                "Reconnect it and run 'hantek6254bc doctor'."
            )
        if len(devices) > 1 and bus is None and address is None:
            locations = ", ".join(
                f"{getattr(d, 'bus', '?')}:{getattr(d, 'address', '?')}" for d in devices
            )
            raise DeviceNotFound(
                f"Multiple scopes found ({locations}); select one with --bus and --device."
            )

        transport = cls(devices[0], usb.core, usb.util)
        transport._claim()
        return transport

    @property
    def bus(self) -> int | None:
        return getattr(self.device, "bus", None)

    @property
    def address(self) -> int | None:
        return getattr(self.device, "address", None)

    def _claim(self) -> None:
        try:
            if self.device.is_kernel_driver_active(INTERFACE):
                self.device.detach_kernel_driver(INTERFACE)
                self._detached_kernel_driver = True
        except (NotImplementedError, self._usb_core.USBError):
            pass

        try:
            self.device.set_configuration()
            self._usb_util.claim_interface(self.device, INTERFACE)
            self._claimed = True
            self.device.set_interface_altsetting(interface=INTERFACE, alternate_setting=0)
        except self._usb_core.USBError as exc:
            self.close()
            if exc.errno in (errno.EACCES, errno.EPERM, 13):
                raise PermissionDenied(
                    "Permission denied opening 04b5:6cde. Install the udev rule with "
                    "'sudo ./install-udev.sh', then unplug and reconnect the scope."
                ) from exc
            raise ProtocolError(f"Could not claim the scope USB interface: {exc}") from exc

    def control_out(self, request: int, data: bytes, value: int = 0) -> int:
        try:
            return int(self.device.ctrl_transfer(0x40, request, value, 0, data, timeout=1000))
        except self._usb_core.USBError as exc:
            raise ProtocolError(f"USB control OUT request 0x{request:02x} failed: {exc}") from exc

    def control_in(self, request: int, length: int, value: int = 0) -> bytes:
        try:
            return bytes(
                self.device.ctrl_transfer(0xC0, request, value, 0, length, timeout=1000)
            )
        except self._usb_core.USBError as exc:
            raise ProtocolError(f"USB control IN request 0x{request:02x} failed: {exc}") from exc

    def write(self, data: bytes) -> None:
        try:
            sent = int(self.device.write(ENDPOINT_OUT, data, timeout=1000))
        except self._usb_core.USBError as exc:
            raise ProtocolError(f"USB bulk write failed: {exc}") from exc
        if sent != len(data):
            raise ProtocolError(f"Short USB write: sent {sent} of {len(data)} bytes")

    def read(self, length: int, timeout_ms: int = 2000) -> bytes:
        try:
            data = bytes(self.device.read(ENDPOINT_IN, length, timeout=timeout_ms))
        except self._usb_core.USBError as exc:
            raise ProtocolError(f"USB bulk read of {length} bytes failed: {exc}") from exc
        if len(data) != length:
            raise ProtocolError(f"Short USB read: received {len(data)} of {length} bytes")
        return data

    def settle(self) -> None:
        time.sleep(0.001)

    def close(self) -> None:
        if self.device is None:
            return
        try:
            if self._claimed:
                self._usb_util.release_interface(self.device, INTERFACE)
        except Exception:
            pass
        try:
            self._usb_util.dispose_resources(self.device)
        except Exception:
            pass
        if self._detached_kernel_driver:
            try:
                self.device.attach_kernel_driver(INTERFACE)
            except Exception:
                pass
        self._claimed = False
        self.device = None

    def __enter__(self) -> "UsbTransport":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

