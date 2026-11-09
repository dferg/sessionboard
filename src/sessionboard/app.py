"""The Textual TUI."""

from __future__ import annotations

import time

from rich.console import Group
from rich.panel import Panel
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from . import __version__, clipboard
from .config import Config, HostSpec
from .model import HostState, ProjectGroup, Session, group_by_project, merge_sessions, relative_age
from .state import SORT_MODES, ViewState, load_state, save_state, state_path
from .sync import discover_targets, load_cache, repatriate_session, sync_all

TOOL_CYCLE = (None, "claude", "codex")
TOOL_STYLE = {"claude": "bold #d97757", "codex": "bold #10a37f"}

HELP = """\
[b]Keys[/b]

  [b]↑/↓ j/k[/b]   move          [b]Enter[/b]    show details for the row
  [b]^F/^B[/b]     page down/up  [b]0[/b] / [b]G[/b]    jump to top / bottom
  [b]c[/b] or [b]y[/b]     copy the resume command for the selected session
  [b]/[/b]         filter (type, Enter to keep, Esc to clear)
  [b]t[/b]         cycle tool: all → claude → codex
  [b]h[/b]         cycle host/group filter
  [b]l[/b]         live sessions only
  [b]g[/b]         group by project (Enter/Space on a project row folds it, [b]z[/b] folds/unfolds all)
  [b]o[/b]         cycle sort: live first → recent → project → host → tool
  [b]p[/b]         pin/unpin the selected session at the top (★); filters still apply
  [b]x[/b]         hide/unhide the selected session (only changes this list)
  [b]H[/b]         show/hide hidden sessions; other filters still apply
  [b]a[/b]         show/hide archived Codex sessions
  [b]s[/b]         sync every host now        [b]S[/b]  sync only the selected session's host
  [b]R[/b]         repatriate: move an inactive session to the project directory it really worked in
  [b]r[/b]         reload from the on-disk cache
  [b]?[/b]         this help                   [b]q[/b]  quit

[b]Indicators[/b]

  [green b]●[/green b]  live, agent is working      [yellow b]●[/yellow b]  live, waiting for you
  [dim]·[/dim]  no process, but touched in the last few minutes
  [dim]◌[/dim]  live-ness guessed from a running agent in that directory

[b]Repatriating[/b]

  A session started in ~/scratch that went on to build ~/Projects/newapp belongs with
  newapp. [b]R[/b] scans the transcript's tool calls for the directories it created and
  edited, ranks them (git repo, child of a projects folder, explicit cd/mkdir, already
  another session's cwd) and lets you confirm or type a path. Then, on the session's own
  host, the transcript is moved into the right ~/.claude/projects/ folder (Codex: kept in
  place) with every [i]cwd[/i] field rewritten, a .bak copy left beside the original, and
  that host re-synced so the session lands in its new project group. Live sessions are
  refused.

[b]Resuming[/b]

  Nothing runs over ssh for you. [b]c[/b] copies
  [i]cd '<project>' && claude --resume <id>[/i]  (or [i]codex resume <id>[/i])
  to your clipboard; paste it in a shell on the host shown in the [b]host[/b] column.
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "dismiss", "close")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Static { width: 84; max-width: 95%; border: round $accent; padding: 1 2;
                          background: $surface; }
    """

    def compose(self) -> ComposeResult:
        yield Static(Text.from_markup(HELP))


