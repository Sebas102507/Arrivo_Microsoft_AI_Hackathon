"""Tiny helper to upsert KEY=value lines into backend/.env."""
from __future__ import annotations

from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / "backend" / ".env"


def upsert_env(key: str, value: str, path: Path = ENV_PATH) -> None:
    """Set KEY=value in the env file, replacing any existing line for KEY."""
    lines = path.read_text().splitlines() if path.exists() else []
    out, found = [], False
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")
