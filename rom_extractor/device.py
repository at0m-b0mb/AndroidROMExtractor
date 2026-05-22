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


@dataclass
class DeviceHealth:
    """Optional ambient device info: battery level, /data free, etc."""
    battery_level: Optional[int] = None     # 0-100
    battery_status: Optional[str] = None    # 'Charging', 'Discharging', 'Full', ...
    battery_temp_c: Optional[float] = None
    data_total_bytes: Optional[int] = None
    data_free_bytes: Optional[int] = None


def _parse_dumpsys_battery(blob: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in blob.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def _parse_df_one_line(blob: str) -> tuple[Optional[int], Optional[int]]:
    """Parse `df -k -P /data` output. Returns (total_bytes, free_bytes)."""
    lines = [l for l in blob.splitlines() if l.strip()]
    if len(lines) < 2:
        return None, None
    parts = lines[-1].split()
    # POSIX format: Filesystem 1024-blocks Used Available Capacity Mounted on
    if len(parts) < 5:
        return None, None
    try:
        total_kb = int(parts[1])
        free_kb = int(parts[3])
        return total_kb * 1024, free_kb * 1024
    except (ValueError, IndexError):
        return None, None


def query_health(serial: Optional[str] = None) -> DeviceHealth:
    """Best-effort fetch of battery + /data storage. Never raises."""
    health = DeviceHealth()
    try:
        blob = adb.shell("dumpsys battery", serial=serial, check=False,
                         timeout=5)
        b = _parse_dumpsys_battery(blob)
        if "level" in b:
            try:
                health.battery_level = int(b["level"])
            except ValueError:
                pass
        if "status" in b:
            health.battery_status = {
                "1": "Unknown", "2": "Charging", "3": "Discharging",
                "4": "Not charging", "5": "Full",
            }.get(b["status"], b["status"])
        if "temperature" in b:
            try:
                # value is in 0.1 °C
                health.battery_temp_c = int(b["temperature"]) / 10.0
            except ValueError:
                pass
    except Exception as e:
        log.debug("battery query failed: %s", e)

    try:
        blob = adb.shell("df -k -P /data", serial=serial, check=False,
                         timeout=5)
        total, free = _parse_df_one_line(blob)
        health.data_total_bytes = total
        health.data_free_bytes = free
    except Exception as e:
        log.debug("storage query failed: %s", e)

    return health


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
