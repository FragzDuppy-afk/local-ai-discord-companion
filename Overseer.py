import json
import os
import subprocess
import time
import urllib.request
from dotenv import load_dotenv
import psutil

load_dotenv()

# Try importing pynvml (nvidia-ml-py) for direct NVIDIA GPU VRAM telemetry
try:
  import pynvml

  NVML_AVAILABLE = True
except ImportError:
  NVML_AVAILABLE = False

# ==============================================================================
# 1. CONFIGURATION: THRESHOLDS, WEBHOOK & SHARED STATE (LOADED FROM .ENV)
# ==============================================================================
DISCORD_WEBHOOK_URL = os.getenv("OVERSEER_WEBHOOK_URL", "")

# Shared JSON state file written by offloader.py in the same directory
OFFLOADER_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "offloader_state.json"
)

# Alert Thresholds (configurable via .env, with safe defaults)
VRAM_THRESHOLD_PERCENT = float(os.getenv("OVERSEER_VRAM_THRESHOLD", "85.0"))
RAM_THRESHOLD_PERCENT = float(os.getenv("OVERSEER_RAM_THRESHOLD", "85.0"))
SUSTAINED_SECONDS = int(os.getenv("OVERSEER_SUSTAINED_SECONDS", "10"))
POLL_INTERVAL_SEC = float(os.getenv("OVERSEER_POLL_INTERVAL", "2.0"))
ALERT_COOLDOWN_SEC = int(os.getenv("OVERSEER_ALERT_COOLDOWN", "300"))


# ==============================================================================
# 2. OFFLOADER JSON BRIDGE & EMERGENCY RAM RELIEF
# ==============================================================================
def get_offloader_state() -> dict:
  """Safely reads offloader_state.json written by offloader.py."""
  default_state = {
      "vram_offloaded": False,
      "reason": "Offloader inactive or state file missing",
      "timestamp": 0.0,
  }
  if not os.path.exists(OFFLOADER_STATE_FILE):
    return default_state

  try:
    with open(OFFLOADER_STATE_FILE, "r", encoding="utf-8") as f:
      data = json.load(f)
      return {
          "vram_offloaded": bool(data.get("vram_offloaded", False)),
          "reason": str(data.get("reason", "")),
          "timestamp": float(data.get("timestamp", 0.0)),
      }
  except (OSError, json.JSONDecodeError, ValueError):
    return default_state


def check_and_relieve_ram(ram_percent: float) -> str:
  """
    Checks offloader status during a spike.
    If System RAM >= threshold and LM Studio is still loaded, runs an emergency unload.
    """
  state = get_offloader_state()

  if state["vram_offloaded"]:
    return f"🛡️ Offloader Status: Active ({state['reason']})"

  if ram_percent >= RAM_THRESHOLD_PERCENT:
    try:
      subprocess.run(
          ["lms", "unload", "--all"],
          check=True,
          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
      )
      return (
          "🚨 Emergency Action: Executed `lms unload --all` to free System RAM"
      )
    except Exception as e:
      return f"⚠️ Emergency Unload Failed: {e}"

  return "🟢 Offloader Status: Idle (LLM loaded)"


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
  """Finds the top GPU VRAM consumers via NVML."""
  if not NVML_AVAILABLE or handle is None:
    return "• NVML unavailable"
  try:
    nv_procs = pynvml.nvmlDeviceGetComputeRunningProcesses(
        handle
    ) + pynvml.nvmlDeviceGetGraphicsRunningProcesses(handle)
    merged = {}
    for item in nv_procs:
      used_mb = (item.usedGpuMemory or 0) / (1024**2)
      try:
        name = psutil.Process(item.pid).name()
      except (psutil.NoSuchProcess, psutil.AccessDenied):
        name = f"PID {item.pid}"
      merged[name] = max(merged.get(name, 0.0), used_mb)

    sorted_gpu = sorted(merged.items(), key=lambda x: x[1], reverse=True)
    if not sorted_gpu:
      return "• No active GPU processes reported"
    return "\n".join(
        f"• {name}: {mb:.0f} MB" for name, mb in sorted_gpu[:limit]
    )
  except pynvml.NVMLError:
    return "• Could not query GPU processes"


def send_discord_alert(message: str) -> None:
  """Sends a formatted telemetry alert to Discord using standard library urllib."""
  print(f"\n[Overseer Alert]\n{message}\n")
  if (
      not DISCORD_WEBHOOK_URL
      or "YOUR_DISCORD_WEBHOOK_URL" in DISCORD_WEBHOOK_URL
  ):
    return

  payload = json.dumps({"content": message}).encode("utf-8")
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
    with urllib.request.urlopen(req, timeout=5):
      pass
  except Exception as e:
    print(f"[Overseer] Discord webhook failed: {e}")


# ==============================================================================
# 4. MAIN TELEMETRY LOOP
# ==============================================================================
def main():
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

  ram_spike_start = 0.0
  vram_spike_start = 0.0
  last_ram_alert = 0.0
  last_vram_alert = 0.0

  try:
    while True:
      now = time.time()
      ram_percent = psutil.virtual_memory().percent
      used_vram, total_vram, vram_percent = get_gpu_vram_stats(gpu_handle)

      # --- Check Sustained System RAM Spike ---
      if ram_percent >= RAM_THRESHOLD_PERCENT:
        if ram_spike_start == 0.0:
          ram_spike_start = now
        elif (now - ram_spike_start >= SUSTAINED_SECONDS) and (
            now - last_ram_alert >= ALERT_COOLDOWN_SEC
        ):
          offloader_note = check_and_relieve_ram(ram_percent)
          top_ram = get_top_ram_processes(3)
          msg = (
              "🧠 **Overseer: High System RAM Usage**\n"
              f"System RAM Usage: **{ram_percent:.1f}%**\n"
              f"{offloader_note}\n\n"
              f"**Top System RAM Processes:**\n{top_ram}"
          )
          send_discord_alert(msg)
          last_ram_alert = now
      else:
        ram_spike_start = 0.0

      # --- Check Sustained GPU VRAM Spike ---
      if vram_percent >= VRAM_THRESHOLD_PERCENT:
        if vram_spike_start == 0.0:
          vram_spike_start = now
        elif (now - vram_spike_start >= SUSTAINED_SECONDS) and (
            now - last_vram_alert >= ALERT_COOLDOWN_SEC
        ):
          offloader_note = check_and_relieve_ram(ram_percent)
          top_vram = get_top_vram_processes(gpu_handle, 3)
          msg = (
              "🎮 **Overseer: High GPU VRAM Usage**\n"
              f"GPU VRAM: **{vram_percent:.1f}%** ({used_vram:.0f} /"
              f" {total_vram:.0f} MB)\n"
              f"{offloader_note}\n\n"
              f"**Top GPU VRAM Processes:**\n{top_vram}"
          )
          send_discord_alert(msg)
          last_vram_alert = now
      else:
        vram_spike_start = 0.0

      time.sleep(POLL_INTERVAL_SEC)

  finally:
    if NVML_AVAILABLE and gpu_handle is not None:
      try:
        pynvml.nvmlShutdown()
      except pynvml.NVMLError:
        pass


if __name__ == "__main__":
  main()
