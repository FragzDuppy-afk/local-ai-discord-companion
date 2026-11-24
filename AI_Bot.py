import asyncio
import ctypes
from datetime import datetime
import json
import os
import random
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
import pynvml
import telemetry_db

load_dotenv()

# ==============================================================================
# SINGLE-INSTANCE GUARD (PREVENTS DUPLICATE BOT PROCESSES)
# ==============================================================================
_mutex = ctypes.windll.kernel32.CreateMutexW(
    None, False, "LocalAIBotSingleInstanceMutex"
)
if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
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

LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://127.0.0.1:1234/v1")
LM_STUDIO_PORT = os.getenv("LM_STUDIO_PORT", "1234")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemma-4-e2b")

if not DISCORD_TOKEN or CHANNEL_ID == 0:
  print(
      "[CONFIG ERROR] Missing DISCORD_TOKEN or CHANNEL_ID in your .env file."
  )
  sys.exit(1)

# Dynamic VRAM percentage thresholds (scales automatically to any GPU size)
RELAXED_VRAM_PERCENT = 80.0
PAUSE_VRAM_PERCENT = 90.0
RESUME_VRAM_PERCENT = 70.0
SOUNDS_FOLDER = "Sounds"
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
SEARCH_KEYWORDS = {
    "search",
    "look up",
    "lookup",
    "google",
    "find out",
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


# ==============================================================================
# CLIENT & STATE INITIALIZATION
# ==============================================================================
llm = OpenAI(base_url=LM_STUDIO_URL, api_key="lm-studio")

# Shared state file written by offloader.py in the same folder
OFFLOADER_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "offloader_state.json"
)