class RepatriateScreen(ModalScreen[str | None]):
    """Pick (or type) the directory an inactive session should be moved to."""

    BINDINGS = [Binding("escape", "dismiss", "cancel")]
    DEFAULT_CSS = """
    RepatriateScreen { align: center middle; }
    RepatriateScreen > Vertical { width: 110; max-width: 96%; height: auto; max-height: 90%;
                                  border: round $accent; padding: 1 2; background: $surface; }
    RepatriateScreen #rp-title { margin-bottom: 1; }
    RepatriateScreen OptionList { height: auto; max-height: 14; margin-bottom: 1; }
    RepatriateScreen #rp-target { margin-bottom: 1; }
    RepatriateScreen Horizontal { height: auto; }
    RepatriateScreen Button { margin-right: 2; }
    """

    def __init__(self, session: Session, host: str, result: dict) -> None:
        super().__init__()
        self.session = session
        self.host = host
        self.result = result
        self.candidates: list[dict] = result.get("candidates") or []

    def compose(self) -> ComposeResult:
        s = self.session
        title = Text()
        title.append("repatriate ", style="bold")
        title.append(s.tool, style=TOOL_STYLE.get(s.tool, ""))
        title.append(f" session {s.id[:8]}  on {self.host}\n", style="bold")
        title.append(f"from  {s.cwd}\n", style="dim")
        title.append(f"      {s.headline[:100]}\n", style="dim")
        n = self.result.get("paths_seen", 0)
        title.append(f"{n} path references in the transcript, {len(self.candidates)} candidate "
                     "directories (↑/↓ then Enter picks one, or type a path)", style="dim")
        with Vertical():
            yield Static(title, id="rp-title")
            opts = [Option(_candidate_text(c), id=str(i)) for i, c in enumerate(self.candidates)]
            if not opts:
                opts = [Option(Text("no candidate directories found — type one below",
                                    style="dim"), disabled=True)]
            yield OptionList(*opts, id="rp-options")
            first = self.candidates[0]["path"] if self.candidates else ""
            yield Input(value=first, placeholder="/absolute/path/on/that/host", id="rp-target")
            with Horizontal():
                yield Button("Repatriate", variant="primary", id="rp-go")
                yield Button("Cancel", id="rp-cancel")
            yield Label(Text("The transcript is moved on the host with a .bak copy left behind; "
                             "only its cwd fields change.", style="dim"))

    def on_mount(self) -> None:
        if self.candidates:
            self.query_one(OptionList).highlighted = 0
            self.query_one(OptionList).focus()
        else:
            self.query_one("#rp-target", Input).focus()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id is not None:
            self.query_one("#rp-target", Input).value = self.candidates[int(event.option.id)]["path"]

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.query_one("#rp-target", Input).value = self.candidates[int(event.option.id)]["path"]
        self._go()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self._go()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "rp-go":
            self._go()
        else:
            self.dismiss(None)

    def _go(self) -> None:
        target = self.query_one("#rp-target", Input).value.strip()
        if not target:
            self.app.notify("type or pick a directory", severity="warning")
            return
        if not target.startswith(("/", "~")):
            self.app.notify("the target must be an absolute path on that host", severity="warning")
            return
        self.dismiss(target)


def _candidate_text(c: dict) -> Text:
    t = Text()
    t.append(c["path"], style="bold")
    tags = []
    if c.get("git"):
        tags.append("git repo")
    if c.get("other_sessions"):
        tags.append(f"{c['other_sessions']} other session{'s' if c['other_sessions'] != 1 else ''} here")
    if c.get("explicit"):
        tags.append(f"cd/mkdir ×{c['explicit']}")
    tags.append(f"{c.get('paths', 0)} paths")
    if c.get("inside_old"):
        tags.append("inside current cwd")
    t.append("   " + " · ".join(tags), style="dim")
    return t


