"""Backup manifest read/write."""
from __future__ import annotations

import json
import platform
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import __version__

MANIFEST_FILENAME = "manifest.json"


@dataclass
class Manifest:
    tool: str = "android-rom-extractor"
    tool_version: str = __version__
    created_at: str = ""
    host: dict[str, str] = field(default_factory=dict)
    device: dict[str, Any] = field(default_factory=dict)
    partitions: list[dict] = field(default_factory=list)

    @classmethod
    def new(cls, device_info: dict[str, Any]) -> "Manifest":
        return cls(
            created_at=datetime.now(timezone.utc).isoformat(),
            host={
                "system": platform.system(),
                "release": platform.release(),
                "python": platform.python_version(),
            },
            device=device_info,
        )

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=False))

    @classmethod
    def read(cls, path: Path) -> "Manifest":
        data = json.loads(path.read_text())
        return cls(**data)

    def find(self, partition_name: str) -> Optional[dict]:
        for p in self.partitions:
            if p["name"] == partition_name:
                return p
        return None
