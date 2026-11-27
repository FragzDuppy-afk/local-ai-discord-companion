import asyncio
import ctypes
from datetime import datetime
import json
import os
import random
import subprocess
import sys
import time
import discord
from discord.ext import tasks
from dotenv import load_dotenv
from openai import OpenAI

from subsystems import hardware
from subsystems import lifecycle
from subsystems import submind

load_dotenv()

# ==============================================================================
# 1. CONFIGURATION, PATHS & PRESET LOADER
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OFFLOADER_STATE_FILE = os.path.join(BASE_DIR, "offloader_state.json")
LMS_LOCK_FILE = os.path.join(BASE_DIR, "lms_cli.lock")
LOCKOUT_FILE = os.path.join(BASE_DIR, "lockout.flag")
SOUNDS_FOLDER = os.path.join(BASE_DIR, "Sounds")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
CYCLE_END_HOUR = int(os.getenv("CYCLE_END_HOUR", "22"))
GOODNIGHT_HOUR = int(os.getenv("GOODNIGHT_HOUR", "3"))
GOODNIGHT_DELAY_SEC = int(os.getenv("GOODNIGHT_DELAY_SEC", "15"))


DEFAULT_LOCATION = os.getenv("DEFAULT_LOCATION", "Chicago, IL")
TIMEZONE_LABEL = os.getenv("TIMEZONE_LABEL", "local time")

LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://127.0.0.1:1234/v1")
LM_STUDIO_PORT = os.getenv("LM_STUDIO_PORT", "1234")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemma-4-e2b")
LMS_TIMEOUT_SEC = float(os.getenv("AI_BOT_LMS_TIMEOUT_SEC", "20.0"))

# Dynamic VRAM percentage thresholds (scales automatically to any GPU size)
RELAXED_VRAM_PERCENT = 80.0
PAUSE_VRAM_PERCENT = 90.0
RESUME_VRAM_PERCENT = 70.0
OFFLOADER_STALE_SEC = float(os.getenv("OFFLOADER_STALE_SEC", "15.0"))
SESSION_ALERTS_ENABLED = (
    os.getenv("SESSION_ALERTS_ENABLED", "true").strip().lower() == "true"
)

UPS_MODE = os.getenv("UPS_MODE", "disabled").strip().lower()
UPS_NAME = os.getenv("UPS_NAME", "ups").strip()
UPS_HOST = os.getenv("UPS_HOST", "127.0.0.1").strip()
UPS_PORT = int(os.getenv("UPS_PORT", "3493"))
UPS_MIN_BATTERY_PCT = int(os.getenv("UPS_MIN_BATTERY_PCT", "25"))
MOCK_UPS_FILE = os.path.join(BASE_DIR, "mock_ups.json")


# Optional free cloud fallback (strictly opt-in via GEMINI_API_KEY)
cloud_fallback = submind.CloudFallbackClient(
    api_key=os.getenv("GEMINI_API_KEY", ""),
    base_url=os.getenv(
        "GEMINI_BASE_URL",
        "https://googleapis.com",
    ),
    model=os.getenv("GEMINI_FALLBACK_MODEL", "gemma-4-26b-a4b-it"),
    reasoning_effort=os.getenv("GEMINI_REASONING_EFFORT", "minimal"),
)

# Character preset loader (presets/<CHARACTER_PRESET>.json)
CHARACTER_PRESET = os.getenv("CHARACTER_PRESET", "default").strip().lower()
PRESET_PATH = os.path.join(BASE_DIR, "presets", f"{CHARACTER_PRESET}.json")

try:
    with open(PRESET_PATH, "r", encoding="utf-8") as f:
        PRESET = json.load(f)
    print(f"[PRESET] Loaded character preset: {PRESET_PATH}")
except FileNotFoundError:
    print(f"[PRESET WARNING] Could not find {PRESET_PATH}. Using built-in fallback.")
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

# ==============================================================================
# 2. CLIENT & RUNTIME STATE
# ==============================================================================
llm = OpenAI(base_url=LM_STUDIO_URL, api_key="lm-studio")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

chat_history = [{"role": "system", "content": SYSTEM_PROMPT}]
llm_lock = asyncio.Lock()

sent_end_today = False
sent_goodnight_today = False
pending_cycle_end = False
game_paused = False
relaxed_mode = False
startup_sent = False
last_handled_milestone = ""
last_transmit_scenario = ""
last_fallback_fragment = ""