class SessionBoard(App[None]):
    TITLE = "sessionboard"
    AUTO_FOCUS = "#table"
    CSS = """
    #status { height: auto; padding: 0 1; background: $panel; color: $text-muted; }
    #table { height: 1fr; }
    #detail { height: auto; max-height: 45%; min-height: 8; border-top: solid $accent;
              padding: 0 1; }
    #filter { display: none; }
    #filter.visible { display: block; }
    DataTable > .datatable--cursor { background: $accent 30%; }
    """
    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("c,y", "copy", "copy resume cmd"),
        Binding("slash", "filter", "filter"),
        Binding("escape", "clear_filter", "clear", show=False),
        Binding("t", "cycle_tool", "tool"),
        Binding("h", "cycle_host", "host"),
        Binding("l", "toggle_live", "live only"),
        Binding("g", "toggle_group", "by project"),
        Binding("o", "cycle_sort", "sort"),
        Binding("p", "toggle_pin", "pin"),
        Binding("x", "toggle_hidden", "hide/unhide"),
        Binding("H", "toggle_show_hidden", "show hidden"),
        Binding("z", "fold_all", "fold all", show=False),
        Binding("space", "select_row", show=False),
        Binding("a", "toggle_archived", "archived", show=False),
        Binding("s", "sync", "sync"),
        Binding("S", "sync_selected", "sync host", show=False),
        Binding("R", "repatriate", "repatriate"),
        Binding("r", "reload", "reload", show=False),
        Binding("question_mark", "help", "help"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("ctrl+f", "page_down", show=False),
        Binding("ctrl+b", "page_up", show=False),
        Binding("0", "jump_top", show=False),
        Binding("G", "jump_bottom", show=False),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.states: list[HostState] = load_cache(config)
        self.sessions: list[Session] = []
        self.rows: list[Session | ProjectGroup] = []
        self._state_path = state_path(config.path)
        saved = load_state(self._state_path)
        self._last_saved = saved
        self._state_error = False
        self.by_project = saved.by_project
        self.collapsed = set(saved.collapsed)
        self.query = ""  # never restored; see ViewState
        self.tool_filter = saved.tool_filter
        self.group_filter = saved.group_filter if saved.group_filter in self._groups() else None
        self.live_only = saved.live_only
        self.show_archived = saved.show_archived
        self.sort_mode = saved.sort_mode
        self.pinned = set(saved.pinned)
        self.hidden = set(saved.hidden)
        self.show_hidden = saved.show_hidden
        self._table_ready = False

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="status")
        with Vertical():
            table: DataTable = DataTable(id="table", cursor_type="row", zebra_stripes=True)
            yield table
            yield Static("", id="detail")
            yield Input(value=self.query, placeholder="filter: project, prompt, host, branch, id…",
                        id="filter")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"v{__version__}"
        table = self.query_one(DataTable)
        table.focus()
        table.add_column(" ", key="live", width=1)
        table.add_column("★", key="pin", width=1)
        table.add_column("tool", key="tool", width=6)
        self._col_host = table.add_column("host", key="host", width=12)
        self._col_project = table.add_column("project", key="project", width=22)
        table.add_column("age", key="age", width=4)
        table.add_column("what", key="what")
        self._table_ready = True
        self._rebuild()
        if self.config.settings.sync_on_start:
            self.action_sync()
        secs = self.config.settings.auto_sync_seconds
        if secs and secs > 0:
            self.set_interval(secs, self.action_sync)
        self.set_interval(30, self._refresh_ages)

    # ------------------------------------------------------------------ data
    def _rebuild(self, keep_cursor: bool = True) -> None:
        self.sessions = merge_sessions(self.states, hide_empty=self.config.settings.hide_empty)
        self._refresh_table(keep_cursor)
        self._refresh_status()

    def _filtered(self) -> list[Session]:
        out = []
        for s in self.sessions:
            if s.key in self.hidden and not self.show_hidden:
                continue
            if self.tool_filter and s.tool != self.tool_filter:
                continue
            if self.group_filter and s.group != self.group_filter:
                continue
            if self.live_only and not s.live:
                continue
            if s.archived and not self.show_archived:
                continue
            if self.query and not s.matches(self.query):
                continue
            out.append(s)
        return out

    def _groups(self) -> list[str]:
        seen: list[str] = []
        for st in self.states:
            if st.group not in seen:
                seen.append(st.group)
        return seen

    def _refresh_table(self, keep_cursor: bool = True) -> None:
        if not self._table_ready:
            return
        table = self.query_one(DataTable)
        selected = self._selected()
        sessions = self._filtered()
        self.rows = self._layout_rows(sessions)
        table.clear()
        now = time.time()
        recent = self.config.settings.recent_minutes * 60
        host_w = max([12] + [len(s.group) for s in sessions])
        host_w = min(host_w, 18)
        proj_w = min(max([10] + [len(s.project) for s in sessions]), 24)
        if self.by_project:
            proj_w = max(proj_w, 14)
        table.columns[self._col_host].width = host_w
        table.columns[self._col_project].width = proj_w
        what_w = max(20, table.size.width - (1 + 1 + 6 + host_w + proj_w + 4 + 7 * 3) - 2)
        for i, row in enumerate(self.rows):
            if isinstance(row, ProjectGroup):
                cells = _group_cells(row, now, recent, host_w, proj_w, what_w,
                                     folded=self._folded(row))
                cells.insert(1, Text(" "))
                table.add_row(*cells, key=row.key)
                continue
            s = row
            if self.by_project and s.key not in self.pinned:
                last = i + 1 >= len(self.rows) or isinstance(self.rows[i + 1], ProjectGroup)
                project_cell = Text("└" if last else "├", style="dim")
            else:
                project_cell = Text(s.project[:proj_w], style="bold" if s.live else "")
            table.add_row(
                _live_cell(s, now, recent),
                Text("★" if s.key in self.pinned else " ", style="yellow"),
                Text(s.tool, style=TOOL_STYLE.get(s.tool, "")),
                Text(s.group[:host_w]),
                project_cell,
                Text(relative_age(s.mtime, now), justify="right",
                     style="" if now - s.mtime < 86400 else "dim"),
                (Text("[hidden] ", style="dim") + _what_cell(s, what_w - 9)
                 if s.key in self.hidden else _what_cell(s, what_w)),
                key=s.key,
            )
        if keep_cursor and selected is not None:
            for i, row in enumerate(self.rows):
                if row.key == selected.key:
                    table.move_cursor(row=i)
                    break
        self._refresh_detail()
        self._save_view()

    def _save_view(self) -> None:
        state = ViewState(
            by_project=self.by_project, collapsed=sorted(self.collapsed),
            tool_filter=self.tool_filter, group_filter=self.group_filter,
            live_only=self.live_only, show_archived=self.show_archived,
            sort_mode=self.sort_mode, pinned=sorted(self.pinned),
            hidden=sorted(self.hidden), show_hidden=self.show_hidden,
        )
        if state == self._last_saved:
            return
        try:
            save_state(self._state_path, state)
        except OSError:
            if not self._state_error:
                self.notify("Could not save view preferences; check the state directory permissions.",
                            severity="warning")
            self._state_error = True
        else:
            self._last_saved = state
            self._state_error = False

    def _folded(self, group: ProjectGroup) -> bool:
        # a text filter always shows the matches, folded or not
        return group.name in self.collapsed and not self.query

    def _layout_rows(self, sessions: list[Session]) -> list[Session | ProjectGroup]:
        sessions = sorted(sessions, key=self._sort_key)
        pinned = [s for s in sessions if s.key in self.pinned]
        unpinned = [s for s in sessions if s.key not in self.pinned]
        if not self.by_project:
            return pinned + unpinned
        rows: list[Session | ProjectGroup] = list(pinned)
        groups = group_by_project(unpinned)
        # Each project is positioned by its first session in the chosen sort order.
        if self.sort_mode != "live":
            groups.sort(key=lambda g: self._sort_key(g.sessions[0]))
        for g in groups:
            rows.append(g)
            if not self._folded(g):
                rows.extend(g.sessions)
        return rows

    def _sort_key(self, session: Session) -> tuple:
        if self.sort_mode == "live":
            return (not session.live, -session.mtime, session.key)
        if self.sort_mode == "recent":
            return (-session.mtime, session.key)
        value = session.group if self.sort_mode == "host" else getattr(session, self.sort_mode)
        return (value.casefold(), -session.mtime, session.key)

    def _refresh_ages(self) -> None:
        # cheap periodic refresh so "3m" becomes "4m" and · markers expire
        self._refresh_table()

    def _refresh_status(self) -> None:
        now = time.time()
        parts: list[Text] = []
        for st in self.states:
            t = Text()
            if st.syncing:
                t.append("⟳ ", style="yellow")
                t.append(st.name)
            elif st.ok and st.synced_at:
                t.append("✓ ", style="green")
                t.append(st.name)
                t.append(f" {relative_age(st.synced_at, now)}", style="dim")
            elif st.error:
                t.append("✗ ", style="red")
                t.append(st.name)
                if st.synced_at:
                    t.append(f" (cached {relative_age(st.synced_at, now)})", style="dim")
            else:
                t.append("○ ", style="dim")
                t.append(st.name, style="dim")
            parts.append(t)
        line = Text("hosts  ", style="bold")
        for i, p in enumerate(parts):
            if i:
                line.append("   ")
            line.append_text(p)
        live = sum(1 for s in self.sessions if s.live)
        filt = []
        if self.tool_filter:
            filt.append(f"tool={self.tool_filter}")
        if self.group_filter:
            filt.append(f"host={self.group_filter}")
        if self.live_only:
            filt.append("live")
        if self.by_project:
            filt.append("by project")
        if self.show_archived:
            filt.append("+archived")
        if self.show_hidden:
            filt.append("+hidden")
        if self.query:
            filt.append(f"/{self.query}")
        shown = sum(1 for r in self.rows if isinstance(r, Session))
        hidden_count = sum(s.key in self.hidden for s in self.sessions)
        summary = Text(
            f"\n{shown}/{len(self.sessions)} sessions · {live} live · sort={self.sort_mode}"
            + (f" · {hidden_count} hidden (H toggles)" if hidden_count else "")
            + ("   filter: " + " ".join(filt) if filt else ""),
            style="dim",
        )
        errors = [f"{st.name}: {st.error}" for st in self.states if st.error and not st.syncing]
        status = Text.assemble(line, summary)
        if errors:
            status.append("\n" + " · ".join(errors)[:200], style="red")
        self.query_one("#status", Static).update(status)

    def _selected(self) -> Session | ProjectGroup | None:
        table = self.query_one(DataTable)
        if not self.rows or table.cursor_row is None or table.cursor_row < 0:
            return None
        if table.cursor_row >= len(self.rows):
            return None
        return self.rows[table.cursor_row]

    def _selected_session(self) -> Session | None:
        row = self._selected()
        return row if isinstance(row, Session) else None

    def _refresh_detail(self) -> None:
        s = self._selected()
        detail = self.query_one("#detail", Static)
        if s is None:
            detail.update(Text("no sessions match", style="dim"))
            return
        now = time.time()
        if isinstance(s, ProjectGroup):
            detail.update(_group_detail(s, now, self._folded(s)))
            return
        head = Text()
        if s.key in self.hidden:
            head.append("hidden · x to unhide  ", style="dim")
        if s.key in self.pinned:
            head.append("★ pinned  ", style="yellow")
        head.append(s.tool, style=TOOL_STYLE.get(s.tool, ""))
        head.append("  ")
        head.append(s.project, style="bold")
        if s.branch:
            head.append(f"  ⎇ {s.branch}", style="cyan")
        head.append("   on ", style="dim")
        head.append(s.group, style="bold")
        if len(s.seen_on) > 1 or s.seen_on != [s.group]:
            head.append(f" (seen from {', '.join(s.seen_on)})", style="dim")
        if s.live:
            head.append("   ")
            head.append(_live_label(s), style="green bold" if _busy(s) else "yellow bold")
            if s.live_on and s.live_on != s.group:
                head.append(f" on {s.live_on}", style="dim")
        if s.archived:
            head.append("   archived", style="dim")

        meta = Text(style="dim")
        meta.append(f"{s.cwd or '?'}\n")
        meta.append(f"id {s.id}   last active {relative_age(s.mtime, now)} ago")
        if s.started:
            meta.append(f"   started {time.strftime('%Y-%m-%d %H:%M', time.localtime(s.started))}")
        if s.size:
            meta.append(f"   {s.size / 1024:.0f} KB")

        body = []
        if s.title:
            body.append(Text.assemble(("title  ", "dim"), (s.title, "bold")))
        body.append(Text.assemble(("first  ", "dim"), s.first_prompt or "(none)"))
        if s.last_prompt and s.last_prompt != s.first_prompt:
            body.append(Text.assemble(("last   ", "dim"), s.last_prompt))

        cmd = Panel(
            Text(s.resume_command, style="bold"),
            title=f"[dim]paste on[/dim] [b]{s.live_on or s.group}[/b]  [dim]·[/dim] [b]c[/b] [dim]copies[/dim]",
            title_align="left",
            border_style="green" if s.live else "blue",
            padding=(0, 1),
        )
        detail.update(Group(head, meta, *body, cmd))

    # ------------------------------------------------------------------ events
    def on_data_table_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        self._refresh_detail()

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        row = self._selected()
        if isinstance(row, ProjectGroup):
            self._toggle_fold(row)
        else:
            self._refresh_detail()

    def action_select_row(self) -> None:
        row = self._selected()
        if isinstance(row, ProjectGroup):
            self._toggle_fold(row)

    def _toggle_fold(self, group: ProjectGroup) -> None:
        if group.name in self.collapsed:
            self.collapsed.discard(group.name)
        else:
            self.collapsed.add(group.name)
        self._refresh_table()

    def action_toggle_group(self) -> None:
        self.by_project = not self.by_project
        self._refresh_table(keep_cursor=True)
        self._refresh_status()

    def action_cycle_sort(self) -> None:
        self.sort_mode = SORT_MODES[(SORT_MODES.index(self.sort_mode) + 1) % len(SORT_MODES)]
        self._refresh_table()
        self._refresh_status()

    def action_toggle_pin(self) -> None:
        session = self._selected_session()
        if session is None:
            self.notify("select a session row to pin", severity="warning")
            return
        if session.key in self.pinned:
            self.pinned.remove(session.key)
            # Keep the unpinned session visible in its proper project.
            self.collapsed.discard(session.project)
        else:
            self.pinned.add(session.key)
        self._refresh_table()
        self._refresh_status()

    def action_toggle_hidden(self) -> None:
        session = self._selected_session()
        if session is None:
            self.notify("select a session row to hide or unhide", severity="warning")
            return
        table = self.query_one(DataTable)
        previous_row = table.cursor_row
        if session.key in self.hidden:
            self.hidden.remove(session.key)
        else:
            self.hidden.add(session.key)
        self._refresh_table()
        if not self.show_hidden and self.rows:
            table.move_cursor(row=min(previous_row, len(self.rows) - 1))
            self._refresh_detail()
        self._refresh_status()

    def action_toggle_show_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        self._refresh_table()
        self._refresh_status()

    def action_fold_all(self) -> None:
        if not self.by_project:
            self.by_project = True
            self._refresh_status()
        names = {g.name for g in group_by_project(self._filtered())}
        if names <= self.collapsed:
            self.collapsed -= names
        else:
            self.collapsed |= names
        self._refresh_table()

    def on_resize(self) -> None:
        self._refresh_table()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self.query = event.value.strip()
            self._refresh_table(keep_cursor=False)
            self._refresh_status()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            self._hide_filter()

    # ------------------------------------------------------------------ actions
    def action_cursor_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    def action_page_down(self) -> None:
        self.query_one(DataTable).action_page_down()

    def action_page_up(self) -> None:
        self.query_one(DataTable).action_page_up()

    def action_jump_top(self) -> None:
        self.query_one(DataTable).action_scroll_top()

    def action_jump_bottom(self) -> None:
        self.query_one(DataTable).action_scroll_bottom()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_filter(self) -> None:
        inp = self.query_one("#filter", Input)
        inp.add_class("visible")
        inp.focus()

    def _hide_filter(self) -> None:
        inp = self.query_one("#filter", Input)
        inp.remove_class("visible")
        self.query_one(DataTable).focus()

    def action_clear_filter(self) -> None:
        inp = self.query_one("#filter", Input)
        if inp.has_class("visible") or self.query:
            inp.value = ""
            self.query = ""
            self._hide_filter()
            self._refresh_table(keep_cursor=False)
            self._refresh_status()

    def action_cycle_tool(self) -> None:
        i = TOOL_CYCLE.index(self.tool_filter)
        self.tool_filter = TOOL_CYCLE[(i + 1) % len(TOOL_CYCLE)]
        self._refresh_table(keep_cursor=False)
        self._refresh_status()

    def action_cycle_host(self) -> None:
        cycle: list[str | None] = [None, *self._groups()]
        i = cycle.index(self.group_filter) if self.group_filter in cycle else 0
        self.group_filter = cycle[(i + 1) % len(cycle)]
        self._refresh_table(keep_cursor=False)
        self._refresh_status()

    def action_toggle_live(self) -> None:
        self.live_only = not self.live_only
        self._refresh_table(keep_cursor=False)
        self._refresh_status()

    def action_toggle_archived(self) -> None:
        self.show_archived = not self.show_archived
        self._refresh_table(keep_cursor=False)
        self._refresh_status()

    def action_reload(self) -> None:
        self.states = load_cache(self.config)
        self._rebuild()
        self.notify("reloaded from cache")

    def action_copy(self) -> None:
        s = self._selected_session()
        if s is None:
            if isinstance(self._selected(), ProjectGroup):
                self.notify("that is a project row — pick a session under it",
                            severity="warning")
            else:
                self.notify("nothing selected", severity="warning")
            return
        text = s.resume_command
        tool = clipboard.copy_local(text)
        # OSC 52 reaches the clipboard of the terminal you are sitting at, which is what
        # you want when sessionboard itself runs over ssh.
        self.copy_to_clipboard(text)
        where = s.live_on or s.group
        if tool:
            how = f"via {tool}"
        elif clipboard.over_ssh():
            how = "via your terminal (OSC 52)"
        else:
            how = "via OSC 52 (no pbcopy/xclip/wl-copy found)"
        self.notify(f"copied {how} — paste on {where}", title="resume command", timeout=4)

    # ------------------------------------------------------------------ repatriation
    def _spec_for(self, s: Session) -> HostSpec | None:
        for name in ([s.live_on] if s.live_on else []) + list(s.seen_on):
            spec = self.config.host(name) if name else None
            if spec:
                return spec
        return None

    def action_repatriate(self) -> None:
        s = self._selected_session()
        if s is None:
            self.notify("select a session row first", severity="warning")
            return
        if s.live:
            self.notify("that session is live — finish or quit it first", severity="error")
            return
        if not s.file or not s.cwd:
            self.notify("no transcript path known for this session; sync first", severity="error")
            return
        spec = self._spec_for(s)
        if spec is None:
            self.notify(f"no configured host for {s.group}", severity="error")
            return
        self.notify(f"scanning transcript on {spec.name}…", timeout=3)
        self._discover_worker(s, spec)

    @work(thread=True, exclusive=True, group="repatriate")
    def _discover_worker(self, s: Session, spec: HostSpec) -> None:
        try:
            result = discover_targets(spec, s.tool, s.file, s.cwd,
                                      self.config.settings.ssh_timeout * 2)
        except Exception as exc:
            self.call_from_thread(self.notify, f"discover failed: {exc}", severity="error", timeout=8)
            return
        self.call_from_thread(self._show_repatriate, s, spec, result)

    def _show_repatriate(self, s: Session, spec: HostSpec, result: dict) -> None:
        def chosen(target: str | None) -> None:
            if target:
                self.notify(f"repatriating to {target} on {spec.name}…", timeout=3)
                self._apply_worker(s, spec, target)

        self.push_screen(RepatriateScreen(s, spec.name, result), chosen)

    @work(thread=True, exclusive=True, group="repatriate")
    def _apply_worker(self, s: Session, spec: HostSpec, target: str) -> None:
        try:
            rep = repatriate_session(spec, s.tool, s.id, s.file, s.cwd or "", target,
                                     self.config.settings.ssh_timeout * 2)
        except Exception as exc:
            self.call_from_thread(self.notify, f"repatriate failed: {exc}", severity="error", timeout=8)
            return
        if not rep.get("ok"):
            self.call_from_thread(self.notify, f"not moved: {rep.get('error')}", severity="error",
                                  timeout=8)
            return
        msg = f"moved to {rep['new_cwd']} ({rep.get('rewritten', 0)} cwd fields rewritten)"
        if rep.get("warnings"):
            msg += "; " + "; ".join(rep["warnings"])[:160]
        self.call_from_thread(self.notify, msg, title="repatriated", timeout=6)
        names = {st.name for st in self.states if st.group == spec.group_name}
        self.call_from_thread(self._start_sync, names)

    def action_sync(self) -> None:
        self._start_sync(None)

    def action_sync_selected(self) -> None:
        s = self._selected()
        if s is None:
            return
        groups = set(s.hosts) if isinstance(s, ProjectGroup) else {s.group}
        names = {st.name for st in self.states if st.group in groups}
        self._start_sync(names)

    def _start_sync(self, only: set[str] | None) -> None:
        for st in self.states:
            if only is None or st.name in only:
                st.syncing = True
        self._refresh_status()
        self._sync_worker(only)

    @work(thread=True, exclusive=True, group="sync")
    def _sync_worker(self, only: set[str] | None) -> None:
        def done(state: HostState) -> None:
            self.call_from_thread(self._host_done, state)

        sync_all(self.config, self.states, only=only, callback=done)
        self.call_from_thread(self._all_done)

    def _host_done(self, state: HostState) -> None:
        self._rebuild()

    def _all_done(self) -> None:
        for st in self.states:
            st.syncing = False
        self._rebuild()
        failed = [st.name for st in self.states if st.error]
        if failed:
            self.notify(f"sync finished, {len(failed)} host(s) failed: {', '.join(failed)}",
                        severity="warning", timeout=6)


