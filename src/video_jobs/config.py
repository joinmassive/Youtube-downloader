"""Minimal local .env loading without making environment configuration a dependency."""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines without replacing variables explicitly set by the shell."""
    dotenv = Path(path)
    if not dotenv.is_file():
        return
    for raw in dotenv.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
