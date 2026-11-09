import json
import stat

from sessionboard.state import ViewState, load_state, save_state, state_path


def test_private_round_trip_and_config_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = state_path(tmp_path / "first.toml")
    other = state_path(tmp_path / "second.toml")
    state = ViewState(by_project=True, collapsed=["demo"],
                      tool_filter="codex", group_filter="example", live_only=True,
                      show_archived=True, sort_mode="recent", pinned=["example|codex|123"],
                      hidden=["example|claude|456"], show_hidden=True)
    save_state(path, state)
    assert load_state(path) == state
    assert load_state(other) == ViewState()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    save_state(path, ViewState())
    assert load_state(path) == ViewState()
    assert list(path.parent.glob("*.tmp")) == []


def test_malformed_and_partial_state(tmp_path):
    path = tmp_path / "state.json"
    for value in ("{", "[]", '{"version": 42}', "null"):
        path.write_text(value)
        assert load_state(path) == ViewState()
    path.write_text(json.dumps({"version": 1, "by_project": True, "collapsed": [1],
                                "pinned": {}, "sort_mode": [], "tool_filter": [],
                                "live_only": "false", "query": 8,
                                "hidden": [1], "show_hidden": "true"}))
    assert load_state(path) == ViewState(by_project=True)


def test_saved_query_from_older_versions_is_ignored(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1, "query": "git", "live_only": True}))
    assert load_state(path) == ViewState(live_only=True)
    save_state(path, load_state(path))
    assert "query" not in json.loads(path.read_text())
