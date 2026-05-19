"""
Filesystem path resolution for cityops-mcp.

Defaults to platform user-data dir (XDG-compliant on Linux,
~/Library/Application Support/cityops-mcp on macOS).

Overrides:
    CITYOPS_DB_PATH    — absolute path to the SQLite file
    CITYOPS_DATA_DIR   — directory for CSVs loaded via load_csv
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "cityops-mcp"


def get_data_root() -> Path:
    root = Path(user_data_dir(APP_NAME))
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_db_path() -> Path:
    override = os.environ.get("CITYOPS_DB_PATH")
    if override:
        path = Path(override).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return get_data_root() / "cityops.sqlite"


def get_csv_dir() -> Path:
    override = os.environ.get("CITYOPS_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    else:
        path = get_data_root() / "csv"
    path.mkdir(parents=True, exist_ok=True)
    return path
