#!/usr/bin/env python3
"""sessionboard collector: summarise the Claude Code and Codex sessions on this host.

Standalone and stdlib-only on purpose: sessionboard ships this file over ssh and runs
it with whatever ``python3`` the remote has (3.6 or newer), so it must not import
anything from the package and must avoid syntax newer than 3.6.

Usage:
    collector.py [--claude-dir DIR] [--codex-dir DIR] [--no-live] [--pretty]

Prints one JSON document on stdout:

    {"schema": 1, "hostname": ..., "collected_at": <epoch>,
     "claude": {"dir": ..., "exists": bool, "sessions": [...], "errors": [...]},
     "codex":  {"dir": ..., "exists": bool, "sessions": [...], "errors": [...]},
     "processes": [{"pid":..,"name":..,"cwd":..}, ...]}

Each session record:

    tool, id, cwd, project, file, size, mtime, started, title, first_prompt,
    last_prompt, branch, archived, live, live_status, live_source, live_name
"""

import argparse
import glob
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time

SCHEMA = 1
TAIL_BYTES = 256 * 1024
HEAD_SCAN_LIMIT = 8 * 1024 * 1024  # stop looking for the first prompt after this much
PROMPT_MAX = 400
RECENT_SECONDS = 15 * 60

# Text that Claude Code / Codex inject as "user" turns but that a human never typed.
CLAUDE_SKIP_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<system-reminder>",
    "<task-notification>",
    "[Request interrupted",
    "Caveat: The messages below",
)
CODEX_SKIP_PREFIXES = (
    "<environment_context>",
    "<user_instructions>",
    "<permissions",
    "<turn_aborted>",
    "<app_context>",
    "# AGENTS.md",
    "<collaboration_mode",
    "<skills",
)
TAG_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
PASTE_RE = re.compile(r"</?pasted_content[^>]*>")
WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- helpers
def _expand(path):
    return os.path.abspath(os.path.expanduser(path))


def _clean_prompt(text):
    if not text:
        return ""
    text = TAG_RE.sub("", text)
    text = PASTE_RE.sub("", text)
    text = WS_RE.sub(" ", text).strip()
    if len(text) > PROMPT_MAX:
        text = text[: PROMPT_MAX - 1] + "…"
    return text


