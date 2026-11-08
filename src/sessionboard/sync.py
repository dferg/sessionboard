"""Collect session summaries from every host and cache them locally.

Remote hosts never get anything installed: the stdlib collector script is streamed to
``ssh <host> python3 -`` on stdin and the JSON it prints comes back on stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import collector
from .config import CACHE_DIR, Config, HostSpec
from .model import HostState

COLLECTOR_SRC = Path(collector.__file__)


def cache_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return CACHE_DIR / f"{safe}.json"


def load_cache(config: Config) -> list[HostState]:
    states = []
    for spec in config.hosts:
        st = HostState(name=spec.name, group=spec.group_name)
        p = cache_path(spec.name)
        if p.exists():
            try:
                blob = json.loads(p.read_text())
                st.ok = bool(blob.get("ok"))
                st.error = blob.get("error")
                st.synced_at = blob.get("synced_at")
                st.attempted_at = blob.get("attempted_at")
                st.data = blob.get("data")
            except Exception as exc:  # corrupt cache: start over for this host
                st.error = f"cache unreadable: {exc}"
        states.append(st)
    return states


def save_cache(state: HostState) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    blob = {
        "host": state.name,
        "ok": state.ok,
        "error": state.error,
        "synced_at": state.synced_at,
        "attempted_at": state.attempted_at,
        "data": state.data,
    }
    tmp = cache_path(state.name).with_suffix(".tmp")
    tmp.write_text(json.dumps(blob))
    os.replace(tmp, cache_path(state.name))


def collect_local(spec: HostSpec) -> dict:
    return collector.collect(spec.claude_dir, spec.codex_dir)


def collect_remote(spec: HostSpec, timeout: int) -> dict:
    return run_remote(spec, [], timeout)


def run_remote(spec: HostSpec, extra_args: list[str], timeout: int) -> dict:
    """Stream the collector to ``ssh <host> python3 -`` with extra CLI args; parse its JSON."""
    assert spec.ssh
    argv = ["--claude-dir", spec.claude_dir, "--codex-dir", spec.codex_dir, *extra_args]
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={min(timeout, 15)}",
        "-o", "LogLevel=ERROR",
        spec.ssh,
        f"{spec.python} - " + " ".join(_sq(a) for a in argv),
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=COLLECTOR_SRC.read_bytes(),
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"timed out after {timeout}s") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(err[-1] if err else f"ssh exit {proc.returncode}")
    try:
        return json.loads(proc.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        head = proc.stdout[:120].decode("utf-8", "replace")
        raise RuntimeError(f"collector returned non-JSON: {head!r}") from exc


def discover_targets(spec: HostSpec, tool: str, file: str, old_cwd: str | None,
                     timeout: int) -> dict:
    """Rank repatriation targets for one session, on the host that owns it."""
    if spec.is_local:
        return collector.discover(tool, file, old_cwd, spec.claude_dir, spec.codex_dir)
    args = ["--mode", "discover", "--tool", tool, "--file", file]
    if old_cwd:
        args += ["--old-cwd", old_cwd]
    return run_remote(spec, args, timeout)


def repatriate_session(spec: HostSpec, tool: str, session_id: str, file: str, old_cwd: str,
                       new_cwd: str, timeout: int, force: bool = False) -> dict:
    """Move/rewrite one session so it belongs to ``new_cwd`` (see collector.repatriate)."""
    if spec.is_local:
        return collector.repatriate(tool, session_id, file, old_cwd, new_cwd,
                                    spec.claude_dir, spec.codex_dir, force=force)
    args = ["--mode", "repatriate", "--tool", tool, "--id", session_id, "--file", file,
            "--old-cwd", old_cwd, "--new-cwd", new_cwd]
    if force:
        args.append("--force")
    return run_remote(spec, args, timeout)


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def sync_host(spec: HostSpec, state: HostState, timeout: int) -> HostState:
    """Collect one host, updating ``state`` in place (old data is kept on failure)."""
    state.attempted_at = time.time()
    try:
        data = collect_local(spec) if spec.is_local else collect_remote(spec, timeout)
        if data.get("schema") != collector.SCHEMA:
            raise RuntimeError(f"collector schema {data.get('schema')} != {collector.SCHEMA}")
        state.data = data
        state.ok = True
        state.error = None
        state.synced_at = state.attempted_at
    except Exception as exc:
        state.ok = False
        state.error = str(exc)
    finally:
        state.syncing = False
    save_cache(state)
    return state


def sync_all(config: Config, states: list[HostState], only: set[str] | None = None,
             callback=None) -> list[HostState]:
    """Collect every (selected) host in parallel. ``callback(state)`` fires per host."""
    by_name = {s.name: s for s in states}
    todo = [h for h in config.hosts if only is None or h.name in only]
    for h in todo:
        by_name.setdefault(h.name, HostState(name=h.name, group=h.group_name))

    def run(spec: HostSpec) -> HostState:
        st = by_name[spec.name]
        st.syncing = True
        sync_host(spec, st, config.settings.ssh_timeout)
        if callback:
            callback(st)
        return st

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(todo)))) as pool:
        list(pool.map(run, todo))
    return states
