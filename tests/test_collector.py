import json
import os
import time
from pathlib import Path

from sessionboard import collector
from sessionboard.model import HostState, merge_sessions, relative_age


def _claude_line(**kw):
    return json.dumps(kw) + "\n"


def _write_claude_session(root, sid, cwd, prompts, title=None, live_pid=None):
    pdir = root / "projects" / cwd.replace("/", "-")
    pdir.mkdir(parents=True, exist_ok=True)
    lines = [
        _claude_line(type="mode", mode="normal", sessionId=sid),
        _claude_line(type="file-history-snapshot", messageId="x", snapshot={"big": "y" * 5000}),
        _claude_line(
            type="user", isMeta=True, cwd=cwd, sessionId=sid,
            message={"role": "user", "content": "<command-name>/clear</command-name>"},
            timestamp="2026-11-08T10:00:00.000Z",
        ),
    ]
    for i, p in enumerate(prompts):
        lines.append(_claude_line(
            type="user", cwd=cwd, sessionId=sid, gitBranch="main",
            message={"role": "user", "content": p},
            timestamp=f"2026-11-08T10:0{i}:00.000Z",
        ))
        lines.append(_claude_line(
            type="assistant", cwd=cwd, sessionId=sid,
            message={"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ))
        lines.append(_claude_line(
            type="user", cwd=cwd, sessionId=sid,
            message={"role": "user", "content": [{"type": "tool_result", "content": "stuff"}]},
        ))
    if title:
        lines.append(_claude_line(type="custom-title", customTitle=title, sessionId=sid))
    (pdir / f"{sid}.jsonl").write_text("".join(lines))
    if live_pid is not None:
        sdir = root / "sessions"
        sdir.mkdir(exist_ok=True)
        (sdir / f"{live_pid}.json").write_text(json.dumps(
            {"pid": live_pid, "sessionId": sid, "cwd": cwd, "status": "busy", "name": "proj-1"}
        ))


def test_claude_sessions(tmp_path):
    root = tmp_path / ".claude"
    _write_claude_session(root, "aaaa-1", "/home/u/proj", ["fix the bug", "now add tests"],
                          title="Bug fix", live_pid=os.getpid())
    _write_claude_session(root, "bbbb-2", "/home/u/other", ["hello world"])
    # a headless/empty session: only the meta line
    _write_claude_session(root, "cccc-3", "/home/u/other", [])
    # dead pid -> not live
    _write_claude_session(root, "dddd-4", "/home/u/dead", ["x"], live_pid=2**22 - 7)

    res = collector.collect_claude(str(root), procs=[], want_live=True)
    by_id = {s["id"]: s for s in res["sessions"]}
    assert set(by_id) == {"aaaa-1", "bbbb-2", "cccc-3", "dddd-4"}
    a = by_id["aaaa-1"]
    assert a["cwd"] == "/home/u/proj"
    assert a["project"] == "proj"
    assert a["first_prompt"] == "fix the bug"
    assert a["last_prompt"] == "now add tests"
    assert a["title"] == "Bug fix"
    assert a["branch"] == "main"
    assert a["live"] is True and a["live_status"] == "busy" and a["live_source"] == "sessions-json"
    assert a["started"] is not None
    assert by_id["bbbb-2"]["live"] is False
    assert by_id["cccc-3"]["first_prompt"] == ""
    assert by_id["dddd-4"]["live"] is False
    assert res["errors"] == []


def test_claude_skips_injected_text():
    rec = {"type": "user", "message": {"role": "user", "content": "<system-reminder>x</system-reminder>"}}
    assert collector._claude_user_line(rec) == ""
    rec = {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>hidden</system-reminder> real question"}]}}
    assert collector._claude_user_line(rec) == "real question"


def _write_codex_session(root, sid, cwd, prompts, archived=False, day="2026/11/08"):
    sub = "archived_sessions" if archived else "sessions"
    d = root / sub / day
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({
        "timestamp": "2026-11-08T09:00:00.000Z", "type": "session_meta",
        "payload": {"id": sid, "timestamp": "2026-11-08T09:00:00.000Z", "cwd": cwd,
                    "originator": "codex_cli_rs", "git": {"branch": "dev"}},
    })]
    lines.append(json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "<environment_context>...</environment_context>"}]}}))
    for p in prompts:
        lines.append(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": p}}))
        lines.append(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}}))
    path = d / f"rollout-2026-11-08T09-00-00-{sid}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_codex_sessions(tmp_path):
    root = tmp_path / ".codex"
    sid = "0199a8e0-1111-7000-8000-000000000001"
    sid2 = "0199a8e0-2222-7000-8000-000000000002"
    path = _write_codex_session(root, sid, "/work/repo", ["refactor module", "ship it"])
    _write_codex_session(root, sid2, "/work/old", ["ancient"], archived=True)
    (root / "session_index.jsonl").write_text(json.dumps({"id": sid, "thread_name": "Refactor"}) + "\n")

    procs = [{"pid": 1, "name": "codex", "cwd": "/work/repo", "files": [str(path)]}]
    res = collector.collect_codex(str(root), procs=procs, want_live=True)
    by_id = {s["id"]: s for s in res["sessions"]}
    s = by_id[sid]
    assert s["cwd"] == "/work/repo" and s["project"] == "repo" and s["branch"] == "dev"
    assert s["first_prompt"] == "refactor module"
    assert s["last_prompt"] == "ship it"
    assert s["title"] == "Refactor"
    assert s["live"] and s["live_source"] == "open-file"
    assert by_id[sid2]["archived"] is True and by_id[sid2]["live"] is False


