import asyncio
import contextlib
import ctypes
from datetime import datetime
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from ddgs import DDGS
import discord
from discord.ext import tasks
from dotenv import load_dotenv
from openai import OpenAI
import psutil
import telemetry_db

# Gracefully handle systems without NVIDIA drivers/libraries (matches Overseer.py)
try:
  import pynvml

  NVML_AVAILABLE = True
except ImportError:
  NVML_AVAILABLE = False

load_dotenv()

# ==============================================================================
# SINGLE-INSTANCE GUARD (PREVENTS DUPLICATE BOT PROCESSES ON WINDOWS & LINUX)
# ==============================================================================
if os.name == "nt":
  _mutex = ctypes.windll.kernel32.CreateMutexW(
      None, False, "LocalAIBotSingleInstanceMutex"
  )
  if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    print("[LOCK] AI Bot is already running in the background. Exiting duplicate.")
    sys.exit(0)
else:
  # On Linux/macOS, binding a local ephemeral port acts as an OS-managed mutex
  # that automatically releases even if the process is killed (SIGKILL).
  _instance_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  try:
    _instance_socket.bind(("127.0.0.1", 49152))
  except socket.error:
    print("[LOCK] AI Bot is already running in the background. Exiting duplicate.")
    sys.exit(0)

# ==============================================================================
# CONFIGURATION & THRESHOLDS
# ==============================================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
CYCLE_END_HOUR = int(os.getenv("CYCLE_END_HOUR", "22"))

DEFAULT_LOCATION = os.getenv("DEFAULT_LOCATION", "Chicago, IL")
TIMEZONE_LABEL = os.getenv("TIMEZONE_LABEL", "local time")

# ==============================================================================
# OPTIONAL FREE CLOUD FALLBACK (STRICTLY OPT-IN VIA GEMINI_API_KEY)
# ==============================================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_BASE_URL = os.getenv(
    "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
).strip()
GEMINI_FALLBACK_MODEL = os.getenv(
    "GEMINI_FALLBACK_MODEL", "gemma-4-26b-a4b-it"
).strip()
GEMINI_REASONING_EFFORT = os.getenv(
    "GEMINI_REASONING_EFFORT", "minimal"
).strip()

# Only instantiate the cloud OpenAI-compatible client when an API key is provided.
# Leaving GEMINI_API_KEY blank keeps the bot 100% local with zero cloud calls.
gemini_llm = (
    OpenAI(base_url=GEMINI_BASE_URL, api_key=GEMINI_API_KEY)
    if GEMINI_API_KEY
    else None
)

LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://127.0.0.1:1234/v1")
LM_STUDIO_PORT = os.getenv("LM_STUDIO_PORT", "1234")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemma-4-e2b")

if not DISCORD_TOKEN or CHANNEL_ID == 0:
  print(
      "[CONFIG ERROR] Missing DISCORD_TOKEN or CHANNEL_ID in your .env file."
  )
  sys.exit(1)

# Dynamic VRAM percentage thresholds (scales automatically to any GPU size)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RELAXED_VRAM_PERCENT = 80.0
PAUSE_VRAM_PERCENT = 90.0
RESUME_VRAM_PERCENT = 70.0
OFFLOADER_STALE_SEC = float(os.getenv("OFFLOADER_STALE_SEC", "15.0"))
SOUNDS_FOLDER = os.path.join(BASE_DIR, "Sounds")
SESSION_ALERTS_ENABLED = (
    os.getenv("SESSION_ALERTS_ENABLED", "true").strip().lower() == "true"
)


# ==============================================================================
# CHARACTER PRESET LOADER (LOADS FROM presets/<CHARACTER_PRESET>.json)
# ==============================================================================
CHARACTER_PRESET = os.getenv("CHARACTER_PRESET", "default").strip().lower()
PRESET_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "presets",
    f"{CHARACTER_PRESET}.json",
)

try:
  with open(PRESET_PATH, "r", encoding="utf-8") as f:
    PRESET = json.load(f)
  print(f"[PRESET] Loaded character preset: {PRESET_PATH}")
except FileNotFoundError:
  print(
      f"[PRESET WARNING] Could not find {PRESET_PATH}. Using built-in fallback."
  )
  PRESET = {}

BOT_NAME = PRESET.get("bot_name", "Atlas")
USER_TITLE = PRESET.get("user_title", "Operator")
AUDIO_COMMANDS = PRESET.get("audio_commands", {})

FALLBACK_FRAGMENTS = PRESET.get(
    "fallback_fragments",
    ["[SYSTEM LOG // STANDBY]\n`Local neural generator currently paused.`"],
)
TRANSMIT_GENERATOR_PROMPT = PRESET.get(
    "transmit_prompt",
    "Write a short, atmospheric 2-paragraph telemetry dispatch with a bracketed header.",
)
TRANSMIT_SCENARIOS = PRESET.get(
    "transmit_scenarios",
    ["Write a midnight diagnostic log from a remote weather station."],
)

