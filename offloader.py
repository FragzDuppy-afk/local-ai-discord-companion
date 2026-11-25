import contextlib
import json
import os
import subprocess
import time
from dotenv import load_dotenv
import psutil

try:
  import pynvml

  NVML_AVAILABLE = True
except ImportError:
  NVML_AVAILABLE = False

# Optional Windows WMI hook for instant process-creation detection (pip install wmi pywin32)
try:
  import wmi

  WMI_AVAILABLE = os.name == "nt"
except ImportError:
  WMI_AVAILABLE = False


load_dotenv()

# ==============================================================================
# 1. CONFIGURATION: DRIVE PATHS, WHITELISTS & TIMERS
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "offloader_state.json")
LMS_LOCK_FILE = os.path.join(BASE_DIR, "lms_cli.lock")
LMS_TIMEOUT_SEC = float(os.getenv("OFFLOADER_LMS_TIMEOUT_SEC", "20.0"))

GAMES_CONFIG_PATH = os.path.join(BASE_DIR, "games_config.json")
GAMES_EXAMPLE_PATH = os.path.join(BASE_DIR, "games_config.example.json")


def load_games_config() -> dict:
  """Loads games_config.json, falling back to games_config.example.json or safe defaults."""
  for path in (GAMES_CONFIG_PATH, GAMES_EXAMPLE_PATH):
    if os.path.exists(path):
      try:
        with open(path, "r", encoding="utf-8") as f:
          print(f"[Offloader] Loaded game config: {os.path.basename(path)}")
          return json.load(f)
      except (OSError, json.JSONDecodeError) as e:
        print(f"[Offloader] Warning: Failed to parse {path} ({e})")
  return {}


_games_cfg = load_games_config()

WATCHED_GAME_DIRS = _games_cfg.get(
    "watched_game_dirs",
    [
        # Windows default libraries
        r"c:\program files (x86)\steam\steamapps\common",
        r"d:\steamlibrary\steamapps\common",
        r"c:\xboxgames",
        r"d:\xboxgames",
        # Linux native & Flatpak Steam libraries (Proton games also run from here)
        "~/.local/share/Steam/steamapps/common",
        "~/.steam/debian-installation/steamapps/common",
        "~/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common",
    ],
)
WHITELISTED_FOLDERS = {
    f.lower()
    for f in _games_cfg.get(
        "whitelisted_folders", ["steamworks shared", "screenshots"]
    )
}
WHITELISTED_EXES = {
    e.lower()
    for e in _games_cfg.get(
        "whitelisted_exes",
        ["crashpad_handler.exe", "unitycrashhandler64.exe"],
    )
}
HEAVY_STANDALONE_EXES = {
    e.lower() for e in _games_cfg.get("heavy_standalone_exes", ["javaw.exe"])
}
WATCHED_PROCESS_NAMES = {
    e.lower() for e in _games_cfg.get("watched_process_names", [])
}

# LM Studio CLI commands (reads LLM_MODEL and AUTO_RELOAD_ENABLED from .env)
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemma-4-e2b")
AUTO_RELOAD_ENABLED = (
    os.getenv("OFFLOADER_AUTO_RELOAD", "false").strip().lower() == "true"
)

UNLOAD_CMD = ["lms", "unload", "--all"]
RELOAD_CMD = ["lms", "load", LLM_MODEL, "--gpu", "max"]

# 0.25s fallback poll keeps reaction fast even without WMI
POLL_INTERVAL_SEC = float(os.getenv("OFFLOADER_POLL_INTERVAL", "0.25"))
RELOAD_COOLDOWN_SEC = float(os.getenv("OFFLOADER_RELOAD_COOLDOWN", "20.0"))
VRAM_SETTLE_MAX_WAIT_SEC = float(os.getenv("OFFLOADER_VRAM_SETTLE_SEC", "8.0"))
VRAM_SETTLE_TARGET_PCT = float(os.getenv("OFFLOADER_VRAM_TARGET_PCT", "40.0"))

RELOAD_MAX_GPU_TEMP_C = int(os.getenv("RELOAD_MAX_GPU_TEMP_C", "60"))

# Gaming session milestone settings
SESSION_REMINDER_ENABLED = (
    os.getenv("SESSION_REMINDER_ENABLED", "true").strip().lower() == "true"
)
SESSION_REMARK_MINS = int(os.getenv("SESSION_REMARK_MINS", "120"))
SESSION_ESCALATE_MINS = int(os.getenv("SESSION_ESCALATE_MINS", "240"))
SESSION_GRACE_SEC = int(os.getenv("SESSION_GRACE_MINS", "5")) * 60


