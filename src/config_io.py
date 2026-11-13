"""The one place that knows how config.toml is opened from disk."""

import tomllib
from pathlib import Path


def load_raw_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)
