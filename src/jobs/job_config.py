"""
src/jobs/job_config.py

One small helper that every job uses: read a job's interval from config.json.
Kept in one place (like line_format.py for the *_lines.py files).

config.json example:
    "snmp":   {"target": "192.168.1.1", "interface": "lte0", "community": "public", "interval_s": 10},
    "health": {"interval_s": 10}
"""

import json
from pathlib import Path

# config.json in the project root (src/jobs/job_config.py -> ../../config.json)
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"


def load_interval_s(section: str, default: float) -> float:
    """
    Return config.json[section]["interval_s"], or the default if the file,
    the section or the setting is missing.
    """
    try:
        with open(CONFIG_PATH) as f:
            return float(json.load(f).get(section, {}).get("interval_s", default))
    except FileNotFoundError:
        return default