def get_gaming_offload_notice() -> str | None:
    """
    Checks offloader_state.json before querying LM Studio.
    Returns a clean status reply if VRAM is offloaded, or None if the LLM is ready.
    """
    if not os.path.exists(OFFLOADER_STATE_FILE):
        return None

    try:
        with open(OFFLOADER_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data.get("vram_offloaded", False):
                reason = data.get("reason", "a heavy application is active")
                return (
                    f"⚡ **Gaming / Relaxed Mode Active:** Local neural processing is temporarily "
                    f"offloaded ({reason}) to preserve system stability and VRAM. "
                    f"Full capabilities will resume automatically once the session concludes."
                )
    except (OSError, json.JSONDecodeError):
        pass

    return None


def check_session_milestone() -> str | None:
  """Reads offloader_state.json for single-fire 2h/4h gaming session milestones."""
  global last_handled_milestone
  if not SESSION_ALERTS_ENABLED or not os.path.exists(OFFLOADER_STATE_FILE):
    return None

  try:
    with open(OFFLOADER_STATE_FILE, "r", encoding="utf-8") as f:
      data = json.load(f)

    if not data.get("vram_offloaded", False):
      last_handled_milestone = ""
      return None

    milestone = str(data.get("session_milestone", ""))
    session_mins = int(data.get("session_minutes", 0))
    reason = str(data.get("reason", "Active session")).replace(
        "Unloaded for ", ""
    )

    if milestone and milestone != last_handled_milestone:
      last_handled_milestone = milestone
      hours = round(session_mins / 60, 1)
      if milestone == "4h":
        return (
            f"⏳ **Extended Session Check-In ({hours}h), {USER_TITLE}:** "
            f"Active workload detected ({reason}). Consider stepping away for water, "
            f"stretching, and resting your eyes before continuing."
        )
      if milestone == "2h":
        return (
            f"🕒 **Session Milestone ({hours}h), {USER_TITLE}:** "
            f"You've been running continuously ({reason}). Good checkpoint to grab water or stretch."
        )
  except (OSError, json.JSONDecodeError, ValueError):
    pass

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


def get_gpu_vram_stats() -> dict:
  """Queries total GPU VRAM usage directly via NVML as a dynamic percentage."""
  try:
    pynvml.nvmlInit()
    handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
    used_gb = round(mem.used / (1024**3), 2)
    total_gb = round(mem.total / (1024**3), 2)
    percent = round((mem.used / mem.total) * 100.0, 1)
    return {"used_gb": used_gb, "total_gb": total_gb, "percent": percent}
  except pynvml.NVMLError:
    return {"used_gb": 0.0, "total_gb": 0.0, "percent": 0.0}
  finally:
    try:
      pynvml.nvmlShutdown()
    except pynvml.NVMLError:
      pass


def unload_vram():
  """Unloads active LM Studio models to immediately free GPU VRAM."""
  try:
    subprocess.run(
        ["lms", "unload", "--all"],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
  except Exception:
    pass


def get_system_status():
  """Builds the formatted hardware manifest for the !status Discord command."""
  cpu = psutil.cpu_percent(interval=0.5)
  ram = psutil.virtual_memory()
  disk = psutil.disk_usage("C:\\")
  vram = get_gpu_vram_stats()
  baseline = telemetry_db.get_rolling_baseline(24.0)

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
    vram_remark = f"CRITICAL (>=90%){vram_avg_str} — Model unloaded"
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

  return (
      "```\n"
      f"=== {BOT_NAME.upper()} HARDWARE & EQUIPMENT MANIFEST ===\n"
      f"CPU LOAD     : {cpu:>5}%  | {cpu_remark}\n"
      f"SYSTEM RAM   : {ram_used_gb}/{ram_total_gb} GB ({ram.percent}%) | {ram_remark}\n"
      f"GPU VRAM     : {vram['used_gb']}/{vram['total_gb']} GB ({vram['percent']}%) | {vram_remark}\n"
      f"PRIMARY SSD  : {disk_free_gb:>5} GB Free | {disk_remark}\n"
      f"24H BASELINE : {baseline_line}\n"
      f"AI BACKEND   : {LLM_MODEL} (Port {LM_STUDIO_PORT}/v1) | {ai_state}\n"
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

def fetch_web_search(query):
  for b in ["brave,bing,yahoo", "auto", "wikipedia"]:
    try:
      results = DDGS().text(query, max_results=3, backend=b)
      snippets = [r.get("body", "") for r in results if r.get("body")]
      if snippets:
        return "\n".join(snippets)
    except Exception as e:
      print(f"[SEARCH WARNING] Backend '{b}' failed: {e}")
  return "Live search temporarily unavailable."

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

  if game_paused or vram["percent"] >= RELAXED_VRAM_PERCENT:
    return _pick_unique_fallback()

  scenarios = (
      [s for s in TRANSMIT_SCENARIOS if s != last_transmit_scenario]
      or TRANSMIT_SCENARIOS
  )
  scenario = random.choice(scenarios)
  last_transmit_scenario = scenario

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
        subprocess.run(
            ["lms", "server", "start", "--port", LM_STUDIO_PORT],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
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
        f"📡 **Audio Relay Offline:** Join a voice channel first to hear **{label}**, {USER_TITLE}.\n\n{fragment}"
    )
    return

  if not os.path.exists(filepath):
    await message.channel.send(
        f"⚠️ **Archive Missing:** Could not locate `{filepath}` in my local memory banks, {USER_TITLE}."
    )
    return

  voice_channel = message.author.voice.channel
  vc = await voice_channel.connect()
  try:
    await message.channel.send(f"🎵 *Broadcasting hidden frequency: **{label}**...*")
    vc.play(discord.FFmpegPCMAudio(filepath))
    while vc.is_playing():
      await asyncio.sleep(1)
  finally:
    await vc.disconnect()

def ask_static(user_text):
  """Sends prompt to LM Studio with dynamic VRAM context-trimming protection."""
  global relaxed_mode
  vram = get_gpu_vram_stats()

  now_str = datetime.now().strftime("%A, %I:%M %p")
  chat_history.append({"role": "user", "content": f"[Current Time: {now_str}] {user_text}"})


  # Dynamic Relaxed Mode: shrink history window when VRAM hits >= 80%
  if vram["percent"] >= RELAXED_VRAM_PERCENT:
    if not relaxed_mode:
      print(
          f"[RELAXED MODE] VRAM at {vram['percent']}% — trimming context"
          " window to save memory."
      )
    relaxed_mode = True
    # Keep index 0 (system prompt) + last 4 messages (2 user/assistant turns)
    if len(chat_history) > 5:
      chat_history[:] = [chat_history[0]] + chat_history[-4:]
  else:
    if relaxed_mode:
      print(
          f"[NORMAL MODE] VRAM recovered ({vram['percent']}%) — restoring full"
          " context window."
      )
    relaxed_mode = False
    # Standard 20-message rolling memory window
    if len(chat_history) > 21:
      del chat_history[1:3]

  # Attempt to query LM Studio; if the local server stopped itself, restart it automatically
  try:
    res = llm.chat.completions.create(
        model = LLM_MODEL, messages=chat_history, temperature=0.7
    )
  except Exception as api_err:
    print(f"[LM STUDIO RECOVERY] Server unreachable ({api_err}) — restarting port 1234...")
    subprocess.run(
        ["lms", "server", "start", "--port", LM_STUDIO_PORT],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    res = llm.chat.completions.create(
        model = LLM_MODEL, messages=chat_history, temperature=0.7
    )

  reply = (res.choices[0].message.content or "").strip()
  if not reply:
    reply = (
        f"Signal received, {USER_TITLE}—though my broadcast buffer came back empty."
        " Could you repeat that transmission?"
    )
  chat_history.append({"role": "assistant", "content": reply})
  return reply


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

  # Hysteresis pause check: pause when offloader is active or VRAM >= 90%, resume below 70%
  should_pause = (
      bool(offload_notice)
      or (vram_pct >= PAUSE_VRAM_PERCENT and not game_paused)
      or (vram_pct > RESUME_VRAM_PERCENT and game_paused)
  )

  if should_pause and not game_paused:
    game_paused = True
    reason = "Offloader Active" if offload_notice else f"{vram_pct}% GPU VRAM"
    print(
        f"High GPU demand detected ({reason}) — unloading VRAM and pausing"
        " AI Bot."
    )
    await asyncio.to_thread(unload_vram)

  elif not should_pause and game_paused:
    game_paused = False
    print(f"GPU demand cleared ({vram_pct}% GPU VRAM) — AI Bot resumed.")
    if pending_cycle_end and channel:
      pending_cycle_end = False
      async with llm_lock:
        reply = await asyncio.to_thread(
            ask_static,
            "[SYSTEM: Cycle End triggered after game session. Deliver your nighttime broadcast.]",
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
        reply = await asyncio.to_thread(
            ask_static,
            "[SYSTEM: Cycle End triggered. Deliver your nighttime broadcast.]",
        )
      await channel.send(reply[:1950])


@client.event
async def on_ready():
  global startup_sent
  print(f"AI Bot connected to Discord as {client.user}")
  if not system_monitor.is_running():
    system_monitor.start()

  if not startup_sent:
    startup_sent = True
    await asyncio.sleep(3)
    channel = client.get_channel(CHANNEL_ID)

    # Skip LLM greeting if the offloader already claimed the GPU
    if channel and not get_gaming_offload_notice():
      weather_info = await asyncio.to_thread(fetch_exact_weather)
      current_time_str = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
      prompt = (
          "[SYSTEM: Cycle Start triggered (NOT Cycle End — do NOT include"
          " wind-down advice or trivia). "
          f"Current local time: {current_time_str}. Live weather data for"
          f" {DEFAULT_LOCATION}: {weather_info}\n"
          "Greet me warmly for this exact time of day (morning/afternoon/evening)"
          " and state the current temperature and weather conditions.]"
      )
      try:
        # Attempt full AI-generated greeting via LM Studio
        async with llm_lock:
          reply = await asyncio.to_thread(ask_static, prompt)
        await channel.send(reply[:1950])
      except Exception as e:
        # Cold-boot fallback: send direct telemetry + weather if weights are still loading
        print(f"[COLD-START FALLBACK] LM Studio still warming up ({e}) — sending direct greeting.")
        fallback_msg = (
            f"⚡ **Systems Online, {USER_TITLE}.** Neural core is warming up in the background.\n"
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
    # 0xFFFF = All monitors, 0x0112 = WM_SYSCOMMAND, 0xF170 = SC_MONITORPOWER, 2 = Off
    ctypes.windll.user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)
    return
  cmd = message.content.strip().lower()

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
  if offload_reply:
    await message.channel.send(offload_reply)
    return

  if game_paused:
    await message.channel.send(f"Broadcast paused while high GPU demand is active, {USER_TITLE}—I'll resume once VRAM clears.")
    return

  async with message.channel.typing():

    text = message.content
    lowered = text.lower()

    if any(w in lowered for w in WEATHER_KEYWORDS):
      print("[DEBUG] Weather keyword matched -> fetching exact weather...")
      weather_info = await asyncio.to_thread(fetch_exact_weather)
      text = f"{text}\n[Live weather context for {DEFAULT_LOCATION}: {weather_info}]"
    elif any(w in lowered for w in SEARCH_KEYWORDS):
      clean_query = lowered
      phrases = [
          "can you search the web for",
          "search the web for",
          "can you search for",
          "search for",
          "search",
          "can you look up",
          "look up",
          "lookup",
          "google",
          "find out",
      ]
      for phrase in phrases:
        clean_query = clean_query.replace(phrase, "")
      clean_query = clean_query.strip(" ?.!:,") or text

      print(f"[DEBUG] Search keyword matched -> querying: '{clean_query}'")
      search_info = await asyncio.to_thread(fetch_web_search, clean_query)
      print(f"[DEBUG] Search snippet received: {search_info[:100]}...")
      text = f"{text}\n[Live web search results: {search_info}]"

    async with llm_lock:
      reply = await asyncio.to_thread(ask_static, text)
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
