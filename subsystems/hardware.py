import contextlib
from datetime import datetime
import json
import os
import subprocess
import time
import discord
import psutil
import telemetry_db

# Gracefully handle systems without NVIDIA drivers/libraries
try:
    import pynvml

    NVML_AVAILABLE = True
except ImportError:
    NVML_AVAILABLE = False

# Hides console popups on Windows; evaluates to 0 on Linux
NO_WINDOW_FLAG = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# ==============================================================================
# 1. GPU VRAM & HARDWARE CLASSIFICATION
# ==============================================================================
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


def classify_hardware_state(
    vram_stats: dict, relaxed_vram_percent: float = 80.0
) -> str:
    """Classifies GPU load/thermals into NOMINAL, WARM, or STRAINED."""
    pct = float(vram_stats.get("percent", 0.0))
    temp_c = vram_stats.get("temp_c")

    if pct >= relaxed_vram_percent or (temp_c is not None and temp_c >= 72):
        return "STRAINED"
    if pct >= 65.0 or (temp_c is not None and temp_c >= 62):
        return "WARM"
    return "NOMINAL"


# ==============================================================================
# 2. CROSS-PROCESS LM STUDIO CLI LOCK & SERVER GUARD
# ==============================================================================
@contextlib.contextmanager
def lms_cli_lock(lock_file: str, wait_sec: float = 5.0):
    """Best-effort cross-process lock around `lms` CLI calls without deadlocking."""
    deadline = time.time() + wait_sec
    acquired = False
    while time.time() < deadline:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            # Clear stale lock files older than 60s in case a process crashed mid-CLI call
            try:
                if time.time() - os.path.getmtime(lock_file) > 60.0:
                    os.remove(lock_file)
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
                os.remove(lock_file)
            except OSError:
                pass