_raw_prompt = PRESET.get(
    "system_prompt",
    f"You are {BOT_NAME}, a helpful local AI assistant. Address the user as {USER_TITLE}.",
)
SYSTEM_PROMPT = (
    _raw_prompt.replace("{USER_TITLE}", USER_TITLE)
    .replace("{DEFAULT_LOCATION}", DEFAULT_LOCATION)
    .replace("{TIMEZONE_LABEL}", TIMEZONE_LABEL)
)


WEATHER_KEYWORDS = {"weather", "temp", "temperature", "forecast", "outside"}

# Expanded to catch natural shopping, stock, location, and availability questions
SEARCH_KEYWORDS = {
    "search",
    "look up",
    "lookup",
    "google",
    "find out",
    "where can i",
    "where could i",
    "where to find",
    "where to buy",
    "nearest",
    "closest",
    "near me",
    "in stock",
    "stock",
    "availability",
    "available at",
    "how much is",
    "price of",
    "msrp",
    "news",
    "latest",
    "current",
    "today",
    "who won",
    "release date",
    "web",
    "internet",
    "online",
}

# Conversational filler words stripped before querying DuckDuckGo
_SEARCH_FILLER_RE = re.compile(
    r"\b("
    r"can you|could you|would you|please|hey|static|"
    r"search the web for|search for|search|look up|lookup|google|find out|"
    r"where i could possibly find|where could i possibly find|where can i find|"
    r"where could i find|where to find|where to buy|tell me|what is|whats|"
    r"the|a|an|at|for|in|my|zip code|zipcode"
    r")\b",
    flags=re.IGNORECASE,
)


def sanitize_search_query(raw_text: str) -> str:
  """Strips conversational filler so DDGS receives high-signal keywords."""
  # Normalize "5060ti" -> "5060 ti" and remove punctuation noise
  cleaned = re.sub(
      r"(\d{4})(ti|super|xt|xtx)\b", r"\1 \2", raw_text, flags=re.IGNORECASE
  )
  cleaned = re.sub(r"[^\w\s\-]", " ", cleaned)
  cleaned = _SEARCH_FILLER_RE.sub(" ", cleaned)
  cleaned = re.sub(r"\s+", " ", cleaned).strip()

  # Fallback to raw stripped text if regex removed too much
  if len(cleaned) < 4:
    cleaned = re.sub(r"[^\w\s\-]", " ", raw_text).strip()

  # Cap at 10 keywords so long prompts don't return empty [] results from DDGS
  tokens = cleaned.split()
  return " ".join(tokens[:10])


# Common retail & local business names for two-step ZIP fallback searches
_RETAILER_RE = re.compile(
    r"\b("
    r"best buy|bestbuy|micro center|microcenter|walmart|target|costco|"
    r"gamestop|newegg|staples|office depot|home depot|lowes|lowe's|"
    r"menards|fleet farm|ace hardware|cvs|walgreens|autozone|oreilly|o'reilly"
    r")\b",
    flags=re.IGNORECASE,
)


def extract_store_location_query(raw_text: str) -> str | None:
  """Extracts a fallback '<retailer> near <zip>' query when product+location search fails."""
  zip_match = re.search(r"\b(\d{5})\b", raw_text)
  if not zip_match:
    return None

  zip_code = zip_match.group(1)
  retailer_match = _RETAILER_RE.search(raw_text)
  if retailer_match:
    retailer = retailer_match.group(1).lower()
    return f"{retailer} stores {zip_code} {DEFAULT_LOCATION}"

  return None


# ==============================================================================
# CLIENT & STATE INITIALIZATION
# ==============================================================================
llm = OpenAI(base_url=LM_STUDIO_URL, api_key="lm-studio")

# Shared state file written by offloader.py in the same folder
OFFLOADER_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "offloader_state.json"
)


def _read_offloader_state() -> dict:
  """Reads offloader_state.json safely."""
  default_state = {
      "vram_offloaded": False,
      "reason": "",
      "session_minutes": 0,
      "session_milestone": "",
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
          "session_minutes": int(data.get("session_minutes", 0)),
          "session_milestone": str(data.get("session_milestone", "")),
          "timestamp": ts,
          "heartbeat_ts": hb,
          "offloader_pid": pid,
      }
  except (OSError, json.JSONDecodeError, ValueError):
    return default_state


def is_offloader_live(state: dict | None = None, now: float | None = None) -> bool:
  """Returns True if offloader.py has a fresh heartbeat and active PID."""
  data = state if state is not None else _read_offloader_state()
  current_ts = time.time() if now is None else now
  last_beat = max(
      float(data.get("heartbeat_ts", 0.0)),
      float(data.get("timestamp", 0.0)),
  )
  if last_beat <= 0.0 or (current_ts - last_beat) > OFFLOADER_STALE_SEC:
    return False

  pid = data.get("offloader_pid")
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


def get_gaming_offload_notice() -> str | None:
  """
  Checks offloader_state.json before querying LM Studio.
  Returns a clean status reply if VRAM is offloaded, or None if the LLM is ready.
  """
  data = _read_offloader_state()
  if data.get("vram_offloaded", False):
    reason = data.get("reason") or "a heavy application is active"
    return (
        "⚡ **Gaming / Relaxed Mode Active:** Local neural processing is"
        f" temporarily offloaded ({reason}) to preserve system stability and"
        " VRAM. Full capabilities will resume automatically once the session"
        " concludes."
    )
  return None


