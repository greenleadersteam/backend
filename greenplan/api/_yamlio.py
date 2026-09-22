"""Tiny atomic-YAML helpers shared by storage.py (metadata, main process
only) and jobs.py (job status, written from worker subprocesses too).
"""

from __future__ import annotations

from pathlib import Path

import yaml


def atomic_write_yaml(path: Path, data: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp_path.replace(path)


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))