def run_lms_guarded(
    cmd: list[str], lock_file: str, timeout_sec: float = 20.0
) -> bool:
    """Executes an `lms` CLI command inside a cross-process lock; returns True on success."""
    try:
        with lms_cli_lock(lock_file):
            subprocess.run(
                cmd,
                check=True,
                timeout=timeout_sec,
                creationflags=NO_WINDOW_FLAG,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return True
    except Exception:
        return False


def unload_vram(lock_file: str, timeout_sec: float = 20.0) -> bool:
    """Fallback-only unload helper when offloader.py is offline."""
    return run_lms_guarded(["lms", "unload", "--all"], lock_file, timeout_sec)


# ==============================================================================
# 3. OFFLOADER JSON BRIDGE & SESSION MILESTONE TRACKER
# ==============================================================================
def read_offloader_state(state_file: str) -> dict:
    """Safely reads offloader_state.json written by offloader.py."""
    default_state = {
        "vram_offloaded": False,
        "reason": "",
        "session_minutes": 0,
        "session_milestone": "",
        "timestamp": 0.0,
        "heartbeat_ts": 0.0,
        "offloader_pid": None,
    }
    if not os.path.exists(state_file):
        return default_state
    try:
        with open(state_file, "r", encoding="utf-8") as f:
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


def is_offloader_live(
    state: dict, stale_sec: float = 15.0, now: float | None = None
) -> bool:
    """Returns True if offloader.py has a fresh heartbeat and active PID."""
    current_ts = time.time() if now is None else now
    last_beat = max(
        float(state.get("heartbeat_ts", 0.0)),
        float(state.get("timestamp", 0.0)),
    )
    if last_beat <= 0.0 or (current_ts - last_beat) > stale_sec:
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


def get_gaming_offload_notice(state_file: str) -> str | None:
    """Returns a status notice if VRAM is offloaded, or None if LLM is ready."""
    data = read_offloader_state(state_file)
    if data.get("vram_offloaded", False):
        reason = data.get("reason") or "a heavy application is active"
        return (
            "⚡ **Gaming / Relaxed Mode Active:** Local neural processing is"
            f" temporarily offloaded ({reason}) to preserve system stability and"
            " VRAM. Full capabilities will resume automatically once the session"
            " concludes."
        )
    return None


def evaluate_session_milestone(
    state_file: str,
    last_handled_milestone: str,
    user_title: str = "Operator",
    alerts_enabled: bool = True,
) -> tuple[str | None, str]:
    """
    Checks offloader_state.json for single-fire 2h/4h gaming session milestones.
    Returns (alert_message_or_none, updated_last_handled_milestone).
    """
    if not alerts_enabled:
        return None, last_handled_milestone

    data = read_offloader_state(state_file)
    if not data.get("vram_offloaded", False):
        return None, ""

    milestone = str(data.get("session_milestone", ""))
    session_mins = int(data.get("session_minutes", 0))
    reason = str(data.get("reason", "Active session")).replace(
        "Unloaded for ", ""
    )

    if milestone and milestone != last_handled_milestone:
        hours = round(session_mins / 60, 1)
        if milestone == "4h":
            msg = (
                f"⏳ **Extended Session Check-In ({hours}h), {user_title}:** "
                f"Active workload detected ({reason}). Consider stepping away for"
                " water, stretching, and resting your eyes before continuing."
            )
            return msg, milestone
        if milestone == "2h":
            msg = (
                f"🕒 **Session Milestone ({hours}h), {user_title}:** "
                f"You've been running continuously ({reason}). Good checkpoint to grab"
                " water or stretch."
            )
            return msg, milestone

    return None, last_handled_milestone


# ==============================================================================
# 4. SYSTEM MANIFEST BUILDER (!status)
# ==============================================================================
def build_system_status_embed(
    bot_name: str,
    llm_model: str,
    lm_studio_port: str,
    state_file: str,
    game_paused: bool = False,
    relaxed_mode: bool = False,
    relaxed_vram_pct: float = 80.0,
    pause_vram_pct: float = 90.0,
    offloader_stale_sec: float = 15.0,
) -> discord.Embed:
    """Builds a color-coded Discord Embed with live hardware telemetry and 24h baseline."""
    cpu = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage(os.path.abspath(os.sep))
    vram = get_gpu_vram_stats()
    baseline = telemetry_db.get_rolling_baseline(24.0)
    state = read_offloader_state(state_file)
    offloader_live = is_offloader_live(state, stale_sec=offloader_stale_sec)

    ram_used_gb = round(ram.used / (1024**3), 1)
    ram_total_gb = round(ram.total / (1024**3), 1)
    disk_free_gb = round(disk.free / (1024**3), 1)

    has_baseline = baseline["samples"] > 0
    ram_avg_str = f"\n24h Avg: `{baseline['avg_ram']}%`" if has_baseline else ""
    vram_avg_str = f"\n24h Avg: `{baseline['avg_vram']}%`" if has_baseline else ""
    temp_str = f" • `{vram['temp_c']}°C`" if vram.get("temp_c") is not None else ""

    # Determine sidebar color & top-level status
    if game_paused or state.get("vram_offloaded"):
        color = discord.Color.red()
        ai_state = "🔴 PAUSED / OFFLOADED"
    elif relaxed_mode or vram["percent"] >= relaxed_vram_pct:
        color = discord.Color.gold()
        ai_state = "🟡 RELAXED (TRIMMED CONTEXT)"
    else:
        color = discord.Color.green()
        ai_state = "🟢 ONLINE (FULL CONTEXT)"

    embed = discord.Embed(
        title=f"📊 {bot_name.upper()} HARDWARE & EQUIPMENT MANIFEST",
        description=f"**AI Backend State:** {ai_state}",
        color=color,
        timestamp=datetime.now(),
    )

    # Row 1: Compute & Memory (2-column grid)
    cpu_status = "Nominal" if cpu < 75 else "⚠️ Elevated"
    embed.add_field(
        name="⚙️ CPU Load",
        value=f"`{cpu}%` ({cpu_status})",
        inline=True,
    )

    ram_status = "Nominal" if ram.percent < 80 else "⚠️ High Load"
    embed.add_field(
        name="🧠 System RAM",
        value=f"`{ram_used_gb}/{ram_total_gb} GB` (`{ram.percent}%`)\nStatus: {ram_status}{ram_avg_str}",
        inline=True,
    )

    # Row 2: GPU VRAM & Storage (2-column grid)
    if vram["percent"] >= pause_vram_pct:
        vram_status = f"Critical (≥{pause_vram_pct:.0f}%)"
    elif vram["percent"] >= relaxed_vram_pct:
        vram_status = f"Relaxed (≥{relaxed_vram_pct:.0f}%)"
    else:
        vram_status = "Nominal"

    embed.add_field(
        name="🎮 GPU VRAM",
        value=(
            f"`{vram['used_gb']}/{vram['total_gb']} GB` (`{vram['percent']}%`){temp_str}\n"
            f"Status: {vram_status}{vram_avg_str}"
        ),
        inline=True,
    )

    disk_status = "Nominal" if disk_free_gb > 50 else "⚠️ Low Space"
    embed.add_field(
        name="💾 Primary SSD",
        value=f"`{disk_free_gb} GB` Free\nStatus: {disk_status}",
        inline=True,
    )

    # Row 3: 24h Telemetry & Offloader Daemon
    baseline_line = (
        f"`{baseline['samples']:,}` Samples Logged • SQLite WAL Active"
        if has_baseline
        else "Calibrating *(Waiting for Overseer samples)*"
    )
    offloader_tag = "🟢 LIVE" if offloader_live else "⚪ OFFLINE"
    offload_reason = (
        f"\nActive Guard: *{state['reason']}*"
        if state.get("vram_offloaded") and state.get("reason")
        else ""
    )

    embed.add_field(
        name="📈 24h Telemetry & Subsystems",
        value=(
            f"**Baseline:** {baseline_line}\n"
            f"**Offloader Daemon:** {offloader_tag}{offload_reason}"
        ),
        inline=False,
    )

    embed.set_footer(text=f"Model: {llm_model} • Port: {lm_studio_port}/v1")
    return embed