# ==============================================================================
# 2. ATOMIC STATE & HELPER FUNCTIONS
# ==============================================================================
def set_offload_state(
    offloaded: bool,
    reason: str = "",
    session_minutes: int = 0,
    session_milestone: str = "",
) -> None:
  """Atomically writes offload, liveness, and session status for AI_Bot.py and Overseer.py."""
  tmp_file = f"{STATE_FILE}.tmp"
  now = time.time()
  payload = {
      "vram_offloaded": offloaded,
      "reason": reason,
      "session_minutes": session_minutes,
      "session_milestone": session_milestone,
      "offloader_pid": os.getpid(),
      "heartbeat_ts": now,
      "timestamp": now,
  }
  try:
    with open(tmp_file, "w", encoding="utf-8") as f:
      json.dump(payload, f, indent=2)
    os.replace(tmp_file, STATE_FILE)
  except OSError as e:
    print(f"[Offloader] Warning: Could not update state file: {e}")


def normalize_path(path_str: str) -> str:
  """Expands user home (~), normalizes separators, and lowercases paths for Windows & Linux."""
  if not path_str:
    return ""
  return os.path.normpath(os.path.expanduser(path_str)).lower()


# Pre-compute watched directory paths once at startup instead of on every loop tick
NORM_WATCHED_ROOTS = [normalize_path(p) for p in WATCHED_GAME_DIRS]


def is_heavy_game_running() -> tuple[bool, str]:
  """
  Scans active processes to detect non-whitelisted games.
  Returns (True, trigger_label) if an offload is needed, else (False, "").
  """
  for proc in psutil.process_iter(["name", "exe"]):
    try:
      proc_name = (proc.info["name"] or "").lower()
      if not proc_name or proc_name in WHITELISTED_EXES:
        continue

      # Fast-path match on process name alone (immune to anti-cheat exe path AccessDenied)
      if proc_name in HEAVY_STANDALONE_EXES or proc_name in WATCHED_PROCESS_NAMES:
        return True, proc_name

      proc_exe = normalize_path(proc.info["exe"])
      if not proc_exe:
        continue

      for root in NORM_WATCHED_ROOTS:
        if proc_exe.startswith(root + os.sep):
          relative_path = os.path.relpath(proc_exe, root)
          game_folder = relative_path.split(os.sep)[0].lower()

          if game_folder not in WHITELISTED_FOLDERS:
            return True, f"{game_folder} ({proc_name})"

    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
      continue

  return False, ""


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


def run_lms_command(cmd: list[str], action_label: str) -> bool:
  """Runs an LM Studio CLI command with a timeout and lock; returns True if successful."""
  try:
    with _lms_cli_lock():
      print(f"[Offloader] Executing {action_label}: {' '.join(cmd)}")
      subprocess.run(
          cmd,
          check=True,
          timeout=LMS_TIMEOUT_SEC,
          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
      )
    return True
  except FileNotFoundError:
    print("[Offloader] Error: 'lms' CLI not found in PATH.")
    return False
  except subprocess.TimeoutExpired:
    print(f"[Offloader] Command timed out ({action_label} after {LMS_TIMEOUT_SEC}s).")
    return False
  except subprocess.CalledProcessError as e:
    print(f"[Offloader] Command failed ({action_label}): {e}")
    return False


def _open_nvml_handle():
  if not NVML_AVAILABLE:
    return None
  try:
    pynvml.nvmlInit()
    return pynvml.nvmlDeviceGetHandleByIndex(0)
  except pynvml.NVMLError:
    return None


def _close_nvml_handle() -> None:
  if not NVML_AVAILABLE:
    return
  try:
    pynvml.nvmlShutdown()
  except pynvml.NVMLError:
    pass


def wait_for_vram_release(
    max_wait_sec: float = VRAM_SETTLE_MAX_WAIT_SEC,
    poll_sec: float = 0.25,
    target_pct: float = VRAM_SETTLE_TARGET_PCT,
) -> float | None:
  """Polls VRAM after unload so downstream scripts don't race GPU driver cleanup."""
  handle = _open_nvml_handle()
  if handle is None:
    time.sleep(1.0)
    return None

  latest_pct = None
  deadline = time.time() + max_wait_sec
  try:
    while time.time() < deadline:
      mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
      if mem.total > 0:
        latest_pct = round((mem.used / mem.total) * 100.0, 1)
        if latest_pct <= target_pct:
          return latest_pct
      time.sleep(poll_sec)
  except pynvml.NVMLError:
    return latest_pct
  finally:
    _close_nvml_handle()

  return latest_pct


def get_gpu_temp_c() -> int | None:
  """Returns current NVIDIA GPU core temp in Celsius, or None if unavailable."""
  handle = _open_nvml_handle()
  if handle is None:
    return None
  try:
    temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
    return int(temp)
  except pynvml.NVMLError:
    return None
  finally:
    _close_nvml_handle()