def test_codex_cwd_heuristic(tmp_path):
    root = tmp_path / ".codex"
    sid = "0199a8e0-3333-7000-8000-000000000003"
    _write_codex_session(root, sid, "/work/repo", ["hi"])
    procs = [{"pid": 1, "name": "codex", "cwd": "/work/repo", "files": []}]
    res = collector.collect_codex(str(root), procs=procs, want_live=True)
    assert res["sessions"][0]["live"] and res["sessions"][0]["live_source"] == "cwd-heuristic"


def test_iso_to_epoch():
    assert collector._iso_to_epoch("1970-01-01T00:00:01Z") == 1.0
    assert collector._iso_to_epoch("1970-01-01T00:00:01.500Z") == 1.5
    assert collector._iso_to_epoch("1970-01-01T01:00:00+01:00") == 0.0
    assert collector._iso_to_epoch("garbage") is None


def test_classify():
    assert collector._classify("claude", "claude") == "claude"
    assert collector._classify("codex", "codex resume x") == "codex"
    assert collector._classify("node", "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js") == "claude"
    assert collector._classify("python3", "python3 foo.py") is None


def _rec(tool, sid, cwd, mtime, live=False, status=None, prompt="p"):
    return {"tool": tool, "id": sid, "cwd": cwd, "project": os.path.basename(cwd), "mtime": mtime,
            "started": None, "title": None, "first_prompt": prompt, "last_prompt": "",
            "branch": None, "archived": False, "live": live, "live_status": status,
            "live_source": "sessions-json" if live else None, "live_name": None, "size": 1,
            "file": "f"}


def test_merge_dedupes_within_group_and_prefers_live():
    now = time.time()
    a = HostState(name="login1", group="site-a", data={
        "claude": {"sessions": [_rec("claude", "s1", "/nfs/p", now - 100)]},
        "codex": {"sessions": []}})
    b = HostState(name="build7", group="site-a", data={
        "claude": {"sessions": [_rec("claude", "s1", "/nfs/p", now - 500, live=True, status="busy"),
                                _rec("claude", "s2", "/nfs/q", now - 50, prompt="")]},
        "codex": {"sessions": [_rec("codex", "c1", "/nfs/p", now - 10)]}})
    c = HostState(name="mbp", group="mbp", data={
        "claude": {"sessions": [_rec("claude", "s1", "/Users/me/p", now - 5)]},
        "codex": {"sessions": []}})
    merged = merge_sessions([a, b, c])
    keys = [s.key for s in merged]
    # live first, then recency; s2 hidden (no prompt); s1 on mbp is a different group
    assert keys[0] == "site-a|claude|s1"
    assert "site-a|claude|s2" not in keys
    assert len(merged) == 3
    s1 = merged[0]
    assert s1.live and s1.live_on == "build7" and sorted(s1.seen_on) == ["build7", "login1"]
    assert s1.mtime == now - 100  # newest mtime wins
    assert s1.resume_command == "cd /nfs/p && claude --resume s1"
    codex = next(s for s in merged if s.tool == "codex")
    assert codex.resume_command == "cd /nfs/p && codex resume c1"
    assert len(merge_sessions([a, b, c], hide_empty=False)) == 4


def test_resume_command_quotes_paths():
    st = HostState(name="h", group="h", data={
        "claude": {"sessions": [_rec("claude", "x", "/tmp/my proj", 1.0)]}, "codex": {"sessions": []}})
    assert merge_sessions([st])[0].resume_command == "cd '/tmp/my proj' && claude --resume x"


def test_relative_age():
    now = 1_000_000.0
    assert relative_age(now - 5, now) == "5s"
    assert relative_age(now - 120, now) == "2m"
    assert relative_age(now - 7200, now) == "2h"
    assert relative_age(now - 3 * 86400, now) == "3d"
    assert relative_age(None, now) == "?"


