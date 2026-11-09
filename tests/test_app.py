import asyncio
import json

from textual.widgets import DataTable, Input

from sessionboard.app import SessionBoard
from sessionboard.config import Config, HostSpec, Settings
from sessionboard.model import HostState, ProjectGroup, Session
from sessionboard.state import load_state


def make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = Config(tmp_path / "config.toml", Settings(sync_on_start=False, auto_sync_seconds=0),
                    [HostSpec("example")])
    app = SessionBoard(config)
    app.states = [HostState(name="example", group="example", data={"claude": {"sessions": [
        {"id": "older", "tool": "claude", "project": "alpha", "mtime": 10,
         "first_prompt": "example older"},
        {"id": "newer", "tool": "claude", "project": "beta", "mtime": 30,
         "first_prompt": "example newer"},
        {"id": "live", "tool": "claude", "project": "beta", "mtime": 20,
         "first_prompt": "example live", "live": True},
    ]}})]
    return app


def test_pin_sort_filter_refresh_and_unpin(tmp_path, monkeypatch):
    async def exercise():
        app = make_app(tmp_path, monkeypatch)
        async with app.run_test(size=(120, 40)) as pilot:
            assert [s.id for s in app.rows] == ["live", "newer", "older"]
            table = app.query_one(DataTable)
            table.move_cursor(row=2)
            await pilot.press("p")
            assert [s.id for s in app.rows] == ["older", "live", "newer"]
            assert app._selected_session().id == "older"
            await pilot.press("o")
            assert app.sort_mode == "recent"
            assert [s.id for s in app.rows] == ["older", "newer", "live"]
            app._rebuild()
            assert app.rows[0].id == "older"
            await pilot.press("l")
            assert [s.id for s in app.rows] == ["live"]  # pins obey filters
            await pilot.press("l")
            table.move_cursor(row=0)
            await pilot.press("p")
            assert [s.id for s in app.rows] == ["newer", "live", "older"]
            assert app._selected_session().id == "older"
    asyncio.run(exercise())


def test_grouped_pin_restart_and_view_preferences(tmp_path, monkeypatch):
    async def exercise():
        app = make_app(tmp_path, monkeypatch)
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(DataTable).move_cursor(row=2)
            await pilot.press("p", "g", "z", "o", "t", "h", "a")
            assert isinstance(app.rows[0], Session) and app.rows[0].id == "older"
            assert all(isinstance(row, ProjectGroup) for row in app.rows[1:])
            app.query_one("#filter", Input).value = "example"
            await pilot.pause()
            saved = load_state(app._state_path)
            assert saved.by_project and saved.collapsed and saved.pinned
            assert saved.sort_mode == "recent" and saved.tool_filter == "claude"
            assert saved.group_filter == "example" and saved.show_archived
            assert "query" not in json.loads(app._state_path.read_text())
        restarted = make_app(tmp_path, monkeypatch)
        async with restarted.run_test(size=(120, 40)) as pilot:
            assert restarted.query == ""
            assert restarted.query_one("#filter", Input).value == ""
            assert load_state(restarted._state_path) == saved
            assert restarted.rows[0].id == "older"
            await pilot.press("p")
            assert not restarted.pinned
            assert isinstance(restarted.rows[0], ProjectGroup)
            assert restarted._selected_session().id == "older"
            # Unpinning unfolds only that session's project; the others stay folded.
            assert sum(isinstance(row, Session) for row in restarted.rows) == 1
    asyncio.run(exercise())


