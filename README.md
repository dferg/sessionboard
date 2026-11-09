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
- **Remember your view.** Grouping, project folds, sort order, filters, and pinned
  sessions survive a restart. Press `p` to pin a session above the rest of the list.
- **Tidy the list.** Press `x` to hide a session and `H` to show hidden sessions so
  you can restore them. Hiding is saved locally and leaves the session untouched.
- **Bring your own host inventory.** An optional local command can supply additional
  hosts without putting private discovery code or hostnames in this repository.
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

### Hosts from an external tool

Keep inventory commands and private configuration in your home directory. Add a
provider to `~/.config/sessionboard/config.toml`:

```toml
[[host_providers]]
command = ["~/.local/bin/list-session-hosts", "--json"]
timeout = 10                # positive integer, seconds (default 10)
format = "json"             # default; alternatively "lines"
```

**Recommended format: a JSON array on stdout.** Each entry can be a hostname string
or an object. Use `ssh` for the connection target and `name` for a stable display
label (defaults to `ssh`). This matches the existing static host configuration and
allows an optional `group` for machines known to share a home directory:

```json
[
  "laptop.example.com",
  {"ssh": "user@dev1.example.com", "name": "dev-one", "group": "shared-home"},
  {"ssh": "user@dev2.example.com", "name": "dev-two", "group": "shared-home"}
]
```

Objects also accept `claude_dir`, `codex_dir`, and `python`, as static hosts do.
All fields must be non-empty strings; provider entries always describe remote hosts.
Avoid inferring `group` from a location or display label: it specifically means the
machines share session files. With `format = "lines"`, print one SSH hostname, alias,
or `user@host` per line; blank lines are ignored. For hostname/name pairs use JSON,
which handles spaces and escaping without ambiguous table parsing.

Providers run once when configuration is loaded, for the TUI and the `list`, `sync`,
and `repatriate` commands. Restart to discover a changed inventory; `s` syncs the
already loaded hosts. Explicit hosts win over providers on matching `name` **or**
`ssh`; earlier providers win over later ones. An empty JSON array (or empty lines
output) means no additional hosts. A timeout, nonzero exit, or invalid output stops
startup with a configuration error, without echoing the provider's output. Run the
command separately to troubleshoot it. Providers should use their own local cache
when offline access matters.

Commands are argument arrays executed directly, with no shell expansion except `~`
in the executable path. For a shell function, use a private executable wrapper that
sources its helper and calls the function, or explicitly invoke a shell. Only use
trusted commands: they run with your user's permissions. Reserve stdout for the
host data and send diagnostics to stderr.

### Saved views, pins, and hidden sessions

Press `o` to cycle sorting: live first (then newest), newest regardless of liveness,
project, host, or tool. Alphabetical sorts use recency within each value. In grouped
view, projects follow the chosen order of their leading session (live sorting uses
the project's live status and newest activity).

Press `p` on a session to pin or unpin it. Pinned sessions appear at the top in the
chosen sort order, above project groups when grouping is enabled, with a `★` marker.
They appear only once and still obey filters. Unpinning returns a session to its
normal sorted position and opens its project if necessary. Pins remain saved when
a session is temporarily missing from the cache.

Press `x` on a session to hide it from the list. Press `H` to include hidden sessions,
marked `[hidden]`, then `x` on one to unhide it. Other filters still apply, so clear
them if a hidden session is not visible. Hiding also excludes pinned sessions but
preserves their pins; unhiding restores their normal sorted or pinned position.
Hidden sessions disappear from project groups, and groups with no remaining visible
sessions disappear too. Hiding affects display only: it does not stop running agents,
delete transcripts, or prevent collection. Hidden IDs and the `H` toggle survive
restarts and cache refreshes, even when a session is temporarily missing.

Preferences are saved automatically to an owner-only JSON file under
`~/.local/state/sessionboard/` (or `$XDG_STATE_HOME/sessionboard/`), separately for
each config path. This includes grouping, collapsed projects, sorting, text/tool/
host/live/archive filters, pins, hidden sessions, and whether to show them.
A host filter that no longer exists is cleared
at startup. Missing or invalid state falls back to defaults. Delete the matching
`view-*.json` file while the app is closed to reset its view.

Keep these files private: state can contain search text, project and host labels,
and session IDs; the existing host cache also contains session summaries. Neither
belongs in the public repository. Provider output is used locally and is not written
back into configuration or repository files. All examples here use fictional hosts.

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
| `o` | cycle sort: live first → recent → project → host → tool |
| `p` | pin/unpin the selected session at the top of the list |
| `x` | hide/unhide the selected session |
| `H` | show/hide hidden sessions (other filters still apply) |
| `a` | show archived Codex sessions |
| `s` | sync every host now (`S`: just the selected session's host) |
| `r` | reload from the on-disk cache |
| `?` | help, `q` quit |

## CLI

```
sessionboard                 TUI
sessionboard sync [HOST...]  collect from every (or the named) host, print a summary
sessionboard list [--live] [--tool claude|codex] [--host H] [--by-project] [--show-hidden] [--json] [-n N] [--sync]
sessionboard repatriate ID [--to DIR | --top] [--force]   move a session (see below)
sessionboard collect [--pretty]   run the collector on this machine, print its JSON
sessionboard init [--force]
```

The cache lives in `~/.local/share/sessionboard/hosts/<host>.json`; a failed sync keeps
the previous data for that host and the status line says how old it is.

`list` excludes hidden sessions unless `--show-hidden` is passed, independently of
the TUI's `H` toggle. JSON listings include a `hidden` boolean. Collection and sync
still include all sessions.

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