def check_session_milestone() -> str | None:
  """Reads offloader_state.json for single-fire 2h/4h gaming session milestones."""
  global last_handled_milestone
  if not SESSION_ALERTS_ENABLED:
    return None

  data = _read_offloader_state()
  if not data.get("vram_offloaded", False):
    last_handled_milestone = ""
    return None

  milestone = str(data.get("session_milestone", ""))
  session_mins = int(data.get("session_minutes", 0))
  reason = str(data.get("reason", "Active session")).replace("Unloaded for ", "")

  if milestone and milestone != last_handled_milestone:
    last_handled_milestone = milestone
    hours = round(session_mins / 60, 1)
    if milestone == "4h":
      return (
          f"⏳ **Extended Session Check-In ({hours}h), {USER_TITLE}:** "
          f"Active workload detected ({reason}). Consider stepping away for"
          " water, stretching, and resting your eyes before continuing."
      )
    if milestone == "2h":
      return (
          f"🕒 **Session Milestone ({hours}h), {USER_TITLE}:** "
          f"You've been running continuously ({reason}). Good checkpoint to grab"
          " water or stretch."
      )
  return None


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
chat_history = [{"role": "system", "content": SYSTEM_PROMPT}]
llm_lock = asyncio.Lock()


sent_end_today = False
pending_cycle_end = False
game_paused = False
relaxed_mode = False
startup_sent = False
last_handled_milestone = ""


# ==============================================================================
# HARDWARE TELEMETRY & DYNAMIC VRAM HELPERS
# ==============================================================================


# Reusable subprocess flag: hides console popups on Windows, evaluates to 0 on Linux
NO_WINDOW_FLAG = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def get_gpu_vram_stats() -> dict:
  """Queries total GPU VRAM usage and core temp directly via NVML."""
  if not NVML_AVAILABLE:
    return {"used_gb": 0.0, "total_gb": 0.0, "percent": 0.0, "temp_c": None}
  try:
    pynvml.nvmlInit()
    handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
    temp_c = int(
        pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
    )
    used_gb = round(mem.used / (1024**3), 2)
    total_gb = round(mem.total / (1024**3), 2)
    percent = round((mem.used / mem.total) * 100.0, 1)
    return {
        "used_gb": used_gb,
        "total_gb": total_gb,
        "percent": percent,
        "temp_c": temp_c,
    }
  except Exception:
    return {"used_gb": 0.0, "total_gb": 0.0, "percent": 0.0, "temp_c": None}
  finally:
    try:
      pynvml.nvmlShutdown()
    except Exception:
      pass


LMS_LOCK_FILE = os.path.join(BASE_DIR, "lms_cli.lock")
LMS_TIMEOUT_SEC = float(os.getenv("AI_BOT_LMS_TIMEOUT_SEC", "20.0"))


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


def _run_lms_guarded(cmd: list[str]) -> None:
  try:
    with _lms_cli_lock():
      subprocess.run(
          cmd,
          timeout=LMS_TIMEOUT_SEC,
          creationflags=NO_WINDOW_FLAG,
          stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
      )
  except Exception:
    pass


def unload_vram():
  """Fallback-only unload helper when offloader.py is not live."""
  _run_lms_guarded(["lms", "unload", "--all"])

def get_system_status():
  """Builds the formatted hardware manifest for the !status Discord command."""
  cpu = psutil.cpu_percent(interval=0.5)
  ram = psutil.virtual_memory()
  # Evaluates to "C:\\" on Windows and "/" on Linux
  disk = psutil.disk_usage(os.path.abspath(os.sep))
  vram = get_gpu_vram_stats()
  baseline = telemetry_db.get_rolling_baseline(24.0)
  offloader_live = is_offloader_live()

  ram_used_gb = round(ram.used / (1024**3), 1)
  ram_total_gb = round(ram.total / (1024**3), 1)
  disk_free_gb = round(disk.free / (1024**3), 1)

  has_baseline = baseline["samples"] > 0
  ram_avg_str = f" (24h Avg: {baseline['avg_ram']}%)" if has_baseline else ""
  vram_avg_str = f" (24h Avg: {baseline['avg_vram']}%)" if has_baseline else ""

  cpu_remark = (
      "NOMINAL" if cpu < 75 else "ELEVATED — Check background processes"
  )
  ram_remark = (
      f"NOMINAL{ram_avg_str}"
      if ram.percent < 80
      else f"HIGH LOAD{ram_avg_str} — Close unused apps"
  )

  if vram["percent"] >= PAUSE_VRAM_PERCENT:
    vram_remark = f"CRITICAL (>=90%){vram_avg_str} — Model paused/unloaded"
  elif vram["percent"] >= RELAXED_VRAM_PERCENT:
    vram_remark = f"RELAXED MODE (>=80%){vram_avg_str} — Context trimmed"
  else:
    vram_remark = f"NOMINAL{vram_avg_str}"

  disk_remark = (
      "NOMINAL" if disk_free_gb > 50 else "LOW SPACE — Archive old logs/builds"
  )
  baseline_line = (
      f"{baseline['samples']:,} Samples Logged | SQLite WAL Active"
      if has_baseline
      else "Calibrating (Waiting for Overseer samples)"
  )

  if game_paused:
    ai_state = "PAUSED"
  elif relaxed_mode:
    ai_state = "RELAXED"
  else:
    ai_state = "ONLINE"

  offloader_tag = "Offloader: LIVE" if offloader_live else "Offloader: OFFLINE"

  return (
      "```\n"
      f"=== {BOT_NAME.upper()} HARDWARE & EQUIPMENT MANIFEST ===\n"
      f"CPU LOAD     : {cpu:>5}%  | {cpu_remark}\n"
      f"SYSTEM RAM   : {ram_used_gb}/{ram_total_gb} GB ({ram.percent}%) | {ram_remark}\n"
      f"GPU VRAM     : {vram['used_gb']}/{vram['total_gb']} GB ({vram['percent']}%) | {vram_remark}\n"
      f"PRIMARY SSD  : {disk_free_gb:>5} GB Free | {disk_remark}\n"
      f"24H BASELINE : {baseline_line}\n"
      f"AI BACKEND   : {LLM_MODEL} (Port {LM_STUDIO_PORT}/v1) | {ai_state} | {offloader_tag}\n"
      "============================================\n"
      "```"
  )


