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


load_dotenv()

# ==============================================================================
# 1. CONFIGURATION: DRIVE PATHS, WHITELISTS & TIMERS
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "offloader_state.json")

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

# LM Studio CLI commands (reads LLM_MODEL and AUTO_RELOAD_ENABLED from .env)
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemma-4-e2b")
AUTO_RELOAD_ENABLED = (
    os.getenv("OFFLOADER_AUTO_RELOAD", "false").strip().lower() == "true"
)

UNLOAD_CMD = ["lms", "unload", "--all"]
RELOAD_CMD = ["lms", "load", LLM_MODEL, "--gpu", "max"]

POLL_INTERVAL_SEC = float(os.getenv("OFFLOADER_POLL_INTERVAL", "1.0"))
RELOAD_COOLDOWN_SEC = float(os.getenv("OFFLOADER_RELOAD_COOLDOWN", "20.0"))

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
  """Atomically writes offload & session status for AI_Bot.py and overseer.py."""
  tmp_file = f"{STATE_FILE}.tmp"
  payload = {
      "vram_offloaded": offloaded,
      "reason": reason,
      "session_minutes": session_minutes,
      "session_milestone": session_milestone,
      "timestamp": time.time(),
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


def is_heavy_game_running() -> tuple[bool, str]:
  """
    Scans active processes to detect non-whitelisted games.
    Returns (True, trigger_label) if an offload is needed, else (False, "").
    """
  norm_roots = [normalize_path(p) for p in WATCHED_GAME_DIRS]

  for proc in psutil.process_iter(["name", "exe"]):
    try:
      proc_name = (proc.info["name"] or "").lower()
      proc_exe = normalize_path(proc.info["exe"])

      if not proc_name or not proc_exe:
        continue

      # Rule 1: Skip explicitly whitelisted executables
      if proc_name in WHITELISTED_EXES:
        continue

      # Rule 2: Catch standalone heavy engines like Modded Java Minecraft
      if proc_name in HEAVY_STANDALONE_EXES:
        return True, proc_name

      # Rule 3: Check if executable runs from a watched game root directory
      for root in norm_roots:
        if proc_exe.startswith(root + os.sep):
          relative_path = os.path.relpath(proc_exe, root)
          game_folder = relative_path.split(os.sep)[0].lower()

          if game_folder not in WHITELISTED_FOLDERS:
            return True, f"{game_folder} ({proc_name})"

    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
      continue

  return False, ""


def run_lms_command(cmd: list[str], action_label: str) -> bool:
  """Runs an LM Studio CLI command silently and returns True if successful."""
  try:
    print(f"[Offloader] Executing {action_label}: {' '.join(cmd)}")
    subprocess.run(
        cmd,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return True
  except FileNotFoundError:
    print("[Offloader] Error: 'lms' CLI not found in PATH.")
    return False
  except subprocess.CalledProcessError as e:
    print(f"[Offloader] Command failed ({action_label}): {e}")
    return False


def get_gpu_temp_c() -> int | None:
  """Returns current NVIDIA GPU core temp in Celsius, or None if unavailable."""
  if not NVML_AVAILABLE:
    return None
  try:
    pynvml.nvmlInit()
    handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
    return int(temp)
  except pynvml.NVMLError:
    return None
  finally:
    try:
      pynvml.nvmlShutdown()
    except pynvml.NVMLError:
      pass


# ==============================================================================
# 3. MAIN WATCHER LOOP
# ==============================================================================
def main():
  print("[Offloader] Active — watching configured game libraries (1.0s poll)...")
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

      # Start a new session clock if none is active (or previous grace period expired)
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
          vram_offloaded = True
          set_offload_state(
              True,
              f"Unloaded for {trigger_source}",
              session_mins,
              current_milestone,
          )
          last_state_sync = now
      elif milestone_changed or (now - last_state_sync >= 60.0):
        # Update session_minutes every 60s (or immediately when a milestone changes)
        set_offload_state(
            True,
            f"Unloaded for {trigger_source}",
            session_mins,
            current_milestone,
        )
        last_state_sync = now

    else:
      # Reset the session timer and sticky milestone only after the grace period expires
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

        # Hold reload if GPU is still heat-soaked above RELOAD_MAX_GPU_TEMP_C
        if gpu_temp is not None and gpu_temp > RELOAD_MAX_GPU_TEMP_C:
          set_offload_state(
              True,
              f"Cooling down ({gpu_temp}°C > {RELOAD_MAX_GPU_TEMP_C}°C target)",
              active_mins,
              current_milestone,
          )
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

    time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
  main()
