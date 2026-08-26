"""Read-only views of what lunad has on disk: memory, jobs, audit, key presence.

Everything here is a read. Jarvis shows what the daemon has written; it never
edits memory files or job directories from the GUI, because those have their
own consistency rules inside the daemon.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("XDG_DATA_HOME",
                                os.path.expanduser("~/.local/share"))) / "luna"
MEMORY_DIR = STATE_DIR / "memory"
JOBS_DIR = STATE_DIR / "jobs"
AUDIT_PATH = STATE_DIR / "audit.jsonl"
LOG_PATH = STATE_DIR / "luna.log"

ENTRY_DELIMITER = "§"          # § — from Hermes, via lunad/config.py

# Tier-1 files and the config key that caps each.
TIER1 = (
    ("LUNA.md", "memory.luna_cap_chars", "Assistant memory"),
    ("USER.md", "memory.user_cap_chars", "User model"),
)

# Where an API key may live. Presence only — the value is never read into
# memory, never returned, and never rendered. `st_size` is enough to answer
# "is there a key".
KEY_FILES = (
    Path(os.path.expanduser("~/.config/jarvis/secrets.env")),
    Path(os.path.expanduser("~/.config/luna/secrets.env")),
    Path(os.path.expanduser("~/.config/voxtype/secrets.env")),
)
KEY_ENV_VARS = ("OPENROUTER_API_KEY", "JARVIS_API_KEY", "LUNA_API_KEY",
                "VOXTYPE_WHISPER_API_KEY")


# ---------------------------------------------------------------- memory

def tier1_usage(values: dict) -> list[dict]:
    out = []
    for fname, cap_key, title in TIER1:
        path = MEMORY_DIR / fname
        cap = values.get(cap_key) or 1
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
            exists = False
        else:
            exists = True
        chars = len(text)
        out.append({
            "file": fname, "title": title, "path": str(path),
            "exists": exists, "chars": chars, "cap": int(cap),
            "pct": min(1.0, chars / cap) if cap else 0.0,
            "entries": len(split_entries(text)),
        })
    return out


def split_entries(text: str) -> list[str]:
    """§-delimited entries, as lunad writes them. A file with no § at all is
    one entry, so a hand-written file still shows something."""
    if not text.strip():
        return []
    parts = [p.strip() for p in text.split(ENTRY_DELIMITER)]
    return [p for p in parts if p]


def read_entries(fname: str) -> tuple[list[str], str]:
    path = MEMORY_DIR / fname
    try:
        return split_entries(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return [], f"{path} does not exist yet"
    except OSError as exc:
        return [], str(exc)


def episodes_info() -> dict:
    path = MEMORY_DIR / "episodes.db"
    try:
        size = path.stat().st_size
    except OSError:
        return {"exists": False, "path": str(path), "size": 0, "count": None}
    count = None
    try:
        import sqlite3
        # read-only URI: the daemon owns this file and may be mid-write.
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        try:
            count = con.execute("select count(*) from episodes").fetchone()[0]
        finally:
            con.close()
    except Exception:
        count = None
    return {"exists": True, "path": str(path), "size": size, "count": count}


# ---------------------------------------------------------------- jobs

def recent_jobs(limit: int = 30) -> list[dict]:
    """Newest first, read from disk so the list survives a daemon restart."""
    rows = []
    try:
        dirs = [d for d in JOBS_DIR.iterdir() if d.is_dir()]
    except OSError:
        return []
    for d in dirs:
        meta = {}
        try:
            meta = json.loads((d / "job.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta.setdefault("id", d.name)
        meta.setdefault("dir", str(d))
        if "started" not in meta:
            try:
                meta["started"] = d.stat().st_mtime
            except OSError:
                meta["started"] = 0
        if "exit_code" not in meta:
            try:
                meta["exit_code"] = int((d / "exit").read_text().strip())
            except (OSError, ValueError):
                pass
        rows.append(meta)
    rows.sort(key=lambda m: m.get("started") or 0, reverse=True)
    return rows[:limit]


def job_age(meta: dict) -> str:
    ts = meta.get("started") or 0
    if not ts:
        return "—"
    delta = max(0, time.time() - ts)
    for unit, secs in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= secs:
            return f"{int(delta // secs)}{unit} ago"
    return "just now"


# ---------------------------------------------------------------- secrets

def key_present() -> tuple[bool, str]:
    """(present, where). NEVER returns or logs the key itself.

    Presence is decided from the file's size and the variable *names* it
    declares. The value after `=` is not read into a variable at any point.
    """
    for path in KEY_FILES:
        try:
            if not path.is_file() or path.stat().st_size == 0:
                continue
            names = set()
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    names.add(line.split("=", 1)[0].strip().lstrip("export "))
            if names & set(KEY_ENV_VARS) or any("KEY" in n for n in names):
                return True, str(path)
        except OSError:
            continue
    for var in KEY_ENV_VARS:
        if os.environ.get(var):
            return True, f"${var} in the environment"
    return False, "no secrets.env with a key variable found"


def key_file_mode(path: str) -> str:
    try:
        return oct(Path(path).stat().st_mode & 0o777)
    except OSError:
        return "—"


# ---------------------------------------------------------------- audit

def audit_tail(n: int = 12) -> list[dict]:
    try:
        with open(AUDIT_PATH, "rb") as fh:
            lines = fh.readlines()[-n:]
    except OSError:
        return []
    out = []
    for raw in reversed(lines):
        try:
            row = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"