def get_help_manifest() -> str:
  """Builds a dynamic command reference including preset-specific audio commands."""
  audio_list = ", ".join(AUDIO_COMMANDS.keys()) if AUDIO_COMMANDS else "None loaded"
  return (
      "```\n"
      f"=== {BOT_NAME.upper()} COMMAND & RELAY DIRECTORY ===\n"
      "!help            : Display this command manifest\n"
      "!status          : Show live CPU, RAM, GPU VRAM, and 24h baseline\n"
      "!clear           : Wipe short-term conversation memory (fresh context)\n"
      "!search <query>  : Force a live DuckDuckGo web search on any topic\n"
      "!transmit        : Generate a lore transmission / archive broadcast\n"
      "!screenoff       : Power down local monitor signal\n"
      f"PRESET AUDIO     : {audio_list}\n"
      "============================================\n"
      "```"
  )


# ==============================================================================
# WEATHER, WEB SEARCH & LLM GENERATION
# ==============================================================================
def fetch_exact_weather():
  try:
    encoded_loc = urllib.parse.quote(DEFAULT_LOCATION)
    url = f"https://wttr.in/{encoded_loc}?format=3"
    req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
      return resp.read().decode("utf-8").strip()
  except Exception:
    return fetch_web_search(
        f"{DEFAULT_LOCATION} current temperature weather today"
    )


def _run_ddgs_backends(search_query: str, max_results: int = 4) -> list[str]:
  """Helper that queries DDGS across fallback backends and formats clean snippets."""
  for b in ["brave,bing,yahoo", "auto", "wikipedia"]:
    try:
      results = list(
          DDGS().text(search_query, max_results=max_results, backend=b)
      )
      if not results:
        continue

      snippets = []
      for r in results:
        title = r.get("title", "").strip()
        body = r.get("body", "").strip()
        href = r.get("href", "").strip()
        if body:
          entry = f"- {title}: {body}" if title else f"- {body}"
          if href:
            entry += f" ({href})"
          snippets.append(entry)

      if snippets:
        return snippets
    except Exception as e:
      print(f"[SEARCH WARNING] Backend '{b}' failed: {e}")
  return []


def fetch_web_search(query: str) -> str:
  """Queries DDGS with sanitized keywords, falling back to store-location search if needed."""
  clean_query = sanitize_search_query(query)
  print(f"[SEARCH] Primary sanitized query: '{clean_query}'")

  # Pass 1: Full product + location query
  snippets = _run_ddgs_backends(clean_query, max_results=4)
  if snippets:
    return "\n".join(snippets)

  # Pass 2: Two-step fallback for "<retailer> near <zip>" when live stock returns empty
  store_query = extract_store_location_query(query)
  if store_query and store_query != clean_query.lower():
    print(
        f"[SEARCH] Primary returned empty -> running store fallback: '{store_query}'"
    )
    store_snippets = _run_ddgs_backends(store_query, max_results=4)
    if store_snippets:
      joined_stores = "\n".join(store_snippets)
      return (
          f"PARTIAL_RESULTS (Live stock/product availability for '{clean_query}' returned zero matches, "
          f"but nearby store locations were found):\n{joined_stores}\n"
          "INSTRUCTION: Share the verified nearby store locations above, and clearly state that "
          "live item stock could not be verified online."
      )

  # Explicit empty-result guardrail so Gemma never guesses or outputs [Insert...]
  return (
      "NO_RESULTS_FOUND: Live web search returned zero verified matches for "
      f"'{clean_query}'. Tell the user no verified locations or stock data were found "
      "and do NOT invent addresses or bracketed placeholders."
  )


