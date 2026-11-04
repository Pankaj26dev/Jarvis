"""Lightweight configuration loading, safe to import before the AI stack."""
import json
from pathlib import Path


CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config(path=CONFIG_PATH):
    """Read the project config, returning an empty mapping if it is absent."""
    path = Path(path)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as config_file:
        data = json.load(config_file)
    if not isinstance(data, dict):
        raise ValueError(f"Config must contain a JSON object: {path}")
    return data
