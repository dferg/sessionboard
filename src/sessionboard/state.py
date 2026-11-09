"""Private, per-config TUI preferences. Session contents never belong here."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

SORT_MODES = ("live", "recent", "project", "host", "tool")


@dataclass
class ViewState:
    by_project: bool = False
    collapsed: list[str] = field(default_factory=list)
    query: str = ""
    tool_filter: str | None = None
    group_filter: str | None = None
    live_only: bool = False
    show_archived: bool = False
    sort_mode: str = "live"
    pinned: list[str] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)
    show_hidden: bool = False


def state_path(config_path: Path) -> Path:
    """Keep separate configurations' preferences separate without storing their paths."""
    root = Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser()
    identity = hashlib.sha256(str(config_path.resolve()).encode()).hexdigest()[:20]
    return root / "sessionboard" / f"view-{identity}.json"


def load_state(path: Path) -> ViewState:
    state = ViewState()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return state
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return state
    for key in ("by_project", "live_only", "show_archived", "show_hidden"):
        if isinstance(raw.get(key), bool):
            setattr(state, key, raw[key])
    for key in ("collapsed", "pinned", "hidden"):
        value = raw.get(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            setattr(state, key, sorted(set(value)))
    if isinstance(raw.get("query"), str):
        state.query = raw["query"]
    if raw.get("tool_filter") in (None, "claude", "codex"):
        state.tool_filter = raw.get("tool_filter")
    if isinstance(raw.get("group_filter"), str):
        state.group_filter = raw["group_filter"]
    if raw.get("sort_mode") in SORT_MODES:
        state.sort_mode = raw["sort_mode"]
    return state


def save_state(path: Path, state: ViewState) -> None:
    """Replace atomically; mkstemp keeps the file owner-readable/writable only."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".view-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"version": 1, **asdict(state)}, stream, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
