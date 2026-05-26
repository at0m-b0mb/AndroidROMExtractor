"""customtkinter GUI for android-rom-extractor.

A polished dark-mode desktop app: sidebar navigation, status pill, empty
states, toast notifications, confirm dialogs, and live per-operation progress.
"""
from __future__ import annotations

import json
import logging
import platform
import queue
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
import traceback
from datetime import date
from pathlib import Path
from tkinter import filedialog
from typing import Callable, Optional

import customtkinter as ctk

from . import __version__, adb as adb_mod, apps as apps_mod, backup as backup_mod
from . import boot_analyze
from . import device as device_mod, fastboot as fastboot_mod, flash as flash_mod
from . import partitions as part_mod
from . import settings as settings_mod
from . import verify as verify_mod
from .device import Device
from .manifest import Manifest, MANIFEST_FILENAME
from .partitions import (DEFAULT_BACKUP_SET, MTK_CRITICAL, Partition,
                         is_dangerous)
from .utils import human_size, notify, sha256_file

IS_MAC = platform.system() == "Darwin"
MOD_KEY = "Command" if IS_MAC else "Control"
MOD_LABEL = "⌘" if IS_MAC else "Ctrl"

log = logging.getLogger(__name__)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ----------------------------------------------------------------------------
# Design tokens
# ----------------------------------------------------------------------------

# Color palette — refined, slightly blue-tinted dark
BG          = "#0b0f1a"   # window background
BG_2        = "#0f1422"   # sidebar
SURFACE     = "#151b2b"   # cards
SURFACE_2   = "#1c2438"   # elevated / interactive
SURFACE_3   = "#252e47"   # hover
BORDER      = "#262e44"   # 1px borders
BORDER_2    = "#2e3852"

TEXT        = "#e8ecf5"
TEXT_DIM    = "#a5acc0"
TEXT_MUTED  = "#6e7790"

ACCENT      = "#6366f1"   # indigo 500
ACCENT_HOV  = "#4f46e5"   # indigo 600
ACCENT_DIM  = "#3730a3"   # indigo 800
ACCENT_GLOW = "#818cf8"

SUCCESS     = "#10b981"
SUCCESS_DIM = "#065f46"
WARN        = "#f59e0b"
WARN_DIM    = "#78350f"
DANGER      = "#ef4444"
DANGER_HOV  = "#dc2626"
DANGER_DIM  = "#7f1d1d"
INFO        = "#06b6d4"
MTK         = "#a78bfa"   # violet

def _isoformat_now() -> str:
    """UTC timestamp as ISO-8601 — used for export payloads."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def device_unavailable_copy(d: Optional[Device], purpose: str,
                            requires_root: bool = False
                            ) -> tuple[str, str, str]:
    """Return (title, body, hint) explaining why `purpose` can't run right now.
    Returns None values shouldn't happen — callers should only call this when
    the device isn't usable for the purpose (None / wrong state / no root)."""
    if d is None:
        return (
            "No device connected",
            f"Connect a phone via USB and click Refresh in the sidebar to "
            f"{purpose}. If nothing shows up, check that USB debugging is on "
            f"and the cable is data-capable.",
            "Connect a device to continue.",
        )
    if d.state == "fastboot":
        return (
            "Device is in fastboot mode",
            f"This page needs Android booted with USB debugging on to "
            f"{purpose}. Tap '⏻ System' in the sidebar to reboot.",
            f"Device in fastboot mode — reboot to system to {purpose}.",
        )
    if d.state in ("recovery", "sideload"):
        return (
            f"Device is in {d.state} mode",
            f"This page needs Android booted with USB debugging on to "
            f"{purpose}. Tap '⏻ System' in the sidebar to reboot.",
            f"Device in {d.state} mode — reboot to system to {purpose}.",
        )
    if d.state == "unauthorized":
        return (
            "Device is unauthorized",
            "Unlock the phone and tap 'Allow' on the USB-debugging prompt. "
            "If you don't see it, toggle USB debugging off and on in "
            "Developer options, then click Refresh.",
            "Accept the USB-debug prompt on the phone.",
        )
    if d.state == "offline":
        return (
            "Device is offline",
            "Replug the USB cable and click Refresh in the sidebar. If the "
            "device stays offline, try a different cable or port.",
            "Device is offline — reconnect to continue.",
        )
    # state == "device" but root missing
    if requires_root and not d.rooted:
        return (
            "Root not available",
            f"This page needs root (su) to {purpose}. Make sure Magisk/SuperSU "
            f"is installed and the device has granted shell access, then click "
            f"Refresh.",
            "Root required to continue.",
        )
    # Fallback — shouldn't normally land here.
    return (
        f"Device state: {d.state}",
        f"Cannot {purpose} from the current state.",
        f"State {d.state!r} not supported.",
    )


# Font helpers (resolved at App.__init__)
def F(size=13, weight="normal"):
    return ctk.CTkFont(size=size, weight=weight)

def F_MONO(size=12):
    return ctk.CTkFont(family="Menlo", size=size)


# ----------------------------------------------------------------------------
# Worker signal
# ----------------------------------------------------------------------------

class WorkerSignal:
    """Thread-safe queue: workers push dicts; UI polls with after()."""

    def __init__(self) -> None:
        self.q: queue.Queue[dict] = queue.Queue()

    def emit(self, payload: dict) -> None:
        self.q.put(payload)

    def drain(self) -> list[dict]:
        out: list[dict] = []
        try:
            while True:
                out.append(self.q.get_nowait())
        except queue.Empty:
            pass
        return out


def _run_thread(fn: Callable[[], None]) -> threading.Thread:
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    return t


# ----------------------------------------------------------------------------
# Reusable components
# ----------------------------------------------------------------------------

class Card(ctk.CTkFrame):
    """A subtle, rounded surface used as a section container."""

    def __init__(self, master, **kwargs):
        kwargs.setdefault("fg_color", SURFACE)
        kwargs.setdefault("corner_radius", 12)
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", BORDER)
        super().__init__(master, **kwargs)


class SectionHeader(ctk.CTkFrame):
    """An icon + title + optional subtitle + right-aligned actions."""

    def __init__(self, master, icon: str, title: str, subtitle: str = "",
                 actions: Optional[list[tuple[str, Callable]]] = None):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            self, text=icon, font=F(size=22), text_color=ACCENT_GLOW, width=32,
        ).grid(row=0, column=0, rowspan=2, padx=(0, 12), sticky="w")

        ctk.CTkLabel(
            self, text=title, anchor="w",
            font=F(size=18, weight="bold"), text_color=TEXT,
        ).grid(row=0, column=1, sticky="w")

        if subtitle:
            ctk.CTkLabel(
                self, text=subtitle, anchor="w",
                font=F(size=12), text_color=TEXT_MUTED,
            ).grid(row=1, column=1, sticky="w")

        if actions:
            bar = ctk.CTkFrame(self, fg_color="transparent")
            bar.grid(row=0, column=3, rowspan=2, sticky="e")
            for i, (label, cmd) in enumerate(actions):
                ctk.CTkButton(
                    bar, text=label, command=cmd, height=30, width=90,
                    fg_color=SURFACE_2, hover_color=SURFACE_3,
                    text_color=TEXT, font=F(size=12),
                    border_width=1, border_color=BORDER_2, corner_radius=8,
                ).pack(side="right", padx=(8, 0))


class StatusPill(ctk.CTkFrame):
    """A colored dot + a status label, e.g. 'Connected'."""

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.dot = ctk.CTkLabel(
            self, text="●", font=F(size=14), text_color=TEXT_MUTED, width=14,
        )
        self.dot.pack(side="left")
        self.label = ctk.CTkLabel(
            self, text="Disconnected", font=F(size=12, weight="bold"),
            text_color=TEXT_DIM,
        )
        self.label.pack(side="left", padx=(6, 0))

    def set(self, state: str) -> None:
        """state in: 'ok', 'warn', 'err', 'idle'"""
        color = {
            "ok": SUCCESS, "warn": WARN, "err": DANGER, "idle": TEXT_MUTED,
        }.get(state, TEXT_MUTED)
        text_color = {
            "ok": SUCCESS, "warn": WARN, "err": DANGER, "idle": TEXT_DIM,
        }.get(state, TEXT_DIM)
        self.dot.configure(text_color=color)
        self.label.configure(text_color=text_color)

    def set_text(self, text: str) -> None:
        self.label.configure(text=text)


class NavButton(ctk.CTkFrame):
    """Sidebar nav button: icon + label, with active highlight."""

    def __init__(self, master, icon: str, label: str, command: Callable):
        super().__init__(master, fg_color="transparent", height=40, corner_radius=8)
        self.icon = icon
        self.label = label
        self.command = command
        self._active = False

        self._icon = ctk.CTkLabel(
            self, text=icon, font=F(size=16), text_color=TEXT_DIM, width=28,
        )
        self._icon.pack(side="left", padx=(12, 0))
        self._label = ctk.CTkLabel(
            self, text=label, font=F(size=13), text_color=TEXT_DIM, anchor="w",
        )
        self._label.pack(side="left", padx=(8, 12), fill="x", expand=True)

        for w in (self, self._icon, self._label):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            w.bind("<Button-1>", lambda e: self.command())

    def _on_enter(self, _e):
        if not self._active:
            self.configure(fg_color=SURFACE_2)
            self._icon.configure(text_color=TEXT)
            self._label.configure(text_color=TEXT)

    def _on_leave(self, _e):
        if not self._active:
            self.configure(fg_color="transparent")
            self._icon.configure(text_color=TEXT_DIM)
            self._label.configure(text_color=TEXT_DIM)

    def set_active(self, active: bool) -> None:
        self._active = active
        if active:
            self.configure(fg_color=ACCENT_DIM)
            self._icon.configure(text_color=ACCENT_GLOW)
            self._label.configure(text_color=TEXT)
        else:
            self.configure(fg_color="transparent")
            self._icon.configure(text_color=TEXT_DIM)
            self._label.configure(text_color=TEXT_DIM)


class Tag(ctk.CTkLabel):
    """A small colored tag for partition annotations."""

    def __init__(self, master, text: str, color: str):
        super().__init__(
            master, text=f" {text} ", font=F(size=9, weight="bold"),
            text_color=color, fg_color=BG, corner_radius=4,
            padx=4,
        )


class EmptyState(ctk.CTkFrame):
    """A large centered empty-state block: icon + title + body + optional action."""

    def __init__(self, master, icon: str, title: str, body: str,
                 action: Optional[tuple[str, Callable]] = None):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(99, weight=1)

        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.grid(row=1, column=0)

        ctk.CTkLabel(
            wrap, text=icon, font=F(size=56), text_color=TEXT_MUTED,
        ).pack(pady=(0, 12))
        self._title_lbl = ctk.CTkLabel(
            wrap, text=title, font=F(size=18, weight="bold"), text_color=TEXT,
        )
        self._title_lbl.pack(pady=(0, 6))
        self._body_lbl = ctk.CTkLabel(
            wrap, text=body, font=F(size=13), text_color=TEXT_DIM,
            justify="center", wraplength=420,
        )
        self._body_lbl.pack(pady=(0, 16))

        if action:
            label, cmd = action
            ctk.CTkButton(
                wrap, text=label, command=cmd, height=36, width=140,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
                font=F(size=13, weight="bold"), corner_radius=8,
            ).pack()

    def set_text(self, title: str, body: str) -> None:
        self._title_lbl.configure(text=title)
        self._body_lbl.configure(text=body)


class Toast(ctk.CTkFrame):
    """Top-right slide-in notification. Auto-dismisses."""

    def __init__(self, master, message: str, kind: str = "info"):
        color = {
            "ok": SUCCESS, "warn": WARN, "err": DANGER, "info": ACCENT_GLOW,
        }.get(kind, ACCENT_GLOW)
        icon = {
            "ok": "✓", "warn": "!", "err": "✕", "info": "ⓘ",
        }.get(kind, "ⓘ")

        super().__init__(
            master, fg_color=SURFACE_2, corner_radius=10,
            border_width=1, border_color=BORDER_2,
        )

        ctk.CTkLabel(
            self, text=icon, font=F(size=15, weight="bold"),
            text_color=color, width=24,
        ).pack(side="left", padx=(14, 10), pady=12)
        ctk.CTkLabel(
            self, text=message, font=F(size=12), text_color=TEXT,
            anchor="w", justify="left", wraplength=320,
        ).pack(side="left", padx=(0, 14), pady=12, fill="x")


class ConfirmDialog(ctk.CTkToplevel):
    """Modal confirmation for destructive actions."""

    def __init__(self, master, title: str, body: str,
                 confirm_text: str = "Confirm", danger: bool = False,
                 require_typed: Optional[str] = None):
        super().__init__(master)
        self.title("")
        self.geometry("440x260")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.transient(master)
        self.grab_set()

        self._result = False
        self._require_typed = require_typed

        wrap = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=12,
                            border_width=1, border_color=BORDER)
        wrap.pack(expand=True, fill="both", padx=16, pady=16)

        head = ctk.CTkFrame(wrap, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(20, 8))

        icon_color = DANGER if danger else WARN
        ctk.CTkLabel(
            head, text="⚠" if danger else "?", font=F(size=22, weight="bold"),
            text_color=icon_color, width=28,
        ).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(
            head, text=title, font=F(size=16, weight="bold"),
            text_color=TEXT, anchor="w",
        ).pack(side="left")

        ctk.CTkLabel(
            wrap, text=body, font=F(size=12), text_color=TEXT_DIM,
            anchor="w", justify="left", wraplength=380,
        ).pack(fill="x", padx=20, pady=(0, 12))

        if require_typed:
            ctk.CTkLabel(
                wrap, text=f"Type \"{require_typed}\" to confirm:",
                font=F(size=11), text_color=TEXT_MUTED, anchor="w",
            ).pack(fill="x", padx=20, pady=(4, 4))
            self._entry = ctk.CTkEntry(
                wrap, fg_color=BG_2, border_color=BORDER_2,
                text_color=TEXT, height=32,
            )
            self._entry.pack(fill="x", padx=20, pady=(0, 8))
            self._entry.bind("<KeyRelease>", self._on_type)

        btns = ctk.CTkFrame(wrap, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=(12, 20), side="bottom")

        ctk.CTkButton(
            btns, text="Cancel", command=self._cancel, height=34, width=100,
            fg_color="transparent", hover_color=SURFACE_2,
            text_color=TEXT_DIM, border_width=1, border_color=BORDER_2,
            font=F(size=12), corner_radius=8,
        ).pack(side="right", padx=(8, 0))

        self._confirm_btn = ctk.CTkButton(
            btns, text=confirm_text, command=self._confirm, height=34, width=140,
            fg_color=DANGER if danger else ACCENT,
            hover_color=DANGER_HOV if danger else ACCENT_HOV,
            text_color="#fff", font=F(size=12, weight="bold"), corner_radius=8,
            state="normal" if not require_typed else "disabled",
        )
        self._confirm_btn.pack(side="right")

        # Keyboard: Return submits when enabled; Escape cancels; window X = cancel.
        self.bind("<Return>", self._maybe_confirm)
        self.bind("<KP_Enter>", self._maybe_confirm)
        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        # Focus appropriately.
        self.after(50, self._focus_default)

    def _focus_default(self) -> None:
        try:
            if self._require_typed:
                self._entry.focus_set()
            else:
                self._confirm_btn.focus_set()
        except Exception:
            pass

    def _maybe_confirm(self, _e=None):
        if str(self._confirm_btn.cget("state")) == "normal":
            self._confirm()

    def _on_type(self, _e):
        ok = self._entry.get().strip() == self._require_typed
        self._confirm_btn.configure(state="normal" if ok else "disabled")

    def _confirm(self):
        self._result = True
        self.destroy()

    def _cancel(self):
        self._result = False
        self.destroy()

    @classmethod
    def ask(cls, master, title: str, body: str, confirm_text: str = "Confirm",
            danger: bool = False, require_typed: Optional[str] = None) -> bool:
        dlg = cls(master, title, body, confirm_text, danger, require_typed)
        master.wait_window(dlg)
        return dlg._result