# ---------------------------------------------------------------------- cells
def _busy(s: Session) -> bool:
    return (s.live_status or "") in ("busy", "running")


def _live_label(s: Session) -> str:
    if not s.live:
        return ""
    if s.live_status == "busy":
        label = "● working"
    elif s.live_status in ("idle", "waiting"):
        label = "● waiting for you"
    else:
        label = "● running"
    if s.live_source == "cwd-heuristic":
        label += " (guessed)"
    return label


def _live_cell(s: Session, now: float, recent: float) -> Text:
    if s.live:
        if s.live_source == "cwd-heuristic":
            return Text("◌", style="green bold")
        return Text("●", style="green bold" if _busy(s) else "yellow bold")
    if now - s.mtime < recent:
        return Text("·", style="dim")
    return Text(" ")


def _group_cells(g: ProjectGroup, now: float, recent: float, host_w: int, proj_w: int,
                 what_w: int, folded: bool) -> list[Text]:
    if g.live:
        live = Text("●", style="green bold" if g.busy else "yellow bold")
    elif now - g.mtime < recent:
        live = Text("·", style="dim")
    else:
        live = Text(" ")
    abbrev = {"claude": "cl", "codex": "cx"}
    tools = "both" if len(g.tools) > 1 else abbrev.get(g.tools[0], g.tools[0][:2])
    summary = Text(overflow="ellipsis", no_wrap=True)
    summary.append(f"{len(g.sessions)} session{'s' if len(g.sessions) != 1 else ''}", style="bold")
    if g.live:
        summary.append(f" · {g.live} live", style="green" if g.busy else "yellow")
    if folded:
        summary.append("   " + (g.sessions[0].headline), style="dim")
    summary.truncate(max(8, what_w), overflow="ellipsis")
    return [
        live,
        Text(("▸ " if folded else "▾ ") + tools, style="dim"),
        Text(", ".join(g.hosts)[:host_w], style="dim"),
        Text(g.name[:proj_w], style="bold"),
        Text(relative_age(g.mtime, now), justify="right",
             style="" if now - g.mtime < 86400 else "dim"),
        summary,
    ]


