# sessionboard

One terminal screen that answers "what was I working on, where, and is anything still
running?" for every Claude Code and Codex session across all of your machines.

```
hosts  ✓ mbp-personal 12s   ✓ work-mbp 14s   ✓ site-a 20s   ✗ site-b (cached 2h)
41/97 sessions · 3 live

   tool    host          project               age  what
 ● claude  mbp-personal  sessionboard           1m  create the Textual TUI for me…
 ● codex   site-a        rtl-sim               12m  make the regression pass on the new…
 ● claude  work-mbp      fw-tools               3h  Why does the flash image fail to…
 · claude  mbp-personal  esphome-ac-condensate  9m  Review the code in this repository…
   claude  site-a        clock-navigator        2d  pinned boxes should keep their…
```

- **Every machine, one list.** Each host is asked over plain `ssh` (your aliases,
  ProxyJump, Tailscale names all work). Nothing is installed on the remote: a small
  stdlib-only Python script is streamed to `python3 -` and summarises the session files
  there, so only a few KB of JSON cross the wire, not the multi-megabyte transcripts.
- **Both tools.** Claude Code (`~/.claude/projects/*/*.jsonl`) and Codex
  (`~/.codex/sessions/**/rollout-*.jsonl`, `.zst`, `archived_sessions`).
- **Live indicators.** `●` green = agent working, `●` yellow = waiting for you,
  `·` = touched in the last 15 min but no process, `◌` = a running agent in that
  directory makes it probably live. Claude Code publishes exact per-process state in
  `~/.claude/sessions/<pid>.json`; Codex is detected from its processes and the rollout
  files they hold open.
- **Shared NFS homes.** Give hosts that share a home directory the same `group`: a
  session is listed once, and it is live if it is live on any of them.
- **Resume without magic.** `c` copies `cd '<project>' && claude --resume <id>` (or
  `codex resume <id>`) to your clipboard (`pbcopy`/`wl-copy`/`xclip`, plus OSC 52 so it
  works when sessionboard itself runs over ssh). Paste it in a shell on the host shown.
  No ssh-to-resume: you decide where and in which terminal it comes back.

## Install

```sh
git clone https://github.com/dferg/sessionboard.git
cd sessionboard
uv tool install .                 # gives you `sessionboard` on PATH (add --editable to hack on it)
sessionboard init                 # writes ~/.config/sessionboard/config.toml
$EDITOR ~/.config/sessionboard/config.toml
sessionboard sync                 # first pull, shows per-host status
sessionboard                      # the TUI
```

Remote hosts need `ssh` key access (BatchMode, no password prompts) and a `python3`
that is 3.6 or newer. Nothing else.

## Config

A fuller annotated example lives in `config.toml.example` at the repo root.

```toml
[settings]
local = true
local_name = "mbp-personal"
sync_on_start = true
auto_sync_seconds = 120     # 0 = manual only (press s)
ssh_timeout = 25
hide_empty = true           # hide sessions that never got a human prompt (claude -p etc.)

[[hosts]]
name = "work-mbp"
ssh = "work-mbp"            # anything `ssh` accepts

[[hosts]]
name = "site-a-login1"
ssh = "login1.site-a"
group = "site-a"            # same NFS home as the next one

[[hosts]]
name = "site-a-build7"
ssh = "build7.site-a"
group = "site-a"
```

## Keys

| key | action |
| --- | --- |
| `c` / `y` | copy the resume command for the selected session |
| `/` | filter by project, prompt, host, branch, id (Esc clears) |
| `t` | cycle tool filter: all → claude → codex |
| `h` | cycle host/group filter |
| `l` | live sessions only |
| `R` | repatriate: move an inactive session to the project directory it really worked in (see below) |
| `g` | group by project: one row per project directory name across hosts and tools, sessions nested beneath; Enter or Space folds a project, `z` folds/unfolds all |
| `a` | show archived Codex sessions |
| `s` | sync every host now (`S`: just the selected session's host) |
| `r` | reload from the on-disk cache |
| `?` | help, `q` quit |

## CLI

```
sessionboard                 TUI
sessionboard sync [HOST...]  collect from every (or the named) host, print a summary
sessionboard list [--live] [--tool claude|codex] [--host H] [--by-project] [--json] [-n N] [--sync]
sessionboard repatriate ID [--to DIR | --top] [--force]   move a session (see below)
sessionboard collect [--pretty]   run the collector on this machine, print its JSON
sessionboard init [--force]
```

The cache lives in `~/.local/share/sessionboard/hosts/<host>.json`; a failed sync keeps
the previous data for that host and the status line says how old it is.

## Repatriating a session

A session started in a scratch directory such as `~/scratch` often goes on to create a
real project somewhere else, say `~/Projects/newapp`, and then shows up under the wrong
project forever. `R` on an inactive session (or `sessionboard repatriate <id>`) fixes that:

1. **Discover.** On the session's own host, the transcript's tool calls are scanned for
   the paths it created and edited. Directories are ranked by volume of work, explicit
   `cd`/`mkdir`/`git init`, being a git repo, sitting directly under a projects folder
   (`~/Projects`, `~/src`, `~/work`, …) and already being another session's cwd. System
   paths, dot-directories, `node_modules`, `.venv` and the like are ignored.
2. **Confirm.** A picker shows the ranked candidates with those signals; pick one or type
   any absolute path on that host. Nothing is touched before you confirm.
3. **Move.** Claude Code: the transcript is moved into `~/.claude/projects/<encoded new
   path>/`, every `cwd` field in it (and in its subagent sidecar files) is rewritten, and
   the matching entries in `~/.claude/history.jsonl` are re-labelled. Codex: rollouts are
   filed by date, so the file stays put and its `cwd` fields plus the sqlite thread
   catalog row are rewritten. The original is kept beside it as `*.bak-<timestamp>`.
4. **Refresh.** That host is re-synced and the session appears in its new project group.
   `cd <new path> && claude --resume <id>` now finds it.

Live sessions are refused (process state is re-checked on the host at move time);
compressed `.zst` Codex rollouts must be decompressed first. Only the `cwd` metadata
changes: the conversation itself, including paths mentioned in it, is untouched.

## How live detection works

| tool | signal | confidence |
| --- | --- | --- |
| Claude Code | `~/.claude/sessions/<pid>.json` with `sessionId`, `cwd`, `status: busy/idle`, pid still alive | exact |
| Codex | a `codex` process holds `rollout-<id>.jsonl` open (`/proc/<pid>/fd` or `lsof`) | exact |
| either | an agent process is running with cwd = the session's project and no exact match exists; the newest session there is marked `◌` | guessed |

With an NFS-shared home, `~/.claude/sessions/*.json` is shared too, so a process on
machine A looks like a dead pid from machine B. That is why hosts in the same group are
each polled: the session shows live if any of them sees the process.

## Development

```sh
uv sync
uv run pytest
uv run sessionboard collect --pretty | head
```

`src/sessionboard/collector.py` must stay stdlib-only and Python 3.6 compatible: it is
the file that gets shipped to remote hosts.