def _iso_to_epoch(value):
    """Parse the ISO-8601 timestamps both tools write ('2026-11-08T11:18:48.123Z')."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    # Python < 3.11 fromisoformat chokes on fractional seconds of != 3/6 digits;
    # do it by hand to stay 3.6-compatible.
    m = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$",
        value.strip(),
    )
    if not m:
        return None
    import calendar

    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    frac = m.group(7)
    tz = m.group(8)
    epoch = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
    if frac:
        epoch += int(frac[:6].ljust(6, "0")) / 1e6
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        tz = tz[1:].replace(":", "")
        epoch -= sign * (int(tz[:2]) * 3600 + int(tz[2:]) * 60)
    return epoch


def _open_maybe_zst(path):
    """Return a binary file object for .jsonl or .jsonl.zst, or None if zstd is unavailable."""
    if not path.endswith(".zst"):
        return open(path, "rb")
    try:
        from compression import zstd  # Python 3.14+

        return zstd.open(path, "rb")
    except Exception:
        pass
    try:
        import zstandard

        return zstandard.open(path, "rb")
    except Exception:
        return None


def _iter_head_lines(path, limit=HEAD_SCAN_LIMIT):
    fh = _open_maybe_zst(path)
    if fh is None:
        return
    read = 0
    try:
        for raw in fh:
            read += len(raw)
            yield raw
            if read > limit:
                break
    finally:
        fh.close()


def _tail_lines(path, nbytes=TAIL_BYTES):
    """Return the complete lines in the last ``nbytes`` of a file (whole file if .zst)."""
    if path.endswith(".zst"):
        fh = _open_maybe_zst(path)
        if fh is None:
            return []
        try:
            data = fh.read()
        finally:
            fh.close()
        return data.splitlines()
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > nbytes:
            fh.seek(size - nbytes)
            data = fh.read()
            # the first line is almost certainly partial
            nl = data.find(b"\n")
            data = data[nl + 1 :] if nl >= 0 else b""
        else:
            data = fh.read()
    return data.splitlines()


def _loads(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


# --------------------------------------------------------------------------- processes
def _list_processes():
    """Return [{pid, name, cwd, files:[...]}] for running claude / codex processes."""
    procs = []
    system = platform.system()
    if system == "Linux":
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            base = "/proc/%d" % pid
            try:
                with open(base + "/comm", "rb") as fh:
                    comm = fh.read().decode("utf-8", "replace").strip()
                with open(base + "/cmdline", "rb") as fh:
                    cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
            except Exception:
                continue
            name = _classify(comm, cmdline)
            if not name:
                continue
            try:
                cwd = os.readlink(base + "/cwd")
            except Exception:
                cwd = None
            files = []
            try:
                for fd in os.listdir(base + "/fd"):
                    try:
                        target = os.readlink(base + "/fd/" + fd)
                    except Exception:
                        continue
                    if "rollout-" in target or target.endswith(".jsonl"):
                        files.append(target)
            except Exception:
                pass
            procs.append({"pid": pid, "name": name, "cwd": cwd, "files": files})
        return procs

    # macOS / BSD: ps for the list, lsof for cwd + open files.
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,comm=,args="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            timeout=10,
        ).stdout
    except Exception:
        return procs
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        comm = parts[1]
        args = parts[2] if len(parts) > 2 else ""
        name = _classify(os.path.basename(comm), args)
        if name:
            procs.append({"pid": pid, "name": name, "cwd": None, "files": []})
    if not procs:
        return procs
    pids = ",".join(str(p["pid"]) for p in procs)
    try:
        out = subprocess.run(
            ["lsof", "-n", "-P", "-p", pids, "-Fpfn"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            timeout=20,
        ).stdout
    except Exception:
        return procs
    by_pid = dict((p["pid"], p) for p in procs)
    cur = None
    fd = None
    for line in out.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            try:
                cur = by_pid.get(int(val))
            except ValueError:
                cur = None
        elif tag == "f":
            fd = val
        elif tag == "n" and cur is not None:
            if fd == "cwd":
                cur["cwd"] = val
            elif "rollout-" in val or val.endswith(".jsonl"):
                cur["files"].append(val)
    return procs


def _classify(comm, cmdline):
    """'claude' / 'codex' for an agent process, else None. Ignores helpers like MCP servers."""
    c = (comm or "").lower()
    args = (cmdline or "").lower()
    if c == "codex" or c.startswith("codex-"):
        return "codex"
    if c == "claude":
        return "claude"
    # node-based installs: the binary is node, the script path ends in /claude or /codex
    first = args.split(" ", 1)
    script = ""
    if len(first) == 2:
        script = first[1].split(" ", 1)[0]
    base = os.path.basename(script)
    if base == "claude" or base.endswith("/claude") or "claude-code/cli.js" in script:
        return "claude"
    if base == "codex" or "codex/bin/codex" in script:
        return "codex"
    return None


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- claude
def _claude_text(message):
    """Human-typed text of a Claude 'user' message, or '' if it is tool output / injected."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(parts)
    else:
        return ""
    text = TAG_RE.sub("", text)
    stripped = text.lstrip()
    for prefix in CLAUDE_SKIP_PREFIXES:
        if stripped.startswith(prefix):
            return ""
    return _clean_prompt(text)


def _claude_user_line(rec):
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return ""
    return _claude_text(rec.get("message"))


def _claude_session(path):
    sess = {
        "tool": "claude",
        "id": os.path.basename(path).split(".")[0],
        "cwd": None,
        "file": path,
        "size": os.path.getsize(path),
        "mtime": os.path.getmtime(path),
        "started": None,
        "title": None,
        "first_prompt": "",
        "last_prompt": "",
        "branch": None,
        "archived": False,
        "live": False,
        "live_status": None,
        "live_source": None,
        "live_name": None,
    }
    for raw in _iter_head_lines(path):
        rec = _loads(raw)
        if not rec:
            continue
        if sess["cwd"] is None and rec.get("cwd"):
            sess["cwd"] = rec["cwd"]
        if sess["branch"] is None and rec.get("gitBranch"):
            sess["branch"] = rec["gitBranch"]
        if sess["started"] is None and rec.get("timestamp"):
            sess["started"] = _iso_to_epoch(rec["timestamp"])
        if rec.get("type") == "custom-title" and rec.get("customTitle"):
            sess["title"] = str(rec["customTitle"])
        if not sess["first_prompt"]:
            text = _claude_user_line(rec)
            if text:
                sess["first_prompt"] = text
        if sess["first_prompt"] and sess["cwd"]:
            break
    for raw in reversed(_tail_lines(path)):
        rec = _loads(raw)
        if not rec:
            continue
        if rec.get("type") == "custom-title" and rec.get("customTitle"):
            sess["title"] = str(rec["customTitle"])
        if rec.get("type") == "summary" and rec.get("summary") and not sess["title"]:
            sess["title"] = str(rec["summary"])
        if sess["cwd"] is None and rec.get("cwd"):
            sess["cwd"] = rec["cwd"]
        if sess["branch"] is None and rec.get("gitBranch"):
            sess["branch"] = rec["gitBranch"]
        if not sess["last_prompt"]:
            text = _claude_user_line(rec)
            if text:
                sess["last_prompt"] = text
        if sess["last_prompt"] and sess["cwd"]:
            break
    if sess["cwd"] is None:
        sess["cwd"] = _decode_project_dir(os.path.basename(os.path.dirname(path)))
    sess["project"] = os.path.basename(sess["cwd"].rstrip("/")) if sess["cwd"] else "?"
    return sess


