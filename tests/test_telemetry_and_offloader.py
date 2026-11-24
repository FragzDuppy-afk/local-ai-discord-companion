import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import offloader
import telemetry_db


class TestTelemetryDB(unittest.TestCase):

  def setUp(self):
    self.tmp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    self.tmp_db.close()
    self.orig_db_path = telemetry_db.DB_PATH
    telemetry_db.DB_PATH = self.tmp_db.name
    telemetry_db.init_db()

  def tearDown(self):
    telemetry_db.DB_PATH = self.orig_db_path
    for ext in ("", "-wal", "-shm"):
      path = self.tmp_db.name + ext
      if os.path.exists(path):
        os.remove(path)

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
    with tempfile.TemporaryDirectory() as tmpdir:
      test_state = os.path.join(tmpdir, "offloader_state.json")
      with patch.object(offloader, "STATE_FILE", test_state):
        offloader.set_offload_state(True, "Unloaded for test_game.exe", 45, "")
        self.assertTrue(os.path.exists(test_state))
        self.assertFalse(os.path.exists(f"{test_state}.tmp"))


if __name__ == "__main__":
  unittest.main()
