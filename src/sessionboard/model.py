"""Session records as the TUI sees them: merged across hosts, with resume commands."""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass, field


@dataclass
class HostState:
    """What we know about one configured host's last collection."""

    name: str
    group: str
    ok: bool = False
    error: str | None = None
    synced_at: float | None = None  # last successful collection
    attempted_at: float | None = None
    data: dict | None = None  # raw collector output
    syncing: bool = False

    @property
    def session_count(self) -> int:
        if not self.data:
            return 0
        return len(self.data.get("claude", {}).get("sessions", [])) + len(
            self.data.get("codex", {}).get("sessions", [])
        )


@dataclass
class Session:
    tool: str  # "claude" | "codex"
    id: str
    group: str  # host or NFS group label shown to the user
    cwd: str | None
    project: str
    mtime: float
    started: float | None
    title: str | None
    first_prompt: str
    last_prompt: str
    branch: str | None
    archived: bool
    live: bool
    live_status: str | None
    live_source: str | None
    live_name: str | None
    live_on: str | None  # host name where the process was seen
    seen_on: list[str] = field(default_factory=list)
    size: int = 0
    file: str = ""

    @property
    def key(self) -> str:
        return f"{self.group}|{self.tool}|{self.id}"

    @property
    def headline(self) -> str:
        return self.title or self.first_prompt or "(no prompt)"

    @property
    def resume_command(self) -> str:
        if self.tool == "claude":
            cmd = f"claude --resume {self.id}"
        else:
            cmd = f"codex resume {self.id}"
        if self.cwd:
            return f"cd {shlex.quote(self.cwd)} && {cmd}"
        return cmd

    def matches(self, needle: str) -> bool:
        n = needle.lower()
        hay = " ".join(
            filter(
                None,
                [
                    self.project,
                    self.cwd or "",
                    self.group,
                    self.tool,
                    self.title or "",
                    self.first_prompt,
                    self.last_prompt,
                    self.branch or "",
                    self.id,
                ],
            )
        ).lower()
        return all(tok in hay for tok in n.split())


def _from_record(rec: dict, group: str, host: str) -> Session:
    return Session(
        tool=rec["tool"],
        id=str(rec["id"]),
        group=group,
        cwd=rec.get("cwd"),
        project=rec.get("project") or "?",
        mtime=float(rec.get("mtime") or 0),
        started=rec.get("started"),
        title=rec.get("title"),
        first_prompt=rec.get("first_prompt") or "",
        last_prompt=rec.get("last_prompt") or "",
        branch=rec.get("branch"),
        archived=bool(rec.get("archived")),
        live=bool(rec.get("live")),
        live_status=rec.get("live_status"),
        live_source=rec.get("live_source"),
        live_name=rec.get("live_name"),
        live_on=host if rec.get("live") else None,
        seen_on=[host],
        size=int(rec.get("size") or 0),
        file=rec.get("file") or "",
    )


def merge_sessions(hosts: list[HostState], hide_empty: bool = True) -> list[Session]:
    """Flatten every host's sessions, de-duplicating within a group (shared NFS home).

    Live wins over not-live, newest mtime wins for everything else.
    """
    merged: dict[str, Session] = {}
    for h in hosts:
        if not h.data:
            continue
        for tool in ("claude", "codex"):
            for rec in h.data.get(tool, {}).get("sessions", []):
                if hide_empty and not (rec.get("first_prompt") or rec.get("title")):
                    continue
                s = _from_record(rec, h.group, h.name)
                cur = merged.get(s.key)
                if cur is None:
                    merged[s.key] = s
                    continue
                cur.seen_on.append(h.name)
                if s.live and not cur.live:
                    cur.live, cur.live_status = True, s.live_status
                    cur.live_source, cur.live_name, cur.live_on = s.live_source, s.live_name, h.name
                if s.mtime > cur.mtime:
                    cur.mtime, cur.last_prompt, cur.size = s.mtime, s.last_prompt or cur.last_prompt, s.size
                    cur.title = s.title or cur.title
                    cur.branch = s.branch or cur.branch
    return sorted(merged.values(), key=lambda s: (not s.live, -s.mtime))


def relative_age(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "?"
    delta = (now or time.time()) - ts
    delta = max(delta, 0)
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    if delta < 30 * 86400:
        return f"{int(delta // 86400)}d"
    if delta < 365 * 86400:
        return f"{int(delta // (30 * 86400))}mo"
    return f"{int(delta // (365 * 86400))}y"


@dataclass
class ProjectGroup:
    """Sessions that belong to one project (by directory name), across tools and hosts."""

    name: str
    sessions: list[Session]

    @property
    def key(self) -> str:
        return f"project|{self.name}"

    @property
    def live(self) -> int:
        return sum(1 for s in self.sessions if s.live)

    @property
    def busy(self) -> bool:
        return any(s.live and (s.live_status or "") in ("busy", "running") for s in self.sessions)

    @property
    def mtime(self) -> float:
        return max((s.mtime for s in self.sessions), default=0.0)

    @property
    def hosts(self) -> list[str]:
        seen: list[str] = []
        for s in self.sessions:
            if s.group not in seen:
                seen.append(s.group)
        return seen

    @property
    def tools(self) -> list[str]:
        return sorted({s.tool for s in self.sessions})

    @property
    def cwds(self) -> list[tuple[str, str]]:
        """Distinct (host, cwd) pairs, in recency order."""
        seen: list[tuple[str, str]] = []
        for s in self.sessions:
            pair = (s.group, s.cwd or "?")
            if pair not in seen:
                seen.append(pair)
        return seen


def group_by_project(sessions: list[Session]) -> list[ProjectGroup]:
    """Bucket sessions by project directory name. Input order (live first, newest first)
    is preserved inside each bucket; buckets are ordered live-first, then by newest."""
    buckets: dict[str, list[Session]] = {}
    for s in sessions:
        buckets.setdefault(s.project, []).append(s)
    groups = [ProjectGroup(name, lst) for name, lst in buckets.items()]
    return sorted(groups, key=lambda g: (g.live == 0, -g.mtime))
