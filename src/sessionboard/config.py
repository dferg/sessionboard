"""Configuration: ~/.config/sessionboard/config.toml (override with --config or $SESSIONBOARD_CONFIG)."""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = Path("~/.config/sessionboard/config.toml").expanduser()
CACHE_DIR = Path(
    os.environ.get("SESSIONBOARD_CACHE") or "~/.local/share/sessionboard/hosts"
).expanduser()

EXAMPLE_CONFIG = """\
# sessionboard configuration
#
# Every machine you run Claude Code or Codex on gets a [[hosts]] entry. The local
# machine is included automatically as `local_name` unless `local = false`.

[settings]
local = true
local_name = "{local_name}"   # label for this machine
sync_on_start = true          # pull every host when the TUI opens
auto_sync_seconds = 120       # background re-sync interval (0 = off)
ssh_timeout = 25              # seconds per host, connect + collect
hide_empty = true             # hide sessions that never got a human prompt (claude -p etc.)
# recent_minutes = 15         # "·" marker: modified this recently but no live process

# [[hosts]]
# name = "work-mbp"
# ssh = "work-mbp"            # anything `ssh` accepts: alias, user@host, Tailscale name
# claude_dir = "~/.claude"    # optional
# codex_dir = "~/.codex"      # optional
# python = "python3"          # optional, interpreter to run the collector with
# group = "work-mbp"          # hosts that share one home directory (NFS) get the same
#                             # group so a session is listed once, live if live anywhere

# [[hosts]]
# name = "site-a-login1"
# ssh = "login1.site-a.example"
# group = "site-a"
#
# [[hosts]]
# name = "site-a-build7"
# ssh = "build7.site-a.example"
# group = "site-a"
"""


class ConfigError(Exception):
    pass


@dataclass
class HostSpec:
    name: str
    ssh: str | None = None  # None = local machine
    claude_dir: str = "~/.claude"
    codex_dir: str = "~/.codex"
    python: str = "python3"
    group: str | None = None

    @property
    def is_local(self) -> bool:
        return self.ssh is None

    @property
    def group_name(self) -> str:
        return self.group or self.name


@dataclass
class Settings:
    local: bool = True
    local_name: str = "local"  # `sessionboard init` fills in the hostname
    sync_on_start: bool = True
    auto_sync_seconds: int = 120
    ssh_timeout: int = 25
    hide_empty: bool = True
    recent_minutes: int = 15


@dataclass
class Config:
    path: Path
    settings: Settings
    hosts: list[HostSpec]

    def host(self, name: str) -> HostSpec | None:
        for h in self.hosts:
            if h.name == name:
                return h
        return None


def _hostspec(raw: dict, idx: int) -> HostSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"[[hosts]] entry {idx} is not a table")
    name = raw.get("name")
    if not name:
        raise ConfigError(f"[[hosts]] entry {idx} has no name")
    known = {"name", "ssh", "claude_dir", "codex_dir", "python", "group"}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"host {name!r}: unknown keys {sorted(unknown)}")
    return HostSpec(
        name=str(name),
        ssh=str(raw["ssh"]) if raw.get("ssh") else None,
        claude_dir=str(raw.get("claude_dir") or "~/.claude"),
        codex_dir=str(raw.get("codex_dir") or "~/.codex"),
        python=str(raw.get("python") or "python3"),
        group=str(raw["group"]) if raw.get("group") else None,
    )


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load the config; a missing file yields the defaults (local machine only)."""
    cfg_path = Path(path or os.environ.get("SESSIONBOARD_CONFIG") or DEFAULT_CONFIG).expanduser()
    raw: dict = {}
    if cfg_path.exists():
        try:
            raw = tomllib.loads(cfg_path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{cfg_path}: {exc}") from exc
    s_raw = raw.get("settings") or {}
    settings = Settings()
    for key, value in s_raw.items():
        if not hasattr(settings, key):
            raise ConfigError(f"[settings] unknown key {key!r}")
        setattr(settings, key, value)
    hosts: list[HostSpec] = []
    if settings.local:
        hosts.append(HostSpec(name=settings.local_name))
    for idx, h in enumerate(raw.get("hosts") or [], start=1):
        spec = _hostspec(h, idx)
        if spec.ssh is None and settings.local:
            raise ConfigError(f"host {spec.name!r}: an entry without `ssh` is the local machine, "
                              "which is already included; set settings.local = false to define it yourself")
        hosts.append(spec)
    names = [h.name for h in hosts]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"duplicate host names: {sorted(dupes)}")
    return Config(path=cfg_path, settings=settings, hosts=hosts)


def write_example(path: Path | None = None, force: bool = False) -> Path:
    target = Path(path or DEFAULT_CONFIG).expanduser()
    if target.exists() and not force:
        raise ConfigError(f"{target} already exists (use --force to overwrite)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG.format(local_name=socket.gethostname().split(".")[0]))
    return target
