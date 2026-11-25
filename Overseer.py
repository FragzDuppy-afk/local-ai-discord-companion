import contextlib
from datetime import datetime
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dotenv import load_dotenv
import psutil
import telemetry_db

load_dotenv()

try:
  import pynvml

  NVML_AVAILABLE = True
except ImportError:
  NVML_AVAILABLE = False

# ==============================================================================
# 1. CONFIGURATION: THRESHOLDS, WEBHOOK & SHARED STATE (LOADED FROM .ENV)
# ==============================================================================
DISCORD_WEBHOOK_URL = os.getenv("OVERSEER_WEBHOOK_URL", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OFFLOADER_STATE_FILE = os.path.join(BASE_DIR, "offloader_state.json")
LMS_LOCK_FILE = os.path.join(BASE_DIR, "lms_cli.lock")
LMS_TIMEOUT_SEC = float(os.getenv("OVERSEER_LMS_TIMEOUT_SEC", "20.0"))

VRAM_THRESHOLD_PERCENT = float(os.getenv("OVERSEER_VRAM_THRESHOLD", "85.0"))
RAM_THRESHOLD_PERCENT = float(os.getenv("OVERSEER_RAM_THRESHOLD", "85.0"))
SUSTAINED_SECONDS = int(os.getenv("OVERSEER_SUSTAINED_SECONDS", "10"))
POLL_INTERVAL_SEC = float(os.getenv("OVERSEER_POLL_INTERVAL", "2.0"))
ALERT_COOLDOWN_SEC = int(os.getenv("OVERSEER_ALERT_COOLDOWN", "300"))
SAMPLE_LOG_INTERVAL_SEC = float(os.getenv("OVERSEER_SAMPLE_INTERVAL", "10.0"))
DRIFT_DELTA_PERCENT = float(os.getenv("OVERSEER_DRIFT_DELTA", "25.0"))
MIN_BASELINE_SAMPLES = int(os.getenv("OVERSEER_MIN_BASELINE_SAMPLES", "60"))
OFFLOADER_STALE_SEC = float(os.getenv("OVERSEER_OFFLOADER_STALE_SEC", "15.0"))


# Embed Colors matched to Overseer's purple avatar & alert severity
COLOR_BOOT = 0x9B59B6      # Purple (Matches Overseer's holographic glow)
COLOR_WARNING = 0xFFAA00   # Amber (Sustained RAM/VRAM spike)
COLOR_CRITICAL = 0xFF2A2A  # Red (Emergency lms unload triggered)
COLOR_RESOLVED = 0x00FF7F  # Green (Memory returned below threshold)


# ==============================================================================
# 2. OFFLOADER JSON BRIDGE & EMERGENCY RAM RELIEF
# ==============================================================================
def get_offloader_state() -> dict:
  """Safely reads offloader_state.json written by offloader.py."""
  default_state = {
      "vram_offloaded": False,
      "reason": "Offloader inactive or state file missing",
      "timestamp": 0.0,
      "heartbeat_ts": 0.0,
      "offloader_pid": None,
  }
  if not os.path.exists(OFFLOADER_STATE_FILE):
    return default_state

  try:
    with open(OFFLOADER_STATE_FILE, "r", encoding="utf-8") as f:
      data = json.load(f)
      ts = float(data.get("timestamp", 0.0))
      hb = float(data.get("heartbeat_ts", ts))
      pid_raw = data.get("offloader_pid")
      pid = int(pid_raw) if pid_raw is not None else None
      return {
          "vram_offloaded": bool(data.get("vram_offloaded", False)),
          "reason": str(data.get("reason", "")),
          "timestamp": ts,
          "heartbeat_ts": hb,
          "offloader_pid": pid,
      }
  except (OSError, json.JSONDecodeError, ValueError):
    return default_state


def is_offloader_live(state: dict, now: float | None = None) -> bool:
  """Returns True if offloader.py has a recent heartbeat and (if known) an active PID."""
  current_ts = time.time() if now is None else now
  last_beat = max(
      float(state.get("heartbeat_ts", 0.0)),
      float(state.get("timestamp", 0.0)),
  )
  if last_beat <= 0.0 or (current_ts - last_beat) > OFFLOADER_STALE_SEC:
    return False

  pid = state.get("offloader_pid")
  if pid is not None:
    try:
      if not psutil.pid_exists(pid):
        return False
      proc = psutil.Process(pid)
      if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
        return False
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
      return False

  return True


@contextlib.contextmanager
def _lms_cli_lock(wait_sec: float = 5.0):
  """Best-effort cross-process lock around `lms` CLI calls without ever deadlocking."""
  deadline = time.time() + wait_sec
  acquired = False
  while time.time() < deadline:
    try:
      fd = os.open(LMS_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
      os.close(fd)
      acquired = True
      break
    except FileExistsError:
      try:
        if time.time() - os.path.getmtime(LMS_LOCK_FILE) > 60.0:
          os.remove(LMS_LOCK_FILE)
          continue
      except OSError:
        pass
      time.sleep(0.1)
    except OSError:
      break
  try:
    yield
  finally:
    if acquired:
      try:
        os.remove(LMS_LOCK_FILE)
      except OSError:
        pass


def check_and_relieve_memory(
    ram_percent: float, vram_percent: float
) -> tuple[str, bool]:
  """
  Tier 2 memory guard: defers to offloader.py when live, or runs an emergency
  unload only if offloader.py is stale/offline during a sustained spike.
  Returns (status_message, is_critical_action_taken).
  """
  state = get_offloader_state()
  offloader_live = is_offloader_live(state)

  if state["vram_offloaded"]:
    owner_tag = "Offloader Live" if offloader_live else "Offloader State Active"
    return f"🛡️ {owner_tag} ({state['reason']})", False

  ram_critical = ram_percent >= RAM_THRESHOLD_PERCENT
  vram_critical = vram_percent >= VRAM_THRESHOLD_PERCENT

  if ram_critical or vram_critical:
    target = "System RAM & GPU VRAM" if (ram_critical and vram_critical) else (
        "GPU VRAM" if vram_critical else "System RAM"
    )
    if offloader_live:
      return (
          f"🛡️ Deferred to Offloader (PID {state['offloader_pid']}) for {target}",
          False,
      )

    try:
      with _lms_cli_lock():
        subprocess.run(
            ["lms", "unload", "--all"],
            check=True,
            timeout=LMS_TIMEOUT_SEC,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
      return (
          f"🚨 Fallback Action (Offloader Offline): Executed `lms unload --all` to free {target}",
          True,
      )
    except FileNotFoundError:
      return (
          "⚠️ Fallback Unload Failed: `lms` CLI not found in PATH (run `lms bootstrap`)",
          True,
      )
    except subprocess.TimeoutExpired:
      return (
          f"⚠️ Fallback Unload Timed Out after {LMS_TIMEOUT_SEC:.0f}s",
          True,
      )
    except Exception as e:
      return f"⚠️ Fallback Unload Failed: {e}", True

  return (
      "🟢 Offloader Live (LLM loaded)"
      if offloader_live
      else "🟡 Offloader Offline / Stale (LLM loaded)"
  ), False


# ==============================================================================
# 3. HARDWARE & PROCESS TELEMETRY HELPERS
# ==============================================================================
def get_gpu_vram_stats(handle) -> tuple[float, float, float]:
  """Returns (used_mb, total_mb, used_percent) for the primary NVIDIA GPU."""
  if not NVML_AVAILABLE or handle is None:
    return 0.0, 0.0, 0.0
  try:
    mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
    used_mb = mem_info.used / (1024**2)
    total_mb = mem_info.total / (1024**2)
    percent = (mem_info.used / mem_info.total) * 100.0
    return used_mb, total_mb, percent
  except pynvml.NVMLError:
    return 0.0, 0.0, 0.0


def get_gpu_name(handle) -> str:
  """Returns the primary NVIDIA GPU model name."""
  if not NVML_AVAILABLE or handle is None:
    return "NVML Unavailable (RAM-Only Mode)"
  try:
    name = pynvml.nvmlDeviceGetName(handle)
    return name.decode("utf-8") if isinstance(name, bytes) else str(name)
  except pynvml.NVMLError:
    return "Unknown NVIDIA GPU"


def get_top_ram_processes(limit: int = 3) -> str:
  """Finds the top system RAM consumers formatted for Discord telemetry."""
  procs = []
  for p in psutil.process_iter(["name", "memory_info"]):
    try:
      mem_info = p.info["memory_info"]
      if mem_info:
        rss_mb = mem_info.rss / (1024**2)
        procs.append((rss_mb, p.info["name"] or "Unknown"))
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
      continue

  procs.sort(reverse=True, key=lambda x: x[0])
  return "\n".join(f"• {name}: {mb:.0f} MB" for mb, name in procs[:limit])


def get_top_vram_processes(handle, limit: int = 3) -> str:
  """Finds top GPU VRAM consumers via NVML, falling back to Windows PDH counters."""
  # 1. Try NVML first
  if NVML_AVAILABLE and handle is not None:
    try:
      nv_procs = pynvml.nvmlDeviceGetComputeRunningProcesses(
          handle
      ) + pynvml.nvmlDeviceGetGraphicsRunningProcesses(handle)
      merged = {}
      for item in nv_procs:
        used_mb = (item.usedGpuMemory or 0) / (1024**2)
        if used_mb > 0:
          try:
            name = psutil.Process(item.pid).name()
          except (psutil.NoSuchProcess, psutil.AccessDenied):
            name = f"PID {item.pid}"
          merged[name] = max(merged.get(name, 0.0), used_mb)

      if merged:
        sorted_gpu = sorted(merged.items(), key=lambda x: x[1], reverse=True)
        return "\n".join(
            f"• {name}: {mb:.0f} MB" for name, mb in sorted_gpu[:limit]
        )
    except pynvml.NVMLError:
      pass

  # 2. Windows WDDM Fallback via PowerShell PDH GPU Process Memory counters
  if os.name == "nt":
    try:
      cmd = [
          "powershell",
          "-NoProfile",
          "-Command",
          r"(Get-Counter '\GPU Process Memory(*)\Dedicated Usage' -ErrorAction SilentlyContinue).CounterSamples | Select-Object InstanceName, CookedValue | ConvertTo-Json",
      ]
      res = subprocess.run(
          cmd,
          capture_output=True,
          text=True,
          timeout=4,
          creationflags=subprocess.CREATE_NO_WINDOW,
      )
      if res.returncode == 0 and res.stdout.strip():
        samples = json.loads(res.stdout)
        if isinstance(samples, dict):
          samples = [samples]
        merged = {}
        for s in samples:
          mb = float(s.get("CookedValue", 0)) / (1024**2)
          if mb < 1.0:
            continue
          match = re.search(r"pid_(\d+)_", s.get("InstanceName", ""))
          if match:
            pid = int(match.group(1))
            try:
              name = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
              name = f"PID {pid}"
            merged[name] = merged.get(name, 0.0) + mb

        if merged:
          sorted_gpu = sorted(merged.items(), key=lambda x: x[1], reverse=True)
          return "\n".join(
              f"• {name}: {mb:.0f} MB" for name, mb in sorted_gpu[:limit]
          )
    except Exception:
      pass

  return "• No active GPU VRAM consumers reported"


# ==============================================================================
# 4. DISCORD WEBHOOK RELAY (WITH SQLITE STORE-AND-FORWARD QUEUE)
# ==============================================================================
def _post_embed_to_discord(
    title: str,
    description: str,
    color: int,
    fields: list[dict] | None = None,
    max_retries: int = 1,
    retry_delay: int = 5,
    custom_timestamp: str | None = None,
) -> bool:
  """Low-level HTTP POST helper for Discord webhooks."""
  if (
      not DISCORD_WEBHOOK_URL
      or "YOUR_DISCORD_WEBHOOK_URL" in DISCORD_WEBHOOK_URL
  ):
    return False

  timestamp = custom_timestamp or datetime.now().strftime("%I:%M:%S %p")
  embed = {
      "title": title,
      "description": description,
      "color": color,
      "footer": {"text": f"Overseer Unit 01 // {timestamp}"},
  }
  if fields:
    embed["fields"] = fields

  payload = json.dumps({
      "username": "Overseer Unit 01",
      "embeds": [embed],
  }).encode("utf-8")

  for attempt in range(1, max_retries + 1):
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "OverseerTelemetry/1.0",
        },
        method="POST",
    )
    try:
      with urllib.request.urlopen(req, timeout=8):
        return True
    except Exception as e:
      print(f"[Overseer] Webhook attempt {attempt}/{max_retries} failed: {e}")
      if attempt < max_retries:
        time.sleep(retry_delay)

  return False


def send_discord_embed(
    title: str,
    description: str,
    color: int,
    fields: list[dict] | None = None,
    max_retries: int = 1,
    retry_delay: int = 5,
) -> bool:
  """Queues embed in SQLite first so alerts survive network drops, then sends."""
  print(f"\n[Overseer] {title}\n{description}\n")
  alert_id = telemetry_db.queue_alert(title, description, color, fields)

  delivered = _post_embed_to_discord(
      title=title,
      description=description,
      color=color,
      fields=fields,
      max_retries=max_retries,
      retry_delay=retry_delay,
  )
  if delivered and alert_id is not None:
    telemetry_db.mark_alert_sent(alert_id)
  return delivered


def flush_pending_alerts() -> None:
  """Replays any queued offline alerts once Discord connectivity returns."""
  pending = telemetry_db.get_pending_alerts(limit=5)
  for item in pending:
    orig_time = datetime.fromtimestamp(item["ts"]).strftime("%I:%M:%S %p")
    delivered = _post_embed_to_discord(
        title=f"{item['title']} (Buffered)",
        description=item["description"],
        color=item["color"],
        fields=item["fields"],
        max_retries=1,
        custom_timestamp=f"Queued {orig_time}",
    )
    if delivered:
      telemetry_db.mark_alert_sent(item["id"])
    else:
      break  # Stop flushing if network is still down


def send_startup_ping(gpu_handle) -> None:
  """Sends a one-time purple boot embed with rolling baseline info."""
  gpu_name = get_gpu_name(gpu_handle)
  used_vram, total_vram, vram_percent = get_gpu_vram_stats(gpu_handle)
  ram_percent = psutil.virtual_memory().percent
  baseline = telemetry_db.get_rolling_baseline(24.0)

  vram_str = (
      f"{used_vram:.0f} / {total_vram:.0f} MB ({vram_percent:.1f}%)"
      if total_vram > 0
      else "Unavailable"
  )
  baseline_str = (
      f"RAM {baseline['avg_ram']}% | VRAM {baseline['avg_vram']}% ({baseline['samples']} samples)"
      if baseline["samples"] > 0
      else "Calibrating initial baseline..."
  )

  fields = [
      {"name": "Primary GPU", "value": gpu_name, "inline": False},
      {"name": "Initial VRAM", "value": vram_str, "inline": True},
      {"name": "System RAM", "value": f"{ram_percent:.1f}%", "inline": True},
      {"name": "24h Rolling Baseline", "value": baseline_str, "inline": False},
      {
          "name": "Alert Thresholds",
          "value": f"VRAM ≥ {VRAM_THRESHOLD_PERCENT:.0f}% | RAM ≥ {RAM_THRESHOLD_PERCENT:.0f}%",
          "inline": True,
      },
  ]

  send_discord_embed(
      title="[OVERSEER UNIT 01 // SUB-PROCESS ONLINE]",
      description=(
          "Automated hardware telemetry & surge relay (`pynvml` / `psutil` / `sqlite3`). "
          "Logging rolling baselines and watching VRAM/RAM capacity."
      ),
      color=COLOR_BOOT,
      fields=fields,
      max_retries=3,
      retry_delay=10,
  )


# ==============================================================================
# 5. MAIN TELEMETRY LOOP
# ==============================================================================
def main():
  telemetry_db.init_db()
  print(f"[Overseer] SQLite telemetry initialized -> {telemetry_db.DB_PATH}")

  if not DISCORD_WEBHOOK_URL:
    print(
        "[Overseer] Warning: OVERSEER_WEBHOOK_URL not set in .env — alerts"
        " will print to console only."
    )

  gpu_handle = None
  if NVML_AVAILABLE:
    try:
      pynvml.nvmlInit()
      gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
      print("[Overseer] NVML initialized — monitoring GPU VRAM & System RAM...")
    except pynvml.NVMLError as e:
      print(f"[Overseer] NVML warning: {e}")
  else:
    print(
        "[Overseer] Warning: 'nvidia-ml-py' not installed. Monitoring System"
        " RAM only."
    )

  send_startup_ping(gpu_handle)

  ram_spike_start = 0.0
  vram_spike_start = 0.0
  last_ram_alert = 0.0
  last_vram_alert = 0.0
  last_sample_log = 0.0
  last_prune_time = time.time()
  ram_alert_active = False
  vram_alert_active = False
  ram_drift_start = 0.0
  vram_drift_start = 0.0
  last_ram_drift_alert = 0.0
  last_vram_drift_alert = 0.0

  try:
    while True:
      now = time.time()
      ram_percent = psutil.virtual_memory().percent
      used_vram, total_vram, vram_percent = get_gpu_vram_stats(gpu_handle)
      offloader_state = get_offloader_state()

      # 1. Log telemetry sample to SQLite every SAMPLE_LOG_INTERVAL_SEC (default 10s)
      if now - last_sample_log >= SAMPLE_LOG_INTERVAL_SEC:
        telemetry_db.log_sample(
            ram_percent=ram_percent,
            vram_percent=vram_percent,
            vram_used_mb=used_vram,
            offloaded=offloader_state["vram_offloaded"],
        )
        last_sample_log = now
        flush_pending_alerts()  # Drain any offline alerts when network is healthy

      # 2. Prune old SQLite rows once per hour to preserve disk headroom
      if now - last_prune_time >= 3600.0:
        telemetry_db.prune_old_records()
        last_prune_time = now

      # 3. Check Sustained System RAM Spike
      if ram_percent >= RAM_THRESHOLD_PERCENT:
        if ram_spike_start == 0.0:
          ram_spike_start = now
        elif (now - ram_spike_start >= SUSTAINED_SECONDS) and (
            now - last_ram_alert >= ALERT_COOLDOWN_SEC
        ):
          offloader_note, is_critical = check_and_relieve_memory(
              ram_percent, vram_percent
          )
          top_ram = get_top_ram_processes(3)
          baseline = telemetry_db.get_rolling_baseline(24.0)
          delta_str = (
              f" (24h avg: {baseline['avg_ram']}%)"
              if baseline["samples"] > 0
              else ""
          )
          send_discord_embed(
              title="🧠 [SYSTEM RAM SATURATION ALERT]",
              description=(
                  "Sustained RAM spike detected above"
                  f" **{RAM_THRESHOLD_PERCENT:.0f}%**."
              ),
              color=COLOR_CRITICAL if is_critical else COLOR_WARNING,
              fields=[
                  {
                      "name": "System RAM",
                      "value": f"**{ram_percent:.1f}%**{delta_str}",
                      "inline": True,
                  },
                  {
                      "name": "Relay Action",
                      "value": offloader_note,
                      "inline": False,
                  },
                  {
                      "name": "Top RAM Consumers",
                      "value": top_ram,
                      "inline": False,
                  },
              ],
          )
          last_ram_alert = now
          ram_alert_active = True
      else:
        ram_spike_start = 0.0
        if ram_alert_active:
          send_discord_embed(
              title="🟢 [CONFLUX STABILIZED // RAM]",
              description=(
                  "System RAM dropped back below"
                  f" **{RAM_THRESHOLD_PERCENT:.0f}%**."
              ),
              color=COLOR_RESOLVED,
              fields=[
                  {
                      "name": "Current System RAM",
                      "value": f"**{ram_percent:.1f}%**",
                      "inline": True,
                  }
              ],
          )
          ram_alert_active = False

      # 4. Check Sustained GPU VRAM Spike
      if vram_percent >= VRAM_THRESHOLD_PERCENT:
        if vram_spike_start == 0.0:
          vram_spike_start = now
        elif (now - vram_spike_start >= SUSTAINED_SECONDS) and (
            now - last_vram_alert >= ALERT_COOLDOWN_SEC
        ):
          offloader_note, is_critical = check_and_relieve_memory(
              ram_percent, vram_percent
          )
          top_vram = get_top_vram_processes(gpu_handle, 3)
          baseline = telemetry_db.get_rolling_baseline(24.0)
          delta_str = (
              f" (24h avg: {baseline['avg_vram']}%)"
              if baseline["samples"] > 0
              else ""
          )
          send_discord_embed(
              title="🎮 [GPU VRAM SATURATION ALERT]",
              description=(
                  "Sustained VRAM spike detected above"
                  f" **{VRAM_THRESHOLD_PERCENT:.0f}%**."
              ),
              color=COLOR_CRITICAL if is_critical else COLOR_WARNING,
              fields=[
                  {
                      "name": "GPU VRAM",
                      "value": (
                          f"**{vram_percent:.1f}%** ({used_vram:.0f} /"
                          f" {total_vram:.0f} MB){delta_str}"
                      ),
                      "inline": True,
                  },
                  {
                      "name": "Relay Action",
                      "value": offloader_note,
                      "inline": False,
                  },
                  {
                      "name": "Top VRAM Consumers",
                      "value": top_vram,
                      "inline": False,
                  },
              ],
          )
          last_vram_alert = now
          vram_alert_active = True
      else:
        vram_spike_start = 0.0
        if vram_alert_active:
          send_discord_embed(
              title="🟢 [CONFLUX STABILIZED // VRAM]",
              description=(
                  "GPU VRAM dropped back below"
                  f" **{VRAM_THRESHOLD_PERCENT:.0f}%**."
              ),
              color=COLOR_RESOLVED,
              fields=[
                  {
                      "name": "Current GPU VRAM",
                      "value": (
                          f"**{vram_percent:.1f}%** ({used_vram:.0f} /"
                          f" {total_vram:.0f} MB)"
                      ),
                      "inline": True,
                  }
              ],
          )
          vram_alert_active = False

      # 5. Guarded Early Baseline-Drift Check (Muted while gaming/offloaded)
      if not offloader_state["vram_offloaded"]:
        baseline = telemetry_db.get_rolling_baseline(24.0)
        if baseline["samples"] >= MIN_BASELINE_SAMPLES:
          ram_delta = ram_percent - baseline["avg_ram"]
          vram_delta = vram_percent - baseline["avg_vram"]

          # --- Early RAM Drift ---
          if (
              ram_delta >= DRIFT_DELTA_PERCENT
              and ram_percent < RAM_THRESHOLD_PERCENT
          ):
            if ram_drift_start == 0.0:
              ram_drift_start = now
            elif (now - ram_drift_start >= SUSTAINED_SECONDS) and (
                now - last_ram_drift_alert >= ALERT_COOLDOWN_SEC
            ):
              top_ram = get_top_ram_processes(3)
              send_discord_embed(
                  title="📈 [EARLY DRIFT WARNING // SYSTEM RAM]",
                  description=(
                      f"RAM jumped **+{ram_delta:.1f}%** above your 24h"
                      " baseline while idle/active."
                  ),
                  color=COLOR_WARNING,
                  fields=[
                      {
                          "name": "Live vs. 24h Baseline",
                          "value": (
                              f"**{ram_percent:.1f}%** (Avg:"
                              f" {baseline['avg_ram']}%)"
                          ),
                          "inline": True,
                      },
                      {
                          "name": "Top RAM Consumers",
                          "value": top_ram,
                          "inline": False,
                      },
                  ],
              )
              last_ram_drift_alert = now
          else:
            ram_drift_start = 0.0

          # --- Early GPU VRAM Drift ---
          if (
              vram_delta >= DRIFT_DELTA_PERCENT
              and vram_percent < VRAM_THRESHOLD_PERCENT
          ):
            if vram_drift_start == 0.0:
              vram_drift_start = now
            elif (now - vram_drift_start >= SUSTAINED_SECONDS) and (
                now - last_vram_drift_alert >= ALERT_COOLDOWN_SEC
            ):
              top_vram = get_top_vram_processes(gpu_handle, 3)
              send_discord_embed(
                  title="📈 [EARLY DRIFT WARNING // GPU VRAM]",
                  description=(
                      f"VRAM jumped **+{vram_delta:.1f}%** above your 24h"
                      " baseline while LLM active."
                  ),
                  color=COLOR_WARNING,
                  fields=[
                      {
                          "name": "Live vs. 24h Baseline",
                          "value": (
                              f"**{vram_percent:.1f}%** (Avg:"
                              f" {baseline['avg_vram']}%)"
                          ),
                          "inline": True,
                      },
                      {
                          "name": "Top VRAM Consumers",
                          "value": top_vram,
                          "inline": False,
                      },
                  ],
              )
              last_vram_drift_alert = now
          else:
            vram_drift_start = 0.0
      else:
        # Reset drift timers cleanly whenever a game is running
        ram_drift_start = 0.0
        vram_drift_start = 0.0

      time.sleep(POLL_INTERVAL_SEC)

  finally:
    if NVML_AVAILABLE and gpu_handle is not None:
      try:
        pynvml.nvmlShutdown()
      except pynvml.NVMLError:
        pass


if __name__ == "__main__":
  main()