last_transmit_scenario = ""
last_fallback_fragment = ""


def generate_hybrid_transmission() -> str:
  """Generates a fresh AI Iterator transmission when VRAM < 80%, or uses archive fallback."""
  global last_transmit_scenario, last_fallback_fragment
  vram = get_gpu_vram_stats()

  def _pick_unique_fallback() -> str:
    global last_fallback_fragment
    choices = (
        [f for f in FALLBACK_FRAGMENTS if f != last_fallback_fragment]
        or FALLBACK_FRAGMENTS
    )
    picked = random.choice(choices)
    last_fallback_fragment = picked
    return picked

  scenarios = (
      [s for s in TRANSMIT_SCENARIOS if s != last_transmit_scenario]
      or TRANSMIT_SCENARIOS
  )
  scenario = random.choice(scenarios)
  last_transmit_scenario = scenario

  if game_paused or vram["percent"] >= RELAXED_VRAM_PERCENT:
    cloud_fragment = ask_gemini_fallback(
        messages_override=[
            {"role": "system", "content": TRANSMIT_GENERATOR_PROMPT},
            {"role": "user", "content": scenario},
        ],
        max_tokens=900,
        temperature=0.88,
    )
    return cloud_fragment if cloud_fragment else _pick_unique_fallback()

  for attempt in range(2):
    try:
      res = llm.chat.completions.create(
          model=LLM_MODEL,
          messages=[
              {"role": "system", "content": TRANSMIT_GENERATOR_PROMPT},
              {"role": "user", "content": scenario},
          ],
          temperature=0.88,
          max_tokens=900,
      )
      fragment = (res.choices[0].message.content or "").strip()
      return fragment if fragment else _pick_unique_fallback()
    except Exception as e:
      if attempt == 0:
        print(f"[TRANSMIT RECOVERY] Waking LM Studio server ({e})...")
        _run_lms_guarded(["lms", "server", "start", "--port", LM_STUDIO_PORT])
        time.sleep(3)
      else:
        print(f"[TRANSMIT FALLBACK] {e}")
        return _pick_unique_fallback()


async def play_easter_egg_audio(message, filename: str, label: str) -> None:
  """Joins user's voice channel to play local MP3, or falls back to hybrid transmission."""
  filepath = os.path.join(SOUNDS_FOLDER, filename)

  if not message.author.voice or not message.author.voice.channel:
    fragment = await asyncio.to_thread(generate_hybrid_transmission)
    await message.channel.send(
        f"📡 **Audio Relay Offline:** Join a voice channel first to hear"
        f" **{label}**, {USER_TITLE}.\n\n{fragment}"
    )
    return

  if not os.path.exists(filepath):
    await message.channel.send(
        f"⚠️ **Archive Missing:** Could not locate `{filepath}` in my local"
        f" memory banks, {USER_TITLE}."
    )
    return

  voice_channel = message.author.voice.channel
  vc = await voice_channel.connect()
  try:
    await message.channel.send(
        f"🎵 *Broadcasting hidden frequency: **{label}**...*"
    )
    vc.play(discord.FFmpegPCMAudio(filepath))
    while vc.is_playing():
      await asyncio.sleep(1)
  finally:
    await vc.disconnect()


async def enrich_user_prompt(raw_text: str) -> tuple[str, str | None]:
  """
  Attaches live weather or DuckDuckGo search context to a user prompt when triggered.
  Returns (enriched_text, error_message_if_invalid).
  """
  text = raw_text.strip()
  lowered = text.lower()

  # 1. Manual override: !search <query> forces a live DDGS lookup immediately
  if lowered.startswith("!search"):
    forced_query = text[7:].strip()
    if not forced_query:
      return (
          "",
          f"⚠️ Include a search topic after `!search`, {USER_TITLE} (e.g.,"
          " `!search RTX 5060 Ti specs`).",
      )
    print(f"[DEBUG] Manual !search triggered -> querying DDGS: '{forced_query}'")
    search_info = await asyncio.to_thread(fetch_web_search, forced_query)
    print(f"[DEBUG] Search snippet received: {search_info[:120]}...")
    return f"{forced_query}\n[Live web search results:\n{search_info}]", None

  # 2. Automatic weather lookup
  if any(w in lowered for w in WEATHER_KEYWORDS):
    print("[DEBUG] Weather keyword matched -> fetching exact weather...")
    weather_info = await asyncio.to_thread(fetch_exact_weather)
    return (
        f"{text}\n[Live weather context for {DEFAULT_LOCATION}: {weather_info}]",
        None,
    )

  # 3. Automatic web search lookup (keywords or 5-digit ZIP codes)
  if any(w in lowered for w in SEARCH_KEYWORDS) or re.search(
      r"\b\d{5}\b", lowered
  ):
    print("[DEBUG] Search trigger matched -> querying DDGS...")
    search_info = await asyncio.to_thread(fetch_web_search, text)
    print(f"[DEBUG] Search snippet received: {search_info[:120]}...")
    return f"{text}\n[Live web search results:\n{search_info}]", None

  return text, None


