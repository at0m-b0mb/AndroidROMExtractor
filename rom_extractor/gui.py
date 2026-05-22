"""customtkinter GUI for android-rom-extractor.

A polished dark-mode desktop app: sidebar navigation, status pill, empty
states, toast notifications, confirm dialogs, and live per-operation progress.
"""
from __future__ import annotations

import json
import logging
import platform
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog
from typing import Callable, Optional

import customtkinter as ctk

from . import __version__, adb as adb_mod, backup as backup_mod
from . import device as device_mod, flash as flash_mod
from . import partitions as part_mod
from . import settings as settings_mod
from . import verify as verify_mod
from .device import Device
from .manifest import Manifest, MANIFEST_FILENAME
from .partitions import (DEFAULT_BACKUP_SET, MTK_CRITICAL, Partition,
                         is_dangerous)
from .utils import human_size, sha256_file

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
        ctk.CTkLabel(
            wrap, text=title, font=F(size=18, weight="bold"), text_color=TEXT,
        ).pack(pady=(0, 6))
        ctk.CTkLabel(
            wrap, text=body, font=F(size=13), text_color=TEXT_DIM,
            justify="center", wraplength=420,
        ).pack(pady=(0, 16))

        if action:
            label, cmd = action
            ctk.CTkButton(
                wrap, text=label, command=cmd, height=36, width=140,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
                font=F(size=13, weight="bold"), corner_radius=8,
            ).pack()


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


# ----------------------------------------------------------------------------
# Main app
# ----------------------------------------------------------------------------