def _group_detail(g: ProjectGroup, now: float, folded: bool) -> Group:
    head = Text()
    head.append("project  ", style="dim")
    head.append(g.name, style="bold")
    head.append(f"   {len(g.sessions)} sessions", style="dim")
    if g.live:
        head.append(f"   ● {g.live} live", style="green bold" if g.busy else "yellow bold")
    head.append(f"   last active {relative_age(g.mtime, now)} ago", style="dim")
    lines = [head]
    for host, cwd in g.cwds[:6]:
        lines.append(Text.assemble(("  ", ""), (host, "bold"), ("  ", ""), (cwd, "dim")))
    if len(g.cwds) > 6:
        lines.append(Text(f"  … {len(g.cwds) - 6} more locations", style="dim"))
    for s in g.sessions[:4]:
        mark = "●" if s.live else " "
        lines.append(Text.assemble(
            (f"  {mark} ", "green bold" if s.live else ""),
            (f"{s.tool:<6} ", TOOL_STYLE.get(s.tool, "")),
            (f"{relative_age(s.mtime, now):>4}  ", "dim"),
            s.headline[:100],
        ))
    if len(g.sessions) > 4:
        lines.append(Text(f"    … {len(g.sessions) - 4} more", style="dim"))
    hint = "Enter unfolds" if folded else "Enter folds"
    lines.append(Text(f"\n{hint} · g back to the flat list · S syncs these hosts", style="dim"))
    return Group(*lines)


def _what_cell(s: Session, width: int) -> Text:
    t = Text(overflow="ellipsis", no_wrap=True)
    if s.title:
        t.append(s.title, style="bold")
        if s.first_prompt:
            t.append("  ")
            t.append(s.first_prompt, style="dim")
    else:
        t.append(s.first_prompt or "(no prompt)", style="" if s.first_prompt else "dim")
    t.truncate(max(8, width), overflow="ellipsis")
    return t