def ask_static(user_text, history_snapshot: list[dict] | None = None):
  """Sends prompt to LM Studio with dynamic VRAM context-trimming protection."""
  global relaxed_mode
  vram = get_gpu_vram_stats()
  temp_c = vram.get("temp_c")

  # Classify live hardware condition so reactive presets (like Vesper) can shift tone
  if vram["percent"] >= RELAXED_VRAM_PERCENT or (
      temp_c is not None and temp_c >= 72
  ):
    hw_state = "STRAINED"
  elif vram["percent"] >= 65.0 or (temp_c is not None and temp_c >= 62):
    hw_state = "WARM"
  else:
    hw_state = "NOMINAL"

  temp_str = f" | GPU Temp: {temp_c}°C" if temp_c is not None else ""
  now_str = datetime.now().strftime("%A, %I:%M %p")
  telemetry_tag = (
      f"[Current Time: {now_str} | VRAM: {vram['percent']}%{temp_str} |"
      f" State: {hw_state}]"
  )
  tagged_user_msg = {"role": "user", "content": f"{telemetry_tag} {user_text}"}

  base_messages = (
      list(history_snapshot)
      if history_snapshot is not None
      else list(chat_history)
  )
  working_messages = base_messages + [tagged_user_msg]

  # Dynamic Relaxed Mode: shrink history window when VRAM hits >= 80%
  if vram["percent"] >= RELAXED_VRAM_PERCENT:
    if not relaxed_mode:
      print(
          f"[RELAXED MODE] VRAM at {vram['percent']}% — trimming context"
          " window to save memory."
      )
    relaxed_mode = True
    # Keep index 0 (system prompt) + last 4 messages (2 user/assistant turns)
    if len(working_messages) > 5:
      working_messages = [working_messages[0]] + working_messages[-4:]
  else:
    if relaxed_mode:
      print(
          f"[NORMAL MODE] VRAM recovered ({vram['percent']}%) — restoring full"
          " context window."
      )
    relaxed_mode = False
    # Standard 20-message rolling memory window
    while len(working_messages) > 21:
      del working_messages[1:3]

  # Attempt to query LM Studio; if the local server stopped itself, restart it automatically
  try:
    res = llm.chat.completions.create(
        model=LLM_MODEL, messages=working_messages, temperature=0.7
    )
  except Exception as api_err:
    print(
        f"[LM STUDIO RECOVERY] Server unreachable ({api_err}) — restarting port"
        f" {LM_STUDIO_PORT}..."
    )
    _run_lms_guarded(["lms", "server", "start", "--port", LM_STUDIO_PORT])
    time.sleep(3)
    res = llm.chat.completions.create(
        model=LLM_MODEL, messages=working_messages, temperature=0.7
    )

  reply = (res.choices[0].message.content or "").strip()
  if not reply:
    reply = (
        f"Signal received, {USER_TITLE}—though my broadcast buffer came back"
        " empty. Could you repeat that transmission?"
    )
  return reply


def ask_gemini_fallback(
    user_text: str | None = None,
    messages_override: list[dict] | None = None,
    max_tokens: int = 700,
    temperature: float = 0.7,
) -> str | None:
  """
  Queries the Gemini OpenAI-compatible endpoint when local VRAM is offloaded.
  Returns None if unconfigured, rate-limited, or errored so callers can fall back.
  """
  if not gemini_llm or not GEMINI_API_KEY:
    return None

  if user_text:
    vram = get_gpu_vram_stats()
    temp_c = vram.get("temp_c")
    temp_str = f" | GPU Temp: {temp_c}°C" if temp_c is not None else ""
    now_str = datetime.now().strftime("%A, %I:%M %p")
    user_text = (
        f"[Current Time: {now_str} | VRAM: {vram['percent']}%{temp_str} | "
        f"State: CLOUD_OFFLOAD] {user_text}"
    )

  if messages_override is not None:
    payload_messages = messages_override
  else:
    recent_turns = chat_history[-10:] if len(chat_history) > 1 else []
    payload_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + [
        m for m in recent_turns if m.get("role") != "system"
    ]
    if user_text:
      last_msg = payload_messages[-1] if payload_messages else {}
      if not (
          last_msg.get("role") == "user" and last_msg.get("content") == user_text
      ):
        payload_messages.append({"role": "user", "content": user_text})

  request_kwargs = {
      "model": GEMINI_FALLBACK_MODEL,
      "messages": payload_messages,
      "temperature": temperature,
      "max_tokens": max_tokens,
  }
  if GEMINI_REASONING_EFFORT:
    request_kwargs["reasoning_effort"] = GEMINI_REASONING_EFFORT

  try:
    res = gemini_llm.chat.completions.create(**request_kwargs)
    return (res.choices[0].message.content or "").strip() or None
  except TypeError:
    # Hotfix guard: retry once without reasoning_effort if endpoint/SDK rejects it
    request_kwargs.pop("reasoning_effort", None)
    try:
      res = gemini_llm.chat.completions.create(**request_kwargs)
      return (res.choices[0].message.content or "").strip() or None
    except Exception as retry_err:
      print(f"[GEMINI FALLBACK WARN] Retry failed: {retry_err}")
      return None
  except Exception as e:
    print(
        f"[GEMINI FALLBACK WARN] Cloud request failed ({e}) — using local"
        " fallback."
    )
    return None


