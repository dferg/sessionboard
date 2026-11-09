import json
import subprocess
import sys

import pytest

from sessionboard.config import ConfigError, _provider_hosts, load_config, EXAMPLE_CONFIG


def provider(monkeypatch, output, **kwargs):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], kwargs.get("returncode", 0), output.encode(), b"private diagnostic"))
    return _provider_hosts({"command": ["example-host-tool"], **kwargs.get("config", {})}, 1)


def test_provider_formats_and_defaults(monkeypatch):
    hosts = provider(monkeypatch, json.dumps([
        "one.example", {"ssh": "two.example", "name": "Second", "group": "shared",
                        "python": "/usr/bin/python3"},
    ]))
    assert [(h.name, h.ssh, h.group_name) for h in hosts] == [
        ("one.example", "one.example", "one.example"), ("Second", "two.example", "shared")]
    assert not any(h.is_local for h in hosts)
    hosts = provider(monkeypatch, "one.example\n\n user@two.example \n",
                     config={"format": "lines"})
    assert [h.ssh for h in hosts] == ["one.example", "user@two.example"]
    assert provider(monkeypatch, "[]") == []


@pytest.mark.parametrize("value", [
    {}, [None], [{"name": "private-value"}], [{"ssh": "-oProxyCommand=private-value"}],
    ["bad host"], [""], [{"ssh": "one.example", "name": 3}],
    [{"ssh": "one.example", "unknown": "private-value"}],
    [{"ssh": "one.example", "name": "bad\nname"}],
])
def test_invalid_provider_records_do_not_disclose_output(monkeypatch, value):
    with pytest.raises(ConfigError) as exc:
        provider(monkeypatch, json.dumps(value))
    assert "private-value" not in str(exc.value)


@pytest.mark.parametrize("config", [
    {"command": "tool --json"}, {"command": []}, {"command": [3]},
    {"command": ["tool"], "timeout": 0}, {"command": ["tool"], "timeout": True},
    {"command": ["tool"], "format": "csv"}, {"command": ["tool"], "extra": True},
])
def test_invalid_provider_config(config):
    with pytest.raises(ConfigError):
        _provider_hosts(config, 1)


def test_execution_contract_and_errors(monkeypatch):
    def run(command, **kwargs):
        assert command == ["/example/tool", "--json"]
        assert kwargs == {"stdin": subprocess.DEVNULL, "capture_output": True, "timeout": 7}
        raise subprocess.TimeoutExpired(command, 7, output=b"private-value")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ConfigError, match="timed out after 7s"):
        _provider_hosts({"command": ["/example/tool", "--json"], "timeout": 7}, 2)
    with pytest.raises(ConfigError, match="exited with status 2") as exc:
        provider(monkeypatch, "private-value", returncode=2)
    assert "private" not in str(exc.value)
    with pytest.raises(ConfigError, match="UTF-8 json"):
        provider(monkeypatch, "private-value")


def test_static_hosts_and_earlier_providers_win(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('''
[settings]
local = false
[[hosts]]
name = "explicit"
ssh = "one.example"
group = "manual"
[[host_providers]]
command = ["example-tool"]
[[host_providers]]
command = ["example-tool"]
''')
    provider(monkeypatch, json.dumps([
        {"name": "alias", "ssh": "one.example"},
        {"name": "explicit", "ssh": "different.example"},
        "two.example",
    ]))
    config = load_config(cfg)
    assert len(config.hosts) == 2
    assert config.hosts[0].group_name == "manual"
    assert config.hosts[1].ssh == "two.example"


def test_real_executable_and_example_config(tmp_path):
    # Exercise actual argv execution, no shell and no network.
    hosts = _provider_hosts({"command": [sys.executable, "-c",
                            'print(\'["one.example"]\')']}, 1)
    assert hosts[0].ssh == "one.example"
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE_CONFIG.format(local_name="example"))
    assert load_config(path).hosts[0].is_local
