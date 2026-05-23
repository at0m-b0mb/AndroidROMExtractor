"""Device discovery and property queries."""
from __future__ import annotations

import logging
import platform
import re
import subprocess
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


# --- MediaTek preloader / BROM detection ------------------------------------
# When a MTK phone is in download (preloader) or BROM mode, neither adb nor
# fastboot can see it — only raw USB enumeration. Detecting this lets the GUI
# tell the user "you need mtkclient / SP Flash Tool" instead of "no device".

MTK_VID = 0x0e8d
MTK_PIDS = {
    0x2000: "MT65xx Preloader (download mode)",
    0x0003: "MT BROM (boot ROM mode)",
    0x2001: "MT preloader (alt)",
}


@dataclass
class MtkUsbDevice:
    pid: int
    description: str

    @property
    def vid_pid(self) -> str:
        return f"{MTK_VID:04x}:{self.pid:04x}"


def detect_mtk_preloader() -> list[MtkUsbDevice]:
    """Best-effort detection of a MediaTek phone in preloader/BROM mode.

    Currently macOS-only (system_profiler). Returns [] on other platforms or
    when no MTK device is found. Never raises.
    """
    if platform.system() != "Darwin":
        return []
    try:
        out = subprocess.run(
            ["system_profiler", "SPUSBDataType"],
            capture_output=True, text=True, timeout=8,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    # system_profiler emits one indented block per USB device, looking like:
    #     <DeviceName>:
    #
    #       Product ID: 0x2000
    #       Vendor ID: 0x0e8d  (MediaTek Inc.)
    #       Version: 1.00
    #       ...
    # Field order varies by macOS version. Walk line-by-line: when the
    # indentation level shrinks or a new device label appears, reset the
    # pending vid/pid pair.
    found: list[MtkUsbDevice] = []
    seen: set[int] = set()
    pending_vid: Optional[int] = None
    pending_pid: Optional[int] = None
    pending_indent = -1

    def commit():
        if pending_vid == MTK_VID and pending_pid is not None \
                and pending_pid not in seen:
            seen.add(pending_pid)
            desc = MTK_PIDS.get(
                pending_pid, f"MediaTek device (pid 0x{pending_pid:04x})")
            found.append(MtkUsbDevice(pid=pending_pid, description=desc))

    for line in out.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        # A device-label line ends with `:` and has no `0x` / no value.
        is_label = line.rstrip().endswith(":") and "0x" not in line
        # New device block (dedent to a label) -> commit current.
        if is_label and indent <= pending_indent:
            commit()
            pending_vid = pending_pid = None
            pending_indent = indent
            continue
        if is_label:
            pending_indent = indent
            continue
        m_vid = re.search(r"Vendor ID:\s*0x0*([0-9a-fA-F]+)", line)
        m_pid = re.search(r"Product ID:\s*0x0*([0-9a-fA-F]+)", line)
        if m_vid:
            pending_vid = int(m_vid.group(1), 16)
        if m_pid:
            pending_pid = int(m_pid.group(1), 16)
        # Commit eagerly once we have both — handles the very last block too.
        if pending_vid is not None and pending_pid is not None:
            commit()
            pending_vid = pending_pid = None
    commit()
    return found


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