def test_sort_modes_and_pins_missing_from_cache(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    app.sessions = []
    from sessionboard.model import merge_sessions

    sessions = merge_sessions(app.states)
    for mode in ("project", "host", "tool"):
        app.sort_mode = mode
        rows = app._layout_rows(sessions)
        expected = ["older", "newer", "live"] if mode == "project" else ["newer", "live", "older"]
        assert [s.id for s in rows] == expected
    key = next(s.key for s in sessions if s.id == "older")
    app.pinned.add(key)
    app._layout_rows([s for s in sessions if s.id != "older"])
    assert key in app.pinned
    assert app._layout_rows(sessions)[0].id == "older"


def test_unpin_opens_folded_project(tmp_path, monkeypatch):
    async def exercise():
        app = make_app(tmp_path, monkeypatch)
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(DataTable).move_cursor(row=2)
            await pilot.press("p", "g", "z")
            assert "alpha" in app.collapsed
            await pilot.press("p")
            assert "alpha" not in app.collapsed
            assert app._selected_session().id == "older"
            assert isinstance(app.rows[-2], ProjectGroup) and app.rows[-2].name == "alpha"
            assert app.rows[-1].id == "older"
    asyncio.run(exercise())


def test_hidden_sessions_restart_filters_and_unhide(tmp_path, monkeypatch):
    async def exercise():
        app = make_app(tmp_path, monkeypatch)
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(DataTable).move_cursor(row=1)
            await pilot.press("x")
            assert [s.id for s in app.rows] == ["live", "older"]
            assert app._selected_session().id == "older"
            assert len(app.sessions) == 3
            app._rebuild()
            assert [s.id for s in app.rows] == ["live", "older"]
            saved = load_state(app._state_path)
            assert saved.hidden and not saved.show_hidden
        restarted = make_app(tmp_path, monkeypatch)
        async with restarted.run_test(size=(120, 40)) as pilot:
            assert [s.id for s in restarted.rows] == ["live", "older"]
            await pilot.press("H")
            assert [s.id for s in restarted.rows] == ["live", "newer", "older"]
            assert "[hidden]" in restarted.query_one(DataTable).get_row_at(1)[-1].plain
            assert load_state(restarted._state_path).show_hidden
            await pilot.press("l")
            assert [s.id for s in restarted.rows] == ["live"]
            await pilot.press("l")
        showing = make_app(tmp_path, monkeypatch)
        async with showing.run_test(size=(120, 40)) as pilot:
            assert showing.show_hidden and len(showing.rows) == 3
            showing.query_one(DataTable).move_cursor(row=1)
            await pilot.press("x", "H")
            assert not showing.hidden and not showing.show_hidden
            assert [s.id for s in showing.rows] == ["live", "newer", "older"]
            assert load_state(showing._state_path).hidden == []
    asyncio.run(exercise())


def test_hidden_pins_groups_and_empty_list(tmp_path, monkeypatch):
    async def exercise():
        app = make_app(tmp_path, monkeypatch)
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(DataTable).move_cursor(row=2)
            await pilot.press("p", "x", "g")
            assert len(app.pinned) == 1  # hiding preserves a pin
            assert all(row.key not in app.hidden for row in app.rows)
            assert [row.name for row in app.rows if isinstance(row, ProjectGroup)] == ["beta"]
            app.query_one(DataTable).move_cursor(row=0)
            await pilot.press("x")  # project rows cannot be hidden
            assert len(app.hidden) == 1
            await pilot.press("H")
            assert app.rows[0].id == "older"
            app.query_one(DataTable).move_cursor(row=0)
            await pilot.press("x", "H", "g")
            assert app.rows[0].id == "older" and not app.hidden
            await pilot.press("x", "x", "x")
            assert app.rows == [] and len(app.hidden) == 3
            await pilot.press("x", "H")
            assert len(app.rows) == 3
    asyncio.run(exercise())


def test_cli_respects_hidden_before_limit(tmp_path, monkeypatch, capsys):
    from sessionboard import cli
    from sessionboard.state import ViewState, save_state

    app = make_app(tmp_path, monkeypatch)
    save_state(app._state_path, ViewState(hidden=["example|claude|live"], show_hidden=True))
    monkeypatch.setattr(cli, "load_cache", lambda config: app.states)
    # CLI visibility is controlled by its own explicit flag, not the TUI toggle.
    args = cli._parser().parse_args(["list", "--json", "-n", "1"])
    assert cli._cmd_list(app.config, args) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["id"] == "newer" and rows[0]["hidden"] is False
    args = cli._parser().parse_args(["list", "--json", "--show-hidden"])
    cli._cmd_list(app.config, args)
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 3 and rows[0]["hidden"] is True


def test_vim_paging_and_jump_keys(tmp_path, monkeypatch):
    async def exercise():
        app = make_app(tmp_path, monkeypatch)
        app.states[0].data["claude"]["sessions"] = [
            {"id": f"s{i:02d}", "tool": "claude", "project": f"p{i}", "mtime": 1000 - i,
             "first_prompt": "example"} for i in range(60)]
        async with app.run_test(size=(120, 30)) as pilot:
            table = app.query_one(DataTable)
            await pilot.press("G")
            assert table.cursor_row == 59
            await pilot.press("0")
            assert table.cursor_row == 0
            await pilot.press("ctrl+f")
            paged = table.cursor_row
            assert paged > 1
            await pilot.press("ctrl+f")
            assert table.cursor_row > paged
            await pilot.press("ctrl+b", "ctrl+b")
            assert table.cursor_row == 0
            # In the filter box the same keys are ordinary text.
            await pilot.press("slash", "0", "G")
            assert app.query == "0G"
    asyncio.run(exercise())


def test_cursor_stays_visible_when_detail_pane_grows(tmp_path, monkeypatch):
    async def exercise():
        app = make_app(tmp_path, monkeypatch)
        sessions = [{"id": f"s{i:02d}", "tool": "claude", "project": f"p{i}", "mtime": 1000 - i,
                     "first_prompt": "short"} for i in range(40)]
        # The last row's long prompts make the detail pane taller than for the others.
        sessions[-1].update(title="title", first_prompt="long " * 200, last_prompt="more " * 200)
        app.states[0].data["claude"]["sessions"] = sessions
        async with app.run_test(size=(100, 40)) as pilot:
            table = app.query_one(DataTable)
            await pilot.pause()  # let the first layout settle, as it has in real use
            before = table.size.height
            await pilot.press("G")
            await pilot.pause()
            assert table.size.height < before
            visible_rows = table.scrollable_content_region.height - table.header_height
            assert table.cursor_row == 39
            assert table.scroll_y <= 39 < table.scroll_y + visible_rows
    asyncio.run(exercise())
