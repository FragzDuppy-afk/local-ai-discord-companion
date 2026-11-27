import os
import socket
import pytest
from subsystems import lifecycle
import json
import threading


def test_loto_lockout_engage_check_and_clear(tmp_path):
    lockout_file = str(tmp_path / "lockout.flag")

    # Initially unlocked
    locked, reason = lifecycle.is_lockout_active(lockout_file)
    assert locked is False
    assert reason == ""

    # Engage lockout
    lifecycle.engage_lockout(lockout_file, reason="UPS on battery < 20%", source="nut_ups")
    locked, reason = lifecycle.is_lockout_active(lockout_file)
    assert locked is True
    assert "UPS on battery" in reason

    # Clear lockout
    assert lifecycle.clear_lockout(lockout_file) is True
    assert lifecycle.clear_lockout(lockout_file) is False
    locked, _ = lifecycle.is_lockout_active(lockout_file)
    assert locked is False


def test_preflight_blocks_when_lockout_active(tmp_path):
    lockout_file = str(tmp_path / "lockout.flag")
    lifecycle.engage_lockout(lockout_file, reason="Manual !estop", source="discord")

    healthy, messages = lifecycle.run_preflight_checks(
        discord_token="valid_token",
        channel_id=123456789,
        lockout_file=lockout_file,
    )
    assert healthy is False
    assert any("[LOTO LOCKOUT]" in m for m in messages)


def test_preflight_blocks_when_credentials_missing(tmp_path):
    lockout_file = str(tmp_path / "lockout.flag")

    healthy, messages = lifecycle.run_preflight_checks(
        discord_token="",
        channel_id=0,
        lockout_file=lockout_file,
    )
    assert healthy is False
    assert any("[CONFIG ERROR]" in m for m in messages)


def test_preflight_passes_with_warning_when_lm_studio_offline(tmp_path):
    lockout_file = str(tmp_path / "lockout.flag")

    # Find an unused ephemeral port so LM Studio is guaranteed unreachable
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        unused_port = s.getsockname()[1]

    healthy, messages = lifecycle.run_preflight_checks(
        discord_token="valid_token",
        channel_id=123456789,
        lockout_file=lockout_file,
        lm_studio_port=unused_port,
    )
    assert healthy is True
    assert any("[PREFLIGHT WARN]" in m for m in messages)


def test_single_instance_guard_socket_conflict(monkeypatch):
    # Force POSIX socket path so we can test duplicate detection safely on Windows or Linux
    monkeypatch.setattr(os, "name", "posix")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        test_port = probe.getsockname()[1]

    guard1 = lifecycle.SingleInstanceGuard(linux_port=test_port)
    guard2 = lifecycle.SingleInstanceGuard(linux_port=test_port)

    try:
        assert guard1.acquire() is True
        assert guard2.acquire() is False
    finally:
        guard1.release()

    # After release, a new guard can acquire the port cleanly
    assert guard2.acquire() is True
    guard2.release()

def test_ups_disabled_mode_skips_checks():
    status, charge, critical = lifecycle.check_ups_status(mode="disabled")
    assert status == "DISABLED"
    assert charge == 100
    assert critical is False


def test_ups_mock_mode_detects_healthy_and_critical_battery(tmp_path):
    mock_file = tmp_path / "mock_ups.json"

    # Healthy on-battery state above threshold
    mock_file.write_text(
        json.dumps({"status": "OB", "battery_charge": 65}), encoding="utf-8"
    )
    status, charge, critical = lifecycle.check_ups_status(
        mode="mock", mock_file=str(mock_file), min_battery_pct=25
    )
    assert status == "OB"
    assert charge == 65
    assert critical is False

    # Critical on-battery state at/below threshold -> triggers LOTO shutdown
    mock_file.write_text(
        json.dumps({"status": "OB", "battery_charge": 20}), encoding="utf-8"
    )
    status, charge, critical = lifecycle.check_ups_status(
        mode="mock", mock_file=str(mock_file), min_battery_pct=25
    )
    assert status == "OB"
    assert charge == 20
    assert critical is True


def test_ups_nut_mode_parses_tcp_server(monkeypatch):
    # Test NUT parsing cleanly without needing a real upsd daemon running
    def fake_nut_query(host, port, ups_name, var_name, timeout=2.0):
        if var_name == "ups.status":
            return "OB LB"
        if var_name == "battery.charge":
            return "14"
        return None

    monkeypatch.setattr(lifecycle, "_query_nut_var", fake_nut_query)
    status, charge, critical = lifecycle.check_ups_status(
        mode="nut", ups_name="cyberpower", min_battery_pct=25
    )
    assert status == "OB LB"
    assert charge == 14
    assert critical is True