NAV_ITEMS = [
    ("backup",     "⤓", "Backup"),
    ("flash",      "⚡", "Flash"),
    ("restore",    "↩", "Restore"),
    ("verify",     "✓", "Verify"),
    ("sideload",   "⇪", "Sideload"),
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
        self.all_devices: list[Device] = []
        self.partitions: list[Partition] = []
        self.signal = WorkerSignal()
        self.nav_buttons: dict[str, NavButton] = {}
        self.views: dict[str, ctk.CTkFrame] = {}
        self.current_view = "backup"
        self._toasts: list[ctk.CTkFrame] = []

        self._build_layout()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.show_view("backup")
        self._poll_signal()
        self.refresh_devices()

    def _on_close(self) -> None:
        try:
            self.settings.window_geometry = self.geometry()
            self.settings.save()
        finally:
            self.destroy()

    def _bind_shortcuts(self) -> None:
        # Cmd/Ctrl+1..8 switch nav, Cmd/Ctrl+R refresh.
        keys = ["1", "2", "3", "4", "5", "6", "7", "8"]
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
                                    padx=14, pady=(0, 10))

        ctk.CTkButton(
            dev_card, text=f"↻  Refresh  {MOD_LABEL}R",
            command=self.refresh_devices,
            height=30, fg_color=SURFACE_2, hover_color=SURFACE_3,
            text_color=TEXT, border_width=1, border_color=BORDER_2,
            font=F(size=11), corner_radius=8,
        ).grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 12))

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
            footer, text="About", command=self._show_about,
            height=26, width=70,
            fg_color="transparent", hover_color=SURFACE_2,
            text_color=TEXT_MUTED, font=F(size=11),
            border_width=0, corner_radius=6,
        ).grid(row=0, column=1, sticky="se", padx=(8, 0))

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

    def _render_device(self, devs: list[Device]) -> None:
        self.all_devices = devs
        if not devs:
            self.current_device = None
            self.status_pill.set("idle")
            self.status_pill.set_text("Disconnected")
            self.device_info_label.configure(
                text="No device connected.\nPlug in a phone via USB.")
            self.device_selector.grid_remove()
            self.status("No devices attached.", "idle")
            self.partitions = []
            self._refresh_views()
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
            self.device_selector.set(labels[0])
            self.device_selector.grid()
        else:
            self.device_selector.grid_remove()

        self._select_device(devs_sorted[0])

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
        target_arg = None if target == "system" else target
        try:
            adb_mod.reboot(target_arg, serial=self.current_device.serial)
            self.toast(f"Rebooting to {target}…", "info")
            self.log(f"[reboot] {self.current_device.serial} -> {target}")
            self.status(f"Reboot to {target} issued.", "ok")
        except Exception as e:
            self.toast(f"Reboot failed: {e}", "err")
            self.log(f"[reboot] failed: {e}")

    def _show_about(self) -> None:
        AboutDialog(self)

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
            self._render_device(ev["devices"])
        elif kind == "partitions":
            self.partitions = ev["partitions"]
            self.status(f"{len(ev['partitions'])} partitions enumerated.", "ok")
            self._refresh_views()
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
        ).grid(row=2, column=0, columnspan=3, padx=20, pady=(0, 16), sticky="w")

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
        tool.grid_columnconfigure(3, weight=1)
        for i, (label, cmd) in enumerate((("Default", self._select_default),
                                          ("All",     lambda: self._set_all(True)),
                                          ("None",    lambda: self._set_all(False)))):
            ctk.CTkButton(
                tool, text=label, command=cmd, height=26, width=68,
                fg_color=SURFACE_2, hover_color=SURFACE_3, text_color=TEXT_DIM,
                border_width=1, border_color=BORDER_2, font=F(size=11),
                corner_radius=6,
            ).grid(row=0, column=i, padx=(0, 6))

        self.search_entry = ctk.CTkEntry(
            tool, height=26, fg_color=BG_2, border_color=BORDER_2,
            text_color=TEXT, font=F(size=11),
            placeholder_text="Search partitions…",
        )
        self.search_entry.grid(row=0, column=4, sticky="e", padx=(12, 0))
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
        import re
        from datetime import date
        name = d.model if d.model and d.model != "unknown" else d.serial
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-").lower() or "device"
        base = Path(self.app.settings.last_output_dir or
                    str(Path.home() / "arom-backups")).parent
        # If user had a previous backup root, reuse that directory.
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
            self.app.views["backup"].parts_count.configure(
                text=f"{visible} of {len(self.partition_rows)} match")
        else:
            self.app.views["backup"].parts_count.configure(
                text=f"{len(self.partition_rows)} found")

    def _cancel_running(self):
        if self._cancel_event:
            self._cancel_event.set()
            self.cancel_btn.configure(state="disabled", text="Cancelling…")
            self.app.status("Cancelling backup…", "warn")

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
            self.selection_label.configure(
                text="Connect a device to begin.", text_color=TEXT_MUTED)
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

        def work():
            try:
                entries = backup_mod.backup_partitions(
                    device=device, parts=sel, out_dir=out_dir,
                    verify_on_device=True, events=emit,
                    cancel=cancel_event,
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
            self.progress_title.configure(text="Backup cancelled")
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
                signal.emit({"event": "_flash_hash", "file": p,
                             "size": size, "sha": sha})
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
        elif kind == "_restore_finished":
            self.restore_btn.configure(state="normal", text="Restore backup")


class VerifyView(ctk.CTkFrame):

    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)

        SectionHeader(
            self, icon="✓", title="Verify",
            subtitle="Check that every file in a backup matches its SHA-256.",
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
        self.dir_entry.grid(row=1, column=0, padx=(20, 8), pady=(0, 16),
                            sticky="ew")
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

        action = Card(self)
        action.grid(row=2, column=0, sticky="ew")
        action.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            action, text="Re-hashes every image and compares to the manifest.",
            anchor="w", font=F(size=12), text_color=TEXT_DIM,
        ).grid(row=0, column=0, padx=20, pady=16, sticky="w")
        self.verify_btn = ctk.CTkButton(
            action, text="Verify", command=self._verify,
            height=40, width=140, fg_color=ACCENT, hover_color=ACCENT_HOV,
            text_color="#fff", font=F(size=13, weight="bold"), corner_radius=8,
        )
        self.verify_btn.grid(row=0, column=1, padx=20, pady=16, sticky="e")

        out_card = Card(self)
        out_card.grid(row=3, column=0, sticky="nsew", pady=(14, 0))
        out_card.grid_columnconfigure(0, weight=1)
        out_card.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(
            out_card, text="Results", font=F(size=12, weight="bold"),
            text_color=TEXT_DIM, anchor="w",
        ).grid(row=0, column=0, padx=20, pady=(16, 6), sticky="w")
        self.results = ctk.CTkTextbox(
            out_card, fg_color=BG_2, text_color=TEXT,
            font=F_MONO(size=12), corner_radius=8,
        )
        self.results.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 16))

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

    def _verify(self):
        d = self.dir_entry.get().strip()
        if not d:
            self.app.toast("Choose a backup directory.", "warn")
            return
        backup_dir = Path(d)
        self.results.delete("1.0", "end")
        self.verify_btn.configure(state="disabled", text="Verifying…")
        self.app.status("Verifying…", "busy")
        signal = self.app.signal

        def work():
            try:
                manifest_path = backup_dir / MANIFEST_FILENAME
                if not manifest_path.exists():
                    signal.emit({"event": "_verify_line",
                                 "text": f"No manifest at {manifest_path}",
                                 "ok": False})
                    signal.emit({"event": "_verify_done", "ok": False})
                    return
                manifest = Manifest.read(manifest_path)
                ok_all = True
                for entry in manifest.partitions:
                    fp = backup_dir / entry["file"]
                    if not fp.exists():
                        signal.emit({"event": "_verify_line",
                                     "text": f"  MISSING       {entry['file']}",
                                     "ok": False})
                        ok_all = False; continue
                    if fp.stat().st_size != entry["size_bytes"]:
                        signal.emit({"event": "_verify_line",
                                     "text": f"  SIZE MISMATCH {entry['file']}",
                                     "ok": False})
                        ok_all = False; continue
                    actual = sha256_file(fp)
                    if actual != entry["sha256"]:
                        signal.emit({"event": "_verify_line",
                                     "text": f"  HASH MISMATCH {entry['file']}",
                                     "ok": False})
                        ok_all = False; continue
                    signal.emit({"event": "_verify_line",
                                 "text": f"  OK            {entry['name']:<14} "
                                         f"{human_size(fp.stat().st_size)}",
                                 "ok": True})
                signal.emit({"event": "_verify_done", "ok": ok_all})
            except Exception as e:
                signal.emit({"event": "error", "error": str(e),
                             "trace": traceback.format_exc()})
            finally:
                signal.emit({"event": "_verify_finished"})

        _run_thread(work)

    def handle_event(self, ev):
        kind = ev.get("event", "")
        if kind == "_verify_line":
            self.results.insert("end", ev["text"] + "\n")
            self.results.see("end")
        elif kind == "_verify_done":
            ok = ev["ok"]
            msg = "Verification OK." if ok else "Verification FAILED."
            self.results.insert("end", "\n" + msg + "\n")
            self.app.toast(msg, "ok" if ok else "err")
            self.app.status(msg, "ok" if ok else "err")
        elif kind == "_verify_finished":
            self.verify_btn.configure(state="normal", text="Verify")


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
        elif kind == "_sideload_finished":
            self.sl_btn.configure(state="normal", text="Sideload")
        elif kind == "_sideload_hash":
            # Replace the "computing…" line with the actual hash.
            text = self.output.get("1.0", "end")
            text = text.replace("sha256:   computing…", f"sha256:   {ev['sha']}")
            self.output.delete("1.0", "end")
            self.output.insert("end", text)


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
