"""Device discovery and property queries."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from . import adb, fastboot

log = logging.getLogger(__name__)


@dataclass
class Device:
    serial: str
    state: str  # 'device', 'recovery', 'sideload', 'fastboot', 'unauthorized', 'offline'
    properties: dict[str, str] = field(default_factory=dict)
    rooted: bool = False

    @property
    def model(self) -> str:
        return self.properties.get("ro.product.model", "unknown")

    @property
    def fingerprint(self) -> str:
        return self.properties.get("ro.build.fingerprint", "unknown")

    @property
    def chipset(self) -> str:
        return (
            self.properties.get("ro.hardware")
            or self.properties.get("ro.board.platform")
            or "unknown"
        )

    @property
    def is_mediatek(self) -> bool:
        return any(
            "mt" in self.properties.get(k, "").lower() or "mediatek" in self.properties.get(k, "").lower()
            for k in ("ro.hardware", "ro.board.platform", "ro.mediatek.platform")
        )


def _parse_getprop(blob: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in blob.splitlines():
        line = line.strip()
        if not line.startswith("[") or "]: [" not in line:
            continue
        try:
            key, val = line.split("]: [", 1)
            key = key[1:]
            val = val.rstrip("]")
            props[key] = val
        except ValueError:
            continue
    return props


def discover() -> list[Device]:
    """Return all currently attached devices in any mode (adb or fastboot)."""
    devices: list[Device] = []

    for serial, state in adb.list_devices():
        dev = Device(serial=serial, state=state)
        if state == "device":
            try:
                dev.properties = _parse_getprop(adb.shell("getprop", serial=serial))
                dev.rooted = adb.has_root(serial=serial)
            except Exception as e:
                log.warning("Could not query properties for %s: %s", serial, e)
        devices.append(dev)

    for serial in fastboot.list_devices():
        # Best-effort: collect a few common fastboot vars.
        props: dict[str, str] = {}
        for var in ("product", "version-bootloader", "unlocked", "secure", "serialno"):
            try:
                props[f"fastboot.{var}"] = fastboot.getvar(var, serial=serial)
            except Exception:
                pass
        devices.append(Device(serial=serial, state="fastboot", properties=props))

    return devices


def pick_one(devices: list[Device], requested: Optional[str] = None) -> Device:
    """Pick the right device given the user's --serial preference."""
    if not devices:
        raise RuntimeError("No devices attached. Plug in a phone and enable USB debugging.")
    if requested:
        for d in devices:
            if d.serial == requested:
                return d
        raise RuntimeError(f"Device {requested!r} not attached.")
    if len(devices) == 1:
        return devices[0]
    raise RuntimeError(
        f"Multiple devices attached: {[d.serial for d in devices]}. "
        "Specify one with --serial."
    )