def _decode_project_dir(name):
    """Best-effort reverse of Claude Code's path encoding ('-Users-alice-Projects-x')."""
    if not name:
        return None
    return name.replace("-", "/")


def _claude_live_map(claude_dir, errors):
    """sessionId -> {status, name, cwd, pid} for Claude processes still running here."""
    live = {}
    sess_dir = os.path.join(claude_dir, "sessions")
    if not os.path.isdir(sess_dir):
        return live
    for path in glob.glob(os.path.join(sess_dir, "*.json")):
        try:
            with open(path, "rb") as fh:
                rec = json.loads(fh.read().decode("utf-8", "replace"))
        except Exception:
            continue
        pid = rec.get("pid")
        sid = rec.get("sessionId")
        if not sid or not isinstance(pid, int):
            continue
        if not _pid_alive(pid):
            continue
        live[sid] = {
            "status": rec.get("status"),
            "name": rec.get("name"),
            "cwd": rec.get("cwd"),
            "pid": pid,
            "updated": (rec.get("updatedAt") or 0) / 1000.0 or None,
        }
    return live


def collect_claude(claude_dir, procs, want_live=True):
    result = {"dir": claude_dir, "exists": os.path.isdir(claude_dir), "sessions": [], "errors": []}
    projects = os.path.join(claude_dir, "projects")
    if not os.path.isdir(projects):
        return result
    live = _claude_live_map(claude_dir, result["errors"]) if want_live else {}
    for proj in sorted(os.listdir(projects)):
        pdir = os.path.join(projects, proj)
        if not os.path.isdir(pdir):
            continue
        for path in glob.glob(os.path.join(pdir, "*.jsonl")):
            base = os.path.basename(path)
            if base.startswith("agent-"):
                continue
            try:
                sess = _claude_session(path)
            except Exception as exc:  # one bad file must not kill the host
                result["errors"].append("%s: %s" % (path, exc))
                continue
            info = live.get(sess["id"])
            if info:
                sess["live"] = True
                sess["live_status"] = info.get("status")
                sess["live_name"] = info.get("name")
                sess["live_source"] = "sessions-json"
            result["sessions"].append(sess)
    if want_live:
        _apply_cwd_heuristic(result["sessions"], [p for p in procs if p["name"] == "claude"])
    return result


# --------------------------------------------------------------------------- codex
def _codex_user_text(rec):
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return ""
    text = None
    if rec.get("type") == "event_msg" and payload.get("type") == "user_message":
        text = payload.get("message")
    elif rec.get("type") == "response_item" and payload.get("type") == "message":
        if payload.get("role") != "user":
            return ""
        parts = []
        for block in payload.get("content") or []:
            if isinstance(block, dict) and block.get("type") in ("input_text", "text"):
                parts.append(block.get("text") or "")
        text = "\n".join(parts)
    if not text or not isinstance(text, str):
        return ""
    stripped = text.lstrip()
    for prefix in CODEX_SKIP_PREFIXES:
        if stripped.startswith(prefix):
            return ""
    return _clean_prompt(text)


