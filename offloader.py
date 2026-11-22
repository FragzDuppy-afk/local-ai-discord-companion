import json
import os
import subprocess
import time
from dotenv import load_dotenv
import psutil

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
        r"c:\program files (x86)\steam\steamapps\common",
        r"d:\steamlibrary\steamapps\common",
        r"c:\xboxgames",
        r"d:\xboxgames",
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


# ==============================================================================
# 2. ATOMIC STATE & HELPER FUNCTIONS
# ==============================================================================
def set_offload_state(offloaded: bool, reason: str = "") -> None:
  """Atomically writes offload status so ai_bot.py & overseer.py never read a partial file."""
  tmp_file = f"{STATE_FILE}.tmp"
  payload = {
      "vram_offloaded": offloaded,
      "reason": reason,
      "timestamp": time.time(),
  }
  try:
    with open(tmp_file, "w", encoding="utf-8") as f:
      json.dump(payload, f, indent=2)
    os.replace(tmp_file, STATE_FILE)
  except OSError as e:
    print(f"[Offloader] Warning: Could not update state file: {e}")


def normalize_path(path_str: str) -> str:
  """Normalizes and lowercases Windows paths safely."""
  if not path_str:
    return ""
  return os.path.normpath(path_str).lower()


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


# ==============================================================================
# 3. MAIN WATCHER LOOP
# ==============================================================================
def main():
  print("[Offloader] Active — watching configured game libraries (1.0s poll)...")
  vram_offloaded = False
  last_game_seen_time = 0.0
  set_offload_state(False, "Offloader initialized")

  while True:
    heavy_active, trigger_source = is_heavy_game_running()

    if heavy_active:
      last_game_seen_time = time.time()
      if not vram_offloaded:
        print(f"[Offloader] Heavy game detected: {trigger_source}")
        if run_lms_command(UNLOAD_CMD, "VRAM/RAM Unload"):
          vram_offloaded = True
          set_offload_state(True, f"Unloaded for {trigger_source}")

    else:
      if vram_offloaded and (
          time.time() - last_game_seen_time >= RELOAD_COOLDOWN_SEC
      ):
        print(f"[Offloader] Game closed and {RELOAD_COOLDOWN_SEC:.0f}s cooldown passed.")
        if AUTO_RELOAD_ENABLED:
          run_lms_command(RELOAD_CMD, "VRAM/RAM Reload")
        vram_offloaded = False
        set_offload_state(False, "Game exited; VRAM/RAM clear")

    time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
  main()