# ==============================================================================
# BACKGROUND MONITOR & DISCORD EVENTS
# ==============================================================================
@tasks.loop(seconds=20)
async def system_monitor():
  global sent_end_today, pending_cycle_end, game_paused, relaxed_mode
  now = datetime.now()
  channel = client.get_channel(CHANNEL_ID)

  offload_notice = get_gaming_offload_notice()
  milestone_msg = check_session_milestone()
  if milestone_msg and channel:
    await channel.send(milestone_msg)

  vram = await asyncio.to_thread(get_gpu_vram_stats)
  vram_pct = vram["percent"]

  relaxed_mode = RELAXED_VRAM_PERCENT <= vram_pct < PAUSE_VRAM_PERCENT

  offloaded_now = bool(offload_notice)
  # Hysteresis pause check: pause when offloader is active or VRAM >= 90%, resume below 70%
  should_pause = (
      offloaded_now
      or (vram_pct >= PAUSE_VRAM_PERCENT)
      or (
          game_paused
          and vram_pct > RESUME_VRAM_PERCENT
          and not (not offloaded_now and vram_pct < PAUSE_VRAM_PERCENT)
      )
  )

  if should_pause and not game_paused:
    game_paused = True
    offloader_live = await asyncio.to_thread(is_offloader_live)
    reason = "Offloader Active" if offload_notice else f"{vram_pct}% GPU VRAM"
    if offloader_live:
      print(
          f"High GPU demand detected ({reason}) — deferring VRAM unload to"
          " offloader.py; pausing AI Bot."
      )
    elif vram_pct >= PAUSE_VRAM_PERCENT:
      print(
          f"High GPU demand detected ({reason}, offloader offline) — fallback"
          " VRAM unload and pausing AI Bot."
      )
      await asyncio.to_thread(unload_vram)
    else:
      print(
          f"High GPU demand detected ({reason}, offloader offline) — pausing"
          " AI Bot."
      )

  elif not should_pause and game_paused:
    game_paused = False
    print(f"GPU demand cleared ({vram_pct}% GPU VRAM) — AI Bot resumed.")
    if pending_cycle_end and channel:
      pending_cycle_end = False
      async with llm_lock:
        snapshot = list(chat_history)
        reply = await asyncio.to_thread(
            ask_static,
            "[SYSTEM: Cycle End triggered after game session. Deliver your"
            " nighttime broadcast.]",
            snapshot,
        )
      await channel.send(reply[:1950])

  if now.hour == 0 and now.minute == 0:
    sent_end_today = False
    pending_cycle_end = False

  if now.hour == CYCLE_END_HOUR and now.minute < 5 and not sent_end_today:
    sent_end_today = True
    if game_paused:
      pending_cycle_end = True
    elif channel:
      async with llm_lock:
        snapshot = list(chat_history)
        reply = await asyncio.to_thread(
            ask_static,
            "[SYSTEM: Cycle End triggered. Deliver your nighttime broadcast.]",
            snapshot,
        )
      await channel.send(reply[:1950])


@client.event
async def on_ready():
  global startup_sent, game_paused
  print(f"AI Bot connected to Discord as {client.user}")
  if not system_monitor.is_running():
    system_monitor.start()

  if not startup_sent:
    startup_sent = True
    await asyncio.sleep(3)
    channel = client.get_channel(CHANNEL_ID)
    offload_notice = get_gaming_offload_notice()
    vram = await asyncio.to_thread(get_gpu_vram_stats)
    if offload_notice or vram["percent"] >= PAUSE_VRAM_PERCENT:
      game_paused = True

    # Skip LLM greeting if offloaded or high VRAM is already active
    if channel and not game_paused:
      weather_info = await asyncio.to_thread(fetch_exact_weather)
      current_time_str = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
      prompt = (
          "[SYSTEM: Cycle Start triggered (NOT Cycle End — do NOT include"
          " wind-down advice or trivia). "
          f"Current local time: {current_time_str}. Live weather data for"
          f" {DEFAULT_LOCATION}: {weather_info}\n"
          "Greet me warmly for this exact time of day"
          " (morning/afternoon/evening) and state the current temperature and"
          " weather conditions.]"
      )
      try:
        # Attempt full AI-generated greeting via LM Studio
        async with llm_lock:
          snapshot = list(chat_history)
          reply = await asyncio.to_thread(ask_static, prompt, snapshot)
        await channel.send(reply[:1950])
      except Exception as e:
        # Cold-boot fallback: send direct telemetry + weather if weights are still loading
        print(
            "[COLD-START FALLBACK] LM Studio still warming up"
            f" ({e}) — sending direct greeting."
        )
        fallback_msg = (
            f"⚡ **Systems Online, {USER_TITLE}.** Neural core is warming up in"
            " the background.\n"
            f"🕒 **Local Time:** {current_time_str}\n"
            f"🌤️ **Weather ({DEFAULT_LOCATION}):** {weather_info}"
        )
        await channel.send(fallback_msg[:1950])