ROLLOUT_ID_RE = re.compile(
    r"rollout-.*?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def _codex_id_from_name(path):
    m = ROLLOUT_ID_RE.search(os.path.basename(path))
    return m.group(1) if m else None


def _codex_session(path, archived):
    sess = {
        "tool": "codex",
        "id": _codex_id_from_name(path),
        "cwd": None,
        "file": path,
        "size": os.path.getsize(path),
        "mtime": os.path.getmtime(path),
        "started": None,
        "title": None,
        "first_prompt": "",
        "last_prompt": "",
        "branch": None,
        "archived": archived,
        "live": False,
        "live_status": None,
        "live_source": None,
        "live_name": None,
    }
    lines_seen = 0
    for raw in _iter_head_lines(path):
        rec = _loads(raw)
        lines_seen += 1
        if not rec:
            continue
        if rec.get("type") == "session_meta":
            payload = rec.get("payload") or {}
            sess["id"] = payload.get("id") or sess["id"]
            sess["cwd"] = payload.get("cwd") or sess["cwd"]
            sess["started"] = _iso_to_epoch(payload.get("timestamp") or rec.get("timestamp"))
            git = payload.get("git") or {}
            if isinstance(git, dict):
                sess["branch"] = git.get("branch")
            continue
        if sess["started"] is None and rec.get("timestamp"):
            sess["started"] = _iso_to_epoch(rec["timestamp"])
        if not sess["first_prompt"]:
            text = _codex_user_text(rec)
            if text:
                sess["first_prompt"] = text
        if sess["first_prompt"] and sess["cwd"]:
            break
    tail = _tail_lines(path)
    for raw in reversed(tail):
        rec = _loads(raw)
        if not rec:
            continue
        if not sess["last_prompt"]:
            text = _codex_user_text(rec)
            if text:
                sess["last_prompt"] = text
                break
    if sess["id"] is None:
        sess["id"] = os.path.basename(path)
    sess["project"] = os.path.basename(sess["cwd"].rstrip("/")) if sess["cwd"] else "?"
    return sess


def _codex_titles(codex_dir, errors):
    """id -> user-given thread name, from session_index.jsonl and/or the sqlite catalog."""
    titles = {}
    index = os.path.join(codex_dir, "session_index.jsonl")
    if os.path.isfile(index):
        try:
            with open(index, "rb") as fh:
                for raw in fh:
                    rec = _loads(raw)
                    if not isinstance(rec, dict):
                        continue
                    sid = rec.get("id") or rec.get("thread_id") or rec.get("session_id")
                    name = rec.get("thread_name") or rec.get("name") or rec.get("title")
                    if sid and name:
                        titles[sid] = str(name)
        except Exception as exc:
            errors.append("session_index.jsonl: %s" % exc)
    for db in glob.glob(os.path.join(codex_dir, "sqlite", "*.db")):
        try:
            import sqlite3

            conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
            try:
                rows = conn.execute(
                    "select thread_id, display_title from local_thread_catalog "
                    "where display_title != ''"
                ).fetchall()
            finally:
                conn.close()
            for sid, title in rows:
                titles.setdefault(sid, title)
        except Exception:
            # table may not exist in this Codex version; titles are optional
            pass
    return titles


def collect_codex(codex_dir, procs, want_live=True):
    result = {"dir": codex_dir, "exists": os.path.isdir(codex_dir), "sessions": [], "errors": []}
    if not result["exists"]:
        return result
    roots = [(os.path.join(codex_dir, "sessions"), False), (os.path.join(codex_dir, "archived_sessions"), True)]
    skipped_zst = 0
    for root, archived in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.startswith("rollout-"):
                    continue
                if not (name.endswith(".jsonl") or name.endswith(".jsonl.zst")):
                    continue
                path = os.path.join(dirpath, name)
                if name.endswith(".zst") and _open_maybe_zst(path) is None:
                    skipped_zst += 1
                    continue
                try:
                    result["sessions"].append(_codex_session(path, archived))
                except Exception as exc:
                    result["errors"].append("%s: %s" % (path, exc))
    if skipped_zst:
        result["errors"].append(
            "%d compressed (.zst) rollouts skipped: no zstd support in this python" % skipped_zst
        )
    titles = _codex_titles(codex_dir, result["errors"])
    for sess in result["sessions"]:
        if sess["id"] in titles:
            sess["title"] = titles[sess["id"]]
    if want_live:
        codex_procs = [p for p in procs if p["name"] == "codex"]
        # exact: a codex process holds the rollout file open
        open_ids = {}
        for p in codex_procs:
            for f in p["files"]:
                sid = _codex_id_from_name(f)
                if sid:
                    open_ids[sid] = p
        for sess in result["sessions"]:
            p = open_ids.get(sess["id"])
            if p:
                sess["live"] = True
                sess["live_source"] = "open-file"
                sess["live_status"] = "running"
        _apply_cwd_heuristic(result["sessions"], codex_procs)
    return result


def _apply_cwd_heuristic(sessions, procs):
    """Mark the newest recently-modified session in a running agent's cwd as live.

    Used when there is no exact process -> session link (Codex that does not keep the
    rollout open, or old Claude Code without sessions/*.json).
    """
    if not procs:
        return
    now = time.time()
    by_cwd = {}
    for p in procs:
        if p.get("cwd"):
            by_cwd.setdefault(os.path.normpath(p["cwd"]), []).append(p)
    claimed = set()
    for sess in sessions:
        if sess["live"] and sess.get("cwd"):
            claimed.add(os.path.normpath(sess["cwd"]))
    for cwd, plist in by_cwd.items():
        if cwd in claimed:
            continue
        candidates = [
            s
            for s in sessions
            if s.get("cwd") and os.path.normpath(s["cwd"]) == cwd and not s["live"]
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda s: s["mtime"], reverse=True)
        for s in candidates[: len(plist)]:
            if now - s["mtime"] <= 6 * 3600:
                s["live"] = True
                s["live_source"] = "cwd-heuristic"
                s["live_status"] = "running"


# --------------------------------------------------------------------------- main
def collect(claude_dir="~/.claude", codex_dir="~/.codex", want_live=True):
    claude_dir = _expand(claude_dir)
    codex_dir = _expand(codex_dir)
    procs = _list_processes() if want_live else []
    return {
        "schema": SCHEMA,
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "user": os.environ.get("USER") or os.environ.get("LOGNAME"),
        "collected_at": time.time(),
        "claude": collect_claude(claude_dir, procs, want_live),
        "codex": collect_codex(codex_dir, procs, want_live),
        "processes": [
            {"pid": p["pid"], "name": p["name"], "cwd": p["cwd"]} for p in procs
        ],
    }

# --------------------------------------------------------------------------- repatriation
# A session that started in one directory but built its project somewhere else can be
# moved ("repatriated") to that directory: for Claude Code the transcript moves into the
# right ~/.claude/projects/<encoded path>/ folder and its `cwd` fields are rewritten; for
# Codex the rollout stays where it is (they are filed by date) and its `cwd` is rewritten.

PATH_RE = re.compile(r"(?<![\w/.-])(~?/(?:[A-Za-z0-9._@+-]+/)*[A-Za-z0-9._@+-]+)")
EXPLICIT_RE = re.compile(
    r"(?:^|[;&|(]\s*|\s)(?:cd|mkdir(?:\s+-p)?|git\s+-C|git\s+init|pushd|cargo\s+new|uv\s+init|"
    r"npm\s+init|cp\s+-r|rsync)\s+(?:--?\S+\s+)*(~?/[A-Za-z0-9._@+/-]+)"
)
BLACKLIST_PARTS = ("node_modules", ".venv", "venv", "site-packages", "__pycache__", ".git",
                   "target", "dist", "build", ".cache", ".npm", ".cargo", ".rustup", "Library")
BLACKLIST_PREFIXES = ("/tmp", "/private", "/usr", "/bin", "/sbin", "/etc", "/var", "/nix",
                      "/proc", "/sys", "/dev", "/System", "/Library", "/Applications", "/opt")
CONTAINER_NAMES = ("Projects", "projects", "src", "code", "Code", "work", "repos", "dev",
                   "git", "Documents", "Developer", "workspace", "workspaces", "scratch")
GENERIC_DIRS = ("src", "lib", "tests", "test", "docs", "doc", "bin", "scripts", "app", "apps",
                "pkg", "cmd", "internal", "public", "static", "assets", "config", "data")


def encode_claude_project(cwd):
    """Claude Code's project-directory name for a cwd: every non-alphanumeric char -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _home():
    return os.path.expanduser("~")


def _iter_all_lines(path):
    fh = _open_maybe_zst(path)
    if fh is None:
        return
    try:
        for raw in fh:
            yield raw
    finally:
        fh.close()


def _tool_texts(tool, path):
    """Yield (text, is_command) for every tool invocation the agent made in a transcript."""
    for raw in _iter_all_lines(path):
        rec = _loads(raw)
        if not rec:
            continue
        if tool == "claude":
            if rec.get("type") != "assistant":
                continue
            content = (rec.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    if isinstance(inp, dict):
                        cmd = inp.get("command")
                        if isinstance(cmd, str):
                            yield cmd, True
                        for key in ("file_path", "path", "notebook_path", "cwd", "directory"):
                            v = inp.get(key)
                            if isinstance(v, str):
                                yield v, False
                        yield json.dumps(inp, ensure_ascii=False), False
        else:
            payload = rec.get("payload")
            if not isinstance(payload, dict):
                continue
            ptype = payload.get("type")
            if rec.get("type") == "response_item" and ptype in ("function_call", "custom_tool_call",
                                                                 "local_shell_call"):
                args = payload.get("arguments") or payload.get("input")
                if isinstance(args, str):
                    try:
                        parsed = json.loads(args)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, dict):
                        cmd = parsed.get("command") or parsed.get("cmd")
                        if isinstance(cmd, list):
                            cmd = " ".join(str(c) for c in cmd)
                        if isinstance(cmd, str):
                            yield cmd, True
                        if isinstance(parsed.get("workdir"), str):
                            yield parsed["workdir"], False
                    yield args, False
                action = payload.get("action")
                if isinstance(action, dict):
                    cmd = action.get("command")
                    if isinstance(cmd, list):
                        yield " ".join(str(c) for c in cmd), True
                    if isinstance(action.get("working_directory"), str):
                        yield action["working_directory"], False


def _normalise_path(p, home):
    p = p.rstrip(".,;:'\")]}")
    if p.startswith("~/"):
        p = home + p[1:]
    return os.path.normpath(p)


def _is_blacklisted(p, home):
    if p == "/":
        return True
    if p.startswith(home + "/"):
        # inside $HOME the system prefixes do not apply (a home under /var is fine),
        # but dot-directories (~/.claude, ~/.cache, ...) are never project roots
        rel = p[len(home) + 1:]
        if rel.split("/")[0].startswith("."):
            return True
    elif p.startswith(BLACKLIST_PREFIXES):
        return True
    return any(part in BLACKLIST_PARTS for part in p.split("/"))


def _known_cwds(claude_dir, codex_dir):
    """cwd of every session on this host (cheap: head of each transcript)."""
    known = {}
    projects = os.path.join(claude_dir, "projects")
    if os.path.isdir(projects):
        for path in glob.glob(os.path.join(projects, "*", "*.jsonl")):
            try:
                with open(path, "rb") as fh:
                    chunk = fh.read(65536)
            except Exception:
                continue
            m = re.search(rb'"cwd":"((?:[^"\\]|\\.)*)"', chunk)
            if m:
                try:
                    cwd = json.loads('"' + m.group(1).decode("utf-8", "replace") + '"')
                except Exception:
                    continue
                known[cwd] = known.get(cwd, 0) + 1
    sessions = os.path.join(codex_dir, "sessions")
    if os.path.isdir(sessions):
        for dirpath, _d, files in os.walk(sessions):
            for name in files:
                if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                    continue
                try:
                    with open(os.path.join(dirpath, name), "rb") as fh:
                        first = fh.readline()
                except Exception:
                    continue
                rec = _loads(first)
                if rec and rec.get("type") == "session_meta":
                    cwd = (rec.get("payload") or {}).get("cwd")
                    if cwd:
                        known[cwd] = known.get(cwd, 0) + 1
    return known


def discover(tool, path, old_cwd, claude_dir="~/.claude", codex_dir="~/.codex", limit=8):
    """Rank the directories a transcript worked in as repatriation targets."""
    claude_dir, codex_dir = _expand(claude_dir), _expand(codex_dir)
    home = _home()
    old_cwd = os.path.normpath(old_cwd) if old_cwd else None
    containers = {"/", home}
    containers.update(os.path.join(home, c) for c in CONTAINER_NAMES)
    containers.update(("/home", "/Users", "/opt", "/srv", "/data", "/work", "/workspace"))
    if old_cwd:
        containers.add(old_cwd)
    score = {}
    explicit = {}
    seen_paths = 0
    for text, is_cmd in _tool_texts(tool, path):
        hits = PATH_RE.findall(text)
        if is_cmd:
            for m in EXPLICIT_RE.finditer(text):
                p = _normalise_path(m.group(1), home)
                explicit[p] = explicit.get(p, 0) + 1
        for raw in hits:
            p = _normalise_path(raw, home)
            if not p.startswith("/") or _is_blacklisted(p, home):
                continue
            seen_paths += 1
            cur = p
            while cur and cur not in containers and cur != "/":
                score[cur] = score.get(cur, 0) + 1
                cur = os.path.dirname(cur)
    known = _known_cwds(claude_dir, codex_dir)
    cands = []
    for p, n in score.items():
        if p == old_cwd or p in containers or not os.path.isdir(p):
            continue
        if _is_blacklisted(p, home):
            continue
        parent = os.path.dirname(p)
        depth = p.count("/")
        has_git = os.path.isdir(os.path.join(p, ".git"))
        is_container_child = parent in containers
        generic = os.path.basename(p) in GENERIC_DIRS
        others = known.get(p, 0)
        exp = explicit.get(p, 0)
        # weighted: volume of work there, explicit cd/mkdir, and "looks like a project root"
        rank = n + 3 * exp
        if has_git:
            rank += 15
        if is_container_child and not generic:
            rank += 15
        if others:
            rank += 5
        if generic:
            rank -= 10
        cands.append({
            "path": p, "paths": n, "explicit": exp, "git": has_git,
            "other_sessions": others, "container_child": is_container_child,
            "inside_old": bool(old_cwd and p.startswith(old_cwd + "/")),
            "score": rank, "depth": depth,
        })
    cands.sort(key=lambda c: (c["score"], -c["depth"]), reverse=True)
    return {
        "tool": tool, "file": path, "old_cwd": old_cwd, "home": home,
        "paths_seen": seen_paths, "candidates": cands[:limit],
    }


def _is_live_here(tool, session_id, path, old_cwd, claude_dir):
    """Refuse to touch a session that has a process behind it on this host."""
    procs = _list_processes()
    if tool == "claude":
        if session_id in _claude_live_map(claude_dir, []):
            return "a Claude Code process still owns this session"
    else:
        for p in procs:
            if p["name"] == "codex" and any(_codex_id_from_name(f) == session_id for f in p["files"]):
                return "a Codex process has the rollout open"
    recent = time.time() - os.path.getmtime(path) < 300
    for p in procs:
        if (recent and p["name"] == tool and p.get("cwd") and old_cwd
                and os.path.normpath(p["cwd"]) == old_cwd):
            return "a %s process is running in %s and the transcript changed <5 min ago" % (tool, old_cwd)
    return None


def _rewrite_cwd_lines(src, dst, old_cwd, new_cwd):
    """Copy src -> dst replacing the top-level "cwd" value; returns lines rewritten."""
    old_json = json.dumps(old_cwd)[1:-1]
    new_json = json.dumps(new_cwd)[1:-1]
    needle = ('"cwd":"%s"' % old_json).encode("utf-8")
    repl = ('"cwd":"%s"' % new_json).encode("utf-8")
    needle_sp = ('"cwd": "%s"' % old_json).encode("utf-8")
    repl_sp = ('"cwd": "%s"' % new_json).encode("utf-8")
    count = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for raw in fin:
            if needle in raw:
                raw = raw.replace(needle, repl)
                count += 1
            elif needle_sp in raw:
                raw = raw.replace(needle_sp, repl_sp)
                count += 1
            fout.write(raw)
        fout.flush()
        os.fsync(fout.fileno())
    return count


def _timestamp():
    return time.strftime("%Y%m%d-%H%M%S")


def repatriate(tool, session_id, path, old_cwd, new_cwd, claude_dir="~/.claude",
               codex_dir="~/.codex", force=False):
    """Move/rewrite one session so it belongs to new_cwd. Returns a JSON-able report."""
    claude_dir, codex_dir = _expand(claude_dir), _expand(codex_dir)
    new_cwd = os.path.normpath(_expand(new_cwd))
    old_cwd = os.path.normpath(old_cwd)
    report = {"ok": False, "tool": tool, "id": session_id, "old_cwd": old_cwd,
              "new_cwd": new_cwd, "warnings": [], "rewritten": 0}
    if not os.path.isfile(path):
        report["error"] = "transcript not found: %s" % path
        return report
    if path.endswith(".zst"):
        report["error"] = "compressed (.zst) rollout; decompress it first"
        return report
    if not os.path.isdir(new_cwd):
        report["error"] = "target directory does not exist on this host: %s" % new_cwd
        return report
    if new_cwd == old_cwd:
        report["error"] = "session already belongs to %s" % new_cwd
        return report
    if not force:
        why = _is_live_here(tool, session_id, path, old_cwd, claude_dir)
        if why:
            report["error"] = "session looks live: %s" % why
            return report
    stamp = _timestamp()
    if tool == "claude":
        new_dir = os.path.join(claude_dir, "projects", encode_claude_project(new_cwd))
        dst = os.path.join(new_dir, os.path.basename(path))
        if os.path.exists(dst):
            report["error"] = "a session with this id already exists in %s" % new_dir
            return report
        if not os.path.isdir(new_dir):
            os.makedirs(new_dir)
        tmp = dst + ".tmp-repatriate"
        report["rewritten"] = _rewrite_cwd_lines(path, tmp, old_cwd, new_cwd)
        os.rename(tmp, dst)
        backup = path + ".bak-" + stamp
        os.rename(path, backup)
        report["backup"] = backup
        report["moved_to"] = dst
        # sidecar directory (subagent transcripts etc.) travels with the session
        side = os.path.splitext(path)[0]
        if os.path.isdir(side):
            side_dst = os.path.join(new_dir, os.path.basename(side))
            if os.path.exists(side_dst):
                report["warnings"].append("sidecar dir already exists at %s; left in place" % side_dst)
            else:
                os.rename(side, side_dst)
                for dirpath, _d, files in os.walk(side_dst):
                    for name in files:
                        if name.endswith(".jsonl"):
                            fp = os.path.join(dirpath, name)
                            tmp = fp + ".tmp-repatriate"
                            try:
                                report["rewritten"] += _rewrite_cwd_lines(fp, tmp, old_cwd, new_cwd)
                                os.rename(tmp, fp)
                            except Exception as exc:
                                report["warnings"].append("%s: %s" % (fp, exc))
                report["sidecar_moved"] = side_dst
        # prompt history: keep the project label in step
        hist = os.path.join(claude_dir, "history.jsonl")
        if os.path.isfile(hist):
            try:
                changed = 0
                tmp = hist + ".tmp-repatriate"
                sid_re = re.compile((r'"sessionId":\s*"%s"' % re.escape(session_id)).encode("utf-8"))
                proj_re = re.compile(
                    (r'"project":\s*"%s"' % re.escape(json.dumps(old_cwd)[1:-1])).encode("utf-8"))
                repl = ('"project":"%s"' % json.dumps(new_cwd)[1:-1]).encode("utf-8")
                with open(hist, "rb") as fin, open(tmp, "wb") as fout:
                    for raw in fin:
                        if sid_re.search(raw) and proj_re.search(raw):
                            raw = proj_re.sub(lambda _m: repl, raw)
                            changed += 1
                        fout.write(raw)
                os.rename(tmp, hist)
                report["history_entries"] = changed
            except Exception as exc:
                report["warnings"].append("history.jsonl not updated: %s" % exc)
    else:
        tmp = path + ".tmp-repatriate"
        report["rewritten"] = _rewrite_cwd_lines(path, tmp, old_cwd, new_cwd)
        backup = path + ".bak-" + stamp
        os.rename(path, backup)
        os.rename(tmp, path)
        report["backup"] = backup
        report["moved_to"] = path
        for db in glob.glob(os.path.join(codex_dir, "sqlite", "*.db")):
            try:
                import sqlite3

                conn = sqlite3.connect(db, timeout=2)
                try:
                    cur = conn.execute(
                        "update local_thread_catalog set cwd = ? where thread_id = ? and cwd = ?",
                        (new_cwd, session_id, old_cwd))
                    conn.commit()
                    report["catalog_rows"] = report.get("catalog_rows", 0) + cur.rowcount
                finally:
                    conn.close()
            except Exception as exc:
                report["warnings"].append("%s: %s" % (os.path.basename(db), exc))
    if report["rewritten"] == 0:
        report["warnings"].append("no cwd fields matched %s; the file was still moved" % old_cwd)
    report["ok"] = True
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="sessionboard host-side helper: summarise, discover or repatriate "
                    "Claude Code and Codex sessions on this host (prints JSON).",
    )
    parser.add_argument("--mode", choices=["collect", "discover", "repatriate"], default="collect",
                        help="collect (default): list sessions; discover: rank target dirs for a "
                             "session; repatriate: move a session to --new-cwd")
    parser.add_argument("--claude-dir", default="~/.claude", help="Claude Code home (default ~/.claude)")
    parser.add_argument("--codex-dir", default="~/.codex", help="Codex home (default ~/.codex)")
    parser.add_argument("--no-live", action="store_true", help="collect: skip process scanning")
    parser.add_argument("--pretty", action="store_true", help="indent the JSON output")
    g = parser.add_argument_group("discover / repatriate")
    g.add_argument("--tool", choices=["claude", "codex"])
    g.add_argument("--id", help="session id")
    g.add_argument("--file", help="transcript path on this host")
    g.add_argument("--old-cwd", help="the session's current cwd")
    g.add_argument("--new-cwd", help="repatriate: directory the session should belong to")
    g.add_argument("--force", action="store_true", help="repatriate: skip the liveness check")
    args = parser.parse_args(argv)
    if args.mode == "collect":
        data = collect(args.claude_dir, args.codex_dir, want_live=not args.no_live)
    elif args.mode == "discover":
        if not (args.tool and args.file):
            parser.error("--mode discover needs --tool and --file")
        data = discover(args.tool, args.file, args.old_cwd, args.claude_dir, args.codex_dir)
    else:
        if not (args.tool and args.id and args.file and args.old_cwd and args.new_cwd):
            parser.error("--mode repatriate needs --tool, --id, --file, --old-cwd and --new-cwd")
        data = repatriate(args.tool, args.id, args.file, args.old_cwd, args.new_cwd,
                          args.claude_dir, args.codex_dir, force=args.force)
    json.dump(data, sys.stdout, indent=2 if args.pretty else None, sort_keys=args.pretty)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
