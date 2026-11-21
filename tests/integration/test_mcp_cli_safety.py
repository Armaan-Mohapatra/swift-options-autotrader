"""Diagnostics must not touch a real broker in CI or offline mode."""
import json

from swift_autotrader.app import main


def test_offline_mcp_diagnostics_connects_nowhere(capsys, monkeypatch):
    monkeypatch.setenv("PAPER_ONLY", "true")
    monkeypatch.setenv("LIVE_ENABLED", "false")
    monkeypatch.setenv("ALLOW_BROKER_WRITES", "false")
    assert main(["mcp-diagnostics"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "REQUIRES_LOCAL_AUTH"
    assert payload["connection_attempted"] is False
    assert payload["real_broker_writes_enabled"] is False


def test_diagnostics_connection_guard_fails_in_ci(capsys, monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("ROBINHOOD_MCP_INTERACTIVE", "true")
    assert main(["mcp-diagnostics", "--connect"]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "ERROR"
    assert payload["reason"] == "LocalAuthNotConfigured"
