"""Command-line entry point.

    sessionboard                 TUI (default)
    sessionboard sync [HOST...]  collect from every (or the named) host, print a summary
    sessionboard list [...]      plain-text session listing from the cache
    sessionboard collect         run the collector on this machine, print its JSON
    sessionboard repatriate ID   move an inactive session to the project dir it worked in
    sessionboard init [--force]  write an example config
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__
from .config import DEFAULT_CONFIG, Config, ConfigError, load_config, write_example
from .model import group_by_project, merge_sessions, relative_age
from .state import load_state, state_path
from .sync import discover_targets, load_cache, repatriate_session, sync_all


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sessionboard",
        description="One TUI for every Claude Code and Codex session across all your machines.",
        epilog="With no sub-command the TUI opens.",
    )
    p.add_argument("--version", action="version", version=f"sessionboard {__version__}")
    p.add_argument("--config", metavar="FILE", help=f"config file (default {DEFAULT_CONFIG})")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("sync", help="collect session summaries from the configured hosts")
    s.add_argument("hosts", nargs="*", help="only these host names (default: all)")
    s.add_argument("--json", action="store_true", help="print the merged session list as JSON")

    ls = sub.add_parser("list", help="list sessions from the local cache")
    ls.add_argument("--live", action="store_true", help="live sessions only")
    ls.add_argument("--tool", choices=["claude", "codex"])
    ls.add_argument("--host", help="host/group label")
    ls.add_argument("--all", action="store_true", help="include sessions with no human prompt")
    ls.add_argument("--show-hidden", action="store_true", help="include sessions hidden in the TUI")
    ls.add_argument("--by-project", action="store_true", help="group the listing by project")
    ls.add_argument("-n", "--limit", type=int, default=40)
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--sync", action="store_true", help="sync first")

    sub.add_parser("collect", help="run the stdlib collector on this machine (debugging)",
                   add_help=False)

    rp = sub.add_parser("repatriate", help="move an inactive session to the project directory "
                                            "it actually worked in (discovers candidates first)")
    rp.add_argument("id", help="session id or unique prefix")
    rp.add_argument("--to", metavar="DIR", help="target directory on the session's host; "
                                                "without it only the ranked candidates are printed")
    rp.add_argument("--top", action="store_true", help="use the best-ranked candidate as --to")
    rp.add_argument("--force", action="store_true", help="skip the liveness check on the host")

    i = sub.add_parser("init", help="write an example config file")
    i.add_argument("--force", action="store_true", help="overwrite an existing file")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = _parser()
    # `collect` forwards its own flags to the collector's parser
    if argv and argv[0] == "collect":
        from . import collector

        return collector.main(argv[1:])
    args = parser.parse_args(argv)

    if args.cmd == "init":
        try:
            path = write_example(args.config, force=args.force)
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {path}")
        return 0

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    if args.cmd == "sync":
        return _cmd_sync(config, set(args.hosts) or None, args.json)
    if args.cmd == "list":
        return _cmd_list(config, args)
    if args.cmd == "repatriate":
        return _cmd_repatriate(config, args)

    from .app import SessionBoard

    SessionBoard(config).run()
    return 0


def _cmd_sync(config: Config, only: set[str] | None, as_json: bool) -> int:
    if only:
        unknown = only - {h.name for h in config.hosts}
        if unknown:
            print(f"unknown host(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
    states = load_cache(config)
    sync_all(config, states, only=only)
    rc = 0
    if as_json:
        print(json.dumps([s.__dict__ for s in merge_sessions(states, hide_empty=False)], indent=1))
        return rc
    now = time.time()
    for st in states:
        if only and st.name not in only:
            continue
        if st.ok:
            data = st.data or {}
            live = sum(
                1 for tool in ("claude", "codex")
                for s in data.get(tool, {}).get("sessions", []) if s.get("live")
            )
            errs = sum(len(data.get(t, {}).get("errors", [])) for t in ("claude", "codex"))
            extra = f", {errs} file error(s)" if errs else ""
            print(f"✓ {st.name:<18} {st.session_count:>4} sessions, {live} live{extra}")
        else:
            rc = 1
            cached = f" (cache from {relative_age(st.synced_at, now)} ago)" if st.synced_at else ""
            print(f"✗ {st.name:<18} {st.error}{cached}")
    return rc


def _cmd_list(config: Config, args: argparse.Namespace) -> int:
    states = load_cache(config)
    if args.sync:
        sync_all(config, states)
    sessions = merge_sessions(states, hide_empty=not args.all)
    hidden = set(load_state(state_path(config.path)).hidden)
    rows = [
        s for s in sessions
        if (not args.live or s.live)
        and (not args.tool or s.tool == args.tool)
        and (not args.host or s.group == args.host)
        and (args.show_hidden or s.key not in hidden)
    ][: args.limit]
    if args.json:
        print(json.dumps([s.__dict__ | {"resume_command": s.resume_command,
                                        "hidden": s.key in hidden} for s in rows], indent=1))
        return 0
    if not rows:
        print("no sessions match (try --show-hidden or run `sessionboard sync`)")
        return 0
    now = time.time()
    if args.by_project:
        for g in group_by_project(rows):
            live = f", {g.live} live" if g.live else ""
            print(f"{'●' if g.live else ' '} {g.name}  ({len(g.sessions)} sessions{live}; "
                  f"{', '.join(g.hosts)})  {relative_age(g.mtime, now)}")
            for s in g.sessions:
                print(f"      {'●' if s.live else ' '} {s.tool:<6} {s.group:<14} "
                      f"{relative_age(s.mtime, now):>4}  "
                      f"{'[hidden] ' if s.key in hidden else ''}{s.headline[:60]}")
                print(f"          {s.resume_command}")
        return 0
    for s in rows:
        mark = "●" if s.live else " "
        print(f"{mark} {s.tool:<6} {s.group:<14} {s.project[:22]:<22} {relative_age(s.mtime, now):>4}  "
              f"{'[hidden] ' if s.key in hidden else ''}{s.headline[:70]}")
        print(f"      {s.resume_command}")
    return 0


def _cmd_repatriate(config: Config, args: argparse.Namespace) -> int:
    states = load_cache(config)
    sessions = merge_sessions(states, hide_empty=False)
    hits = [s for s in sessions if s.id.startswith(args.id)]
    if not hits:
        print(f"no cached session matches {args.id!r} (run `sessionboard sync`?)", file=sys.stderr)
        return 2
    if len(hits) > 1:
        print(f"{args.id!r} is ambiguous:", file=sys.stderr)
        for s in hits:
            print(f"  {s.id}  {s.tool} {s.group} {s.project}", file=sys.stderr)
        return 2
    s = hits[0]
    if s.live and not args.force:
        print(f"refusing: session is live on {s.live_on or s.group}", file=sys.stderr)
        return 1
    spec = next((config.host(n) for n in ([s.live_on] if s.live_on else []) + s.seen_on
                 if config.host(n)), None)
    if spec is None:
        print(f"no configured host for {s.group}", file=sys.stderr)
        return 1
    timeout = config.settings.ssh_timeout * 2
    target = args.to
    if not target or args.top:
        found = discover_targets(spec, s.tool, s.file, s.cwd, timeout)
        cands = found.get("candidates") or []
        print(f"{s.tool} {s.id} on {spec.name}\n  from {s.cwd}\n  {s.headline[:90]}\n")
        if not cands:
            print("no candidate directories found; pass --to DIR")
            return 1
        print("candidates:")
        for i, c in enumerate(cands, 1):
            tags = [t for t, on in (("git", c.get("git")),
                                    (f"{c.get('other_sessions')} other sessions", c.get("other_sessions")),
                                    (f"cd/mkdir x{c.get('explicit')}", c.get("explicit"))) if on]
            print(f"  {i}. {c['path']}   ({c.get('paths')} paths{', ' + ', '.join(tags) if tags else ''})")
        if not args.top:
            print("\nre-run with --to DIR (or --top) to move it")
            return 0
        target = cands[0]["path"]
    rep = repatriate_session(spec, s.tool, s.id, s.file, s.cwd or "", target, timeout,
                             force=args.force)
    if not rep.get("ok"):
        print(f"not moved: {rep.get('error')}", file=sys.stderr)
        return 1
    print(f"moved {s.tool} {s.id[:8]} -> {rep['new_cwd']} on {spec.name}: "
          f"{rep.get('rewritten', 0)} cwd fields rewritten, backup {rep.get('backup')}")
    for w in rep.get("warnings", []):
        print(f"  warning: {w}")
    names = {st.name for st in states if st.group == spec.group_name}
    sync_all(config, states, only=names)
    print(f"re-synced {', '.join(sorted(names))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
