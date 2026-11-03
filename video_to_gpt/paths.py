from __future__ import annotations

from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]


def ensure_runtime_dirs(root: Path = APP_ROOT) -> None:
    for rel in (
        "input",
        "output",
        "templates",
        "config",
        "tools/ffmpeg",
        "tools/whisper/models",
        "tools/runtime_ascii",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)
