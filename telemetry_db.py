import json
import os
import sqlite3
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("TELEMETRY_DB_PATH", os.path.join(BASE_DIR, "telemetry.db"))
RETENTION_DAYS = float(os.getenv("TELEMETRY_RETENTION_DAYS", "7"))


def get_conn() -> sqlite3.Connection:
  """Opens a crash-safe SQLite connection with WAL mode for concurrent reads/writes."""
  conn = sqlite3.connect(DB_PATH, timeout=5.0)
  conn.execute("PRAGMA journal_mode=WAL;")
  conn.execute("PRAGMA synchronous=NORMAL;")
  return conn


def init_db() -> None:
  """Creates telemetry and store-and-forward alert tables if they do not exist."""
  with get_conn() as conn:
    conn.execute("""
            CREATE TABLE IF NOT EXISTS telemetry_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                ram_percent REAL NOT NULL,
                vram_percent REAL NOT NULL,
                vram_used_mb REAL NOT NULL,
                offloaded INTEGER NOT NULL
            )
        """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry_samples(ts);"
    )
    conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                color INTEGER NOT NULL,
                fields_json TEXT,
                sent INTEGER DEFAULT 0
            )
        """)


def log_sample(
    ram_percent: float, vram_percent: float, vram_used_mb: float, offloaded: bool
) -> None:
  """Inserts one hardware telemetry snapshot into SQLite."""
  try:
    with get_conn() as conn:
      conn.execute(
          """
                INSERT INTO telemetry_samples (ts, ram_percent, vram_percent, vram_used_mb, offloaded)
                VALUES (?, ?, ?, ?, ?)
                """,
          (
              time.time(),
              round(ram_percent, 2),
              round(vram_percent, 2),
              round(vram_used_mb, 1),
              1 if offloaded else 0,
          ),
      )
  except sqlite3.Error as e:
    print(f"[TelemetryDB] Warning: Could not log sample ({e})")


def get_rolling_baseline(hours: float = 24.0) -> dict:
  """Returns rolling average RAM/VRAM percentages and sample count over the lookback window."""
  cutoff = time.time() - (hours * 3600.0)
  try:
    with get_conn() as conn:
      row = conn.execute(
          """
                SELECT COUNT(*), AVG(ram_percent), AVG(vram_percent)
                FROM telemetry_samples
                WHERE ts >= ? AND offloaded = 0
                """,
          (cutoff,),
      ).fetchone()
      if row and row[0] > 0:
        return {
            "samples": int(row[0]),
            "avg_ram": round(float(row[1]), 1),
            "avg_vram": round(float(row[2]), 1),
        }
  except sqlite3.Error as e:
    print(f"[TelemetryDB] Warning: Baseline query failed ({e})")
  return {"samples": 0, "avg_ram": 0.0, "avg_vram": 0.0}


def queue_alert(
    title: str, description: str, color: int, fields: list[dict] | None = None
) -> int | None:
  """Persists an outbound Discord embed to SQLite before network transmission."""
  fields_str = json.dumps(fields) if fields else None
  try:
    with get_conn() as conn:
      cur = conn.execute(
          """
                INSERT INTO alert_queue (ts, title, description, color, fields_json, sent)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
          (time.time(), title, description, color, fields_str),
      )
      return cur.lastrowid
  except sqlite3.Error as e:
    print(f"[TelemetryDB] Warning: Could not queue alert ({e})")
    return None


def mark_alert_sent(alert_id: int) -> None:
  """Marks a queued Discord alert as delivered."""
  try:
    with get_conn() as conn:
      conn.execute("UPDATE alert_queue SET sent = 1 WHERE id = ?", (alert_id,))
  except sqlite3.Error:
    pass


def get_pending_alerts(limit: int = 5) -> list[dict]:
  """Fetches unsent alerts so Overseer can flush them once connectivity returns."""
  try:
    with get_conn() as conn:
      rows = conn.execute(
          """
                SELECT id, ts, title, description, color, fields_json
                FROM alert_queue
                WHERE sent = 0
                ORDER BY id ASC
                LIMIT ?
                """,
          (limit,),
      ).fetchall()
      pending = []
      for row in rows:
        pending.append({
            "id": row[0],
            "ts": row[1],
            "title": row[2],
            "description": row[3],
            "color": row[4],
            "fields": json.loads(row[5]) if row[5] else None,
        })
      return pending
  except sqlite3.Error:
    return []


def prune_old_records() -> None:
  """Deletes old telemetry samples and delivered alerts to preserve disk headroom."""
  cutoff = time.time() - (RETENTION_DAYS * 86400.0)
  try:
    with get_conn() as conn:
      conn.execute("DELETE FROM telemetry_samples WHERE ts < ?", (cutoff,))
      conn.execute(
          "DELETE FROM alert_queue WHERE sent = 1 AND ts < ?", (cutoff,)
      )
  except sqlite3.Error as e:
    print(f"[TelemetryDB] Warning: Prune failed ({e})")
