import json
import os
import time
from subsystems import hardware


def test_classify_hardware_state_thresholds():
    assert hardware.classify_hardware_state({"percent": 45.0, "temp_c": 50}) == "NOMINAL"
    assert hardware.classify_hardware_state({"percent": 68.0, "temp_c": 55}) == "WARM"
    assert hardware.classify_hardware_state({"percent": 40.0, "temp_c": 65}) == "WARM"
    assert hardware.classify_hardware_state({"percent": 82.0, "temp_c": 58}) == "STRAINED"
    assert hardware.classify_hardware_state({"percent": 50.0, "temp_c": 75}) == "STRAINED"


def test_offloader_liveness_detects_stale_heartbeat(tmp_path):
    state_file = tmp_path / "offloader_state.json"
    stale_ts = time.time() - 30.0
    payload = {
        "vram_offloaded": True,
        "reason": "Unloaded for game.exe",
        "heartbeat_ts": stale_ts,
        "timestamp": stale_ts,
        "offloader_pid": os.getpid(),
    }
    state_file.write_text(json.dumps(payload), encoding="utf-8")

    state = hardware.read_offloader_state(str(state_file))
    assert state["vram_offloaded"] is True
    assert hardware.is_offloader_live(state, stale_sec=15.0) is False


def test_offloader_liveness_accepts_fresh_heartbeat():
    now = time.time()
    state = {
        "vram_offloaded": False,
        "heartbeat_ts": now,
        "timestamp": now,
        "offloader_pid": os.getpid(),
    }
    assert hardware.is_offloader_live(state, stale_sec=15.0, now=now) is True


def test_evaluate_session_milestone_fires_once(tmp_path):
    state_file = tmp_path / "offloader_state.json"
    payload = {
        "vram_offloaded": True,
        "reason": "Unloaded for Helldivers2",
        "session_minutes": 125,
        "session_milestone": "2h",
    }
    state_file.write_text(json.dumps(payload), encoding="utf-8")

    # First check triggers the 2h milestone
    msg1, last1 = hardware.evaluate_session_milestone(str(state_file), "", user_title="Operator")
    assert msg1 is not None
    assert "2.1h" in msg1
    assert last1 == "2h"

    # Second check with same milestone does not re-fire
    msg2, last2 = hardware.evaluate_session_milestone(str(state_file), last1, user_title="Operator")
    assert msg2 is None
    assert last2 == "2h"