class FastbootToolsDialog(ctk.CTkToplevel):
    """Wipe partitions + unlock/lock bootloader. Only meaningful when the
    device is in fastboot mode; if it isn't, the dialog shows a hint."""

    def __init__(self, master):
        super().__init__(master)
        self.app = master
        self.title("Fastboot tools")
        self.geometry("560x520")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.transient(master)
        self.grab_set()

        wrap = ctk.CTkFrame(
            self, fg_color=SURFACE, corner_radius=12,
            border_width=1, border_color=BORDER,
        )
        wrap.pack(expand=True, fill="both", padx=16, pady=16)
        wrap.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            wrap, text="Fastboot tools",
            font=F(size=16, weight="bold"), text_color=TEXT,
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(18, 4))

        d = self.app.current_device
        in_fastboot = d is not None and d.state == "fastboot"
        sub = ("Device is in fastboot mode — commands will run on it."
               if in_fastboot else
               "⚠ Device is NOT in fastboot mode. These commands will fail "
               "until you reboot to bootloader (Power section in the sidebar).")
        ctk.CTkLabel(
            wrap, text=sub, font=F(size=11),
            text_color=SUCCESS if in_fastboot else WARN,
            anchor="w", justify="left", wraplength=480,
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 14))

        # --- Erase partition ----------------------------------------------
        ctk.CTkLabel(
            wrap, text="Erase partition",
            font=F(size=12, weight="bold"), text_color=TEXT, anchor="w",
        ).grid(row=2, column=0, sticky="w", padx=20, pady=(0, 4))
        ctk.CTkLabel(
            wrap,
            text="Wipes a partition with `fastboot erase`. Common targets: "
                 "cache, userdata, metadata. Will not erase dynamic-partition "
                 "members (system/vendor/product) on Android 10+.",
            font=F(size=10), text_color=TEXT_MUTED,
            anchor="w", justify="left", wraplength=480,
        ).grid(row=3, column=0, sticky="w", padx=20, pady=(0, 8))

        row_e = ctk.CTkFrame(wrap, fg_color="transparent")
        row_e.grid(row=4, column=0, sticky="ew", padx=20, pady=(0, 14))
        row_e.grid_columnconfigure(0, weight=1)
        self.erase_entry = ctk.CTkEntry(
            row_e, height=34, fg_color=BG_2, border_color=BORDER_2,
            text_color=TEXT, font=F_MONO(size=12),
            placeholder_text="partition name (e.g. cache)",
        )
        self.erase_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(
            row_e, text="Erase", command=self._do_erase,
            height=34, width=110,
            fg_color=SURFACE_2, hover_color=DANGER_DIM, text_color=DANGER,
            border_width=1, border_color=DANGER_DIM,
            font=F(size=12, weight="bold"), corner_radius=8,
        ).grid(row=0, column=1, sticky="e")

        # --- Unlock / lock ------------------------------------------------
        ctk.CTkLabel(
            wrap, text="Bootloader lock",
            font=F(size=12, weight="bold"), text_color=TEXT, anchor="w",
        ).grid(row=5, column=0, sticky="w", padx=20, pady=(0, 4))
        ctk.CTkLabel(
            wrap,
            text="Unlock factory-resets the device on most phones and the "
                 "user must confirm on the phone screen. Uses the modern "
                 "`fastboot flashing unlock` command; older devices may need "
                 "`fastboot oem unlock` (the OEM button below).",
            font=F(size=10), text_color=TEXT_MUTED,
            anchor="w", justify="left", wraplength=480,
        ).grid(row=6, column=0, sticky="w", padx=20, pady=(0, 8))

        row_l = ctk.CTkFrame(wrap, fg_color="transparent")
        row_l.grid(row=7, column=0, sticky="ew", padx=20, pady=(0, 14))
        row_l.grid_columnconfigure(0, weight=1)
        row_l.grid_columnconfigure(1, weight=1)
        row_l.grid_columnconfigure(2, weight=1)
        ctk.CTkButton(
            row_l, text="flashing unlock",
            command=lambda: self._do_flashing("unlock"),
            height=34, fg_color=SURFACE_2, hover_color=DANGER_DIM,
            text_color=DANGER, border_width=1, border_color=DANGER_DIM,
            font=F(size=12, weight="bold"), corner_radius=8,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ctk.CTkButton(
            row_l, text="flashing lock",
            command=lambda: self._do_flashing("lock"),
            height=34, fg_color=SURFACE_2, hover_color=SURFACE_3,
            text_color=TEXT_DIM, border_width=1, border_color=BORDER_2,
            font=F(size=12), corner_radius=8,
        ).grid(row=0, column=1, sticky="ew", padx=4)
        ctk.CTkButton(
            row_l, text="oem unlock (legacy)",
            command=lambda: self._do_oem("unlock"),
            height=34, fg_color=SURFACE_2, hover_color=SURFACE_3,
            text_color=TEXT_DIM, border_width=1, border_color=BORDER_2,
            font=F(size=12), corner_radius=8,
        ).grid(row=0, column=2, sticky="ew", padx=(4, 0))

        # --- Output -------------------------------------------------------
        ctk.CTkLabel(
            wrap, text="Output",
            font=F(size=11, weight="bold"), text_color=TEXT_DIM, anchor="w",
        ).grid(row=8, column=0, sticky="w", padx=20, pady=(0, 4))
        self.output = ctk.CTkTextbox(
            wrap, fg_color=BG_2, text_color=TEXT_DIM,
            font=F_MONO(size=11), corner_radius=8, height=120, wrap="word",
        )
        self.output.grid(row=9, column=0, sticky="ew", padx=20, pady=(0, 14))
        self.output.configure(state="disabled")

        ctk.CTkButton(
            wrap, text="Close", command=self.destroy,
            height=30, width=110,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT_DIM,
            border_width=1, border_color=BORDER_2,
            font=F(size=11), corner_radius=8,
        ).grid(row=10, column=0, sticky="e", padx=20, pady=(0, 18))

        self.bind("<Escape>", lambda e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _serial(self) -> Optional[str]:
        return (self.app.current_device.serial
                if self.app.current_device else None)

    def _log(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", text + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")

    def _do_erase(self) -> None:
        part = self.erase_entry.get().strip()
        if not part:
            self._log("Enter a partition name.")
            return
        if not ConfirmDialog.ask(
            self.app, title=f"Erase {part}?",
            body=f"`fastboot erase {part}` will wipe the partition. "
                 f"This is destructive and not always reversible. Continue?",
            confirm_text=f"Erase {part}", danger=True, require_typed=part,
        ):
            return
        self._log(f"$ fastboot erase {part}")
        def work():
            try:
                out = fastboot_mod.erase(part, serial=self._serial())
                self.after(0, lambda: self._log(out.strip() or "(ok)"))
            except Exception as e:
                self.after(0, lambda: self._log(f"ERROR: {e}"))
        _run_thread(work)

    def _do_flashing(self, action: str) -> None:
        if action == "unlock":
            if not ConfirmDialog.ask(
                self.app, title="Unlock bootloader?",
                body="`fastboot flashing unlock` factory-resets the device on "
                     "most phones (all user data wiped) and must be confirmed "
                     "on the phone screen. The device shows a 'bootloader "
                     "unlocked' warning on every subsequent boot. Continue?",
                confirm_text="Unlock", danger=True, require_typed="UNLOCK",
            ):
                return
        self._log(f"$ fastboot flashing {action}")
        def work():
            try:
                out = fastboot_mod.flashing(action, serial=self._serial())
                self.after(0, lambda: self._log(out.strip() or "(ok)"))
            except Exception as e:
                self.after(0, lambda: self._log(f"ERROR: {e}"))
        _run_thread(work)

    def _do_oem(self, action: str) -> None:
        if not ConfirmDialog.ask(
            self.app, title=f"fastboot oem {action}?",
            body=f"This sends `fastboot oem {action}` — behavior is "
                 f"device-specific. On older devices this is the unlock "
                 f"command; on others it's a no-op or returns an error. "
                 f"Continue?",
            confirm_text="Send", danger=True,
        ):
            return
        self._log(f"$ fastboot oem {action}")
        def work():
            try:
                out = fastboot_mod.oem(action, serial=self._serial())
                self.after(0, lambda: self._log(out.strip() or "(ok)"))
            except Exception as e:
                self.after(0, lambda: self._log(f"ERROR: {e}"))
        _run_thread(work)


class WirelessAdbDialog(ctk.CTkToplevel):
    """Pair + connect to a device over WiFi. Two-step UI for Android 11+:
    pair once (6-digit code), then connect (port 5555 by default)."""

    def __init__(self, master):
        super().__init__(master)
        self.app = master
        self.title("Wireless ADB")
        self.geometry("500x460")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.transient(master)
        self.grab_set()

        wrap = ctk.CTkFrame(
            self, fg_color=SURFACE, corner_radius=12,
            border_width=1, border_color=BORDER,
        )
        wrap.pack(expand=True, fill="both", padx=16, pady=16)
        wrap.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            wrap, text="Wireless ADB",
            font=F(size=16, weight="bold"), text_color=TEXT,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(18, 4))
        ctk.CTkLabel(
            wrap, text="On the phone: Developer options → Wireless debugging.",
            font=F(size=11), text_color=TEXT_DIM, anchor="w",
            justify="left", wraplength=440,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 14))

        # --- Pair (Android 11+) -------------------------------------------
        ctk.CTkLabel(
            wrap, text="1. Pair (Android 11+, one-time)",
            font=F(size=12, weight="bold"), text_color=TEXT,
            anchor="w",
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 4))
        ctk.CTkLabel(
            wrap, text="On phone: 'Pair device with pairing code'. "
                       "Enter the IP, the pairing port, and the 6-digit code.",
            font=F(size=10), text_color=TEXT_MUTED, anchor="w",
            justify="left", wraplength=440,
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 8))

        row_p = ctk.CTkFrame(wrap, fg_color="transparent")
        row_p.grid(row=4, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 4))
        row_p.grid_columnconfigure(0, weight=2)
        row_p.grid_columnconfigure(1, weight=1)
        row_p.grid_columnconfigure(2, weight=1)
        self.pair_ip = self._entry(row_p, "192.168.1.42")
        self.pair_ip.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.pair_port = self._entry(row_p, "37099")
        self.pair_port.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.pair_code = self._entry(row_p, "123456")
        self.pair_code.grid(row=0, column=2, sticky="ew")

        self.pair_btn = ctk.CTkButton(
            wrap, text="Pair", command=self._do_pair,
            height=32, fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT,
            border_width=1, border_color=BORDER_2, font=F(size=12),
            corner_radius=8,
        )
        self.pair_btn.grid(row=5, column=0, columnspan=2, sticky="ew",
                           padx=20, pady=(4, 14))

        # --- Connect ------------------------------------------------------
        ctk.CTkLabel(
            wrap, text="2. Connect",
            font=F(size=12, weight="bold"), text_color=TEXT,
            anchor="w",
        ).grid(row=6, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 4))
        ctk.CTkLabel(
            wrap, text="Use the IP + port shown in the Wireless debugging "
                       "screen (the larger numbers — usually port 5555).",
            font=F(size=10), text_color=TEXT_MUTED, anchor="w",
            justify="left", wraplength=440,
        ).grid(row=7, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 8))

        row_c = ctk.CTkFrame(wrap, fg_color="transparent")
        row_c.grid(row=8, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 4))
        row_c.grid_columnconfigure(0, weight=2)
        row_c.grid_columnconfigure(1, weight=1)
        self.conn_ip = self._entry(row_c, "192.168.1.42")
        self.conn_ip.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.conn_port = self._entry(row_c, "5555")
        self.conn_port.grid(row=0, column=1, sticky="ew")

        self.conn_btn = ctk.CTkButton(
            wrap, text="Connect", command=self._do_connect,
            height=32, fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="#fff",
            font=F(size=12, weight="bold"), corner_radius=8,
        )
        self.conn_btn.grid(row=9, column=0, columnspan=2, sticky="ew",
                           padx=20, pady=(4, 14))

        # --- Result ------------------------------------------------------
        self.result_lbl = ctk.CTkLabel(
            wrap, text="", font=F(size=11), text_color=TEXT_MUTED,
            anchor="w", justify="left", wraplength=440,
        )
        self.result_lbl.grid(row=10, column=0, columnspan=2, sticky="ew",
                             padx=20, pady=(0, 14))

        # --- Footer ------------------------------------------------------
        ctk.CTkButton(
            wrap, text="Close", command=self.destroy,
            height=30, width=110,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT_DIM,
            border_width=1, border_color=BORDER_2,
            font=F(size=11), corner_radius=8,
        ).grid(row=11, column=1, sticky="e", padx=20, pady=(0, 18))

        self.bind("<Escape>", lambda e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _entry(self, parent, placeholder: str) -> ctk.CTkEntry:
        return ctk.CTkEntry(
            parent, height=34, fg_color=BG_2, border_color=BORDER_2,
            text_color=TEXT, font=F_MONO(size=12), placeholder_text=placeholder,
        )

    def _set_result(self, text: str, kind: str = "info") -> None:
        color = {"ok": SUCCESS, "err": DANGER, "busy": ACCENT_GLOW}.get(
            kind, TEXT_MUTED)
        self.result_lbl.configure(text=text, text_color=color)

    def _do_pair(self) -> None:
        ip = self.pair_ip.get().strip()
        port_s = self.pair_port.get().strip()
        code = self.pair_code.get().strip()
        if not ip or not port_s or not code:
            self._set_result("Enter IP, pairing port, and 6-digit code.", "err")
            return
        try:
            port = int(port_s)
        except ValueError:
            self._set_result("Pairing port must be a number.", "err")
            return
        if not code.isdigit() or len(code) != 6:
            self._set_result("Pairing code must be exactly 6 digits.", "err")
            return
        self.pair_btn.configure(state="disabled", text="Pairing…")
        self._set_result(f"Pairing with {ip}:{port}…", "busy")

        def work():
            try:
                msg = adb_mod.pair(ip, port, code)
                self.after(0, lambda: self._pair_done(True, msg, ip))
            except Exception as e:
                self.after(0, lambda: self._pair_done(False, str(e), ip))
        _run_thread(work)

    def _pair_done(self, ok: bool, msg: str, ip: str) -> None:
        self.pair_btn.configure(state="normal", text="Pair")
        if ok:
            self._set_result(f"Paired with {ip}. Now click Connect.", "ok")
            # Auto-fill connect IP from pair IP.
            if not self.conn_ip.get().strip():
                self.conn_ip.delete(0, "end"); self.conn_ip.insert(0, ip)
        else:
            self._set_result(f"Pair failed: {msg}", "err")

    def _do_connect(self) -> None:
        ip = self.conn_ip.get().strip()
        port_s = self.conn_port.get().strip() or "5555"
        if not ip:
            self._set_result("Enter the device IP.", "err")
            return
        try:
            port = int(port_s)
        except ValueError:
            self._set_result("Connection port must be a number.", "err")
            return
        self.conn_btn.configure(state="disabled", text="Connecting…")
        self._set_result(f"Connecting to {ip}:{port}…", "busy")

        def work():
            try:
                msg = adb_mod.connect(ip, port)
                self.after(0, lambda: self._connect_done(True, msg))
            except Exception as e:
                self.after(0, lambda: self._connect_done(False, str(e)))
        _run_thread(work)

    def _connect_done(self, ok: bool, msg: str) -> None:
        self.conn_btn.configure(state="normal", text="Connect")
        if ok:
            self._set_result(f"Connected. {msg}", "ok")
            # Kick a refresh so the new device shows in the sidebar.
            self.app.refresh_devices()
        else:
            self._set_result(f"Connect failed: {msg}", "err")


class DiagnosticDialog(ctk.CTkToplevel):
    """'Why isn't my device showing up?' — runs adb devices, fastboot devices,
    macOS USB enumeration, and MTK preloader detection. Renders a verdict."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Connection diagnostics")
        self.geometry("620x520")
        self.resizable(True, True)
        self.configure(fg_color=BG)
        self.transient(master)
        self.grab_set()

        wrap = ctk.CTkFrame(
            self, fg_color=SURFACE, corner_radius=12,
            border_width=1, border_color=BORDER,
        )
        wrap.pack(expand=True, fill="both", padx=16, pady=16)
        wrap.grid_columnconfigure(0, weight=1)
        wrap.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            wrap, text="Connection diagnostics",
            font=F(size=16, weight="bold"), text_color=TEXT,
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(18, 4))
        self.subtitle = ctk.CTkLabel(
            wrap, text="Scanning USB, adb, and fastboot…",
            font=F(size=11), text_color=TEXT_DIM,
        )
        self.subtitle.grid(row=1, column=0, sticky="w", padx=20, pady=(0, 12))

        self.box = ctk.CTkTextbox(
            wrap, fg_color=BG_2, text_color=TEXT,
            font=F_MONO(size=11), corner_radius=8, wrap="word",
        )
        self.box.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 12))
        self.box.configure(state="disabled")

        btn_row = ctk.CTkFrame(wrap, fg_color="transparent")
        btn_row.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 16))
        btn_row.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            btn_row, text="Re-run", command=self._run,
            height=34, width=110,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT,
            border_width=1, border_color=BORDER_2,
            font=F(size=12), corner_radius=8,
        ).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(
            btn_row, text="Copy", command=self._copy,
            height=34, width=90,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT,
            border_width=1, border_color=BORDER_2,
            font=F(size=12), corner_radius=8,
        ).grid(row=0, column=2, padx=(0, 8))
        ctk.CTkButton(
            btn_row, text="Close", command=self.destroy,
            height=34, width=110,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="#fff",
            font=F(size=12, weight="bold"), corner_radius=8,
        ).grid(row=0, column=3)

        self.bind("<Escape>", lambda e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self._run()

    def _run(self) -> None:
        self._set_text("Running diagnostics…\n")
        self.subtitle.configure(text="Scanning USB, adb, and fastboot…")

        def work():
            report = self._collect()
            self.after(0, lambda: self._render(report))

        _run_thread(work)

    def _collect(self) -> dict:
        out: dict = {"adb": [], "fastboot": [], "mtk": [],
                     "adb_err": None, "fastboot_err": None}
        try:
            out["adb"] = adb_mod.list_devices()
        except Exception as e:
            out["adb_err"] = str(e)
        try:
            out["fastboot"] = fastboot_mod.list_devices()
        except Exception as e:
            out["fastboot_err"] = str(e)
        try:
            out["mtk"] = device_mod.detect_mtk_preloader()
        except Exception as e:
            out["mtk_err"] = str(e)
        return out

    def _render(self, r: dict) -> None:
        lines: list[str] = []

        # --- adb -----------------------------------------------------------
        lines.append("adb devices")
        lines.append("─" * 36)
        if r["adb_err"]:
            lines.append(f"  ERROR: {r['adb_err']}")
        elif not r["adb"]:
            lines.append("  (no devices)")
        else:
            for serial, state in r["adb"]:
                lines.append(f"  {serial:<24} {state}")
        lines.append("")

        # --- fastboot ------------------------------------------------------
        lines.append("fastboot devices")
        lines.append("─" * 36)
        if r["fastboot_err"]:
            lines.append(f"  ERROR: {r['fastboot_err']}")
        elif not r["fastboot"]:
            lines.append("  (no devices)")
        else:
            for serial in r["fastboot"]:
                lines.append(f"  {serial:<24} fastboot")
        lines.append("")

        # --- MTK preloader/BROM -------------------------------------------
        lines.append("MediaTek preloader / BROM (macOS USB scan)")
        lines.append("─" * 42)
        if r.get("mtk_err"):
            lines.append(f"  ERROR: {r['mtk_err']}")
        elif not r["mtk"]:
            lines.append("  (none detected)")
        else:
            for dev in r["mtk"]:
                lines.append(f"  {dev.vid_pid}  {dev.description}")
        lines.append("")

        # --- Verdict -------------------------------------------------------
        verdict, hint = self._verdict(r)
        lines.append("Verdict")
        lines.append("─" * 36)
        lines.append(f"  {verdict}")
        if hint:
            lines.append("")
            for ln in hint.splitlines():
                lines.append(f"  {ln}")

        self.subtitle.configure(text=verdict)
        self._set_text("\n".join(lines))

    def _verdict(self, r: dict) -> tuple[str, str]:
        n_adb = len(r["adb"])
        n_fb = len(r["fastboot"])
        n_mtk = len(r["mtk"])

        if n_adb + n_fb + n_mtk == 0:
            return (
                "No device visible to adb, fastboot, or USB enumeration.",
                "Most likely cause: the phone isn't plugged in, the cable is "
                "charge-only (no data), or the phone is powered off.\n"
                "Try: a known-good USB-C/A data cable; a different Mac port "
                "(USB-A direct if possible); confirm the phone is on.",
            )
        if n_adb == 0 and n_fb == 0 and n_mtk:
            d = r["mtk"][0]
            return (
                f"Phone is in {d.description}.",
                "Neither adb nor fastboot can talk to this mode — you need a "
                "preloader-aware tool such as mtkclient (open source) or "
                "SP Flash Tool. Booting out of preloader is usually:\n"
                "  hold Power for 15s, or remove battery if possible.",
            )
        unauth = [s for s, st in r["adb"] if st in ("unauthorized", "offline")]
        if unauth:
            return (
                f"Device is {r['adb'][0][1]}.",
                "Unlock the phone, tap 'Allow' on the USB-debugging prompt. "
                "If you don't see one, toggle USB debugging off/on in "
                "Developer options, then re-run.",
            )
        if n_fb and not n_adb:
            return (
                "Device is in fastboot mode.",
                "Use the Flash page, or click '⏻ System' in the sidebar to "
                "reboot into Android.",
            )
        if n_adb:
            states = ", ".join(f"{s} ({st})" for s, st in r["adb"])
            return (f"adb sees: {states}", "")
        return ("Device visible.", "")

    def _set_text(self, text: str) -> None:
        self.box.configure(state="normal")
        self.box.delete("1.0", "end")
        self.box.insert("1.0", text)
        self.box.configure(state="disabled")

    def _copy(self) -> None:
        text = self.box.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(text)


class AboutDialog(ctk.CTkToplevel):
    """A small modal with project info, version, and a link."""

    def __init__(self, master):
        super().__init__(master)
        self.title("")
        self.geometry("440x340")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.transient(master)
        self.grab_set()

        wrap = ctk.CTkFrame(
            self, fg_color=SURFACE, corner_radius=12,
            border_width=1, border_color=BORDER,
        )
        wrap.pack(expand=True, fill="both", padx=16, pady=16)

        ctk.CTkLabel(
            wrap, text="◆", font=F(size=44), text_color=ACCENT_GLOW,
        ).pack(pady=(24, 6))
        ctk.CTkLabel(
            wrap, text="Android ROM Extractor",
            font=F(size=18, weight="bold"), text_color=TEXT,
        ).pack()
        ctk.CTkLabel(
            wrap, text=f"v{__version__}",
            font=F(size=11), text_color=TEXT_MUTED,
        ).pack(pady=(2, 18))
        ctk.CTkLabel(
            wrap,
            text="Extract partition images from your Android device,\n"
                 "verify them, and flash them back. Built for tinkerers.",
            font=F(size=12), text_color=TEXT_DIM,
            justify="center", wraplength=380,
        ).pack(padx=24, pady=(0, 8))
        ctk.CTkLabel(
            wrap,
            text=f"Running on: {platform.system()} {platform.release()}  ·  "
                 f"Python {platform.python_version()}",
            font=F(size=10), text_color=TEXT_MUTED,
        ).pack(pady=(8, 18))

        ctk.CTkButton(
            wrap, text="Close", command=self.destroy,
            height=34, width=120,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="#fff",
            font=F(size=12, weight="bold"), corner_radius=8,
        ).pack(pady=(0, 18))

        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)


class SettingsDialog(ctk.CTkToplevel):
    """Edit user preferences. Changes save immediately on close."""

    def __init__(self, master):
        super().__init__(master)
        self.app = master
        self.title("")
        self.geometry("520x540")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.transient(master)
        self.grab_set()

        s = master.settings

        wrap = ctk.CTkFrame(
            self, fg_color=SURFACE, corner_radius=12,
            border_width=1, border_color=BORDER,
        )
        wrap.pack(expand=True, fill="both", padx=16, pady=16)

        ctk.CTkLabel(
            wrap, text="Settings", font=F(size=18, weight="bold"),
            text_color=TEXT, anchor="w",
        ).pack(fill="x", padx=24, pady=(24, 16))

        # General section
        self._section_label(wrap, "GENERAL")

        self.auto_verify = tk.BooleanVar(value=s.auto_verify_after_backup)
        ctk.CTkSwitch(
            wrap, text="Auto-verify backups when they complete",
            variable=self.auto_verify, progress_color=SUCCESS,
            font=F(size=12), text_color=TEXT_DIM,
        ).pack(fill="x", padx=24, pady=4, anchor="w")

        # Recent backups
        self._section_label(wrap, "RECENT BACKUPS")
        recent_count = len(s.recent_backups)
        ctk.CTkLabel(
            wrap,
            text=f"{recent_count} backup{'s' if recent_count != 1 else ''} "
                 "remembered.",
            font=F(size=11), text_color=TEXT_MUTED, anchor="w",
        ).pack(fill="x", padx=24, pady=(0, 6))
        ctk.CTkButton(
            wrap, text="Clear recent backups", command=self._clear_recent,
            height=30, width=200,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT_DIM,
            border_width=1, border_color=BORDER_2, font=F(size=11),
            corner_radius=8,
        ).pack(padx=24, pady=(0, 4), anchor="w")

        # Presets
        self._section_label(wrap, "PARTITION PRESETS")
        preset_count = len(s.partition_presets)
        ctk.CTkLabel(
            wrap,
            text=(f"{preset_count} saved preset(s): "
                  f"{', '.join(sorted(s.partition_presets)) or '—'}"),
            font=F(size=11), text_color=TEXT_MUTED, anchor="w",
            wraplength=440, justify="left",
        ).pack(fill="x", padx=24, pady=(0, 6))
        ctk.CTkButton(
            wrap, text="Clear all presets", command=self._clear_presets,
            height=30, width=200,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT_DIM,
            border_width=1, border_color=BORDER_2, font=F(size=11),
            corner_radius=8,
        ).pack(padx=24, pady=(0, 4), anchor="w")

        # Config file
        self._section_label(wrap, "CONFIG FILE")
        cfg_path = settings_mod._config_path()
        ctk.CTkLabel(
            wrap, text=str(cfg_path),
            font=F_MONO(size=10), text_color=TEXT_MUTED, anchor="w",
            wraplength=440, justify="left",
        ).pack(fill="x", padx=24, pady=(0, 6))
        row = ctk.CTkFrame(wrap, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(0, 4), anchor="w")
        ctk.CTkButton(
            row, text="Reveal in Finder", command=self._reveal,
            height=30, width=160,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT_DIM,
            border_width=1, border_color=BORDER_2, font=F(size=11),
            corner_radius=8,
        ).pack(side="left", padx=(0, 8))

        # Footer
        ctk.CTkButton(
            wrap, text="Done", command=self._save_and_close,
            height=34, width=120,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="#fff",
            font=F(size=12, weight="bold"), corner_radius=8,
        ).pack(side="bottom", pady=(0, 24))

        self.bind("<Escape>", lambda e: self._save_and_close())
        self.bind("<Return>", lambda e: self._save_and_close())
        self.protocol("WM_DELETE_WINDOW", self._save_and_close)

    def _section_label(self, parent, text: str) -> None:
        ctk.CTkLabel(
            parent, text=text, font=F(size=10, weight="bold"),
            text_color=TEXT_MUTED, anchor="w",
        ).pack(fill="x", padx=24, pady=(16, 6))

    def _clear_recent(self) -> None:
        self.app.settings.recent_backups = []
        self.app.settings.save()
        self.app.toast("Recent backups cleared.", "ok")
        self.destroy()

    def _clear_presets(self) -> None:
        self.app.settings.partition_presets = {}
        self.app.settings.save()
        self.app.toast("All partition presets cleared.", "ok")
        self.destroy()

    def _reveal(self) -> None:
        cfg = settings_mod._config_path()
        try:
            cfg.parent.mkdir(parents=True, exist_ok=True)
            if not cfg.exists():
                cfg.write_text("{}")
            if IS_MAC:
                subprocess.Popen(["open", "-R", str(cfg)])
            elif platform.system() == "Linux":
                subprocess.Popen(["xdg-open", str(cfg.parent)])
            else:
                subprocess.Popen(["explorer", "/select,", str(cfg)])
        except Exception as e:
            self.app.toast(f"Couldn't reveal: {e}", "err")

    def _save_and_close(self) -> None:
        s = self.app.settings
        s.auto_verify_after_backup = bool(self.auto_verify.get())
        s.save()
        # Mirror back into the backup view if open.
        bv = self.app.views.get("backup")
        if bv and hasattr(bv, "auto_verify"):
            bv.auto_verify.set(s.auto_verify_after_backup)
        self.destroy()


class TextInputDialog(ctk.CTkToplevel):
    """Modal that prompts for a single line of text. .result is None on cancel."""

    def __init__(self, master, title: str, prompt: str, placeholder: str = ""):
        super().__init__(master)
        self.title("")
        self.geometry("420x220")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.transient(master)
        self.grab_set()

        self.result: Optional[str] = None

        wrap = ctk.CTkFrame(
            self, fg_color=SURFACE, corner_radius=12,
            border_width=1, border_color=BORDER,
        )
        wrap.pack(expand=True, fill="both", padx=16, pady=16)

        ctk.CTkLabel(
            wrap, text=title, font=F(size=16, weight="bold"),
            text_color=TEXT, anchor="w",
        ).pack(fill="x", padx=20, pady=(20, 4))
        ctk.CTkLabel(
            wrap, text=prompt, font=F(size=12), text_color=TEXT_DIM,
            anchor="w", justify="left", wraplength=360,
        ).pack(fill="x", padx=20, pady=(0, 12))

        self.entry = ctk.CTkEntry(
            wrap, height=36, fg_color=BG_2, border_color=BORDER_2,
            text_color=TEXT, font=F(size=12), placeholder_text=placeholder,
        )
        self.entry.pack(fill="x", padx=20, pady=(0, 16))
        self.entry.focus_set()

        btns = ctk.CTkFrame(wrap, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=(0, 18))

        ctk.CTkButton(
            btns, text="Cancel", command=self._cancel, height=32, width=90,
            fg_color="transparent", hover_color=SURFACE_2,
            text_color=TEXT_DIM, border_width=1, border_color=BORDER_2,
            font=F(size=12), corner_radius=8,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btns, text="Save", command=self._submit, height=32, width=120,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="#fff",
            font=F(size=12, weight="bold"), corner_radius=8,
        ).pack(side="right")

        self.bind("<Return>", lambda e: self._submit())
        self.bind("<KP_Enter>", lambda e: self._submit())
        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _submit(self):
        self.result = self.entry.get().strip()
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


# ----------------------------------------------------------------------------
# Main app
# ----------------------------------------------------------------------------

NAV_ITEMS = [
    ("backup",     "⤓", "Backup"),
    ("flash",      "⚡", "Flash"),
    ("restore",    "↩", "Restore"),
    ("verify",     "✓", "Verify"),
    ("sideload",   "⇪", "Sideload"),
    ("apps",       "◎", "Apps"),
    ("logcat",     "≡", "Logcat"),
    ("properties", "ⓘ", "Properties"),
    ("logs",       "⎘", "Logs"),
]


class App(ctk.CTk):

    def __init__(self) -> None:
        super().__init__()
        self.settings = settings_mod.Settings.load()

        self.title("Android ROM Extractor")
        self.geometry(self.settings.window_geometry or "1280x820")
        self.minsize(1100, 720)
        self.configure(fg_color=BG)

        self.current_device: Optional[Device] = None
        self._last_selected_serial: Optional[str] = None
        self.all_devices: list[Device] = []
        self.current_health: Optional[device_mod.DeviceHealth] = None
        self.partitions: list[Partition] = []
        self.BATTERY_DESTRUCTIVE_THRESHOLD = 15  # percent
        self.signal = WorkerSignal()
        self.nav_buttons: dict[str, NavButton] = {}
        self.views: dict[str, ctk.CTkFrame] = {}
        self.current_view = "backup"
        self._toasts: list[ctk.CTkFrame] = []
        # Auto-poll: re-runs device discovery silently every few seconds and
        # only re-renders when something changes. Signature = sorted tuple of
        # (serial, state, rooted) per device.
        self._last_device_signature: Optional[tuple] = None
        self._auto_poll_after_id: Optional[str] = None
        self.AUTO_POLL_INTERVAL_MS = 3500

        self._build_layout()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.show_view("backup")
        self._poll_signal()
        self.refresh_devices()
        self._start_auto_poll()

    def _on_close(self) -> None:
        try:
            if self._auto_poll_after_id is not None:
                try:
                    self.after_cancel(self._auto_poll_after_id)
                except Exception:
                    pass
                self._auto_poll_after_id = None
            self.settings.window_geometry = self.geometry()
            self.settings.save()
        finally:
            self.destroy()

    def _start_auto_poll(self) -> None:
        self._auto_poll_after_id = self.after(
            self.AUTO_POLL_INTERVAL_MS, self._auto_poll_tick)

    def _auto_poll_tick(self) -> None:
        # Silent discover — _render_device's signature check skips re-render
        # when nothing has changed, so this is cheap when the user is mid-op.
        def work():
            try:
                devs = device_mod.discover()
                self.signal.emit({"event": "devices",
                                  "devices": devs, "silent": True})
            except Exception as e:
                log.debug("auto-poll failed: %s", e)
        _run_thread(work)
        self._auto_poll_after_id = self.after(
            self.AUTO_POLL_INTERVAL_MS, self._auto_poll_tick)

    def _bind_shortcuts(self) -> None:
        # Cmd/Ctrl+1..8 switch nav, Cmd/Ctrl+R refresh.
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9"]
        for i, (key, _icon, _label) in enumerate(NAV_ITEMS):
            if i >= len(keys):
                break
            self.bind_all(f"<{MOD_KEY}-Key-{keys[i]}>",
                          lambda e, k=key: self.show_view(k))
        self.bind_all(f"<{MOD_KEY}-r>",
                      lambda e: self.refresh_devices())
        self.bind_all(f"<{MOD_KEY}-R>",
                      lambda e: self.refresh_devices())

    # ------- layout ----------------------------------------------------------

    def _build_layout(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_content()
        self._build_statusbar()

    def _build_sidebar(self) -> None:
        side = ctk.CTkFrame(self, width=280, corner_radius=0, fg_color=BG_2)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(99, weight=1)

        # Brand
        brand = ctk.CTkFrame(side, fg_color="transparent", height=72)
        brand.grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 14))
        brand.grid_propagate(False)
        ctk.CTkLabel(
            brand, text="◆", font=F(size=22), text_color=ACCENT_GLOW,
        ).pack(side="left", padx=(0, 10))
        title_box = ctk.CTkFrame(brand, fg_color="transparent")
        title_box.pack(side="left")
        ctk.CTkLabel(
            title_box, text="ROM Extractor", font=F(size=15, weight="bold"),
            text_color=TEXT, anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_box, text=f"v{__version__}", font=F(size=10),
            text_color=TEXT_MUTED, anchor="w",
        ).pack(anchor="w")

        # Device card
        dev_card = ctk.CTkFrame(
            side, fg_color=SURFACE, corner_radius=10,
            border_width=1, border_color=BORDER,
        )
        dev_card.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 16))
        dev_card.grid_columnconfigure(0, weight=1)

        self.status_pill = StatusPill(dev_card)
        self.status_pill.grid(row=0, column=0, sticky="w", padx=14, pady=(12, 6))

        self.device_selector = ctk.CTkOptionMenu(
            dev_card, values=["(no devices)"],
            command=self._on_device_picked,
            fg_color=SURFACE_2, button_color=ACCENT,
            button_hover_color=ACCENT_HOV, text_color=TEXT,
            font=F(size=11), height=30, dropdown_font=F(size=11),
        )
        self.device_selector.grid(row=1, column=0, sticky="ew",
                                  padx=14, pady=(2, 8))
        self.device_selector.grid_remove()

        self.device_info_label = ctk.CTkLabel(
            dev_card, text="No device connected.\nPlug in a phone via USB.",
            font=F(size=11), text_color=TEXT_MUTED,
            anchor="w", justify="left", wraplength=220,
        )
        self.device_info_label.grid(row=2, column=0, sticky="ew",
                                    padx=14, pady=(0, 6))

        # Health row: battery (with bar) + storage free.
        self.health_frame = ctk.CTkFrame(dev_card, fg_color="transparent")
        self.health_frame.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 8))
        self.health_frame.grid_columnconfigure(0, weight=1)

        self.battery_label = ctk.CTkLabel(
            self.health_frame, text="", anchor="w",
            font=F(size=10), text_color=TEXT_MUTED,
        )
        self.battery_label.grid(row=0, column=0, sticky="ew")
        self.battery_bar = ctk.CTkProgressBar(
            self.health_frame, height=4, progress_color=SUCCESS, fg_color=BG_2,
            corner_radius=2,
        )
        self.battery_bar.grid(row=1, column=0, sticky="ew", pady=(2, 4))
        self.battery_bar.set(0)
        self.storage_label = ctk.CTkLabel(
            self.health_frame, text="", anchor="w",
            font=F(size=10), text_color=TEXT_MUTED,
        )
        self.storage_label.grid(row=2, column=0, sticky="ew")
        self.health_frame.grid_remove()

        btn_row = ctk.CTkFrame(dev_card, fg_color="transparent")
        btn_row.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 12))
        btn_row.grid_columnconfigure(0, weight=1)
        btn_row.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(
            btn_row, text=f"↻  Refresh  {MOD_LABEL}R",
            command=self.refresh_devices,
            height=30, fg_color=SURFACE_2, hover_color=SURFACE_3,
            text_color=TEXT, border_width=1, border_color=BORDER_2,
            font=F(size=11), corner_radius=8,
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ctk.CTkButton(
            btn_row, text="Diagnose",
            command=lambda: DiagnosticDialog(self),
            height=28, fg_color=SURFACE_2, hover_color=SURFACE_3,
            text_color=TEXT_DIM, border_width=1, border_color=BORDER_2,
            font=F(size=11), corner_radius=8,
        ).grid(row=1, column=0, sticky="ew", padx=(0, 3))
        ctk.CTkButton(
            btn_row, text="WiFi ADB",
            command=lambda: WirelessAdbDialog(self),
            height=28, fg_color=SURFACE_2, hover_color=SURFACE_3,
            text_color=TEXT_DIM, border_width=1, border_color=BORDER_2,
            font=F(size=11), corner_radius=8,
        ).grid(row=1, column=1, sticky="ew", padx=(3, 0))

        # Nav
        nav_label = ctk.CTkLabel(
            side, text="NAVIGATION", anchor="w",
            font=F(size=10, weight="bold"), text_color=TEXT_MUTED,
        )
        nav_label.grid(row=2, column=0, sticky="ew", padx=24, pady=(8, 6))

        nav_wrap = ctk.CTkFrame(side, fg_color="transparent")
        nav_wrap.grid(row=3, column=0, sticky="ew", padx=12)
        nav_wrap.grid_columnconfigure(0, weight=1)
        for i, (key, icon, label) in enumerate(NAV_ITEMS):
            btn = NavButton(
                nav_wrap, icon=icon, label=label,
                command=lambda k=key: self.show_view(k),
            )
            btn.grid(row=i, column=0, sticky="ew", pady=2)
            self.nav_buttons[key] = btn

        # Reboot section
        ctk.CTkLabel(
            side, text="POWER", anchor="w",
            font=F(size=10, weight="bold"), text_color=TEXT_MUTED,
        ).grid(row=4, column=0, sticky="ew", padx=24, pady=(20, 6))

        power_wrap = ctk.CTkFrame(side, fg_color="transparent")
        power_wrap.grid(row=5, column=0, sticky="ew", padx=12)
        power_wrap.grid_columnconfigure(0, weight=1)

        for i, target in enumerate(["system", "bootloader", "recovery", "sideload"]):
            ctk.CTkButton(
                power_wrap, text=f"⏻   {target.capitalize()}", height=32,
                fg_color="transparent", hover_color=SURFACE_2, anchor="w",
                text_color=TEXT_DIM, font=F(size=12),
                border_width=0, corner_radius=8,
                command=lambda t=target: self.reboot(t),
            ).grid(row=i, column=0, sticky="ew", pady=2, padx=2)

        # Fastboot tools (erase, flashing unlock/lock) live one click below
        # the reboot buttons since they need the same fastboot session.
        ctk.CTkButton(
            power_wrap, text="⚙   Fastboot tools…", height=32,
            fg_color="transparent", hover_color=SURFACE_2, anchor="w",
            text_color=WARN, font=F(size=12),
            border_width=0, corner_radius=8,
            command=lambda: FastbootToolsDialog(self),
        ).grid(row=4, column=0, sticky="ew", pady=(8, 2), padx=2)

        # Footer
        footer = ctk.CTkFrame(side, fg_color="transparent")
        footer.grid(row=100, column=0, sticky="sew", padx=24, pady=(0, 18))
        footer.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            footer,
            text="Destructive operations are irreversible.\n"
                 "Verify backups before flashing.",
            anchor="w", justify="left",
            font=F(size=10), text_color=WARN, wraplength=200,
        ).grid(row=0, column=0, sticky="ew")

        ctk.CTkButton(
            footer, text="Settings", command=self._show_settings,
            height=26, width=80,
            fg_color="transparent", hover_color=SURFACE_2,
            text_color=TEXT_MUTED, font=F(size=11),
            border_width=0, corner_radius=6,
        ).grid(row=0, column=1, sticky="se", padx=(8, 4))

        ctk.CTkButton(
            footer, text="About", command=self._show_about,
            height=26, width=70,
            fg_color="transparent", hover_color=SURFACE_2,
            text_color=TEXT_MUTED, font=F(size=11),
            border_width=0, corner_radius=6,
        ).grid(row=0, column=2, sticky="se", padx=(4, 0))

    def _build_content(self) -> None:
        self.content = ctk.CTkFrame(self, fg_color=BG)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        self.views["backup"]     = BackupView(self.content, self)
        self.views["flash"]      = FlashView(self.content, self)
        self.views["restore"]    = RestoreView(self.content, self)
        self.views["verify"]     = VerifyView(self.content, self)
        self.views["sideload"]   = SideloadView(self.content, self)
        self.views["apps"]       = AppsView(self.content, self)
        self.views["logcat"]     = LogcatView(self.content, self)
        self.views["properties"] = PropertiesView(self.content, self)
        self.views["logs"]       = LogsView(self.content, self)

        for v in self.views.values():
            v.grid(row=0, column=0, sticky="nsew", padx=28, pady=24)
            v.grid_remove()

    def _build_statusbar(self) -> None:
        bar = ctk.CTkFrame(self, height=36, corner_radius=0, fg_color=BG_2)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        bar.grid_propagate(False)

        self.bar_dot = ctk.CTkLabel(
            bar, text="●", font=F(size=12), text_color=TEXT_MUTED, width=14,
        )
        self.bar_dot.grid(row=0, column=0, padx=(20, 8), pady=8, sticky="w")
        self.status_label = ctk.CTkLabel(
            bar, text="Ready.", anchor="w", font=F(size=11), text_color=TEXT_DIM,
        )
        self.status_label.grid(row=0, column=1, sticky="w", pady=8)

        self.bar_extra = ctk.CTkLabel(
            bar, text="", anchor="e", font=F(size=11), text_color=TEXT_MUTED,
        )
        self.bar_extra.grid(row=0, column=2, padx=(8, 20), pady=8, sticky="e")

    # ------- view management -------------------------------------------------

    def show_view(self, key: str) -> None:
        for k, v in self.views.items():
            if k == key:
                v.grid()
            else:
                v.grid_remove()
        for k, btn in self.nav_buttons.items():
            btn.set_active(k == key)
        self.current_view = key
        # Tell view it became active (in case it needs to refresh).
        view = self.views[key]
        if hasattr(view, "on_show"):
            view.on_show()

    # ------- status / toast --------------------------------------------------

    def status(self, msg: str, kind: str = "idle") -> None:
        color = {
            "idle": TEXT_MUTED, "ok": SUCCESS, "warn": WARN, "err": DANGER,
            "busy": ACCENT_GLOW,
        }.get(kind, TEXT_MUTED)
        self.bar_dot.configure(text_color=color)
        self.status_label.configure(text=msg, text_color=TEXT_DIM)

    def toast(self, message: str, kind: str = "info") -> None:
        t = Toast(self, message, kind)
        # Position top-right, stack downward
        x_pad = 24
        y_pad = 24
        y_off = sum(w.winfo_reqheight() + 10 for w in self._toasts)
        t.place(relx=1.0, x=-x_pad, y=y_pad + y_off, anchor="ne")
        self._toasts.append(t)
        self.after(4200, lambda: self._dismiss_toast(t))

    def _dismiss_toast(self, t: ctk.CTkFrame) -> None:
        try:
            t.destroy()
            if t in self._toasts:
                self._toasts.remove(t)
            # Reposition remaining toasts.
            y_off = 0
            for w in self._toasts:
                w.place_configure(y=24 + y_off)
                y_off += w.winfo_reqheight() + 10
        except tk.TclError:
            pass

    def log(self, msg: str) -> None:
        view = self.views.get("logs")
        if view:
            view.append(msg)

    # ------- device handling -------------------------------------------------

    def refresh_devices(self) -> None:
        self.status("Refreshing devices…", "busy")

        def work():
            try:
                devs = device_mod.discover()
                self.signal.emit({"event": "devices", "devices": devs})
            except Exception as e:
                self.signal.emit({"event": "error", "error": str(e),
                                  "trace": traceback.format_exc()})

        _run_thread(work)

    def _render_device(self, devs: list[Device], silent: bool = False) -> None:
        # Signature lets auto-poll skip re-render when nothing changed —
        # otherwise we'd re-enumerate partitions every poll tick.
        sig = tuple(sorted((d.serial, d.state, d.rooted) for d in devs))
        if silent and sig == self._last_device_signature:
            return
        self._last_device_signature = sig

        self.all_devices = devs
        if not devs:
            self.current_device = None
            self.current_health = None
            self.status_pill.set("idle")
            self.status_pill.set_text("Disconnected")
            self.device_info_label.configure(
                text="No device connected.\nPlug in a phone via USB.")
            self.device_selector.grid_remove()
            if not silent:
                self.status("No devices attached.", "idle")
            self.partitions = []
            self._refresh_views()  # single call only — was duplicated before
            return

        # Sort: prefer adb-mode device with root for the default pick.
        devs_sorted = sorted(
            devs, key=lambda d: (
                0 if d.state == "device" else 1,
                0 if d.rooted else 1,
            ),
        )

        # Multi-device dropdown only when >1.
        if len(devs) > 1:
            labels = [self._device_label(d) for d in devs_sorted]
            self._device_map = dict(zip(labels, devs_sorted))
            self.device_selector.configure(values=labels)
            self.device_selector.grid()
        else:
            self.device_selector.grid_remove()

        # Auto-reconnect: if the previously-selected serial is still attached
        # (possibly in a different mode after a reboot), re-select it instead
        # of the sort-default. Keeps the user's choice stable across
        # fastboot <-> adb transitions.
        prev_serial = getattr(self, "_last_selected_serial", None)
        pick = next(
            (d for d in devs_sorted if d.serial == prev_serial),
            devs_sorted[0],
        )
        if len(devs) > 1:
            self.device_selector.set(self._device_label(pick))
        self._select_device(pick)

    def _device_label(self, d: Device) -> str:
        name = d.model if d.model and d.model != "unknown" else d.serial
        return f"{name} • {d.state}"

    def _on_device_picked(self, label: str) -> None:
        m = getattr(self, "_device_map", {})
        d = m.get(label)
        if d:
            self._select_device(d)

    def _select_device(self, d: Device) -> None:
        self.current_device = d
        self._last_selected_serial = d.serial  # for auto-reconnect across reboots

        state_kind = "ok" if d.state == "device" and d.rooted else "warn"
        if d.state in ("offline", "unauthorized"):
            state_kind = "err"
        label_text = {
            "device":       "Connected" if d.rooted else "Connected (no root)",
            "fastboot":     "Fastboot mode",
            "recovery":     "Recovery mode",
            "sideload":     "Sideload mode",
            "unauthorized": "Unauthorized",
            "offline":      "Offline",
        }.get(d.state, d.state.capitalize())

        self.status_pill.set(state_kind)
        self.status_pill.set_text(label_text)

        info_lines = []
        if d.model and d.model != "unknown":
            info_lines.append(d.model)
        info_lines.append(d.serial)
        chip = d.chipset + (" • MTK" if d.is_mediatek else "")
        if chip and chip != "unknown":
            info_lines.append(chip)
        self.device_info_label.configure(text="\n".join(info_lines))

        # Hide health until we successfully query.
        self.health_frame.grid_remove()
        if d.state in ("device", "recovery"):
            self._query_health(d.serial)

        if d.state == "device" and d.rooted:
            self.status(f"Connected to {d.model or d.serial}.", "ok")
            self._load_partitions()
            # Offer a default output directory the moment a device is ready.
            bv = self.views.get("backup")
            if bv and hasattr(bv, "suggest_output_dir"):
                bv.suggest_output_dir(d)
        else:
            self.status(label_text, state_kind)
            self.partitions = []
            self._refresh_views()

    def confirm_battery_ok(self, op_name: str) -> bool:
        """Pre-flight check before destructive operations.

        Returns True if it's safe to proceed (battery unknown, or above
        threshold, or user accepted the warning); False if the user cancels.
        If battery info isn't available (fastboot mode, unrooted recovery,
        etc.) we don't block — there's no way to know."""
        h = self.current_health
        if h is None or h.battery_level is None:
            return True
        if h.battery_level >= self.BATTERY_DESTRUCTIVE_THRESHOLD:
            return True
        charging = (h.battery_status or "").lower() in ("charging", "full")
        body = (
            f"Battery is at {h.battery_level}%"
            + (" (charging)" if charging else "")
            + f", below the {self.BATTERY_DESTRUCTIVE_THRESHOLD}% pre-flight "
              f"threshold. If the phone dies mid-{op_name}, the partition can "
              f"be left in an unbootable state — recovery from that usually "
              f"needs SP Flash Tool or mtkclient.\n\n"
              f"Recommendation: plug into a wall charger and wait a few "
              f"minutes."
        )
        return ConfirmDialog.ask(
            self, title=f"Low battery — {op_name}?", body=body,
            confirm_text=f"{op_name.capitalize()} anyway", danger=True,
        )

    def _query_health(self, serial: str) -> None:
        signal = self.signal

        def work():
            try:
                h = device_mod.query_health(serial=serial)
                signal.emit({"event": "_health", "health": h, "serial": serial})
            except Exception as e:
                log.debug("health query failed: %s", e)

        _run_thread(work)

    def _render_health(self, health: "device_mod.DeviceHealth") -> None:
        # Cache for pre-flight checks before destructive ops.
        self.current_health = health
        # Bail if user has switched devices since query started.
        parts: list[str] = []
        if health.battery_level is not None:
            level = max(0, min(100, health.battery_level))
            self.battery_bar.set(level / 100.0)
            color = (DANGER if level < 20 else
                     WARN if level < 50 else SUCCESS)
            self.battery_bar.configure(progress_color=color)
            status = (f" • {health.battery_status}"
                      if health.battery_status else "")
            self.battery_label.configure(
                text=f"Battery {level}%{status}")
            parts.append("battery")
        else:
            self.battery_label.configure(text="Battery unknown")
            self.battery_bar.set(0)

        if health.data_total_bytes and health.data_free_bytes is not None:
            used = health.data_total_bytes - health.data_free_bytes
            pct = (used / health.data_total_bytes) * 100
            self.storage_label.configure(
                text=f"/data  {human_size(health.data_free_bytes)} free "
                     f"of {human_size(health.data_total_bytes)} ({pct:.0f}% used)"
            )
            parts.append("storage")
        else:
            self.storage_label.configure(text="")

        if parts:
            self.health_frame.grid()

    def _load_partitions(self) -> None:
        self.status("Enumerating partitions…", "busy")
        # Capture serial up front — `current_device` can change before the thread runs.
        serial = self.current_device.serial if self.current_device else None
        if not serial:
            return

        def work():
            try:
                parts = part_mod.list_partitions(serial=serial)
                self.signal.emit({"event": "partitions", "partitions": parts})
            except Exception as e:
                self.signal.emit({"event": "error", "error": str(e),
                                  "trace": traceback.format_exc()})

        _run_thread(work)

    def _refresh_views(self) -> None:
        for v in self.views.values():
            if hasattr(v, "on_device_changed"):
                v.on_device_changed()

    # ------- power -----------------------------------------------------------

    def reboot(self, target: str) -> None:
        if not self.current_device:
            self.toast("No device selected.", "warn")
            return
        d = self.current_device
        target_arg = None if target == "system" else target
        try:
            if d.state == "fastboot":
                # fastboot can't reboot directly into sideload.
                if target == "sideload":
                    raise RuntimeError(
                        "Sideload isn't reachable from fastboot. "
                        "Reboot to recovery first, then enter sideload from there."
                    )
                fastboot_mod.reboot(target_arg, serial=d.serial)
            elif d.state in ("device", "recovery", "sideload"):
                adb_mod.reboot(target_arg, serial=d.serial)
            else:
                raise RuntimeError(
                    f"Cannot reboot: device is {d.state!r}. "
                    "Accept the USB-debug prompt on the phone, or replug it."
                )
            self.toast(f"Rebooting to {target}…", "info")
            self.log(f"[reboot] {d.serial} ({d.state}) -> {target}")
            self.status(f"Reboot to {target} issued.", "ok")
        except Exception as e:
            self.toast(f"Reboot failed: {e}", "err")
            self.log(f"[reboot] failed: {e}")

    def _show_about(self) -> None:
        AboutDialog(self)

    def _show_settings(self) -> None:
        SettingsDialog(self)

    # ------- event dispatcher ------------------------------------------------

    def _poll_signal(self) -> None:
        try:
            for ev in self.signal.drain():
                self._handle_event(ev)
        finally:
            self.after(80, self._poll_signal)

    def _handle_event(self, ev: dict) -> None:
        kind = ev.get("event")
        if kind == "devices":
            self._render_device(ev["devices"], silent=ev.get("silent", False))
        elif kind == "partitions":
            self.partitions = ev["partitions"]
            self.status(f"{len(ev['partitions'])} partitions enumerated.", "ok")
            self._refresh_views()
        elif kind == "_health":
            # Drop the event if user has switched devices.
            if self.current_device and ev.get("serial") == self.current_device.serial:
                self._render_health(ev["health"])
        elif kind == "error":
            self.toast(ev["error"], "err")
            self.log("[error] " + ev["error"])
            self.log(ev.get("trace", ""))
            self.status("Error — see Logs.", "err")
        else:
            # delegate to view that registered for this event
            for v in self.views.values():
                if hasattr(v, "handle_event"):
                    v.handle_event(ev)


# ----------------------------------------------------------------------------
# Views
# ----------------------------------------------------------------------------

class BackupView(ctk.CTkFrame):

    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.partition_vars: dict[str, tk.BooleanVar] = {}
        self.partition_rows: dict[str, ctk.CTkFrame] = {}
        self._cancel_event: Optional[threading.Event] = None
        self._backup_started_at: float = 0.0
        self.grid_columnconfigure(0, weight=1)

        SectionHeader(
            self, icon="⤓", title="Backup",
            subtitle="Stream partition images from your phone to this Mac.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 18))

        # Output card
        out_card = Card(self)
        out_card.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        out_card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            out_card, text="Output directory", font=F(size=12, weight="bold"),
            text_color=TEXT_DIM,
        ).grid(row=0, column=0, columnspan=3, padx=20, pady=(16, 6), sticky="w")
        self.out_entry = ctk.CTkEntry(
            out_card, height=36, fg_color=BG_2, border_color=BORDER_2,
            text_color=TEXT, placeholder_text="~/backups/galaxy-2026-05-21",
        )
        self.out_entry.grid(row=1, column=0, columnspan=2, padx=(20, 8),
                            pady=(0, 8), sticky="ew")
        if self.app.settings.last_output_dir:
            self.out_entry.insert(0, self.app.settings.last_output_dir)
        self.out_entry.bind("<KeyRelease>", lambda e: self._update_selection_label())
        ctk.CTkButton(
            out_card, text="Browse", command=self._pick, height=36, width=90,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT,
            border_width=1, border_color=BORDER_2, font=F(size=12),
            corner_radius=8,
        ).grid(row=1, column=2, padx=(0, 20), pady=(0, 8))

        self.auto_verify = tk.BooleanVar(
            value=self.app.settings.auto_verify_after_backup)
        ctk.CTkSwitch(
            out_card, text="Auto-verify backup when complete",
            variable=self.auto_verify, progress_color=SUCCESS,
            font=F(size=12), text_color=TEXT_DIM,
        ).grid(row=2, column=0, columnspan=3, padx=20, pady=(0, 4), sticky="w")

        # gzip toggle — host-side compression, ~50% smaller backups, hashes
        # still cross-check against on-device sha256sum because we hash the
        # uncompressed bytes.
        self.compress_var = tk.BooleanVar(
            value=getattr(self.app.settings, "compress_backups", False))
        ctk.CTkSwitch(
            out_card, text="Compress with gzip (smaller backups, slightly slower)",
            variable=self.compress_var, progress_color=ACCENT,
            font=F(size=12), text_color=TEXT_DIM,
        ).grid(row=3, column=0, columnspan=3, padx=20, pady=(0, 16), sticky="w")

        # Partitions card
        parts_card = Card(self)
        parts_card.grid(row=2, column=0, sticky="nsew", pady=(0, 14))
        parts_card.grid_columnconfigure(0, weight=1)
        parts_card.grid_rowconfigure(2, weight=1)
        self.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(parts_card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 8))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            head, text="Partitions", font=F(size=12, weight="bold"),
            text_color=TEXT_DIM, anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.parts_count = ctk.CTkLabel(
            head, text="", font=F(size=11), text_color=TEXT_MUTED, anchor="e",
        )
        self.parts_count.grid(row=0, column=1, sticky="e")

        tool = ctk.CTkFrame(parts_card, fg_color="transparent")
        tool.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 8))
        tool.grid_columnconfigure(5, weight=1)
        for i, (label, cmd) in enumerate((("Default", self._select_default),
                                          ("All",     lambda: self._set_all(True)),
                                          ("None",    lambda: self._set_all(False)))):
            ctk.CTkButton(
                tool, text=label, command=cmd, height=26, width=68,
                fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT_DIM,
                border_width=1, border_color=BORDER_2, font=F(size=11),
                corner_radius=6,
            ).grid(row=0, column=i, padx=(0, 6))

        # Presets
        self.preset_btn = ctk.CTkButton(
            tool, text="Presets ▾", command=self._show_presets_menu,
            height=26, width=84,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT_DIM,
            border_width=1, border_color=BORDER_2, font=F(size=11),
            corner_radius=6,
        )
        self.preset_btn.grid(row=0, column=3, padx=(6, 6))

        ctk.CTkButton(
            tool, text="Save…", command=self._save_preset,
            height=26, width=64,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT_DIM,
            border_width=1, border_color=BORDER_2, font=F(size=11),
            corner_radius=6,
        ).grid(row=0, column=4, padx=(0, 6))

        self.search_entry = ctk.CTkEntry(
            tool, height=26, fg_color=BG_2, border_color=BORDER_2,
            text_color=TEXT, font=F(size=11),
            placeholder_text="Search partitions…",
        )
        self.search_entry.grid(row=0, column=6, sticky="e", padx=(12, 0))
        self.search_entry.bind("<KeyRelease>", lambda e: self._apply_filter())

        self.parts_container = ctk.CTkFrame(parts_card, fg_color="transparent")
        self.parts_container.grid(row=2, column=0, sticky="nsew",
                                  padx=12, pady=(0, 16))
        self.parts_container.grid_columnconfigure(0, weight=1)
        self.parts_container.grid_rowconfigure(0, weight=1)

        self.parts_scroll = ctk.CTkScrollableFrame(
            self.parts_container, fg_color=BG_2, corner_radius=8,
        )
        self.parts_scroll.grid(row=0, column=0, sticky="nsew")
        self.parts_scroll.grid_columnconfigure(0, weight=1)
        self.empty_parts = EmptyState(
            self.parts_container, icon="📱",
            title="No device connected",
            body="Connect a rooted Android phone via USB and click Refresh "
                 "in the sidebar to enumerate partitions.",
            action=("Refresh", self.app.refresh_devices),
        )

        # Footer / action
        foot = Card(self)
        foot.grid(row=3, column=0, sticky="ew")
        foot.grid_columnconfigure(0, weight=1)

        self.selection_label = ctk.CTkLabel(
            foot, text="Select partitions to enable backup.",
            anchor="w", font=F(size=12), text_color=TEXT_DIM,
        )
        self.selection_label.grid(row=0, column=0, padx=20, pady=16, sticky="w")

        self.start_btn = ctk.CTkButton(
            foot, text="Start backup →", command=self._start, state="disabled",
            height=40, width=180, fg_color=ACCENT, hover_color=ACCENT_HOV,
            text_color="#fff", font=F(size=13, weight="bold"), corner_radius=8,
        )
        self.start_btn.grid(row=0, column=1, padx=20, pady=16, sticky="e")

        # Progress card (hidden until running)
        self.progress_card = Card(self)
        self.progress_card.grid_columnconfigure(0, weight=1)

        prog_head = ctk.CTkFrame(self.progress_card, fg_color="transparent")
        prog_head.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 4))
        prog_head.grid_columnconfigure(0, weight=1)
        self.progress_title = ctk.CTkLabel(
            prog_head, text="Backing up…", anchor="w",
            font=F(size=13, weight="bold"), text_color=TEXT,
        )
        self.progress_title.grid(row=0, column=0, sticky="w")
        self.progress_pct = ctk.CTkLabel(
            prog_head, text="0%", anchor="e",
            font=F(size=12, weight="bold"), text_color=ACCENT_GLOW,
        )
        self.progress_pct.grid(row=0, column=1, sticky="e")

        self.progress_sub = ctk.CTkLabel(
            self.progress_card, text="", anchor="w",
            font=F(size=11), text_color=TEXT_MUTED,
        )
        self.progress_sub.grid(row=1, column=0, sticky="ew", padx=20)

        self.progress_bar = ctk.CTkProgressBar(
            self.progress_card, height=10, progress_color=ACCENT, fg_color=BG_2,
            corner_radius=4,
        )
        self.progress_bar.grid(row=2, column=0, sticky="ew", padx=20, pady=(8, 8))
        self.progress_bar.set(0)

        prog_foot = ctk.CTkFrame(self.progress_card, fg_color="transparent")
        prog_foot.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 16))
        prog_foot.grid_columnconfigure(0, weight=1)

        self.progress_stats = ctk.CTkLabel(
            prog_foot, text="", anchor="w", font=F_MONO(size=11),
            text_color=TEXT_MUTED,
        )
        self.progress_stats.grid(row=0, column=0, sticky="w")

        self.cancel_btn = ctk.CTkButton(
            prog_foot, text="Cancel", command=self._cancel_running,
            height=30, width=90, fg_color=SURFACE_2, hover_color=DANGER_DIM,
            text_color=DANGER, border_width=1, border_color=DANGER_DIM,
            font=F(size=12), corner_radius=8, state="disabled",
        )
        self.cancel_btn.grid(row=0, column=1, sticky="e", padx=(8, 0))

        self._running = False
        self.on_device_changed()

    # --- behavior ---

    def _pick(self):
        p = filedialog.askdirectory(
            title="Choose output directory",
            initialdir=(self.app.settings.last_output_dir or str(Path.home())),
        )
        if p:
            self.out_entry.delete(0, "end"); self.out_entry.insert(0, p)
            self._update_selection_label()

    def suggest_output_dir(self, d: Device) -> None:
        """Pre-fill the output entry with a sensible default — only when empty."""
        if self.out_entry.get().strip():
            return
        name = d.model if d.model and d.model != "unknown" else d.serial
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-").lower() or "device"
        # Reuse the parent of the user's last backup dir if they have one,
        # so backups stay in their preferred location.
        if self.app.settings.last_output_dir:
            base = Path(self.app.settings.last_output_dir).parent
        else:
            base = Path.home() / "arom-backups"
        suggested = base / f"{slug}-{date.today().isoformat()}"
        self.out_entry.delete(0, "end")
        self.out_entry.insert(0, str(suggested))
        self._update_selection_label()

    def _apply_filter(self):
        needle = self.search_entry.get().strip().lower()
        visible = 0
        for name, row in self.partition_rows.items():
            match = (not needle) or (needle in name.lower())
            if match:
                row.grid()
                visible += 1
            else:
                row.grid_remove()
        if needle:
            self.parts_count.configure(
                text=f"{visible} of {len(self.partition_rows)} match")
        else:
            self.parts_count.configure(text=f"{len(self.partition_rows)} found")

    def _cancel_running(self):
        if self._cancel_event:
            self._cancel_event.set()
            self.cancel_btn.configure(state="disabled", text="Cancelling…")
            self.app.status("Cancelling backup…", "warn")

    # ---- presets ----

    def _show_presets_menu(self) -> None:
        presets = self.app.settings.partition_presets
        if not presets:
            self.app.toast("No saved presets yet. Click Save… to add one.",
                           "info")
            return
        menu = tk.Menu(self, tearoff=False)
        for name in sorted(presets):
            menu.add_command(
                label=f"Load: {name}  ({len(presets[name])} parts)",
                command=lambda n=name: self._load_preset(n),
            )
        menu.add_separator()
        for name in sorted(presets):
            menu.add_command(label=f"Delete: {name}",
                             command=lambda n=name: self._delete_preset(n))
        try:
            menu.tk_popup(self.preset_btn.winfo_rootx(),
                          self.preset_btn.winfo_rooty()
                              + self.preset_btn.winfo_height())
        finally:
            menu.grab_release()

    def _load_preset(self, name: str) -> None:
        parts = set(self.app.settings.partition_presets.get(name, []))
        if not parts:
            self.app.toast(f"Preset '{name}' is empty.", "warn")
            return
        applied = 0
        for partname, var in self.partition_vars.items():
            on = partname in parts
            var.set(on)
            if on:
                applied += 1
        self._update_selection_label()
        self.app.toast(f"Loaded preset '{name}' ({applied} partitions).", "ok")

    def _delete_preset(self, name: str) -> None:
        if name in self.app.settings.partition_presets:
            del self.app.settings.partition_presets[name]
            self.app.settings.save()
            self.app.toast(f"Preset '{name}' deleted.", "ok")

    def _save_preset(self) -> None:
        sel = [n for n, v in self.partition_vars.items() if v.get()]
        if not sel:
            self.app.toast("Select at least one partition first.", "warn")
            return
        dlg = TextInputDialog(
            self.app, title="Save preset",
            prompt=f"Save {len(sel)} selected partition"
                   f"{'s' if len(sel)!=1 else ''} as a named preset:",
            placeholder="e.g. minimal, full, mtk-critical",
        )
        self.app.wait_window(dlg)
        name = (dlg.result or "").strip()
        if not name:
            return
        self.app.settings.partition_presets[name] = sorted(sel)
        self.app.settings.save()
        self.app.toast(f"Saved preset '{name}' ({len(sel)} partitions).", "ok")

    def _set_all(self, val: bool):
        for name, v in self.partition_vars.items():
            if is_dangerous(name) and val:
                continue  # never auto-select dangerous
            v.set(val)
        self._update_selection_label()

    def _select_default(self):
        default = set(DEFAULT_BACKUP_SET)
        for name, v in self.partition_vars.items():
            v.set(name in default)
        self._update_selection_label()

    def _update_selection_label(self):
        sel = [n for n, v in self.partition_vars.items() if v.get()]
        if not sel:
            self.selection_label.configure(
                text="Select partitions to enable backup.", text_color=TEXT_MUTED)
            self.start_btn.configure(state="disabled")
            return
        total = sum((p.size_bytes or 0) for p in self.app.partitions
                    if p.name in sel)
        self.selection_label.configure(
            text=f"{len(sel)} partitions selected  ·  ~{human_size(total)}",
            text_color=TEXT,
        )
        self.start_btn.configure(state="normal" if self.out_entry.get().strip() else "disabled")

    def on_device_changed(self) -> None:
        # clear & rebuild list
        for w in self.parts_scroll.winfo_children():
            w.destroy()
        self.partition_vars.clear()
        self.partition_rows.clear()

        if not self.app.partitions:
            self.parts_scroll.grid_remove()
            self.empty_parts.grid(row=0, column=0, sticky="nsew")
            self.parts_count.configure(text="")
            title, body, hint = device_unavailable_copy(
                self.app.current_device, "enumerate partitions",
                requires_root=True,
            )
            self.empty_parts.set_text(title, body)
            self.selection_label.configure(text=hint, text_color=TEXT_MUTED)
            self.start_btn.configure(state="disabled")
            return

        self.empty_parts.grid_remove()
        self.parts_scroll.grid()
        self.parts_count.configure(text=f"{len(self.app.partitions)} found")

        # Default selection prefers settings if non-empty, else DEFAULT_BACKUP_SET.
        saved = set(self.app.settings.default_partition_selection)
        default = saved if saved else set(DEFAULT_BACKUP_SET)

        for i, p in enumerate(self.app.partitions):
            row = self._make_partition_row(p, in_default=p.name in default)
            row.grid(row=i, column=0, sticky="ew", padx=4, pady=1)
            self.partition_rows[p.name] = row
        self._update_selection_label()
        self._apply_filter()

    def _make_partition_row(self, p: Partition, in_default: bool) -> ctk.CTkFrame:
        row = ctk.CTkFrame(self.parts_scroll, fg_color="transparent",
                           corner_radius=6, height=30)
        row.grid_columnconfigure(1, weight=1)

        var = tk.BooleanVar(value=in_default and not is_dangerous(p.name))
        self.partition_vars[p.name] = var

        cb = ctk.CTkCheckBox(
            row, text="", variable=var, width=20,
            fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER_2,
            command=self._update_selection_label,
        )
        cb.grid(row=0, column=0, padx=(10, 8), pady=5)

        ctk.CTkLabel(
            row, text=p.name, anchor="w", font=F(size=12), text_color=TEXT,
        ).grid(row=0, column=1, sticky="w")

        ctk.CTkLabel(
            row, text=human_size(p.size_bytes) if p.size_bytes else "?",
            anchor="e", font=F_MONO(size=11), text_color=TEXT_MUTED, width=80,
        ).grid(row=0, column=2, sticky="e", padx=(0, 6))

        tags = ctk.CTkFrame(row, fg_color="transparent")
        tags.grid(row=0, column=3, sticky="e", padx=(0, 10))
        if is_dangerous(p.name):
            Tag(tags, "DANGER", DANGER).pack(side="left", padx=2)
        if p.name in MTK_CRITICAL:
            Tag(tags, "MTK",    MTK).pack(side="left", padx=2)
        if p.name in ("userdata", "data"):
            Tag(tags, "LARGE",  WARN).pack(side="left", padx=2)

        # Hover effect.
        def on_enter(_e):
            if not self._running:
                row.configure(fg_color=SURFACE_2)
        def on_leave(_e):
            row.configure(fg_color="transparent")
        for w in (row,):
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)

        return row

    def on_show(self) -> None:
        self._update_selection_label()

    # --- start ---

    def _start(self) -> None:
        if self._running:
            return
        out = self.out_entry.get().strip()
        if not out:
            self.app.toast("Choose an output directory first.", "warn")
            return
        sel = [p for p in self.app.partitions if self.partition_vars[p.name].get()]
        if not sel:
            self.app.toast("Select at least one partition.", "warn")
            return

        if any(is_dangerous(p.name) for p in sel):
            danger_names = [p.name for p in sel if is_dangerous(p.name)]
            ok = ConfirmDialog.ask(
                self.app,
                title="Backing up dangerous partitions",
                body=("You've selected partitions that are typically read-only or "
                      "device-critical:\n\n  " + ", ".join(danger_names) +
                      "\n\nBacking these up is safe, but flashing them later "
                      "can brick your phone. Continue?"),
                confirm_text="Yes, back up",
            )
            if not ok:
                return

        # Disk-space safety: warn if the target volume doesn't have enough room
        # (with 20% headroom for filesystem overhead / other writes).
        needed = sum(p.size_bytes or 0 for p in sel)
        target_root = Path(out).expanduser()
        probe_dir = target_root if target_root.exists() else target_root.parent
        while probe_dir != probe_dir.parent and not probe_dir.exists():
            probe_dir = probe_dir.parent
        try:
            free = shutil.disk_usage(probe_dir).free
        except Exception:
            free = None
        if free is not None and free < int(needed * 1.2):
            ok = ConfirmDialog.ask(
                self.app,
                title="Not enough free disk space",
                body=(f"This backup needs ~{human_size(needed)} (plus headroom).\n"
                      f"Free on {probe_dir}: {human_size(free)}.\n\n"
                      "Continue anyway?"),
                confirm_text="Continue", danger=True,
            )
            if not ok:
                return

        out_dir = Path(out)
        total_bytes = sum(p.size_bytes or 0 for p in sel) or 1
        bytes_done = 0
        per_part_total = {p.name: (p.size_bytes or 0) for p in sel}

        # Persist user choices.
        self.app.settings.last_output_dir = str(out_dir)
        self.app.settings.default_partition_selection = [p.name for p in sel]
        self.app.settings.auto_verify_after_backup = self.auto_verify.get()
        if hasattr(self.app.settings, "compress_backups"):
            self.app.settings.compress_backups = bool(self.compress_var.get())
        self.app.settings.save()

        self._running = True
        self._cancel_event = threading.Event()
        self._backup_started_at = time.monotonic()
        self.start_btn.configure(state="disabled", text="Running…")
        self.cancel_btn.configure(state="normal", text="Cancel")
        self.progress_card.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        self.progress_title.configure(text="Starting…")
        self.progress_sub.configure(text="")
        self.progress_pct.configure(text="0%")
        self.progress_stats.configure(text="")
        self.progress_bar.set(0)
        self.app.status(f"Backing up {len(sel)} partitions…", "busy")
        self.app.log(f"[backup] -> {out_dir}; partitions: "
                     f"{', '.join(p.name for p in sel)}")
        self.app.toast(f"Started backup of {len(sel)} partitions.", "info")

        device = self.app.current_device
        signal = self.app.signal
        cancel_event = self._cancel_event
        do_verify = self.auto_verify.get()

        def emit(ev: dict) -> None:
            nonlocal bytes_done
            if ev["type"] == "advance":
                bytes_done += ev["bytes"]
                signal.emit({"event": "_backup_progress",
                             "bytes_done": bytes_done,
                             "total_bytes": total_bytes,
                             "current": ev["name"],
                             "current_total": per_part_total.get(ev["name"], 0)})
            else:
                signal.emit({"event": f"_backup_{ev['type']}", **ev,
                             "expected": per_part_total.get(ev.get("name"), 0)})

        compress = bool(self.compress_var.get())

        def work():
            try:
                entries = backup_mod.backup_partitions(
                    device=device, parts=sel, out_dir=out_dir,
                    verify_on_device=True, events=emit,
                    cancel=cancel_event, compress=compress,
                )
                if not entries:
                    # Cancelled before any entry was completed.
                    signal.emit({"event": "_backup_aborted",
                                 "out": str(out_dir)})
                    return
                manifest = Manifest.new(device_info={
                    "serial": device.serial, "model": device.model,
                    "fingerprint": device.fingerprint, "chipset": device.chipset,
                    "is_mediatek": device.is_mediatek,
                    "properties": device.properties,
                })
                manifest.partitions = entries
                manifest.write(out_dir / MANIFEST_FILENAME)
                signal.emit({"event": "_backup_complete", "out": str(out_dir),
                             "count": len(entries),
                             "do_verify": do_verify,
                             "cancelled": cancel_event.is_set()})
            except Exception as e:
                signal.emit({"event": "error", "error": str(e),
                             "trace": traceback.format_exc()})
            finally:
                signal.emit({"event": "_backup_finished"})

        _run_thread(work)

    # --- event handlers ---

    def handle_event(self, ev: dict) -> None:
        kind = ev.get("event", "")
        if kind == "_backup_start":
            self.progress_title.configure(text=f"Backing up {ev['name']}")
            self.progress_sub.configure(
                text=f"~{human_size(ev.get('expected', 0))}")
        elif kind == "_backup_progress":
            frac = ev["bytes_done"] / ev["total_bytes"]
            self.progress_bar.set(frac)
            self.progress_pct.configure(text=f"{int(frac * 100)}%")
            self.progress_title.configure(text=f"Backing up {ev['current']}")
            cur_h = human_size(ev["bytes_done"])
            tot_h = human_size(ev["total_bytes"])
            self.progress_sub.configure(text=f"{cur_h} / {tot_h}")

            elapsed = max(0.001, time.monotonic() - self._backup_started_at)
            speed = ev["bytes_done"] / elapsed
            remaining = ev["total_bytes"] - ev["bytes_done"]
            eta_s = remaining / speed if speed > 0 else 0
            self.progress_stats.configure(
                text=f"{human_size(int(speed))}/s   ·   "
                     f"ETA {_fmt_eta(eta_s)}   ·   "
                     f"elapsed {_fmt_eta(elapsed)}"
            )
        elif kind == "_backup_done":
            self.app.log(f"[backup] OK    {ev['name']}  sha256={ev['sha256'][:12]}…  "
                         f"size={human_size(ev['written'])}")
        elif kind == "_backup_error":
            self.app.log(f"[backup] FAIL  {ev['name']}: {ev['error']}")
            self.app.toast(f"{ev['name']}: {ev['error']}", "err")
        elif kind == "_backup_cancelled":
            self.app.log(f"[backup] CANCELLED at {ev['name']}")
        elif kind == "_backup_complete":
            cancelled = ev.get("cancelled", False)
            self.app.settings.push_recent_backup(ev["out"])
            self.app.settings.save()
            if cancelled:
                msg = f"Backup cancelled after {ev['count']} partitions."
                kind_ = "warn"
            else:
                msg = f"Backup complete — {ev['count']} partitions saved."
                kind_ = "ok"
            self.app.log(f"[backup] {'CANCELLED' if cancelled else 'COMPLETE'} "
                         f"— {ev['count']} partitions in {ev['out']}")
            self.app.toast(msg, kind_)
            self.app.status(msg, kind_)
            # Native notification — backups can take 10+ min, user has moved on.
            notify("ROM Extractor — Backup", msg, sound=not cancelled)
            self.progress_title.configure(
                text="Backup cancelled" if cancelled else "Backup complete")
            self.progress_pct.configure(text=f"{int(self.progress_bar.get()*100)}%"
                                        if cancelled else "100%")
            if not cancelled:
                self.progress_bar.set(1.0)
            if ev.get("do_verify") and not cancelled:
                self.after(400, lambda out=ev["out"]: self._kick_auto_verify(out))
        elif kind == "_backup_aborted":
            self.app.log("[backup] cancelled before any partition completed")
            self.app.toast("Backup cancelled.", "warn")
            self.app.status("Backup cancelled.", "warn")
            # Hide progress card after a moment so the user can see why it stopped.
            self.progress_title.configure(text="Backup cancelled")
            self.after(2500, lambda: self.progress_card.grid_remove())
        elif kind == "_backup_finished":
            self._running = False
            self._cancel_event = None
            self.start_btn.configure(state="normal", text="Start backup →")
            self.cancel_btn.configure(state="disabled", text="Cancel")

    def _kick_auto_verify(self, out_dir: str) -> None:
        v = self.app.views.get("verify")
        if not v:
            return
        v.dir_entry.delete(0, "end")
        v.dir_entry.insert(0, out_dir)
        self.app.show_view("verify")
        v._verify()


class FlashView(ctk.CTkFrame):

    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)

        SectionHeader(
            self, icon="⚡", title="Flash",
            subtitle="Write a single image to a partition via fastboot.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 18))

        card = Card(self)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        card.grid_columnconfigure(1, weight=1)

        self._field(card, "Image file", row=0)
        self.image_entry = ctk.CTkEntry(
            card, height=36, fg_color=BG_2, border_color=BORDER_2,
            text_color=TEXT, placeholder_text="path/to/boot.img",
        )
        self.image_entry.grid(row=1, column=0, columnspan=2,
                              padx=(20, 8), pady=(0, 14), sticky="ew")
        ctk.CTkButton(
            card, text="Browse", command=self._pick_image, height=36, width=90,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT,
            border_width=1, border_color=BORDER_2, font=F(size=12),
            corner_radius=8,
        ).grid(row=1, column=2, padx=(0, 20), pady=(0, 14))

        self._field(card, "Partition", row=2)
        self.partition_combo = ctk.CTkComboBox(
            card, values=["boot", "recovery", "system", "vendor", "dtbo"],
            height=36, fg_color=BG_2, border_color=BORDER_2, text_color=TEXT,
            button_color=ACCENT, button_hover_color=ACCENT_HOV,
        )
        self.partition_combo.grid(row=3, column=0, columnspan=3,
                                  padx=20, pady=(0, 14), sticky="ew")

        self.boot_only = tk.BooleanVar(value=False)
        ctk.CTkSwitch(
            card, text="Boot only — test the image without writing it",
            variable=self.boot_only, progress_color=ACCENT,
            font=F(size=12), text_color=TEXT_DIM,
        ).grid(row=4, column=0, columnspan=3, padx=20, pady=4, sticky="w")

        self.force = tk.BooleanVar(value=False)
        ctk.CTkSwitch(
            card, text="Allow dangerous partitions (preloader, lk, tee…)",
            variable=self.force, progress_color=DANGER,
            font=F(size=12), text_color=TEXT_DIM,
        ).grid(row=5, column=0, columnspan=3, padx=20, pady=(4, 16), sticky="w")

        # Warning banner
        banner = ctk.CTkFrame(
            self, fg_color=DANGER_DIM, corner_radius=8, border_width=1,
            border_color=DANGER,
        )
        banner.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        banner.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            banner, text="⚠", font=F(size=20, weight="bold"),
            text_color=DANGER, width=32,
        ).grid(row=0, column=0, padx=(16, 8), pady=12)
        ctk.CTkLabel(
            banner,
            text="Flashing the wrong partition can brick your device. "
                 "Make sure you have a verified backup first.",
            font=F(size=12), text_color=TEXT, anchor="w", wraplength=720,
        ).grid(row=0, column=1, sticky="w", padx=(0, 16), pady=12)

        action = Card(self)
        action.grid(row=3, column=0, sticky="ew")
        action.grid_columnconfigure(0, weight=1)
        self.action_hint = ctk.CTkLabel(
            action, text="Device must be in fastboot mode.",
            anchor="w", font=F(size=12), text_color=TEXT_DIM,
        )
        self.action_hint.grid(row=0, column=0, padx=20, pady=16, sticky="w")
        self.flash_btn = ctk.CTkButton(
            action, text="Flash image", command=self._flash,
            height=40, width=180, fg_color=DANGER, hover_color=DANGER_HOV,
            text_color="#fff", font=F(size=13, weight="bold"), corner_radius=8,
        )
        self.flash_btn.grid(row=0, column=1, padx=20, pady=16, sticky="e")

        # Output card
        out_card = Card(self)
        out_card.grid(row=4, column=0, sticky="nsew", pady=(14, 0))
        out_card.grid_columnconfigure(0, weight=1)
        out_card.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(
            out_card, text="Image info / output", font=F(size=12, weight="bold"),
            text_color=TEXT_DIM, anchor="w",
        ).grid(row=0, column=0, padx=20, pady=(16, 6), sticky="w")

        self.output = ctk.CTkTextbox(
            out_card, fg_color=BG_2, text_color=TEXT_DIM,
            font=F_MONO(size=11), corner_radius=8, height=180,
        )
        self.output.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 16))

    def _field(self, parent, label: str, row: int):
        ctk.CTkLabel(
            parent, text=label, font=F(size=12, weight="bold"),
            text_color=TEXT_DIM, anchor="w",
        ).grid(row=row, column=0, columnspan=3, padx=20, pady=(16, 4), sticky="w")

    def _pick_image(self):
        p = filedialog.askopenfilename(
            title="Choose image file",
            filetypes=[("Images", "*.img *.bin"), ("All files", "*.*")],
        )
        if not p:
            return
        self.image_entry.delete(0, "end"); self.image_entry.insert(0, p)
        try:
            size = Path(p).stat().st_size
            self.output.delete("1.0", "end")
            self.output.insert("end",
                f"file:     {p}\n"
                f"size:     {human_size(size)}\n"
                f"sha256:   computing…\n"
            )
        except Exception as e:
            self.output.insert("end", f"\nerror reading file: {e}\n")
            return

        signal = self.app.signal

        def work():
            try:
                sha = sha256_file(Path(p))
                # Also run a cheap boot-image analysis. Skips for files that
                # aren't Android boot/recovery — output reflects that.
                info = None
                try:
                    info = boot_analyze.analyze(Path(p))
                except Exception:
                    pass
                signal.emit({"event": "_flash_hash", "file": p,
                             "size": size, "sha": sha,
                             "boot_info": info})
            except Exception as e:
                signal.emit({"event": "_flash_hash_err", "error": str(e)})

        _run_thread(work)

    def on_device_changed(self) -> None:
        opts = sorted({p.name for p in self.app.partitions})
        if opts:
            self.partition_combo.configure(values=opts)

    def on_show(self) -> None:
        pass

    def _flash(self):
        image = self.image_entry.get().strip()
        partition = self.partition_combo.get().strip()
        if not image or not partition:
            self.app.toast("Image and partition are required.", "warn")
            return
        ip = Path(image)
        if not ip.exists():
            self.app.toast("Image file does not exist.", "err")
            return

        boot_only = self.boot_only.get()
        force = self.force.get()

        title = "Boot image (test mode)" if boot_only else "Flash image"
        body = (
            f"This will {'boot' if boot_only else 'overwrite the'} "
            f"{partition} {'image' if boot_only else 'partition'} on your device.\n\n"
            f"Image:  {ip.name}\n"
            f"Size:   {human_size(ip.stat().st_size)}\n\n"
            "The device must be in fastboot mode."
        )
        require_typed = None if boot_only else partition
        ok = ConfirmDialog.ask(
            self.app, title=title, body=body,
            confirm_text="Boot it" if boot_only else f"Flash {partition}",
            danger=not boot_only, require_typed=require_typed,
        )
        if not ok:
            return

        # Battery pre-flight (only matters for actual flash, not test boot).
        if not boot_only and not self.app.confirm_battery_ok("flash"):
            return

        serial = self.app.current_device.serial if self.app.current_device else None
        self.flash_btn.configure(state="disabled", text="Flashing…")
        self.app.status(f"{'Booting' if boot_only else 'Flashing'} {partition}…", "busy")

        signal = self.app.signal

        def work():
            try:
                flash_mod.flash_image(
                    partition=partition, image=ip, serial=serial,
                    boot_only=boot_only, dry_run=False,
                    force=force, assume_yes=True,
                )
                signal.emit({"event": "_flash_done",
                             "partition": partition, "boot_only": boot_only})
            except Exception as e:
                signal.emit({"event": "error", "error": str(e),
                             "trace": traceback.format_exc()})
            finally:
                signal.emit({"event": "_flash_finished"})

        _run_thread(work)

    def handle_event(self, ev: dict) -> None:
        kind = ev.get("event", "")
        if kind == "_flash_done":
            msg = (f"Booted {ev['partition']}." if ev["boot_only"]
                   else f"Flashed {ev['partition']}.")
            self.output.insert("end", "\n" + msg + "\n")
            self.app.toast(msg, "ok")
            self.app.status(msg, "ok")
            notify("ROM Extractor — Flash", msg, sound=True)
        elif kind == "_flash_finished":
            self.flash_btn.configure(state="normal", text="Flash image")
        elif kind == "_flash_hash":
            # Only update if user hasn't moved on to a different file.
            if self.image_entry.get().strip() == ev["file"]:
                self.output.delete("1.0", "end")
                self.output.insert("end",
                    f"file:     {ev['file']}\n"
                    f"size:     {human_size(ev['size'])}\n"
                    f"sha256:   {ev['sha']}\n"
                )
                info = ev.get("boot_info")
                if info is not None:
                    self.output.insert("end", f"boot:     {info.summary}\n")
                    if info.magisk_patched:
                        self.output.insert(
                            "end",
                            "warning:  image looks Magisk-patched — flashing this "
                            "WILL keep root, but verify it matches your device.\n")
                    elif info.other_root:
                        self.output.insert(
                            "end",
                            f"warning:  image contains {', '.join(info.other_root)} "
                            "markers — incompatible with Magisk on-device.\n")
        elif kind == "_flash_hash_err":
            self.output.insert("end", f"\nhash error: {ev['error']}\n")


class RestoreView(ctk.CTkFrame):

    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)

        SectionHeader(
            self, icon="↩", title="Restore",
            subtitle="Re-flash an entire backup directory using its manifest.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 18))

        card = Card(self)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            card, text="Backup directory", font=F(size=12, weight="bold"),
            text_color=TEXT_DIM, anchor="w",
        ).grid(row=0, column=0, columnspan=3, padx=20, pady=(16, 6), sticky="w")

        self.dir_entry = ctk.CTkEntry(
            card, height=36, fg_color=BG_2, border_color=BORDER_2,
            text_color=TEXT, placeholder_text="path/to/backup-…",
        )
        self.dir_entry.grid(row=1, column=0, padx=(20, 8), pady=(0, 14),
                            sticky="ew")
        self.recent_btn = ctk.CTkButton(
            card, text="Recent ▾", command=self._show_recent,
            height=36, width=84,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT_DIM,
            border_width=1, border_color=BORDER_2, font=F(size=12),
            corner_radius=8,
        )
        self.recent_btn.grid(row=1, column=1, padx=(0, 8), pady=(0, 14))
        ctk.CTkButton(
            card, text="Browse", command=self._pick, height=36, width=90,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT,
            border_width=1, border_color=BORDER_2, font=F(size=12),
            corner_radius=8,
        ).grid(row=1, column=2, padx=(0, 20), pady=(0, 14))

        self.include_userdata = tk.BooleanVar(value=False)
        ctk.CTkSwitch(
            card, text="Include userdata (large; rarely wanted)",
            variable=self.include_userdata, progress_color=WARN,
            font=F(size=12), text_color=TEXT_DIM,
        ).grid(row=2, column=0, columnspan=3, padx=20, pady=4, sticky="w")

        self.force = tk.BooleanVar(value=False)
        ctk.CTkSwitch(
            card, text="Allow dangerous partitions",
            variable=self.force, progress_color=DANGER,
            font=F(size=12), text_color=TEXT_DIM,
        ).grid(row=3, column=0, columnspan=3, padx=20, pady=(4, 16), sticky="w")

        action = Card(self)
        action.grid(row=2, column=0, sticky="ew")
        action.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            action, text="Device must be in fastboot mode.",
            anchor="w", font=F(size=12), text_color=TEXT_DIM,
        ).grid(row=0, column=0, padx=20, pady=16, sticky="w")
        self.restore_btn = ctk.CTkButton(
            action, text="Restore backup", command=self._restore,
            height=40, width=180, fg_color=DANGER, hover_color=DANGER_HOV,
            text_color="#fff", font=F(size=13, weight="bold"), corner_radius=8,
        )
        self.restore_btn.grid(row=0, column=1, padx=20, pady=16, sticky="e")

        out_card = Card(self)
        out_card.grid(row=3, column=0, sticky="nsew", pady=(14, 0))
        out_card.grid_columnconfigure(0, weight=1)
        out_card.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(
            out_card, text="Output", font=F(size=12, weight="bold"),
            text_color=TEXT_DIM, anchor="w",
        ).grid(row=0, column=0, padx=20, pady=(16, 6), sticky="w")
        self.output = ctk.CTkTextbox(
            out_card, fg_color=BG_2, text_color=TEXT_DIM,
            font=F_MONO(size=11), corner_radius=8, height=200,
        )
        self.output.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 16))

    def _pick(self):
        initial = self.app.settings.last_backup_dir or self.app.settings.last_output_dir
        p = filedialog.askdirectory(title="Choose backup directory",
                                    initialdir=initial or str(Path.home()))
        if p:
            self.dir_entry.delete(0, "end"); self.dir_entry.insert(0, p)
            self.app.settings.last_backup_dir = p
            self.app.settings.save()

    def _show_recent(self):
        recent = self.app.settings.recent_backups
        if not recent:
            self.app.toast("No recent backups yet.", "info")
            return
        menu = tk.Menu(self, tearoff=False)
        for path in recent:
            menu.add_command(label=path,
                             command=lambda p=path: self._pick_recent(p))
        try:
            menu.tk_popup(self.recent_btn.winfo_rootx(),
                          self.recent_btn.winfo_rooty()
                              + self.recent_btn.winfo_height())
        finally:
            menu.grab_release()

    def _pick_recent(self, path: str):
        self.dir_entry.delete(0, "end"); self.dir_entry.insert(0, path)

    def on_device_changed(self): pass
    def on_show(self): pass

    def _restore(self):
        d = self.dir_entry.get().strip()
        if not d:
            self.app.toast("Choose a backup directory.", "warn")
            return
        backup_dir = Path(d)
        if not (backup_dir / MANIFEST_FILENAME).exists():
            self.app.toast("No manifest.json in that directory.", "err")
            return

        ok = ConfirmDialog.ask(
            self.app, title="Restore backup?",
            body=("This will overwrite multiple partitions on your device with "
                  "the images from this backup.\n\n"
                  f"Source: {backup_dir}\n\n"
                  "Make sure the device is in fastboot mode."),
            confirm_text="Restore", danger=True, require_typed="RESTORE",
        )
        if not ok:
            return

        # Restoring multiple partitions takes minutes — battery check is
        # especially important here.
        if not self.app.confirm_battery_ok("restore"):
            return

        serial = self.app.current_device.serial if self.app.current_device else None
        include_userdata = self.include_userdata.get()
        force = self.force.get()
        self.restore_btn.configure(state="disabled", text="Restoring…")
        self.output.delete("1.0", "end")
        self.app.status("Restoring backup…", "busy")

        signal = self.app.signal

        def work():
            try:
                flash_mod.restore_backup(
                    backup_dir=backup_dir, serial=serial,
                    include_userdata=include_userdata, force=force,
                    dry_run=False, assume_yes=True,
                )
                signal.emit({"event": "_restore_done"})
            except Exception as e:
                signal.emit({"event": "error", "error": str(e),
                             "trace": traceback.format_exc()})
            finally:
                signal.emit({"event": "_restore_finished"})

        _run_thread(work)

    def handle_event(self, ev):
        kind = ev.get("event", "")
        if kind == "_restore_done":
            self.output.insert("end", "Restore complete.\n")
            self.app.toast("Restore complete.", "ok")
            self.app.status("Restore complete.", "ok")
            notify("ROM Extractor — Restore",
                   "Restore complete.", sound=True)
        elif kind == "_restore_finished":
            self.restore_btn.configure(state="normal", text="Restore backup")


class VerifyView(ctk.CTkFrame):
    """Manifest browser + per-partition verify status."""

    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self._rows: dict[str, dict] = {}     # name -> {frame, status_label, ...}
        self._manifest: Optional[Manifest] = None
        self._backup_dir: Optional[Path] = None

        SectionHeader(
            self, icon="✓", title="Verify",
            subtitle="Browse a backup manifest and re-check every SHA-256.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 18))

        card = Card(self)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card, text="Backup directory", font=F(size=12, weight="bold"),
            text_color=TEXT_DIM, anchor="w",
        ).grid(row=0, column=0, columnspan=4, padx=20, pady=(16, 6), sticky="w")
        self.dir_entry = ctk.CTkEntry(
            card, height=36, fg_color=BG_2, border_color=BORDER_2,
            text_color=TEXT, placeholder_text="path/to/backup-…",
        )
        self.dir_entry.grid(row=1, column=0, padx=(20, 8), pady=(0, 16),
                            sticky="ew")
        self.dir_entry.bind("<Return>", lambda e: self._load_manifest())
        self.dir_entry.bind("<FocusOut>", lambda e: self._load_manifest())

        self.recent_btn = ctk.CTkButton(
            card, text="Recent ▾", command=self._show_recent,
            height=36, width=84,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT_DIM,
            border_width=1, border_color=BORDER_2, font=F(size=12),
            corner_radius=8,
        )
        self.recent_btn.grid(row=1, column=1, padx=(0, 8), pady=(0, 16))
        ctk.CTkButton(
            card, text="Browse", command=self._pick, height=36, width=90,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT,
            border_width=1, border_color=BORDER_2, font=F(size=12),
            corner_radius=8,
        ).grid(row=1, column=2, padx=(0, 20), pady=(0, 16))

        # Manifest summary card
        self.summary_card = Card(self)
        self.summary_card.grid_columnconfigure(0, weight=1)
        self.summary_card.grid_columnconfigure(1, weight=1)
        self.summary_card.grid_columnconfigure(2, weight=1)
        self.summary_card.grid_columnconfigure(3, weight=1)
        self._summary_labels: dict[str, ctk.CTkLabel] = {}
        for i, (key, label) in enumerate((
                ("device",     "Device"),
                ("chipset",    "Chipset"),
                ("created",    "Created"),
                ("partitions", "Partitions"),
        )):
            box = ctk.CTkFrame(self.summary_card, fg_color="transparent")
            box.grid(row=0, column=i, padx=20, pady=(16, 14), sticky="ew")
            ctk.CTkLabel(
                box, text=label.upper(), font=F(size=10, weight="bold"),
                text_color=TEXT_MUTED, anchor="w",
            ).pack(anchor="w")
            v = ctk.CTkLabel(
                box, text="—", font=F(size=14, weight="bold"),
                text_color=TEXT, anchor="w",
            )
            v.pack(anchor="w", pady=(4, 0))
            self._summary_labels[key] = v

        # Table card
        table_card = Card(self)
        table_card.grid(row=3, column=0, sticky="nsew", pady=(14, 14))
        table_card.grid_columnconfigure(0, weight=1)
        table_card.grid_rowconfigure(2, weight=1)
        self.grid_rowconfigure(3, weight=1)

        head = ctk.CTkFrame(table_card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 6))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            head, text="Partitions", font=F(size=12, weight="bold"),
            text_color=TEXT_DIM, anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.table_count = ctk.CTkLabel(
            head, text="", anchor="e", font=F(size=11), text_color=TEXT_MUTED,
        )
        self.table_count.grid(row=0, column=1, sticky="e")

        # Column header
        col_head = ctk.CTkFrame(table_card, fg_color="transparent")
        col_head.grid(row=1, column=0, sticky="ew", padx=20, pady=(2, 4))
        col_head.grid_columnconfigure(1, weight=1)
        for col, (txt, w) in enumerate(((" ", 32), ("Name", 120), ("Size", 80),
                                        ("SHA-256", 120))):
            ctk.CTkLabel(
                col_head, text=txt, font=F(size=10, weight="bold"),
                text_color=TEXT_MUTED, anchor="w", width=w,
            ).grid(row=0, column=col, padx=(4 if col else 0, 4), sticky="w")

        self.table_scroll = ctk.CTkScrollableFrame(
            table_card, fg_color=BG_2, corner_radius=8,
        )
        self.table_scroll.grid(row=2, column=0, sticky="nsew", padx=14,
                               pady=(0, 14))
        self.table_scroll.grid_columnconfigure(0, weight=1)

        self.empty_state = EmptyState(
            table_card, icon="📂",
            title="No backup loaded",
            body="Pick a backup directory above to browse its manifest. "
                 "Then click Verify to re-hash every image.",
            action=("Browse", self._pick),
        )

        # Action row
        action = Card(self)
        action.grid(row=4, column=0, sticky="ew")
        action.grid_columnconfigure(0, weight=1)
        self.action_hint = ctk.CTkLabel(
            action, text="Pick a backup to begin.",
            anchor="w", font=F(size=12), text_color=TEXT_DIM,
        )
        self.action_hint.grid(row=0, column=0, padx=20, pady=16, sticky="w")
        self.verify_btn = ctk.CTkButton(
            action, text="Verify all", command=self._verify, state="disabled",
            height=40, width=160, fg_color=ACCENT, hover_color=ACCENT_HOV,
            text_color="#fff", font=F(size=13, weight="bold"), corner_radius=8,
        )
        self.verify_btn.grid(row=0, column=1, padx=20, pady=16, sticky="e")

        # Initial state
        self._show_empty()

    # ---- helpers ----

    def _pick(self):
        initial = (self.app.settings.last_backup_dir or
                   self.app.settings.last_output_dir)
        p = filedialog.askdirectory(title="Choose backup directory",
                                    initialdir=initial or str(Path.home()))
        if p:
            self.dir_entry.delete(0, "end"); self.dir_entry.insert(0, p)
            self.app.settings.last_backup_dir = p
            self.app.settings.save()
            self._load_manifest()

    def _show_recent(self):
        recent = self.app.settings.recent_backups
        if not recent:
            self.app.toast("No recent backups yet.", "info")
            return
        menu = tk.Menu(self, tearoff=False)
        for path in recent:
            menu.add_command(label=path,
                             command=lambda p=path: self._pick_recent(p))
        try:
            menu.tk_popup(self.recent_btn.winfo_rootx(),
                          self.recent_btn.winfo_rooty()
                              + self.recent_btn.winfo_height())
        finally:
            menu.grab_release()

    def _pick_recent(self, path: str):
        self.dir_entry.delete(0, "end"); self.dir_entry.insert(0, path)
        self._load_manifest()

    def on_device_changed(self): pass
    def on_show(self): pass

    def _short_hash(self, h: Optional[str]) -> str:
        if not h:
            return "—"
        return f"{h[:8]}…{h[-6:]}"

    def _show_empty(self) -> None:
        self.summary_card.grid_remove()
        self.table_scroll.grid_remove()
        self.empty_state.grid(row=2, column=0, sticky="nsew", padx=20, pady=20)
        self.table_count.configure(text="")
        self.verify_btn.configure(state="disabled")
        self.action_hint.configure(text="Pick a backup to begin.",
                                   text_color=TEXT_DIM)
        self._manifest = None
        self._backup_dir = None

    def _load_manifest(self) -> None:
        d = self.dir_entry.get().strip()
        if not d:
            self._show_empty()
            return
        backup_dir = Path(d)
        manifest_path = backup_dir / MANIFEST_FILENAME
        if not manifest_path.exists():
            self._show_empty()
            self.action_hint.configure(
                text=f"No manifest.json found in {backup_dir.name}",
                text_color=DANGER,
            )
            return

        try:
            manifest = Manifest.read(manifest_path)
        except Exception as e:
            self._show_empty()
            self.action_hint.configure(
                text=f"Could not parse manifest: {e}", text_color=DANGER)
            return

        self._manifest = manifest
        self._backup_dir = backup_dir
        self._render_manifest(manifest)

    def _render_manifest(self, manifest: Manifest) -> None:
        self.empty_state.grid_remove()
        self.summary_card.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        self.table_scroll.grid()

        dev = manifest.device or {}
        model = dev.get("model") or "unknown"
        chip = dev.get("chipset") or "—"
        if dev.get("is_mediatek"):
            chip += " (MTK)"
        created = (manifest.created_at or "").split("T")[0] or "—"
        self._summary_labels["device"].configure(text=model)
        self._summary_labels["chipset"].configure(text=chip)
        self._summary_labels["created"].configure(text=created)
        self._summary_labels["partitions"].configure(
            text=str(len(manifest.partitions)))

        # Rebuild rows
        for w in self.table_scroll.winfo_children():
            w.destroy()
        self._rows.clear()

        for i, entry in enumerate(manifest.partitions):
            row = ctk.CTkFrame(self.table_scroll, fg_color="transparent",
                               height=30)
            row.grid(row=i, column=0, sticky="ew", padx=4, pady=1)
            row.grid_columnconfigure(1, weight=1)

            status_lbl = ctk.CTkLabel(
                row, text="?", font=F(size=13, weight="bold"),
                text_color=TEXT_MUTED, width=28,
            )
            status_lbl.grid(row=0, column=0, padx=(8, 6), pady=4)

            name_lbl = ctk.CTkLabel(
                row, text=entry["name"], anchor="w",
                font=F(size=12), text_color=TEXT,
            )
            name_lbl.grid(row=0, column=1, sticky="w")

            ctk.CTkLabel(
                row, text=human_size(entry["size_bytes"]),
                anchor="e", font=F_MONO(size=11),
                text_color=TEXT_MUTED, width=80,
            ).grid(row=0, column=2, sticky="e", padx=(0, 6))

            ctk.CTkLabel(
                row, text=self._short_hash(entry.get("sha256")),
                anchor="e", font=F_MONO(size=10),
                text_color=TEXT_MUTED, width=120,
            ).grid(row=0, column=3, sticky="e", padx=(0, 10))

            self._rows[entry["name"]] = {
                "frame": row,
                "status": status_lbl,
                "name": name_lbl,
            }

        self.table_count.configure(text=f"{len(manifest.partitions)} entries")
        self.verify_btn.configure(state="normal")
        self.action_hint.configure(
            text=f"Loaded {self._backup_dir.name} — click Verify to re-hash.",
            text_color=TEXT_DIM,
        )

    def _set_row_status(self, name: str, kind: str, tooltip: str = "") -> None:
        r = self._rows.get(name)
        if not r:
            return
        symbol, color = {
            "ok":     ("✓", SUCCESS),
            "fail":   ("✕", DANGER),
            "miss":   ("—", DANGER),
            "busy":   ("…", ACCENT_GLOW),
            "pending": ("?", TEXT_MUTED),
        }.get(kind, ("?", TEXT_MUTED))
        r["status"].configure(text=symbol, text_color=color)

    def _verify(self) -> None:
        if not self._manifest or not self._backup_dir:
            self.app.toast("Load a backup directory first.", "warn")
            return

        for name in self._rows:
            self._set_row_status(name, "pending")

        self.verify_btn.configure(state="disabled", text="Verifying…")
        self.app.status("Verifying…", "busy")

        manifest = self._manifest
        backup_dir = self._backup_dir
        signal = self.app.signal

        def work():
            try:
                ok_all = True
                for entry in manifest.partitions:
                    name = entry["name"]
                    signal.emit({"event": "_verify_row",
                                 "name": name, "kind": "busy"})
                    fp = backup_dir / entry["file"]
                    if not fp.exists():
                        signal.emit({"event": "_verify_row",
                                     "name": name, "kind": "miss"})
                        ok_all = False
                        continue
                    if fp.stat().st_size != entry["size_bytes"]:
                        signal.emit({"event": "_verify_row",
                                     "name": name, "kind": "fail"})
                        ok_all = False
                        continue
                    actual = sha256_file(fp)
                    if actual != entry["sha256"]:
                        signal.emit({"event": "_verify_row",
                                     "name": name, "kind": "fail"})
                        ok_all = False
                        continue
                    signal.emit({"event": "_verify_row",
                                 "name": name, "kind": "ok"})
                signal.emit({"event": "_verify_done", "ok": ok_all})
            except Exception as e:
                signal.emit({"event": "error", "error": str(e),
                             "trace": traceback.format_exc()})
            finally:
                signal.emit({"event": "_verify_finished"})

        _run_thread(work)

    def handle_event(self, ev):
        kind = ev.get("event", "")
        if kind == "_verify_row":
            self._set_row_status(ev["name"], ev["kind"])
        elif kind == "_verify_done":
            ok = ev["ok"]
            msg = "All hashes match." if ok else "Some hashes failed."
            self.action_hint.configure(
                text=msg, text_color=SUCCESS if ok else DANGER)
            self.app.toast("Verification OK." if ok else "Verification FAILED.",
                           "ok" if ok else "err")
            self.app.status(msg, "ok" if ok else "err")
            notify("ROM Extractor — Verify",
                   "Verification OK." if ok else "Verification FAILED.",
                   sound=not ok)
        elif kind == "_verify_finished":
            self.verify_btn.configure(state="normal", text="Verify all")


class SideloadView(ctk.CTkFrame):

    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)

        SectionHeader(
            self, icon="⇪", title="Sideload",
            subtitle="adb sideload an OTA-style ZIP into recovery.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 18))

        card = Card(self)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            card, text="ZIP file", font=F(size=12, weight="bold"),
            text_color=TEXT_DIM, anchor="w",
        ).grid(row=0, column=0, columnspan=3, padx=20, pady=(16, 6), sticky="w")
        self.zip_entry = ctk.CTkEntry(
            card, height=36, fg_color=BG_2, border_color=BORDER_2,
            text_color=TEXT, placeholder_text="path/to/ota.zip",
        )
        self.zip_entry.grid(row=1, column=0, columnspan=2,
                            padx=(20, 8), pady=(0, 16), sticky="ew")
        ctk.CTkButton(
            card, text="Browse", command=self._pick, height=36, width=90,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT,
            border_width=1, border_color=BORDER_2, font=F(size=12),
            corner_radius=8,
        ).grid(row=1, column=2, padx=(0, 20), pady=(0, 16))

        # Info banner
        banner = ctk.CTkFrame(
            self, fg_color=SURFACE, corner_radius=8, border_width=1,
            border_color=BORDER,
        )
        banner.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        banner.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            banner, text="ⓘ", font=F(size=18), text_color=INFO, width=32,
        ).grid(row=0, column=0, padx=(16, 8), pady=12)
        ctk.CTkLabel(
            banner,
            text="Boot the device into Recovery → Apply update from ADB. "
                 "Use the Power section in the sidebar to reboot.",
            font=F(size=12), text_color=TEXT_DIM, anchor="w", wraplength=720,
        ).grid(row=0, column=1, sticky="w", padx=(0, 16), pady=12)

        action = Card(self)
        action.grid(row=3, column=0, sticky="ew")
        action.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            action, text="", anchor="w", font=F(size=12), text_color=TEXT_DIM,
        ).grid(row=0, column=0, padx=20, pady=16, sticky="w")
        self.sl_btn = ctk.CTkButton(
            action, text="Sideload", command=self._sideload,
            height=40, width=140, fg_color=ACCENT, hover_color=ACCENT_HOV,
            text_color="#fff", font=F(size=13, weight="bold"), corner_radius=8,
        )
        self.sl_btn.grid(row=0, column=1, padx=20, pady=16, sticky="e")

        out_card = Card(self)
        out_card.grid(row=4, column=0, sticky="nsew", pady=(14, 0))
        out_card.grid_columnconfigure(0, weight=1)
        out_card.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(4, weight=1)
        ctk.CTkLabel(
            out_card, text="Output", font=F(size=12, weight="bold"),
            text_color=TEXT_DIM, anchor="w",
        ).grid(row=0, column=0, padx=20, pady=(16, 6), sticky="w")
        self.output = ctk.CTkTextbox(
            out_card, fg_color=BG_2, text_color=TEXT_DIM,
            font=F_MONO(size=11), corner_radius=8,
        )
        self.output.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 16))

    def _pick(self):
        p = filedialog.askopenfilename(
            title="Choose ZIP",
            filetypes=[("ZIP files", "*.zip"), ("All files", "*.*")],
        )
        if p:
            self.zip_entry.delete(0, "end"); self.zip_entry.insert(0, p)

    def on_device_changed(self): pass
    def on_show(self): pass

    def _sideload(self):
        z = self.zip_entry.get().strip()
        if not z:
            self.app.toast("Choose a ZIP.", "warn")
            return
        zp = Path(z)
        if not zp.exists():
            self.app.toast("ZIP does not exist.", "err")
            return

        if not self.app.confirm_battery_ok("sideload"):
            return

        serial = self.app.current_device.serial if self.app.current_device else None
        self.output.delete("1.0", "end")
        self.output.insert("end",
            f"zip:      {zp}\n"
            f"size:     {human_size(zp.stat().st_size)}\n"
            f"sha256:   computing…\n"
            "streaming via adb sideload…\n"
        )
        self.sl_btn.configure(state="disabled", text="Sideloading…")
        self.app.status("Sideloading…", "busy")

        signal = self.app.signal

        def work():
            try:
                # Hash in background so the UI doesn't freeze on big ZIPs.
                sha = sha256_file(zp)
                signal.emit({"event": "_sideload_hash", "zip": str(zp), "sha": sha})
                flash_mod.sideload_zip(zp, serial=serial, dry_run=False,
                                      assume_yes=True)
                signal.emit({"event": "_sideload_done"})
            except Exception as e:
                signal.emit({"event": "error", "error": str(e),
                             "trace": traceback.format_exc()})
            finally:
                signal.emit({"event": "_sideload_finished"})

        _run_thread(work)

    def handle_event(self, ev):
        kind = ev.get("event", "")
        if kind == "_sideload_done":
            self.output.insert("end", "Sideload complete.\n")
            self.app.toast("Sideload complete.", "ok")
            self.app.status("Sideload complete.", "ok")
            notify("ROM Extractor — Sideload",
                   "Sideload complete.", sound=True)
        elif kind == "_sideload_finished":
            self.sl_btn.configure(state="normal", text="Sideload")
        elif kind == "_sideload_hash":
            # Replace the "computing…" line with the actual hash.
            text = self.output.get("1.0", "end")
            text = text.replace("sha256:   computing…", f"sha256:   {ev['sha']}")
            self.output.delete("1.0", "end")
            self.output.insert("end", text)


class AppsView(ctk.CTkFrame):
    """List installed apps on the device and pull their APKs to disk."""

    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        self._packages: list[apps_mod.AppPackage] = []
        self._rows: dict[str, dict] = {}      # package -> {frame, var}
        self._loading = False

        SectionHeader(
            self, icon="◎", title="Apps",
            subtitle="List installed packages and pull their APKs to your Mac.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 18))

        # Controls
        ctrl = Card(self)
        ctrl.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        ctrl.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            ctrl, text="Output directory", font=F(size=12, weight="bold"),
            text_color=TEXT_DIM, anchor="w",
        ).grid(row=0, column=0, columnspan=3, padx=20, pady=(16, 6), sticky="w")
        self.out_entry = ctk.CTkEntry(
            ctrl, height=36, fg_color=BG_2, border_color=BORDER_2,
            text_color=TEXT, placeholder_text="~/apks",
        )
        self.out_entry.grid(row=1, column=0, columnspan=2,
                            padx=(20, 8), pady=(0, 12), sticky="ew")
        ctk.CTkButton(
            ctrl, text="Browse", command=self._pick_out, height=36, width=90,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT,
            border_width=1, border_color=BORDER_2, font=F(size=12),
            corner_radius=8,
        ).grid(row=1, column=2, padx=(0, 20), pady=(0, 12))

        self.include_system = tk.BooleanVar(value=False)
        ctk.CTkSwitch(
            ctrl, text="Include system apps (large, may need root to pull)",
            variable=self.include_system, progress_color=WARN,
            font=F(size=12), text_color=TEXT_DIM,
            command=self._reload,
        ).grid(row=2, column=0, columnspan=3, padx=20, pady=(0, 16), sticky="w")

        # Filter + toolbar
        list_card = Card(self)
        list_card.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        list_card.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(list_card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 8))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            head, text="Installed packages", font=F(size=12, weight="bold"),
            text_color=TEXT_DIM, anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.count_lbl = ctk.CTkLabel(
            head, text="", anchor="e", font=F(size=11), text_color=TEXT_MUTED,
        )
        self.count_lbl.grid(row=0, column=1, sticky="e")

        tool = ctk.CTkFrame(list_card, fg_color="transparent")
        tool.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 16))
        tool.grid_columnconfigure(4, weight=1)
        for i, (label, cmd) in enumerate((
                ("Reload", self._reload),
                ("All",    lambda: self._set_all(True)),
                ("None",   lambda: self._set_all(False)),
                ("Export", self._export_list),
        )):
            ctk.CTkButton(
                tool, text=label, command=cmd, height=26, width=68,
                fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT_DIM,
                border_width=1, border_color=BORDER_2, font=F(size=11),
                corner_radius=6,
            ).grid(row=0, column=i, padx=(0, 6))
        self.search_entry = ctk.CTkEntry(
            tool, height=26, fg_color=BG_2, border_color=BORDER_2,
            text_color=TEXT, font=F(size=11),
            placeholder_text="Search packages…",
        )
        self.search_entry.grid(row=0, column=5, sticky="e", padx=(12, 0))
        self.search_entry.bind("<KeyRelease>", lambda e: self._apply_filter())

        # List container
        self.list_container = Card(self)
        self.list_container.grid(row=3, column=0, sticky="nsew")
        self.list_container.grid_columnconfigure(0, weight=1)
        self.list_container.grid_rowconfigure(0, weight=1)

        self.scroll = ctk.CTkScrollableFrame(
            self.list_container, fg_color=BG_2, corner_radius=8,
        )
        self.scroll.grid(row=0, column=0, sticky="nsew", padx=14, pady=14)
        self.scroll.grid_columnconfigure(0, weight=1)

        self.empty_state = EmptyState(
            self.list_container, icon="📱",
            title="No device connected",
            body="Connect a phone via USB and click Reload to list installed apps. "
                 "Root isn't required for user-installed packages.",
            action=("Reload", self._reload),
        )

        # Action bar
        action = Card(self)
        action.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        action.grid_columnconfigure(0, weight=1)
        self.action_hint = ctk.CTkLabel(
            action, text="Connect a device to begin.",
            anchor="w", font=F(size=12), text_color=TEXT_DIM,
        )
        self.action_hint.grid(row=0, column=0, padx=20, pady=16, sticky="w")
        self.pull_btn = ctk.CTkButton(
            action, text="Pull selected APKs →", command=self._pull,
            state="disabled", height=40, width=200,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="#fff",
            font=F(size=13, weight="bold"), corner_radius=8,
        )
        self.pull_btn.grid(row=0, column=1, padx=20, pady=16, sticky="e")

        self._show_empty()

    # ---- behavior ----

    def _pick_out(self):
        p = filedialog.askdirectory(title="Choose output directory")
        if p:
            self.out_entry.delete(0, "end"); self.out_entry.insert(0, p)
            self._update_action_label()

    def _show_empty(self) -> None:
        self.scroll.grid_remove()
        self.empty_state.grid(row=0, column=0, sticky="nsew", padx=14, pady=14)
        self.count_lbl.configure(text="")

    def _show_list(self) -> None:
        self.empty_state.grid_remove()
        self.scroll.grid()

    def on_device_changed(self) -> None:
        d = self.app.current_device
        if d is None or d.state != "device":
            self._show_empty()
            self._packages = []
            self._rows.clear()
            title, body, hint = device_unavailable_copy(d, "list installed apps")
            self.empty_state.set_text(title, body)
            self.action_hint.configure(text=hint, text_color=TEXT_DIM)
            self.pull_btn.configure(state="disabled")
            return

    def on_show(self) -> None:
        # Refresh the empty-state copy for whatever mode the device is in.
        self.on_device_changed()
        if not self._packages and self.app.current_device \
                and self.app.current_device.state == "device":
            self._reload()

    def _reload(self) -> None:
        d = self.app.current_device
        if d is None or d.state != "device":
            self.app.toast("Device must be in adb mode.", "warn")
            return
        if self._loading:
            return
        self._loading = True
        self.action_hint.configure(text="Listing packages…", text_color=ACCENT_GLOW)
        serial = d.serial
        include_system = self.include_system.get()
        signal = self.app.signal

        def work():
            try:
                pkgs = apps_mod.list_packages(serial=serial,
                                             include_system=include_system)
                signal.emit({"event": "_apps_loaded", "packages": pkgs})
            except Exception as e:
                signal.emit({"event": "error", "error": str(e),
                             "trace": traceback.format_exc()})
            finally:
                signal.emit({"event": "_apps_load_finished"})

        _run_thread(work)

    def _render_packages(self, pkgs: list[apps_mod.AppPackage]) -> None:
        for w in self.scroll.winfo_children():
            w.destroy()
        self._rows.clear()
        self._packages = pkgs

        if not pkgs:
            self._show_empty()
            self.count_lbl.configure(text="0 packages")
            return

        self._show_list()
        for i, pkg in enumerate(pkgs):
            row = ctk.CTkFrame(self.scroll, fg_color="transparent",
                               corner_radius=6, height=30)
            row.grid(row=i, column=0, sticky="ew", padx=4, pady=1)
            row.grid_columnconfigure(1, weight=1)

            var = tk.BooleanVar(value=False)
            cb = ctk.CTkCheckBox(
                row, text="", variable=var, width=20,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER_2,
                command=self._update_action_label,
            )
            cb.grid(row=0, column=0, padx=(10, 8), pady=5)

            ctk.CTkLabel(
                row, text=pkg.package, anchor="w",
                font=F_MONO(size=11), text_color=TEXT,
            ).grid(row=0, column=1, sticky="w")

            if pkg.is_system:
                Tag(row, "SYSTEM", WARN).grid(row=0, column=2, padx=(0, 6))
            if len(pkg.paths) > 1:
                Tag(row, f"SPLIT×{len(pkg.paths)}", INFO).grid(
                    row=0, column=3, padx=(0, 10))

            self._rows[pkg.package] = {"frame": row, "var": var}

        self.count_lbl.configure(text=f"{len(pkgs)} packages")
        self._apply_filter()
        self._update_action_label()

    def _apply_filter(self):
        needle = self.search_entry.get().strip().lower()
        visible = 0
        for name, r in self._rows.items():
            if (not needle) or (needle in name.lower()):
                r["frame"].grid(); visible += 1
            else:
                r["frame"].grid_remove()
        if needle:
            self.count_lbl.configure(
                text=f"{visible} of {len(self._rows)} match")
        else:
            self.count_lbl.configure(text=f"{len(self._rows)} packages")

    def _set_all(self, val: bool):
        for r in self._rows.values():
            r["var"].set(val)
        self._update_action_label()

    def _export_list(self) -> None:
        """Save the currently-loaded package list as JSON or CSV.

        Respects the search filter — exports only what's visible. JSON or CSV
        is chosen by file extension. Honest about what we have: each row is
        (package, paths, is_system). No app *name* — pm list doesn't give us
        the human-readable label without a `dumpsys package` call per app."""
        if not self._packages:
            self.app.toast("Reload first — no packages to export.", "warn")
            return
        # Apply search filter so the export matches what the user sees.
        needle = self.search_entry.get().strip().lower()
        rows = [p for p in self._packages
                if not needle or needle in p.package.lower()]
        if not rows:
            self.app.toast("Filter excludes everything — nothing to export.", "warn")
            return

        default = f"apps-{int(time.time())}.json"
        path = filedialog.asksaveasfilename(
            title="Export package list",
            defaultextension=".json",
            initialfile=default,
            filetypes=[("JSON", "*.json"), ("CSV", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            p = Path(path)
            if p.suffix.lower() == ".csv":
                import csv
                with p.open("w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["package", "paths", "is_system"])
                    for pkg in rows:
                        w.writerow([pkg.package, ";".join(pkg.paths),
                                    "yes" if pkg.is_system else "no"])
            else:
                payload = {
                    "device": (
                        {"serial": self.app.current_device.serial,
                         "model":  self.app.current_device.model,
                         "fingerprint": self.app.current_device.fingerprint}
                        if self.app.current_device else None
                    ),
                    "exported_at": _isoformat_now(),
                    "count": len(rows),
                    "packages": [
                        {"package": pkg.package, "paths": pkg.paths,
                         "is_system": pkg.is_system}
                        for pkg in rows
                    ],
                }
                p.write_text(json.dumps(payload, indent=2))
            self.app.toast(f"Saved {len(rows)} packages to {p.name}.", "ok")
        except Exception as e:
            self.app.toast(f"Export failed: {e}", "err")

    def _update_action_label(self):
        sel = [n for n, r in self._rows.items() if r["var"].get()]
        if not sel:
            self.action_hint.configure(
                text="Select packages to enable pull.", text_color=TEXT_MUTED)
            self.pull_btn.configure(state="disabled")
            return
        out = self.out_entry.get().strip()
        self.action_hint.configure(
            text=f"{len(sel)} package{'s' if len(sel)!=1 else ''} selected.",
            text_color=TEXT,
        )
        self.pull_btn.configure(state="normal" if out else "disabled")

    def _pull(self) -> None:
        sel = [n for n, r in self._rows.items() if r["var"].get()]
        if not sel:
            return
        out = self.out_entry.get().strip()
        if not out:
            self.app.toast("Choose an output directory first.", "warn")
            return
        out_dir = Path(out)
        serial = self.app.current_device.serial if self.app.current_device else None

        self.pull_btn.configure(state="disabled", text="Pulling…")
        self.app.status(f"Pulling {len(sel)} APKs…", "busy")
        signal = self.app.signal

        def work():
            pulled_total = 0
            failed: list[str] = []
            try:
                for pkg in sel:
                    try:
                        pulled = apps_mod.pull_apk(pkg, out_dir, serial=serial)
                        signal.emit({"event": "_apk_pulled",
                                     "package": pkg, "files": len(pulled)})
                        pulled_total += len(pulled)
                    except Exception as e:
                        failed.append(pkg)
                        signal.emit({"event": "_apk_failed",
                                     "package": pkg, "error": str(e)})
                signal.emit({"event": "_apk_done",
                             "pulled": pulled_total, "failed": failed,
                             "out": str(out_dir)})
            except Exception as e:
                signal.emit({"event": "error", "error": str(e),
                             "trace": traceback.format_exc()})
            finally:
                signal.emit({"event": "_apk_finished"})

        _run_thread(work)

    def handle_event(self, ev: dict) -> None:
        kind = ev.get("event", "")
        if kind == "_apps_loaded":
            self._render_packages(ev["packages"])
        elif kind == "_apps_load_finished":
            self._loading = False
            if self._packages:
                self.action_hint.configure(
                    text=f"{len(self._packages)} package(s) listed. "
                         "Select and pull.",
                    text_color=TEXT_DIM,
                )
        elif kind == "_apk_pulled":
            self.app.log(f"[apps] pulled {ev['package']} ({ev['files']} file(s))")
        elif kind == "_apk_failed":
            self.app.log(f"[apps] FAIL  {ev['package']}: {ev['error']}")
        elif kind == "_apk_done":
            n = ev["pulled"]
            fail = ev["failed"]
            if fail:
                self.app.toast(
                    f"Pulled {n} files; {len(fail)} packages failed.", "warn")
                self.app.status(
                    f"Pulled {n} APK files. {len(fail)} failures.", "warn")
            else:
                self.app.toast(f"Pulled {n} APK file(s) to {ev['out']}", "ok")
                self.app.status(f"Pulled {n} APK file(s).", "ok")
        elif kind == "_apk_finished":
            self.pull_btn.configure(state="normal",
                                    text="Pull selected APKs →")


class LogsView(ctk.CTkFrame):

    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        SectionHeader(
            self, icon="⎘", title="Logs",
            subtitle="Every event from this session.",
            actions=[("Clear", self._clear), ("Copy", self._copy)],
        ).grid(row=0, column=0, sticky="ew", pady=(0, 18))

        card = Card(self)
        card.grid(row=1, column=0, sticky="nsew")
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=1)

        self.box = ctk.CTkTextbox(
            card, fg_color=BG_2, text_color=TEXT_DIM,
            font=F_MONO(size=11), corner_radius=8,
        )
        self.box.grid(row=0, column=0, sticky="nsew", padx=20, pady=20)

    def append(self, msg: str) -> None:
        self.box.insert("end", msg + "\n")
        self.box.see("end")

    def _clear(self): self.box.delete("1.0", "end")
    def _copy(self):
        text = self.box.get("1.0", "end")
        self.app.clipboard_clear()
        self.app.clipboard_append(text)
        self.app.toast("Logs copied to clipboard.", "ok")

    def on_device_changed(self): pass
    def on_show(self): pass


class LogcatView(ctk.CTkFrame):
    """Streams `adb logcat -v threadtime` from the connected device."""

    LEVEL_COLORS = {
        "V": TEXT_MUTED, "D": TEXT_DIM, "I": INFO,
        "W": WARN, "E": DANGER, "F": DANGER,
    }

    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lines_seen = 0

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        SectionHeader(
            self, icon="≡", title="Logcat",
            subtitle="Live stream of the device's system log.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 18))

        # Controls
        controls = Card(self)
        controls.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        controls.grid_columnconfigure(2, weight=1)

        self.start_btn = ctk.CTkButton(
            controls, text="▶  Start", command=self._start,
            height=34, width=110,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="#fff",
            font=F(size=12, weight="bold"), corner_radius=8,
        )
        self.start_btn.grid(row=0, column=0, padx=(16, 8), pady=12)

        self.stop_btn = ctk.CTkButton(
            controls, text="■  Stop", command=self._stop,
            height=34, width=90,
            fg_color=SURFACE_2, hover_color=DANGER_DIM, text_color=DANGER,
            border_width=1, border_color=DANGER_DIM,
            font=F(size=12), corner_radius=8, state="disabled",
        )
        self.stop_btn.grid(row=0, column=1, padx=(0, 8), pady=12)

        self.filter_entry = ctk.CTkEntry(
            controls, height=34, fg_color=BG_2, border_color=BORDER_2,
            text_color=TEXT, font=F(size=12),
            placeholder_text="Filter lines (case-insensitive substring)",
        )
        self.filter_entry.grid(row=0, column=2, padx=(0, 8), pady=12, sticky="ew")

        self.level_combo = ctk.CTkComboBox(
            controls, values=["All", "V", "D", "I", "W", "E", "F"],
            height=34, width=80,
            fg_color=BG_2, border_color=BORDER_2,
            button_color=ACCENT, button_hover_color=ACCENT_HOV,
            text_color=TEXT, font=F(size=12),
        )
        self.level_combo.set("All")
        self.level_combo.grid(row=0, column=3, padx=(0, 8), pady=12)

        ctk.CTkButton(
            controls, text="Clear", command=self._clear,
            height=34, width=80,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT_DIM,
            border_width=1, border_color=BORDER_2,
            font=F(size=12), corner_radius=8,
        ).grid(row=0, column=4, padx=(0, 8), pady=12)

        ctk.CTkButton(
            controls, text="Save…", command=self._save,
            height=34, width=80,
            fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT_DIM,
            border_width=1, border_color=BORDER_2,
            font=F(size=12), corner_radius=8,
        ).grid(row=0, column=5, padx=(0, 16), pady=12)

        # Body
        body = Card(self)
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self.box = ctk.CTkTextbox(
            body, fg_color=BG_2, text_color=TEXT_DIM,
            font=F_MONO(size=11), corner_radius=8, wrap="none",
        )
        self.box.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)
        # Color tags for level prefixes.
        for level, color in self.LEVEL_COLORS.items():
            self.box.tag_config(f"lvl_{level}", foreground=color)

        # Footer status line.
        self.stat = ctk.CTkLabel(
            self, text="Idle.", anchor="w",
            font=F(size=11), text_color=TEXT_MUTED,
        )
        self.stat.grid(row=3, column=0, sticky="ew", pady=(8, 0))

    def on_device_changed(self) -> None:
        if self._proc and self.app.current_device is None:
            self._stop()

    def on_show(self) -> None:
        pass

    def _start(self) -> None:
        if self._proc is not None:
            return
        d = self.app.current_device
        if not d or d.state != "device":
            self.app.toast("Connect a device in adb mode first.", "warn")
            return

        # Spawn `adb logcat -v threadtime` and stream stdout into the textbox.
        try:
            from .adb import _adb_binary
            args = [_adb_binary(), "-s", d.serial, "logcat", "-v", "threadtime"]
            self._proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except Exception as e:
            self.app.toast(f"Could not start logcat: {e}", "err")
            return

        self._stop_event.clear()
        self._lines_seen = 0
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.stat.configure(text=f"Streaming logcat from {d.serial}…",
                            text_color=ACCENT_GLOW)
        self.app.status("Logcat running.", "busy")

        signal = self.app.signal
        proc = self._proc
        stop_event = self._stop_event

        def reader():
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    if stop_event.is_set():
                        break
                    signal.emit({"event": "_logcat_line", "line": line.rstrip()})
            except Exception as e:
                signal.emit({"event": "_logcat_line",
                             "line": f"[reader error: {e}]"})
            finally:
                signal.emit({"event": "_logcat_done"})

        self._reader = threading.Thread(target=reader, daemon=True)
        self._reader.start()

    def _stop(self) -> None:
        self._stop_event.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._proc = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.stat.configure(
            text=f"Stopped — {self._lines_seen} lines captured.",
            text_color=TEXT_MUTED,
        )
        self.app.status("Logcat stopped.", "idle")

    def _clear(self) -> None:
        self.box.delete("1.0", "end")
        self._lines_seen = 0
        self.stat.configure(text="Cleared.")

    def _save(self) -> None:
        text = self.box.get("1.0", "end").rstrip()
        if not text:
            self.app.toast("Nothing to save yet.", "info")
            return
        p = filedialog.asksaveasfilename(
            title="Save logcat",
            defaultextension=".log",
            initialfile=f"logcat-{int(time.time())}.log",
            filetypes=[("Log files", "*.log"), ("All files", "*.*")],
        )
        if not p:
            return
        try:
            Path(p).write_text(text)
            self.app.toast(f"Saved {Path(p).name}.", "ok")
        except Exception as e:
            self.app.toast(f"Save failed: {e}", "err")

    def _passes_filter(self, line: str) -> bool:
        needle = self.filter_entry.get().strip().lower()
        if needle and needle not in line.lower():
            return False
        level = self.level_combo.get()
        if level != "All":
            # `threadtime`: "MM-DD HH:MM:SS.SSS PID TID L tag: msg"
            parts = line.split()
            if len(parts) >= 5 and parts[4] != level:
                return False
        return True

    def handle_event(self, ev: dict) -> None:
        kind = ev.get("event", "")
        if kind == "_logcat_line":
            line = ev["line"]
            if not self._passes_filter(line):
                return
            # Detect level for colorizing.
            parts = line.split()
            level = parts[4] if len(parts) >= 5 else None
            tag = f"lvl_{level}" if level in self.LEVEL_COLORS else None
            if tag:
                self.box.insert("end", line + "\n", (tag,))
            else:
                self.box.insert("end", line + "\n")
            self._lines_seen += 1
            # Keep buffer manageable (last 8000 lines).
            line_count = int(self.box.index("end-1c").split(".")[0])
            if line_count > 8000:
                self.box.delete("1.0", "2000.0")
            self.box.see("end")
            self.stat.configure(text=f"Streaming — {self._lines_seen} lines")
        elif kind == "_logcat_done":
            self._stop()


class PropertiesView(ctk.CTkFrame):
    """Searchable browser of the device's `getprop` output."""

    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        SectionHeader(
            self, icon="ⓘ", title="Properties",
            subtitle="All Android system properties from the connected device.",
            actions=[("Copy JSON", self._copy_json),
                     ("Export…",  self._export),
                     ("Refresh",   self._refresh)],
        ).grid(row=0, column=0, sticky="ew", pady=(0, 18))

        # Search box
        search_row = ctk.CTkFrame(self, fg_color="transparent")
        search_row.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        search_row.grid_columnconfigure(0, weight=1)
        self.search = ctk.CTkEntry(
            search_row, height=34, fg_color=SURFACE, border_color=BORDER_2,
            text_color=TEXT, font=F(size=12),
            placeholder_text="Filter properties (e.g. ro.product, hardware, build)",
        )
        self.search.grid(row=0, column=0, sticky="ew")
        self.search.bind("<KeyRelease>", lambda e: self._render())
        self.count_label = ctk.CTkLabel(
            search_row, text="", font=F(size=11), text_color=TEXT_MUTED,
        )
        self.count_label.grid(row=0, column=1, sticky="e", padx=(12, 0))

        # Body
        self.card = Card(self)
        self.card.grid(row=2, column=0, sticky="nsew")
        self.card.grid_columnconfigure(0, weight=1)
        self.card.grid_rowconfigure(0, weight=1)

        self.scroll = ctk.CTkScrollableFrame(
            self.card, fg_color=BG_2, corner_radius=8,
        )
        self.scroll.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)
        self.scroll.grid_columnconfigure(1, weight=1)

        self.empty = EmptyState(
            self.card, icon="📡",
            title="No device connected",
            body="Connect a phone in adb mode to browse its system properties.",
            action=("Refresh", self.app.refresh_devices),
        )

    def _refresh(self):
        self.app.refresh_devices()

    def _copy_json(self):
        d = self.app.current_device
        if not d:
            self.app.toast("No device connected.", "warn")
            return
        self.app.clipboard_clear()
        self.app.clipboard_append(json.dumps(d.properties, indent=2, sort_keys=True))
        self.app.toast("Properties JSON copied.", "ok")

    def _export(self):
        """Save the property dump as JSON or as raw `getprop` text."""
        d = self.app.current_device
        if not d or not d.properties:
            self.app.toast("No properties loaded — connect and refresh.", "warn")
            return
        default = f"props-{d.serial}-{int(time.time())}.json"
        path = filedialog.asksaveasfilename(
            title="Export properties",
            defaultextension=".json",
            initialfile=default,
            filetypes=[("JSON", "*.json"), ("Text (getprop)", "*.txt"),
                       ("All files", "*.*")],
        )
        if not path:
            return
        try:
            p = Path(path)
            if p.suffix.lower() == ".txt":
                lines = [f"[{k}]: [{v}]" for k, v in sorted(d.properties.items())]
                p.write_text("\n".join(lines) + "\n")
            else:
                payload = {
                    "device": {"serial": d.serial, "model": d.model,
                               "fingerprint": d.fingerprint},
                    "exported_at": _isoformat_now(),
                    "properties": dict(sorted(d.properties.items())),
                }
                p.write_text(json.dumps(payload, indent=2))
            self.app.toast(f"Saved {len(d.properties)} props to {p.name}.", "ok")
        except Exception as e:
            self.app.toast(f"Export failed: {e}", "err")

    def on_device_changed(self) -> None:
        self._render()

    def on_show(self) -> None:
        self._render()

    def _render(self) -> None:
        for w in self.scroll.winfo_children():
            w.destroy()
        d = self.app.current_device
        props = d.properties if d else {}

        if not props:
            self.scroll.grid_remove()
            self.empty.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)
            self.count_label.configure(text="")
            title, body, _ = device_unavailable_copy(d, "browse system properties")
            self.empty.set_text(title, body)
            return

        self.empty.grid_remove()
        self.scroll.grid()

        needle = self.search.get().strip().lower()
        shown = 0
        for i, key in enumerate(sorted(props)):
            val = props[key]
            if needle and needle not in key.lower() and needle not in val.lower():
                continue
            row = ctk.CTkFrame(self.scroll, fg_color="transparent")
            row.grid(row=i, column=0, sticky="ew", pady=1)
            row.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(
                row, text=key, anchor="w", font=F_MONO(size=11),
                text_color=ACCENT_GLOW, width=260,
            ).grid(row=0, column=0, sticky="w", padx=(6, 12))
            ctk.CTkLabel(
                row, text=val or "(empty)", anchor="w", font=F_MONO(size=11),
                text_color=TEXT_DIM, wraplength=600, justify="left",
            ).grid(row=0, column=1, sticky="w")
            shown += 1

        if needle:
            self.count_label.configure(
                text=f"{shown} of {len(props)} match")
        else:
            self.count_label.configure(text=f"{len(props)} properties")

    def handle_event(self, ev: dict) -> None:
        pass


def _fmt_eta(seconds: float) -> str:
    """Format seconds as mm:ss or h:mm:ss."""
    if seconds < 0 or seconds == float("inf"):
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ----------------------------------------------------------------------------
# entrypoint
# ----------------------------------------------------------------------------

def run() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    run()