@client.event
async def on_message(message):
  if message.author == client.user or message.channel.id != CHANNEL_ID:
    return

  if message.content.strip().lower() == "!status":
    status_report = await asyncio.to_thread(get_system_status)
    await message.channel.send(status_report)
    return

  if message.content.strip().lower() == "!screenoff":
    await message.channel.send(f"Powering down display signal, {USER_TITLE}.")
    await asyncio.sleep(1)
    if os.name == "nt":
      # 0xFFFF = All monitors, 0x0112 = WM_SYSCOMMAND, 0xF170 = SC_MONITORPOWER, 2 = Off
      ctypes.windll.user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)
    else:
      # Linux X11 DPMS fallback (Wayland compositors restrict external display power-off)
      try:
        subprocess.run(["xset", "dpms", "force", "off"], check=True)
      except Exception:
        await message.channel.send(
            "⚠️ Display power control is unavailable in this Linux session,"
            f" {USER_TITLE}."
        )
    return
  cmd = message.content.strip().lower()

  if cmd == "!help":
    await message.channel.send(get_help_manifest())
    return

  if cmd == "!clear":
    async with llm_lock:
      chat_history[:] = [chat_history[0]]
    print(f"[MEMORY] Chat history cleared by {USER_TITLE}.")
    await message.channel.send(
        f"🧹 **Memory Buffer Flushed, {USER_TITLE}.** Short-term context has"
        " been reset to baseline."
    )
    return

  if cmd == "!transmit":
    async with message.channel.typing():
      fragment = await asyncio.to_thread(generate_hybrid_transmission)
      await message.channel.send(fragment[:1950])
    return

  if cmd in AUDIO_COMMANDS:
    track = AUDIO_COMMANDS[cmd]
    await play_easter_egg_audio(message, track["file"], track["label"])
    return

  offload_reply = get_gaming_offload_notice()
  is_offloaded_or_paused = bool(offload_reply) or game_paused

  if is_offloaded_or_paused and not gemini_llm:
    await message.channel.send(
        offload_reply
        or (
            "Broadcast paused while high GPU demand is active,"
            f" {USER_TITLE}—I'll resume once VRAM clears."
        )
    )
    return

  async with message.channel.typing():
    raw_text = message.content.strip()

    enriched_text, validation_err = await enrich_user_prompt(raw_text)
    if validation_err:
      await message.channel.send(validation_err)
      return

    if is_offloaded_or_paused:
      cloud_reply = await asyncio.to_thread(ask_gemini_fallback, enriched_text)
      if cloud_reply:
        async with llm_lock:
          chat_history.append({"role": "user", "content": raw_text})
          chat_history.append({"role": "assistant", "content": cloud_reply})
          if len(chat_history) > 21:
            del chat_history[1:3]
        await message.channel.send(cloud_reply[:1950])
        return

      await message.channel.send(
          offload_reply
          or (
              "Broadcast paused while high GPU demand is active,"
              f" {USER_TITLE}—I'll resume once VRAM clears."
          )
      )
      return

    async with llm_lock:
      now_str = datetime.now().strftime("%A, %I:%M %p")
      vram_snap = await asyncio.to_thread(get_gpu_vram_stats)
      temp_snap = vram_snap.get("temp_c")
      if vram_snap["percent"] >= RELAXED_VRAM_PERCENT or (
          temp_snap is not None and temp_snap >= 72
      ):
        hw_state = "STRAINED"
      elif vram_snap["percent"] >= 65.0 or (
          temp_snap is not None and temp_snap >= 62
      ):
        hw_state = "WARM"
      else:
        hw_state = "NOMINAL"
      temp_str = f" | GPU Temp: {temp_snap}°C" if temp_snap is not None else ""
      telemetry_tag = (
          f"[Current Time: {now_str} | VRAM: {vram_snap['percent']}%{temp_str} |"
          f" State: {hw_state}]"
      )
      user_entry = {"role": "user", "content": f"{telemetry_tag} {enriched_text}"}
      snapshot = list(chat_history)
      reply = await asyncio.to_thread(ask_static, enriched_text, snapshot)
      chat_history.append(user_entry)
      chat_history.append({"role": "assistant", "content": reply})
      if vram_snap["percent"] >= RELAXED_VRAM_PERCENT:
        if len(chat_history) > 5:
          chat_history[:] = [chat_history[0]] + chat_history[-4:]
      else:
        while len(chat_history) > 21:
          del chat_history[1:3]
    await message.channel.send(reply[:1950])


while True:
  try:
    client.run(DISCORD_TOKEN)
    print(f"\n[SHUTDOWN] AI Bot stopped cleanly by {USER_TITLE}.")
    break  # Exits the loop immediately when Ctrl + C finishes client.run()
  except KeyboardInterrupt:
    print(f"\n[SHUTDOWN] AI Bot stopped cleanly by {USER_TITLE}.")
    break
  except Exception as e:
    print(f"[BOOT/NET WARN] Discord connection failed ({e}). Retrying in 10s...")
    time.sleep(10)