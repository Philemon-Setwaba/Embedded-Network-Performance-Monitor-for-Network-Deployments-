"""
src/reporting/line_format.py

Small helper functions that build VictoriaMetrics lines.
Every *_lines.py file (ping_lines.py, health_lines.py, ...) uses these,
so they live in ONE place: a fix here fixes every file.

(These are the helpers that used to be inside victoria.py.)

Every line has 4 parts:

    ping_rtt_ms{device="pi5",target="8.8.8.8",count="5"} 19.2 1791216953458
    └── name ─┘└────────────── labels ─────────────────┘ └val┘└─ time ────┘

  - name   : what was measured
  - labels : TEXT that says which device / target it belongs to
  - value  : must be a NUMBER (True/False -> 1/0, statuses -> code numbers)
  - time   : milliseconds since 1 January 1970 (UTC)

This format was tested by hand on the Pi with curl and it worked.
"""

# datetime is used to turn the timestamp text into milliseconds.
from datetime import datetime

# These are only "type hints" - they describe what goes in and out of
# each function. They do not change how the code runs.
from typing import Dict


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

# Put on every line as  device="pi5"  so that, if more Pis are added
# later, their data can be told apart.
DEVICE = "pi5"

# Error messages are stored in a label. Keep them at most 200 characters,
# so one long, strange message cannot create a huge label.
MAX_LABEL_LEN = 200


# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------

def to_ms(iso_timestamp: str) -> int:
    """
    Turn the timestamp text from a result into milliseconds.

    Example:
        '2026-10-05T16:15:53.458147+00:00'  ->  1791216953458
    """
    # fromisoformat() reads the text as a date and time.
    # .timestamp() gives seconds since 1970, e.g. 1791216953.458147
    # * 1000 gives milliseconds, and int() drops the decimals.
    return int(datetime.fromisoformat(iso_timestamp).timestamp() * 1000)


def escape(text: str) -> str:
    """
    Make text safe to put inside a label's quotes.

    Inside "...", three characters have special meaning, so they must be
    written differently, or VictoriaMetrics would misread the line:
        \\       (backslash)    ->  \\\\
        "        (double quote) ->  \\"
        newline                 ->  \\n
    """
    return (
        text.replace("\\", "\\\\")     # do backslash first, or we'd double the others
            .replace('"', '\\"')
            .replace("\n", "\\n")
    )


def line(name: str, labels: Dict[str, str], value: float, ts_ms: int) -> str:
    """
    Build ONE line.

    Example:
        line("ping_rtt_ms",
             {"device": "pi5", "target": "8.8.8.8", "count": "5"},
             19.2,
             1791216953458)
    gives:
        ping_rtt_ms{device="pi5",target="8.8.8.8",count="5"} 19.2 1791216953458
    """
    # Turn the labels dictionary into:  device="pi5",target="8.8.8.8",count="5"
    label_text = ",".join(f'{key}="{escape(val)}"' for key, val in labels.items())

    # Put the 4 parts together. ({{ and }} print a single { and }.)
    return f"{name}{{{label_text}}} {value} {ts_ms}"


def short_text(text: str) -> str:
    """
    Keep only the first line of an error message, at most 200 characters.

    Example:
        'ping: doesnotexist.invalid: Name or service not known'
    stays the same (it is one short line).
    """
    if not text.strip():
        return ""
    first_line = text.strip().splitlines()[0]
    return first_line[:MAX_LABEL_LEN]