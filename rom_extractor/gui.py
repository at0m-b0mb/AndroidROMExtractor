"""customtkinter GUI for android-rom-extractor.

A modern dark-mode desktop app that wraps the same backup/flash/restore/verify
logic the CLI uses. Long-running operations run in worker threads and stream
events back to the UI through a thread-safe queue.
"""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog
from typing import Callable, Optional

import customtkinter as ctk

from . import __version__, adb as adb_mod, backup as backup_mod
from . import device as device_mod, flash as flash_mod, fastboot as fastboot_mod
from . import partitions as part_mod, verify as verify_mod
from .device import Device
from .manifest import Manifest, MANIFEST_FILENAME
from .partitions import (DEFAULT_BACKUP_SET, MTK_CRITICAL, Partition,
                         is_dangerous)
from .utils import human_size, sha256_file

log = logging.getLogger(__name__)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

ACCENT = "#3b82f6"     # blue-500
ACCENT_HOVER = "#2563eb"
DANGER = "#ef4444"
DANGER_HOVER = "#dc2626"
OK = "#22c55e"
WARN = "#f59e0b"
SURFACE = "#1f2937"
SURFACE_2 = "#111827"
MUTED = "#9ca3af"


# ============================================================================
# helpers
# ============================================================================

class WorkerSignal:
    """Thread-safe queue for worker -> UI messages.

    A worker pushes dicts; the UI polls via Tk's `after()` and dispatches them.
    """

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


# ============================================================================
# main window
# ============================================================================

