import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import Overseer
import offloader
import telemetry_db


class TestTelemetryDB(unittest.TestCase):

  def setUp(self):
    self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    self.orig_db_path = telemetry_db.DB_PATH
    telemetry_db.DB_PATH = os.path.join(self.tmp_dir.name, "test_telemetry.db")
    telemetry_db.init_db()

  def tearDown(self):
    telemetry_db.DB_PATH = self.orig_db_path
    self.tmp_dir.cleanup()

  def test_rolling_baseline_ignores_offloaded_samples(self):
    telemetry_db.log_sample(40.0, 20.0, 1600.0, offloaded=False)
    telemetry_db.log_sample(60.0, 40.0, 3200.0, offloaded=False)
    # High VRAM during active gaming should not skew idle baseline
    telemetry_db.log_sample(95.0, 98.0, 7900.0, offloaded=True)

    baseline = telemetry_db.get_rolling_baseline(hours=24.0)
    self.assertEqual(baseline["samples"], 2)
    self.assertEqual(baseline["avg_ram"], 50.0)
    self.assertEqual(baseline["avg_vram"], 30.0)

  def test_store_and_forward_alert_queue(self):
    alert_id = telemetry_db.queue_alert(
        title="Test Alert",
        description="Simulated offline spike",
        color=0xFFAA00,
        fields=[{"name": "RAM", "value": "90%", "inline": True}],
    )
    self.assertIsNotNone(alert_id)

    pending = telemetry_db.get_pending_alerts(limit=5)
    self.assertEqual(len(pending), 1)
    self.assertEqual(pending[0]["title"], "Test Alert")

    telemetry_db.mark_alert_sent(alert_id)
    self.assertEqual(len(telemetry_db.get_pending_alerts(limit=5)), 0)


class TestOffloaderRules(unittest.TestCase):

  def test_atomic_state_roundtrip(self):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
      test_state = os.path.join(tmpdir, "offloader_state.json")
      with patch.object(offloader, "STATE_FILE", test_state):
        offloader.set_offload_state(True, "Unloaded for test_game.exe", 45, "")
        self.assertTrue(os.path.exists(test_state))
        self.assertFalse(os.path.exists(f"{test_state}.tmp"))


class TestOverseerTelemetry(unittest.TestCase):

  def test_pdh_vram_fallback_parses_and_sums_processes(self):
    fake_pdh_json = """[
      {"InstanceName": "pid_1001_luid_0x00000000_0x0000d123_phys_0", "CookedValue": 2147483648},
      {"InstanceName": "pid_1001_luid_0x00000000_0x0000d123_phys_0_part2", "CookedValue": 1073741824},
      {"InstanceName": "pid_2002_luid_0x00000000_0x0000d123_phys_0", "CookedValue": 524288000}
    ]"""

    mock_proc_result = MagicMock(returncode=0, stdout=fake_pdh_json)

    def fake_psutil_process(pid):
      proc = MagicMock()
      proc.name.return_value = (
          "llama-server.exe" if pid == 1001 else "dwm.exe"
      )
      return proc

    with (
        patch.object(Overseer.os, "name", "nt"),
        patch.object(
            Overseer.subprocess, "run", return_value=mock_proc_result
        ),
        patch.object(
            Overseer.psutil, "Process", side_effect=fake_psutil_process
        ),
    ):
      report = Overseer.get_top_vram_processes(handle=None, limit=2)

    lines = report.splitlines()
    self.assertEqual(len(lines), 2)
    # 2048 MB + 1024 MB = 3072 MB summed for PID 1001
    self.assertEqual(lines[0], "• llama-server.exe: 3072 MB")
    # 500 MB for PID 2002
    self.assertEqual(lines[1], "• dwm.exe: 500 MB")


if __name__ == "__main__":
  unittest.main()