# ==============================================================================
# 3. MAIN WATCHER LOOP
# ==============================================================================
def wait_for_next_tick(watcher, timeout_sec: float = POLL_INTERVAL_SEC) -> None:
  """
  Blocks up to timeout_sec, but wakes the millisecond Windows spawns a new process.
  Falls back cleanly to time.sleep() on Linux or if WMI is unavailable.
  """
  if watcher is None:
    time.sleep(timeout_sec)
    return
  try:
    # Block until a process creation event fires or timeout_ms expires
    event = watcher(timeout_ms=int(timeout_sec * 1000))
    # Check .Name first (standard for Win32_Process), falling back to .ProcessName
    proc_name = (
        getattr(event, "Name", None) or getattr(event, "ProcessName", "") or ""
    ).lower()
    # Wake immediately if it's a known heavy/watched process (or any non-whitelisted process)
    if proc_name and proc_name not in WHITELISTED_EXES:
      return
  except Exception:
    # WMI timeout (normal when no process spawned) or transient error — proceed to scan
    pass


def main():
  # Initialize WMI process creation watcher on Windows for sub-millisecond launch detection
  proc_watcher = None
  if WMI_AVAILABLE:
    try:
      c = wmi.WMI()
      proc_watcher = c.Win32_Process.watch_for("creation")
      print(
          f"[Offloader] Active — WMI instant hook + {POLL_INTERVAL_SEC}s"
          " fallback poll..."
      )
    except Exception as e:
      print(
          f"[Offloader] WMI hook unavailable ({e}); using"
          f" {POLL_INTERVAL_SEC}s poll..."
      )
  else:
    print(
        "[Offloader] Active — watching configured game libraries"
        f" ({POLL_INTERVAL_SEC}s poll)..."
    )

  vram_offloaded = False
  last_game_seen_time = 0.0
  session_start_time = 0.0
  current_milestone = ""
  last_state_sync = 0.0
  set_offload_state(False, "Offloader initialized")

  while True:
    now = time.time()
    heavy_active, trigger_source = is_heavy_game_running()

    if heavy_active:
      last_game_seen_time = now

      if session_start_time == 0.0:
        session_start_time = now
        current_milestone = ""

      session_mins = int((now - session_start_time) // 60)
      prev_milestone = current_milestone

      if SESSION_REMINDER_ENABLED:
        if session_mins >= SESSION_ESCALATE_MINS:
          current_milestone = "4h"
        elif session_mins >= SESSION_REMARK_MINS and current_milestone != "4h":
          current_milestone = "2h"

      milestone_changed = current_milestone != prev_milestone

      if not vram_offloaded:
        print(f"[Offloader] Heavy game detected: {trigger_source}")
        if run_lms_command(UNLOAD_CMD, "VRAM/RAM Unload"):
          settled_pct = wait_for_vram_release()
          settle_note = (
              f" (VRAM at {settled_pct}%)" if settled_pct is not None else ""
          )
          print(f"[Offloader] Unload handoff complete{settle_note}.")
          vram_offloaded = True
          set_offload_state(
              True,
              f"Unloaded for {trigger_source}",
              session_mins,
              current_milestone,
          )
          last_state_sync = time.time()
      elif milestone_changed or (now - last_state_sync >= 10.0):
        set_offload_state(
            True,
            f"Unloaded for {trigger_source}",
            session_mins,
            current_milestone,
        )
        last_state_sync = now

    else:
      if session_start_time > 0.0 and (
          now - last_game_seen_time >= SESSION_GRACE_SEC
      ):
        session_start_time = 0.0
        current_milestone = ""

      if vram_offloaded and (now - last_game_seen_time >= RELOAD_COOLDOWN_SEC):
        gpu_temp = get_gpu_temp_c()
        active_mins = (
            int((now - session_start_time) // 60)
            if session_start_time > 0.0
            else 0
        )

        if gpu_temp is not None and gpu_temp > RELOAD_MAX_GPU_TEMP_C:
          set_offload_state(
              True,
              f"Cooling down ({gpu_temp}°C > {RELOAD_MAX_GPU_TEMP_C}°C target)",
              active_mins,
              current_milestone,
          )
          last_state_sync = now
        else:
          temp_note = f" (GPU at {gpu_temp}°C)" if gpu_temp is not None else ""
          print(
              f"[Offloader] Game closed, {RELOAD_COOLDOWN_SEC:.0f}s cooldown"
              f" passed{temp_note}."
          )
          if AUTO_RELOAD_ENABLED:
            run_lms_command(RELOAD_CMD, "VRAM/RAM Reload")
          vram_offloaded = False
          set_offload_state(False, "Game exited; VRAM/RAM clear", 0, "")
          last_state_sync = now
      elif now - last_state_sync >= 10.0:
        # Refresh heartbeat periodically while idle or cooling down
        set_offload_state(
            vram_offloaded,
            "Game exited; VRAM/RAM clear"
            if not vram_offloaded
            else f"Cooling down ({get_gpu_temp_c()}°C > {RELOAD_MAX_GPU_TEMP_C}°C target)",
            int((now - session_start_time) // 60)
            if vram_offloaded and session_start_time > 0.0
            else 0,
            current_milestone if vram_offloaded else "",
        )
        last_state_sync = now

    # Wait for either a WMI process-creation event or the short fallback poll timer
    wait_for_next_tick(proc_watcher, POLL_INTERVAL_SEC)


if __name__ == "__main__":
  main()