class App(ctk.CTk):

    def __init__(self) -> None:
        super().__init__()
        self.title("Android ROM Extractor")
        self.geometry("1180x780")
        self.minsize(1000, 680)

        self.current_device: Optional[Device] = None
        self.partitions: list[Partition] = []
        self.signal = WorkerSignal()

        self._build_layout()
        self._poll_signal()
        self.refresh_devices()

    # ------- layout ----------------------------------------------------------

    def _build_layout(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main()
        self._build_statusbar()

    def _build_sidebar(self) -> None:
        side = ctk.CTkFrame(self, width=260, corner_radius=0, fg_color=SURFACE_2)
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)
        side.grid_rowconfigure(99, weight=1)

        title = ctk.CTkLabel(
            side, text="ROM Extractor", anchor="w",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        title.grid(row=0, column=0, padx=20, pady=(20, 4), sticky="ew")

        subtitle = ctk.CTkLabel(
            side, text=f"v{__version__}", anchor="w", text_color=MUTED,
            font=ctk.CTkFont(size=11),
        )
        subtitle.grid(row=1, column=0, padx=20, pady=(0, 16), sticky="ew")

        ctk.CTkLabel(side, text="Device", anchor="w",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=MUTED).grid(row=2, column=0, padx=20, pady=(8, 4), sticky="ew")

        self.device_dropdown = ctk.CTkOptionMenu(
            side, values=["(none)"], command=self._on_device_selected,
            fg_color=SURFACE, button_color=ACCENT, button_hover_color=ACCENT_HOVER,
            width=220,
        )
        self.device_dropdown.grid(row=3, column=0, padx=20, pady=4, sticky="ew")

        ctk.CTkButton(
            side, text="Refresh", command=self.refresh_devices,
            fg_color=SURFACE, hover_color=ACCENT, height=32,
        ).grid(row=4, column=0, padx=20, pady=(4, 12), sticky="ew")

        self.device_card = ctk.CTkFrame(side, fg_color=SURFACE, corner_radius=8)
        self.device_card.grid(row=5, column=0, padx=20, pady=8, sticky="ew")
        self.device_card.grid_columnconfigure(0, weight=1)

        self.device_info_label = ctk.CTkLabel(
            self.device_card, text="No device connected.",
            anchor="w", justify="left", wraplength=200,
            font=ctk.CTkFont(size=12),
        )
        self.device_info_label.grid(row=0, column=0, padx=12, pady=12, sticky="ew")

        ctk.CTkLabel(side, text="Reboot to…", anchor="w",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=MUTED).grid(row=6, column=0, padx=20, pady=(16, 4), sticky="ew")

        for i, target in enumerate(["system", "bootloader", "recovery", "sideload"]):
            ctk.CTkButton(
                side, text=target, height=28,
                fg_color=SURFACE, hover_color=ACCENT,
                command=lambda t=target: self.reboot(t),
            ).grid(row=7 + i, column=0, padx=20, pady=2, sticky="ew")

        warn = ctk.CTkLabel(
            side,
            text="⚠ Destructive operations cannot be undone.\n"
                 "Always have a verified backup before flashing.",
            anchor="w", justify="left", wraplength=220,
            font=ctk.CTkFont(size=10),
            text_color=WARN,
        )
        warn.grid(row=98, column=0, padx=20, pady=(24, 20), sticky="sew")

    def _build_main(self) -> None:
        main = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=0, pady=0)
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(
            main,
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOVER,
        )
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=16, pady=(16, 0))

        for name in ("Backup", "Flash", "Restore", "Verify", "Sideload", "Logs"):
            self.tabs.add(name)

        self._build_backup_tab(self.tabs.tab("Backup"))
        self._build_flash_tab(self.tabs.tab("Flash"))
        self._build_restore_tab(self.tabs.tab("Restore"))
        self._build_verify_tab(self.tabs.tab("Verify"))
        self._build_sideload_tab(self.tabs.tab("Sideload"))
        self._build_logs_tab(self.tabs.tab("Logs"))

    def _build_statusbar(self) -> None:
        bar = ctk.CTkFrame(self, height=42, corner_radius=0, fg_color=SURFACE_2)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        bar.grid_propagate(False)

        self.status_label = ctk.CTkLabel(
            bar, text="Ready.", anchor="w",
            font=ctk.CTkFont(size=12),
        )
        self.status_label.grid(row=0, column=0, padx=16, pady=8, sticky="w")

        self.global_progress = ctk.CTkProgressBar(
            bar, height=8, progress_color=ACCENT,
        )
        self.global_progress.grid(row=0, column=1, padx=16, pady=8, sticky="ew")
        self.global_progress.set(0)

    # ------- tabs ------------------------------------------------------------

    def _build_backup_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", pady=(12, 0))
        top.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top, text="Output:", width=80, anchor="w").grid(
            row=0, column=0, padx=(0, 8), sticky="w")
        self.backup_out = ctk.CTkEntry(top, placeholder_text="/path/to/backup-dir")
        self.backup_out.grid(row=0, column=1, sticky="ew")
        ctk.CTkButton(top, text="Browse", width=80,
                      fg_color=SURFACE, hover_color=ACCENT,
                      command=self._pick_backup_dir).grid(row=0, column=2, padx=(8, 0))

        mid = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=8)
        mid.grid(row=1, column=0, sticky="nsew", pady=12)
        mid.grid_columnconfigure(0, weight=1)
        mid.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(mid, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))

        ctk.CTkButton(toolbar, text="Select default set",
                      command=self._partitions_select_default,
                      fg_color=SURFACE_2, hover_color=ACCENT).pack(side="left", padx=(0, 8))
        ctk.CTkButton(toolbar, text="Select all",
                      command=lambda: self._partitions_set_all(True),
                      fg_color=SURFACE_2, hover_color=ACCENT).pack(side="left", padx=(0, 8))
        ctk.CTkButton(toolbar, text="Clear",
                      command=lambda: self._partitions_set_all(False),
                      fg_color=SURFACE_2, hover_color=ACCENT).pack(side="left", padx=(0, 8))

        self.partition_frame = ctk.CTkScrollableFrame(
            mid, fg_color="transparent", label_text="Partitions",
        )
        self.partition_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(4, 12))
        self.partition_frame.grid_columnconfigure(0, weight=1)
        self.partition_vars: dict[str, tk.BooleanVar] = {}

        bottom = ctk.CTkFrame(parent, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        bottom.grid_columnconfigure(0, weight=1)

        self.backup_status = ctk.CTkLabel(
            bottom, text="Select partitions and an output directory.",
            anchor="w", text_color=MUTED,
        )
        self.backup_status.grid(row=0, column=0, sticky="ew", padx=4)

        self.backup_button = ctk.CTkButton(
            bottom, text="Start backup", height=40, width=180,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.start_backup,
        )
        self.backup_button.grid(row=0, column=1, padx=4)

    def _build_flash_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)

        card = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=8)
        card.grid(row=0, column=0, sticky="ew", pady=12)
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text="Image:", width=100, anchor="w").grid(
            row=0, column=0, padx=12, pady=8, sticky="w")
        self.flash_image = ctk.CTkEntry(card, placeholder_text="path/to/boot.img")
        self.flash_image.grid(row=0, column=1, padx=4, pady=8, sticky="ew")
        ctk.CTkButton(card, text="Browse", width=80,
                      fg_color=SURFACE_2, hover_color=ACCENT,
                      command=self._pick_flash_image).grid(row=0, column=2, padx=12, pady=8)

        ctk.CTkLabel(card, text="Partition:", width=100, anchor="w").grid(
            row=1, column=0, padx=12, pady=8, sticky="w")
        self.flash_partition = ctk.CTkComboBox(
            card, values=["boot", "recovery", "system", "vendor", "dtbo", "vbmeta"],
            fg_color=SURFACE_2, button_color=ACCENT, button_hover_color=ACCENT_HOVER,
        )
        self.flash_partition.grid(row=1, column=1, padx=4, pady=8, sticky="ew")

        self.flash_boot_only = tk.BooleanVar(value=False)
        ctk.CTkSwitch(
            card, text="Boot only (don't write to flash) — for testing recoveries",
            variable=self.flash_boot_only, progress_color=ACCENT,
        ).grid(row=2, column=0, columnspan=3, padx=12, pady=8, sticky="w")

        self.flash_force = tk.BooleanVar(value=False)
        ctk.CTkSwitch(
            card, text="Allow dangerous partitions (preloader, lk, tee, …)",
            variable=self.flash_force, progress_color=DANGER,
        ).grid(row=3, column=0, columnspan=3, padx=12, pady=(8, 12), sticky="w")

        ctk.CTkLabel(
            parent,
            text="Device must be in fastboot mode. Use the sidebar to reboot.",
            anchor="w", text_color=MUTED,
        ).grid(row=1, column=0, sticky="ew", padx=4, pady=(4, 4))

        self.flash_button = ctk.CTkButton(
            parent, text="Flash image", height=40, width=180,
            fg_color=DANGER, hover_color=DANGER_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.start_flash,
        )
        self.flash_button.grid(row=2, column=0, sticky="e", pady=8)

        self.flash_output = ctk.CTkTextbox(parent, height=240, fg_color=SURFACE)
        self.flash_output.grid(row=3, column=0, sticky="nsew", pady=12)
        parent.grid_rowconfigure(3, weight=1)

    def _build_restore_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)

        card = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=8)
        card.grid(row=0, column=0, sticky="ew", pady=12)
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text="Backup dir:", width=100, anchor="w").grid(
            row=0, column=0, padx=12, pady=8, sticky="w")
        self.restore_dir = ctk.CTkEntry(card,
                                        placeholder_text="path/to/backup-2026-05-21")
        self.restore_dir.grid(row=0, column=1, padx=4, pady=8, sticky="ew")
        ctk.CTkButton(card, text="Browse", width=80,
                      fg_color=SURFACE_2, hover_color=ACCENT,
                      command=self._pick_restore_dir).grid(row=0, column=2, padx=12, pady=8)

        self.restore_include_userdata = tk.BooleanVar(value=False)
        ctk.CTkSwitch(
            card, text="Include userdata (huge, rarely wanted)",
            variable=self.restore_include_userdata, progress_color=WARN,
        ).grid(row=1, column=0, columnspan=3, padx=12, pady=8, sticky="w")

        self.restore_force = tk.BooleanVar(value=False)
        ctk.CTkSwitch(
            card, text="Allow dangerous partitions",
            variable=self.restore_force, progress_color=DANGER,
        ).grid(row=2, column=0, columnspan=3, padx=12, pady=(8, 12), sticky="w")

        ctk.CTkLabel(
            parent,
            text="Device must be in fastboot mode.",
            anchor="w", text_color=MUTED,
        ).grid(row=1, column=0, sticky="ew", padx=4)

        self.restore_button = ctk.CTkButton(
            parent, text="Restore from backup", height=40, width=200,
            fg_color=DANGER, hover_color=DANGER_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.start_restore,
        )
        self.restore_button.grid(row=2, column=0, sticky="e", pady=8)

        self.restore_output = ctk.CTkTextbox(parent, height=240, fg_color=SURFACE)
        self.restore_output.grid(row=3, column=0, sticky="nsew", pady=12)
        parent.grid_rowconfigure(3, weight=1)

    def _build_verify_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)

        card = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=8)
        card.grid(row=0, column=0, sticky="ew", pady=12)
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text="Backup dir:", width=100, anchor="w").grid(
            row=0, column=0, padx=12, pady=8, sticky="w")
        self.verify_dir = ctk.CTkEntry(card)
        self.verify_dir.grid(row=0, column=1, padx=4, pady=8, sticky="ew")
        ctk.CTkButton(card, text="Browse", width=80,
                      fg_color=SURFACE_2, hover_color=ACCENT,
                      command=self._pick_verify_dir).grid(row=0, column=2, padx=12, pady=8)

        self.verify_button = ctk.CTkButton(
            parent, text="Verify checksums", height=40, width=180,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.start_verify,
        )
        self.verify_button.grid(row=1, column=0, sticky="e", pady=8)

        self.verify_output = ctk.CTkTextbox(parent, height=400, fg_color=SURFACE,
                                            font=ctk.CTkFont(family="Menlo", size=12))
        self.verify_output.grid(row=2, column=0, sticky="nsew", pady=12)
        parent.grid_rowconfigure(2, weight=1)

    def _build_sideload_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)

        card = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=8)
        card.grid(row=0, column=0, sticky="ew", pady=12)
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text="ZIP:", width=100, anchor="w").grid(
            row=0, column=0, padx=12, pady=8, sticky="w")
        self.sideload_zip = ctk.CTkEntry(card, placeholder_text="path/to/ota.zip")
        self.sideload_zip.grid(row=0, column=1, padx=4, pady=8, sticky="ew")
        ctk.CTkButton(card, text="Browse", width=80,
                      fg_color=SURFACE_2, hover_color=ACCENT,
                      command=self._pick_sideload_zip).grid(row=0, column=2, padx=12, pady=8)

        ctk.CTkLabel(
            parent,
            text="Device must already be in `adb sideload` mode "
                 "(Recovery → Apply update from ADB).",
            anchor="w", text_color=MUTED,
        ).grid(row=1, column=0, sticky="ew", padx=4)

        self.sideload_button = ctk.CTkButton(
            parent, text="Sideload ZIP", height=40, width=180,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.start_sideload,
        )
        self.sideload_button.grid(row=2, column=0, sticky="e", pady=8)

        self.sideload_output = ctk.CTkTextbox(parent, height=240, fg_color=SURFACE)
        self.sideload_output.grid(row=3, column=0, sticky="nsew", pady=12)
        parent.grid_rowconfigure(3, weight=1)

    def _build_logs_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        self.logs = ctk.CTkTextbox(
            parent, fg_color=SURFACE,
            font=ctk.CTkFont(family="Menlo", size=11),
        )
        self.logs.grid(row=0, column=0, sticky="nsew", pady=12)

    # ------- file pickers ----------------------------------------------------

    def _pick_backup_dir(self):
        p = filedialog.askdirectory(title="Choose output directory")
        if p:
            self.backup_out.delete(0, "end"); self.backup_out.insert(0, p)

    def _pick_flash_image(self):
        p = filedialog.askopenfilename(
            title="Choose image",
            filetypes=[("Images", "*.img *.bin"), ("All files", "*.*")],
        )
        if p:
            self.flash_image.delete(0, "end"); self.flash_image.insert(0, p)

    def _pick_restore_dir(self):
        p = filedialog.askdirectory(title="Choose backup directory")
        if p:
            self.restore_dir.delete(0, "end"); self.restore_dir.insert(0, p)

    def _pick_verify_dir(self):
        p = filedialog.askdirectory(title="Choose backup directory")
        if p:
            self.verify_dir.delete(0, "end"); self.verify_dir.insert(0, p)

    def _pick_sideload_zip(self):
        p = filedialog.askopenfilename(
            title="Choose ZIP",
            filetypes=[("ZIP files", "*.zip"), ("All files", "*.*")],
        )
        if p:
            self.sideload_zip.delete(0, "end"); self.sideload_zip.insert(0, p)

    # ------- log / status ----------------------------------------------------

    def log(self, msg: str) -> None:
        self.logs.insert("end", msg + "\n")
        self.logs.see("end")

    def status(self, msg: str) -> None:
        self.status_label.configure(text=msg)

    # ------- device handling -------------------------------------------------

    def refresh_devices(self) -> None:
        self.status("Refreshing devices…")

        def work():
            try:
                devs = device_mod.discover()
                self.signal.emit({"event": "devices", "devices": devs})
            except Exception as e:
                self.signal.emit({"event": "error", "error": str(e),
                                  "trace": traceback.format_exc()})

        _run_thread(work)

    def _on_device_selected(self, label: str) -> None:
        if not hasattr(self, "_device_map"):
            return
        dev = self._device_map.get(label)
        self.current_device = dev
        self._render_device_info()
        if dev and dev.state == "device" and dev.rooted:
            self._load_partitions()
        else:
            self._clear_partitions()

    def _render_device_info(self) -> None:
        d = self.current_device
        if d is None:
            self.device_info_label.configure(text="No device selected.")
            return
        root = "yes" if d.rooted else "no"
        chip = d.chipset + (" (MTK)" if d.is_mediatek else "")
        text = (
            f"Serial:  {d.serial}\n"
            f"State:   {d.state}\n"
            f"Model:   {d.model}\n"
            f"Chip:    {chip}\n"
            f"Root:    {root}\n"
            f"Build:   {d.fingerprint}"
        )
        self.device_info_label.configure(text=text)

    def _load_partitions(self) -> None:
        self.status("Enumerating partitions…")

        def work():
            try:
                parts = part_mod.list_partitions(serial=self.current_device.serial)
                self.signal.emit({"event": "partitions", "partitions": parts})
            except Exception as e:
                self.signal.emit({"event": "error", "error": str(e),
                                  "trace": traceback.format_exc()})

        _run_thread(work)

    def _clear_partitions(self) -> None:
        for w in self.partition_frame.winfo_children():
            w.destroy()
        self.partition_vars.clear()
        self.partitions = []

    def _render_partitions(self, parts: list[Partition]) -> None:
        self._clear_partitions()
        self.partitions = parts
        default = set(DEFAULT_BACKUP_SET)
        flash_options = sorted({p.name for p in parts})
        self.flash_partition.configure(values=flash_options or ["boot"])

        # Header
        header = ctk.CTkFrame(self.partition_frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        header.grid_columnconfigure(1, weight=1)
        for col, txt in enumerate(("", "Name", "Size", "Notes")):
            ctk.CTkLabel(header, text=txt, anchor="w", text_color=MUTED,
                         font=ctk.CTkFont(size=11, weight="bold")
                         ).grid(row=0, column=col, padx=4, sticky="w")

        for i, p in enumerate(parts, start=1):
            row = ctk.CTkFrame(self.partition_frame, fg_color="transparent")
            row.grid(row=i, column=0, sticky="ew", pady=1)
            row.grid_columnconfigure(1, weight=1)

            var = tk.BooleanVar(value=p.name in default)
            self.partition_vars[p.name] = var
            ctk.CTkCheckBox(row, text="", variable=var, width=20,
                            fg_color=ACCENT).grid(row=0, column=0, padx=4)

            ctk.CTkLabel(row, text=p.name, anchor="w").grid(
                row=0, column=1, padx=4, sticky="w")
            ctk.CTkLabel(row, text=human_size(p.size_bytes) if p.size_bytes else "?",
                         anchor="e", text_color=MUTED, width=80).grid(
                row=0, column=2, padx=4, sticky="e")
            notes = []
            if is_dangerous(p.name):
                notes.append(("DANGER", DANGER))
            if p.name in MTK_CRITICAL:
                notes.append(("MTK", "#a78bfa"))
            if p.name in ("userdata", "data"):
                notes.append(("LARGE", WARN))
            note_frame = ctk.CTkFrame(row, fg_color="transparent")
            note_frame.grid(row=0, column=3, padx=4, sticky="e")
            for txt, color in notes:
                ctk.CTkLabel(note_frame, text=txt, text_color=color,
                             font=ctk.CTkFont(size=10, weight="bold")
                             ).pack(side="left", padx=2)

    def _partitions_set_all(self, val: bool) -> None:
        for v in self.partition_vars.values():
            v.set(val)

    def _partitions_select_default(self) -> None:
        default = set(DEFAULT_BACKUP_SET)
        for name, v in self.partition_vars.items():
            v.set(name in default)

    # ------- operations ------------------------------------------------------

    def reboot(self, target: str) -> None:
        if not self.current_device:
            self.status("No device selected.")
            return
        target_arg = None if target == "system" else target
        try:
            adb_mod.reboot(target_arg, serial=self.current_device.serial)
            self.log(f"[reboot] {self.current_device.serial} -> {target}")
            self.status(f"Rebooting to {target}…")
        except Exception as e:
            self.log(f"[reboot] failed: {e}")
            self.status(f"Reboot failed: {e}")

    def start_backup(self) -> None:
        if not self.current_device or not self.current_device.rooted:
            self.status("Need a rooted device for backup.")
            return
        out = self.backup_out.get().strip()
        if not out:
            self.status("Choose an output directory.")
            return
        selected = [p for p in self.partitions if self.partition_vars[p.name].get()]
        if not selected:
            self.status("Select at least one partition.")
            return

        out_dir = Path(out)
        total_bytes = sum(p.size_bytes or 0 for p in selected) or 1
        bytes_done = 0
        per_part_total = {p.name: (p.size_bytes or 0) for p in selected}

        self.backup_button.configure(state="disabled")
        self.global_progress.set(0)
        self.status(f"Backing up {len(selected)} partitions…")
        self.log(f"[backup] -> {out_dir}, partitions={[p.name for p in selected]}")

        device = self.current_device

        def emit(ev: dict) -> None:
            nonlocal bytes_done
            if ev["type"] == "advance":
                bytes_done += ev["bytes"]
                self.signal.emit({"event": "backup_progress",
                                  "bytes_done": bytes_done,
                                  "total_bytes": total_bytes,
                                  "current": ev["name"]})
            else:
                self.signal.emit({"event": f"backup_{ev['type']}", **ev,
                                  "expected": per_part_total.get(ev.get("name"), 0)})

        def work():
            try:
                entries = backup_mod.backup_partitions(
                    device=device,
                    parts=selected,
                    out_dir=out_dir,
                    verify_on_device=True,
                    events=emit,
                )
                manifest = Manifest.new(device_info={
                    "serial": device.serial,
                    "model": device.model,
                    "fingerprint": device.fingerprint,
                    "chipset": device.chipset,
                    "is_mediatek": device.is_mediatek,
                    "properties": device.properties,
                })
                manifest.partitions = entries
                manifest.write(out_dir / MANIFEST_FILENAME)
                self.signal.emit({"event": "backup_complete", "out": str(out_dir),
                                  "count": len(entries)})
            except Exception as e:
                self.signal.emit({"event": "error", "error": str(e),
                                  "trace": traceback.format_exc()})
            finally:
                self.signal.emit({"event": "backup_finished"})

        _run_thread(work)

    def start_flash(self) -> None:
        image = self.flash_image.get().strip()
        partition = self.flash_partition.get().strip()
        if not image or not partition:
            self.status("Image and partition required.")
            return
        image_path = Path(image)
        if not image_path.exists():
            self.status("Image file does not exist.")
            return

        boot_only = self.flash_boot_only.get()
        force = self.flash_force.get()
        serial = self.current_device.serial if self.current_device else None

        self.flash_output.delete("1.0", "end")
        self.flash_output.insert("end",
            f"Image:     {image_path}\n"
            f"Size:      {human_size(image_path.stat().st_size)}\n"
            f"SHA-256:   {sha256_file(image_path)}\n"
            f"Target:    {'BOOT (no flash)' if boot_only else 'flash ' + partition}\n"
        )

        self.flash_button.configure(state="disabled")
        self.status(f"{'Booting' if boot_only else 'Flashing'} {partition}…")

        def work():
            try:
                flash_mod.flash_image(
                    partition=partition, image=image_path, serial=serial,
                    boot_only=boot_only, dry_run=False,
                    force=force, assume_yes=True,
                )
                self.signal.emit({"event": "flash_done", "partition": partition,
                                  "boot_only": boot_only})
            except Exception as e:
                self.signal.emit({"event": "error", "error": str(e),
                                  "trace": traceback.format_exc()})
            finally:
                self.signal.emit({"event": "flash_finished"})

        _run_thread(work)

    def start_restore(self) -> None:
        d = self.restore_dir.get().strip()
        if not d:
            self.status("Choose a backup directory.")
            return
        backup_dir = Path(d)
        if not (backup_dir / MANIFEST_FILENAME).exists():
            self.status("No manifest.json in that directory.")
            return

        include_userdata = self.restore_include_userdata.get()
        force = self.restore_force.get()
        serial = self.current_device.serial if self.current_device else None

        self.restore_output.delete("1.0", "end")
        self.restore_button.configure(state="disabled")
        self.status("Restoring backup…")

        def work():
            try:
                flash_mod.restore_backup(
                    backup_dir=backup_dir,
                    serial=serial,
                    include_userdata=include_userdata,
                    force=force,
                    dry_run=False,
                    assume_yes=True,
                )
                self.signal.emit({"event": "restore_done"})
            except Exception as e:
                self.signal.emit({"event": "error", "error": str(e),
                                  "trace": traceback.format_exc()})
            finally:
                self.signal.emit({"event": "restore_finished"})

        _run_thread(work)

    def start_verify(self) -> None:
        d = self.verify_dir.get().strip()
        if not d:
            self.status("Choose a backup directory.")
            return
        backup_dir = Path(d)
        self.verify_output.delete("1.0", "end")
        self.verify_button.configure(state="disabled")
        self.status("Verifying checksums…")

        def work():
            try:
                manifest_path = backup_dir / MANIFEST_FILENAME
                if not manifest_path.exists():
                    self.signal.emit({"event": "verify_line",
                                      "text": f"No manifest at {manifest_path}",
                                      "color": DANGER})
                    self.signal.emit({"event": "verify_done", "ok": False})
                    return
                manifest = Manifest.read(manifest_path)
                ok = True
                for entry in manifest.partitions:
                    fp = backup_dir / entry["file"]
                    if not fp.exists():
                        self.signal.emit({"event": "verify_line",
                                          "text": f"MISSING  {entry['file']}",
                                          "color": DANGER})
                        ok = False; continue
                    if fp.stat().st_size != entry["size_bytes"]:
                        self.signal.emit({"event": "verify_line",
                                          "text": f"SIZE     {entry['file']}",
                                          "color": DANGER})
                        ok = False; continue
                    actual = sha256_file(fp)
                    if actual != entry["sha256"]:
                        self.signal.emit({"event": "verify_line",
                                          "text": f"HASH MISMATCH  {entry['file']}",
                                          "color": DANGER})
                        ok = False; continue
                    self.signal.emit({"event": "verify_line",
                                      "text": f"OK       {entry['name']:<14} "
                                              f"{human_size(fp.stat().st_size)}",
                                      "color": OK})
                self.signal.emit({"event": "verify_done", "ok": ok})
            except Exception as e:
                self.signal.emit({"event": "error", "error": str(e),
                                  "trace": traceback.format_exc()})
            finally:
                self.signal.emit({"event": "verify_finished"})

        _run_thread(work)

    def start_sideload(self) -> None:
        z = self.sideload_zip.get().strip()
        if not z:
            self.status("Choose a ZIP.")
            return
        zip_path = Path(z)
        if not zip_path.exists():
            self.status("ZIP does not exist.")
            return

        serial = self.current_device.serial if self.current_device else None
        self.sideload_output.delete("1.0", "end")
        self.sideload_output.insert("end",
            f"ZIP:     {zip_path}\n"
            f"Size:    {human_size(zip_path.stat().st_size)}\n"
            f"SHA-256: {sha256_file(zip_path)}\n"
            "Streaming via adb sideload…\n"
        )
        self.sideload_button.configure(state="disabled")
        self.status("Sideloading…")

        def work():
            try:
                flash_mod.sideload_zip(zip_path, serial=serial, dry_run=False,
                                       assume_yes=True)
                self.signal.emit({"event": "sideload_done"})
            except Exception as e:
                self.signal.emit({"event": "error", "error": str(e),
                                  "trace": traceback.format_exc()})
            finally:
                self.signal.emit({"event": "sideload_finished"})

        _run_thread(work)

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
            devs: list[Device] = ev["devices"]
            labels = []
            self._device_map = {}
            for d in devs:
                label = f"{d.serial}  ({d.state})"
                labels.append(label)
                self._device_map[label] = d
            if labels:
                self.device_dropdown.configure(values=labels)
                self.device_dropdown.set(labels[0])
                self._on_device_selected(labels[0])
                self.status(f"{len(devs)} device(s) attached.")
            else:
                self.device_dropdown.configure(values=["(none)"])
                self.device_dropdown.set("(none)")
                self.current_device = None
                self._render_device_info()
                self._clear_partitions()
                self.status("No devices attached.")

        elif kind == "partitions":
            self._render_partitions(ev["partitions"])
            self.status(f"{len(ev['partitions'])} partitions enumerated.")

        elif kind == "backup_start":
            self.log(f"[backup] start {ev['name']} "
                     f"({human_size(ev.get('expected', 0))})")
        elif kind == "backup_progress":
            self.global_progress.set(min(1.0, ev["bytes_done"] / ev["total_bytes"]))
            self.status(f"Backing up {ev['current']}…  "
                        f"{human_size(ev['bytes_done'])} / "
                        f"{human_size(ev['total_bytes'])}")
        elif kind == "backup_done":
            self.log(f"[backup] OK    {ev['name']}  "
                     f"sha256={ev['sha256'][:12]}…  "
                     f"size={human_size(ev['written'])}")
        elif kind == "backup_error":
            self.log(f"[backup] FAIL  {ev['name']}: {ev['error']}")
        elif kind == "backup_complete":
            self.log(f"[backup] COMPLETE — {ev['count']} partitions in {ev['out']}")
            self.global_progress.set(1.0)
            self.status(f"Backup complete — {ev['count']} partitions saved.")
        elif kind == "backup_finished":
            self.backup_button.configure(state="normal")

        elif kind == "flash_done":
            msg = (f"Booted {ev['partition']}." if ev['boot_only']
                   else f"Flashed {ev['partition']}.")
            self.flash_output.insert("end", msg + "\n")
            self.status(msg)
        elif kind == "flash_finished":
            self.flash_button.configure(state="normal")

        elif kind == "restore_done":
            self.restore_output.insert("end", "Restore complete.\n")
            self.status("Restore complete.")
        elif kind == "restore_finished":
            self.restore_button.configure(state="normal")

        elif kind == "verify_line":
            self.verify_output.insert("end", ev["text"] + "\n")
            self.verify_output.see("end")
        elif kind == "verify_done":
            msg = "Verification OK." if ev["ok"] else "Verification FAILED."
            self.verify_output.insert("end", "\n" + msg + "\n")
            self.status(msg)
        elif kind == "verify_finished":
            self.verify_button.configure(state="normal")

        elif kind == "sideload_done":
            self.sideload_output.insert("end", "Sideload complete.\n")
            self.status("Sideload complete.")
        elif kind == "sideload_finished":
            self.sideload_button.configure(state="normal")

        elif kind == "error":
            self.log("[error] " + ev["error"])
            self.log(ev.get("trace", ""))
            self.status("Error — see Logs tab.")


def run() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    run()
