"""Lightweight persistent settings: ~/.config/arom/settings.json."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "arom" / "settings.json"


@dataclass
class Settings:
    last_output_dir: str = ""
    last_backup_dir: str = ""
    auto_verify_after_backup: bool = True
    default_partition_selection: list[str] = field(default_factory=list)
    recent_backups: list[str] = field(default_factory=list)
    partition_presets: dict[str, list[str]] = field(default_factory=dict)
    window_geometry: str = ""

    @classmethod
    def load(cls) -> "Settings":
        path = _config_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
            known = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in data.items() if k in known})
        except Exception as e:
            log.warning("Could not load settings (%s); using defaults.", e)
            return cls()

    def save(self) -> None:
        path = _config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), indent=2))
        except Exception as e:
            log.warning("Could not save settings: %s", e)

    def push_recent_backup(self, path: str, max_keep: int = 8) -> None:
        if path in self.recent_backups:
            self.recent_backups.remove(path)
        self.recent_backups.insert(0, path)
        self.recent_backups = self.recent_backups[:max_keep]