# ==============================================================================
# 3. GENERATION, TRANSMIT & AUDIO HELPERS
# ==============================================================================
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
        "!goodnight       : Unload VRAM & power down PC (guarded if game active)"
        f"PRESET AUDIO     : {audio_list}\n"
        "============================================\n"
        "```"
    )


def build_telemetry_tag(vram: dict) -> str:
    """Formats a single consistent hardware telemetry prefix for user turns."""
    hw_state = hardware.classify_hardware_state(vram, RELAXED_VRAM_PERCENT)
    temp_c = vram.get("temp_c")
    temp_str = f" | GPU Temp: {temp_c}°C" if temp_c is not None else ""
    now_str = datetime.now().strftime("%A, %I:%M %p")
    return (
        f"[Current Time: {now_str} | VRAM: {vram['percent']}%{temp_str} |"
        f" State: {hw_state}]"
    )


def generate_hybrid_transmission() -> str:
    """Generates a fresh AI transmission when VRAM < 80%, or uses cloud/archive fallback."""
    global last_transmit_scenario, last_fallback_fragment
    vram = hardware.get_gpu_vram_stats()

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
        cloud_fragment = cloud_fallback.query(
            system_prompt=SYSTEM_PROMPT,
            chat_history=chat_history,
            vram_stats=vram,
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
                hardware.run_lms_guarded(
                    ["lms", "server", "start", "--port", LM_STUDIO_PORT],
                    LMS_LOCK_FILE,
                    LMS_TIMEOUT_SEC,
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

def ask_static(
    user_text: str,
    history_snapshot: list[dict] | None = None,
    vram_snapshot: dict | None = None,
) -> tuple[str, str]:
    """
    Sends a prompt to LM Studio with dynamic VRAM context-trimming protection.
    Returns (assistant_reply, formatted_user_entry_text) so telemetry tags are built once.
    """
    global relaxed_mode
    vram = (
        vram_snapshot
        if vram_snapshot is not None
        else hardware.get_gpu_vram_stats()
    )
    formatted_user_text = f"{build_telemetry_tag(vram)} {user_text}"
    tagged_user_msg = {"role": "user", "content": formatted_user_text}

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
        if len(working_messages) > 5:
            working_messages = [working_messages[0]] + working_messages[-4:]
    else:
        if relaxed_mode:
            print(
                f"[NORMAL MODE] VRAM recovered ({vram['percent']}%) — restoring full"
                " context window."
            )
        relaxed_mode = False
        while len(working_messages) > 21:
            del working_messages[1:3]

    try:
        res = llm.chat.completions.create(
            model=LLM_MODEL, messages=working_messages, temperature=0.7
        )
    except Exception as api_err:
        if hardware.get_gaming_offload_notice(OFFLOADER_STATE_FILE):
            raise RuntimeError("VRAM offloaded mid-request") from api_err
        print(
            f"[LM STUDIO RECOVERY] Server unreachable ({api_err}) — restarting port"
            f" {LM_STUDIO_PORT}..."
        )
        hardware.run_lms_guarded(
            ["lms", "server", "start", "--port", LM_STUDIO_PORT],
            LMS_LOCK_FILE,
            LMS_TIMEOUT_SEC,
        )
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
    return reply, formatted_user_text


# ==============================================================================
# 4. BACKGROUND MONITOR & DISCORD EVENTS
# ==============================================================================
@tasks.loop(seconds=20)
async def system_monitor():
    global sent_end_today, pending_cycle_end, game_paused, relaxed_mode, last_handled_milestone
    now = datetime.now()
    channel = client.get_channel(CHANNEL_ID)

    # Check LOTO lockout flag during runtime so !estop or UPS shutdowns halt cleanly
    locked, lock_reason = lifecycle.is_lockout_active(LOCKOUT_FILE)
    if locked:
        print(f"[LOTO LOCKOUT] Halting bot gateway: {lock_reason}")
        await client.close()
        return

    ups_status, ups_charge, ups_critical = await asyncio.to_thread(
        lifecycle.check_ups_status,
        UPS_MODE,
        MOCK_UPS_FILE,
        UPS_NAME,
        UPS_HOST,
        UPS_PORT,
        UPS_MIN_BATTERY_PCT,
    )
    if ups_critical:
        reason = f"UPS Critical Power ({ups_status} @ {ups_charge}%)"
        print(f"[UPS LOTO] {reason} — engaging lockout and unloading VRAM.")
        lifecycle.engage_lockout(LOCKOUT_FILE, reason=reason, source="ups_monitor")
        if channel:
            await channel.send(
                f"🪫 **UPS CRITICAL POWER ({ups_status} @ {ups_charge}%), {USER_TITLE}:** "
                "Engaging Lockout/Tagout (`lockout.flag`), unloading GPU VRAM, and "
                "halting AI Bot cleanly before battery depletion."
            )
        await asyncio.to_thread(hardware.unload_vram, LMS_LOCK_FILE, LMS_TIMEOUT_SEC)
        await client.close()
        return


    offload_notice = hardware.get_gaming_offload_notice(OFFLOADER_STATE_FILE)
    milestone_msg, last_handled_milestone = hardware.evaluate_session_milestone(
        OFFLOADER_STATE_FILE,
        last_handled_milestone,
        user_title=USER_TITLE,
        alerts_enabled=SESSION_ALERTS_ENABLED,
    )
    if milestone_msg and channel:
        await channel.send(milestone_msg)

    vram = await asyncio.to_thread(hardware.get_gpu_vram_stats)
    vram_pct = vram["percent"]

    relaxed_mode = RELAXED_VRAM_PERCENT <= vram_pct < PAUSE_VRAM_PERCENT
    offloaded_now = bool(offload_notice)

    should_pause = (
        offloaded_now
        or (vram_pct >= PAUSE_VRAM_PERCENT)
        or (game_paused and vram_pct > RESUME_VRAM_PERCENT)
    )

    if should_pause and not game_paused:
        game_paused = True
        state = hardware.read_offloader_state(OFFLOADER_STATE_FILE)
        offloader_live = await asyncio.to_thread(
            hardware.is_offloader_live, state, OFFLOADER_STALE_SEC
        )
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
            await asyncio.to_thread(
                hardware.unload_vram, LMS_LOCK_FILE, LMS_TIMEOUT_SEC
            )
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
                reply, _ = await asyncio.to_thread(
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
                reply, _ = await asyncio.to_thread(
                    ask_static,
                    "[SYSTEM: Cycle End triggered. Deliver your nighttime broadcast.]",
                    snapshot,
                )
            await channel.send(reply[:1950])

    if now.hour == 0 and now.minute == 0:
        sent_end_today = False
        pending_cycle_end = False
        sent_goodnight_today = False

    if (
        GOODNIGHT_HOUR >= 0
        and now.hour == GOODNIGHT_HOUR
        and now.minute < 5
        and not sent_goodnight_today
    ):
        state = hardware.read_offloader_state(OFFLOADER_STATE_FILE)
        if state.get("vram_offloaded", False):
            # Defer scheduled shutdown while a game is actively running
            return
        sent_goodnight_today = True
        await asyncio.to_thread(hardware.unload_vram, LMS_LOCK_FILE, LMS_TIMEOUT_SEC)
        ok, gn_msg = lifecycle.execute_goodnight_protocol(
            offloaded=False, delay_sec=GOODNIGHT_DELAY_SEC
        )
        if channel:
            await channel.send(f"🕒 **Scheduled Goodnight Cycle, {USER_TITLE}:**\n{gn_msg}")
        if ok:
            await client.close()
            return

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
        offload_notice = hardware.get_gaming_offload_notice(OFFLOADER_STATE_FILE)
        vram = await asyncio.to_thread(hardware.get_gpu_vram_stats)
        if offload_notice or vram["percent"] >= PAUSE_VRAM_PERCENT:
            game_paused = True

        if channel and not game_paused:
            weather_info = await asyncio.to_thread(
                submind.fetch_exact_weather, DEFAULT_LOCATION
            )
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
                async with llm_lock:
                    snapshot = list(chat_history)
                    reply, _ = await asyncio.to_thread(ask_static, prompt, snapshot)
                await channel.send(reply[:1950])
            except Exception as e:
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

    cmd = message.content.strip().lower()

    if cmd == "!status":
        status_report = await asyncio.to_thread(
            hardware.build_system_status,
            BOT_NAME,
            LLM_MODEL,
            LM_STUDIO_PORT,
            OFFLOADER_STATE_FILE,
            game_paused,
            relaxed_mode,
            RELAXED_VRAM_PERCENT,
            PAUSE_VRAM_PERCENT,
            OFFLOADER_STALE_SEC,
        )
        await message.channel.send(status_report)
        return

    if cmd == "!screenoff":
        await message.channel.send(f"Powering down display signal, {USER_TITLE}.")
        await asyncio.sleep(1)
        if os.name == "nt":
            ctypes.windll.user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)
        else:
            try:
                subprocess.run(["xset", "dpms", "force", "off"], check=True)
            except Exception:
                await message.channel.send(
                    "⚠️ Display power control is unavailable in this Linux session,"
                    f" {USER_TITLE}."
                )
        return

    if cmd in ("!goodnight", "!goodnight force"):
        force_shutdown = cmd == "!goodnight force"
        state = hardware.read_offloader_state(OFFLOADER_STATE_FILE)
        offloaded = bool(state.get("vram_offloaded", False))
        if offloaded and not force_shutdown:
            _, guard_msg = lifecycle.execute_goodnight_protocol(
                offloaded=True,
                offload_reason=state.get("reason", ""),
                delay_sec=GOODNIGHT_DELAY_SEC,
            )
            await message.channel.send(guard_msg)
            return

        await asyncio.to_thread(hardware.unload_vram, LMS_LOCK_FILE, LMS_TIMEOUT_SEC)
        ok, gn_msg = lifecycle.execute_goodnight_protocol(
            offloaded=offloaded,
            offload_reason=state.get("reason", ""),
            delay_sec=GOODNIGHT_DELAY_SEC,
            force=force_shutdown,
        )
        await message.channel.send(gn_msg)
        if ok:
            await client.close()
        return


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

    offload_reply = hardware.get_gaming_offload_notice(OFFLOADER_STATE_FILE)
    is_offloaded_or_paused = bool(offload_reply) or game_paused

    if is_offloaded_or_paused and not cloud_fallback.is_enabled:
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

        enriched_text, validation_err = await submind.enrich_user_prompt(
            raw_text, user_title=USER_TITLE, default_location=DEFAULT_LOCATION
        )
        if validation_err:
            await message.channel.send(validation_err)
            return

        if is_offloaded_or_paused:
            vram_snap = await asyncio.to_thread(hardware.get_gpu_vram_stats)
            cloud_reply = await asyncio.to_thread(
                cloud_fallback.query,
                SYSTEM_PROMPT,
                chat_history,
                enriched_text,
                vram_snap,
            )
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
            vram_snap = await asyncio.to_thread(hardware.get_gpu_vram_stats)
            snapshot = list(chat_history)
            try:
                reply, formatted_user_text = await asyncio.to_thread(
                    ask_static, enriched_text, snapshot, vram_snap
                )
            except RuntimeError:
                notice = hardware.get_gaming_offload_notice(OFFLOADER_STATE_FILE)
                if notice:
                    cloud_reply = (
                        await asyncio.to_thread(
                            cloud_fallback.query,
                            SYSTEM_PROMPT,
                            chat_history,
                            enriched_text,
                            vram_snap,
                        )
                        if cloud_fallback.is_enabled
                        else None
                    )
                    await message.channel.send((cloud_reply or notice)[:1950])
                    return
                raise

            chat_history.append({"role": "user", "content": formatted_user_text})
            chat_history.append({"role": "assistant", "content": reply})
            if vram_snap["percent"] >= RELAXED_VRAM_PERCENT:
                if len(chat_history) > 5:
                    chat_history[:] = [chat_history[0]] + chat_history[-4:]
            else:
                while len(chat_history) > 21:
                    del chat_history[1:3]

        await message.channel.send(reply[:1950])


# ==============================================================================
# 5. SAFE ENTRYPOINT (NO TOP-LEVEL MUTEX OR SYS.EXIT ON IMPORT)
# ==============================================================================
def main() -> None:
    guard = lifecycle.SingleInstanceGuard()
    if not guard.acquire():
        print("[LOCK] AI Bot is already running in the background. Exiting duplicate.")
        sys.exit(0)

    try:
        healthy, messages = lifecycle.run_preflight_checks(
            discord_token=DISCORD_TOKEN,
            channel_id=CHANNEL_ID,
            lockout_file=LOCKOUT_FILE,
            lm_studio_port=int(LM_STUDIO_PORT),
        )
        for msg in messages:
            print(msg)
        if not healthy:
            sys.exit(1)

        while True:
            try:
                client.run(DISCORD_TOKEN)
                print(f"\n[SHUTDOWN] AI Bot stopped cleanly by {USER_TITLE}.")
                break
            except KeyboardInterrupt:
                print(f"\n[SHUTDOWN] AI Bot stopped cleanly by {USER_TITLE}.")
                break
            except Exception as e:
                print(f"[BOOT/NET WARN] Discord connection failed ({e}). Retrying in 10s...")
                time.sleep(10)
    finally:
        guard.release()


if __name__ == "__main__":
    main()