def test_group_by_project():
    from sessionboard.model import group_by_project
    now = time.time()
    st = HostState(name="h", group="h", data={"claude": {"sessions": [
        _rec("claude", "a", "/x/alpha", now - 10),
        _rec("claude", "b", "/x/beta", now - 5000, live=True, status="busy"),
        _rec("codex", "c", "/y/alpha", now - 20),
    ]}, "codex": {"sessions": []}})
    groups = group_by_project(merge_sessions([st]))
    assert [g.name for g in groups] == ["beta", "alpha"]  # live first
    alpha = groups[1]
    assert len(alpha.sessions) == 2 and alpha.tools == ["claude", "codex"]
    assert alpha.cwds == [("h", "/x/alpha"), ("h", "/y/alpha")]
    assert groups[0].busy and groups[0].live == 1


# ---------------------------------------------------------------- repatriation
def test_encode_claude_project():
    assert collector.encode_claude_project("/Users/alice/Projects/x") == "-Users-alice-Projects-x"
    assert collector.encode_claude_project("/Users/alice/.local/share/chezmoi") == "-Users-alice--local-share-chezmoi"


def _claude_tool_line(cwd, cmd=None, file_path=None):
    inp = {}
    if cmd:
        inp["command"] = cmd
    if file_path:
        inp["file_path"] = file_path
    return _claude_line(type="assistant", cwd=cwd, message={
        "role": "assistant", "content": [{"type": "tool_use", "name": "x", "input": inp}]})


def test_discover_and_repatriate_claude(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    proj = home / "Projects" / "newapp"
    (proj / "src").mkdir(parents=True)
    (proj / ".git").mkdir()
    (home / "Projects" / "other").mkdir()
    root = home / ".claude"
    old_cwd = str(home / "scratch")
    (home / "scratch").mkdir()
    sid = "aaaa-1"
    _write_claude_session(root, sid, old_cwd, ["build me an app"])
    path = root / "projects" / old_cwd.replace("/", "-") / f"{sid}.jsonl"
    with path.open("a") as fh:
        fh.write(_claude_tool_line(old_cwd, cmd=f"mkdir -p {proj} && cd {proj} && git init"))
        for i in range(5):
            fh.write(_claude_tool_line(old_cwd, file_path=f"{proj}/src/mod{i}.py"))
        fh.write(_claude_tool_line(old_cwd, file_path=f"{home}/Projects/other/README.md"))
    # sidecar dir travels along
    side = path.with_suffix("")
    side.mkdir()
    (side / "sub.jsonl").write_text(_claude_line(type="user", cwd=old_cwd, message={"role": "user", "content": "x"}))
    hist = root / "history.jsonl"
    hist.write_text(json.dumps({"display": "build me an app", "project": old_cwd, "sessionId": sid}) + "\n")

    found = collector.discover("claude", str(path), old_cwd, str(root), str(home / ".codex"))
    assert found["candidates"][0]["path"] == str(proj)
    assert found["candidates"][0]["git"] and found["candidates"][0]["explicit"] >= 1

    # a live session is refused
    monkeypatch.setattr(collector, "_is_live_here", lambda *a: "pretend live")
    rep = collector.repatriate("claude", sid, str(path), old_cwd, str(proj), str(root), str(home / ".codex"))
    assert not rep["ok"] and "live" in rep["error"]
    monkeypatch.setattr(collector, "_is_live_here", lambda *a: None)

    rep = collector.repatriate("claude", sid, str(path), old_cwd, str(proj), str(root), str(home / ".codex"))
    assert rep["ok"], rep
    new_dir = root / "projects" / collector.encode_claude_project(str(proj))
    moved = new_dir / f"{sid}.jsonl"
    assert moved.exists() and not path.exists()
    assert Path(rep["backup"]).exists()
    assert rep["rewritten"] >= 2
    for raw in moved.read_text().splitlines():
        rec = json.loads(raw)
        if "cwd" in rec:
            assert rec["cwd"] == str(proj)
    assert (new_dir / sid / "sub.jsonl").exists()
    assert json.loads(hist.read_text())["project"] == str(proj)
    # the collector now files it under the new project
    res = collector.collect_claude(str(root), procs=[], want_live=False)
    assert [s["project"] for s in res["sessions"]] == ["newapp"]
    # second attempt: nothing left to move
    rep2 = collector.repatriate("claude", sid, str(path), old_cwd, str(proj), str(root), str(home / ".codex"))
    assert not rep2["ok"]


def test_repatriate_codex(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    root = home / ".codex"
    new = home / "work" / "repo"
    new.mkdir(parents=True)
    sid = "0199a8e0-4444-7000-8000-000000000004"
    path = _write_codex_session(root, sid, "/old/place", ["go"])
    monkeypatch.setattr(collector, "_is_live_here", lambda *a: None)
    rep = collector.repatriate("codex", sid, str(path), "/old/place", str(new), str(home / ".claude"), str(root))
    assert rep["ok"], rep
    first = json.loads(path.read_text().splitlines()[0])
    assert first["payload"]["cwd"] == str(new)
    assert rep["rewritten"] == 1
    res = collector.collect_codex(str(root), procs=[], want_live=False)
    assert res["sessions"][0]["project"] == "repo"
