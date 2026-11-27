import ctypes
import json
import os
import socket
import time
import subprocess


# ==============================================================================
# 1. SINGLE-INSTANCE OS MUTEX (MOVED OUT OF TOP-LEVEL IMPORT)
# ==============================================================================
class SingleInstanceGuard:
    """Prevents duplicate bot processes without blocking pytest imports."""

    def __init__(
        self,
        mutex_name: str = "LocalAIBotSingleInstanceMutex",
        linux_port: int = 49152,
    ):
        self.mutex_name = mutex_name
        self.linux_port = linux_port
        self._handle = None

    def acquire(self) -> bool:
        """Returns True if this process owns the lock, False if another instance is active."""
        if os.name == "nt":
            self._handle = ctypes.windll.kernel32.CreateMutexW(
                None, False, self.mutex_name
            )
            # ERROR_ALREADY_EXISTS == 183
            if ctypes.windll.kernel32.GetLastError() == 183:
                if self._handle:
                    ctypes.windll.kernel32.CloseHandle(self._handle)
                    self._handle = None
                return False
            return True

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", self.linux_port))
            self._handle = sock
            return True
        except OSError:
            sock.close()
            return False

    def release(self) -> None:
        if self._handle is None:
            return
        if os.name == "nt":
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
        else:
            try:
                self._handle.close()
            except Exception:
                pass
        self._handle = None


# ==============================================================================
# 2. LOCKOUT / TAGOUT (LOTO) & PRE-FLIGHT HEALTH GATE
# ==============================================================================
def is_lockout_active(lockout_file: str) -> tuple[bool, str]:
    """Checks if an emergency or UPS LOTO lockout.flag is engaged."""
    if not os.path.exists(lockout_file):
        return False, ""
    try:
        with open(lockout_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return True, str(data.get("reason", "Manual or UPS lockout active"))
    except (OSError, json.JSONDecodeError):
        return True, "Lockout flag present (unreadable payload)"


def engage_lockout(lockout_file: str, reason: str, source: str = "system") -> None:
    """Atomically writes lockout.flag to halt auto-restart loops during power/estop events."""
    tmp_file = f"{lockout_file}.tmp"
    payload = {
        "locked": True,
        "reason": reason,
        "source": source,
        "timestamp": time.time(),
    }
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_file, lockout_file)


def clear_lockout(lockout_file: str) -> bool:
    """Removes lockout.flag so normal startup can resume."""
    if os.path.exists(lockout_file):
        os.remove(lockout_file)
        return True
    return False


def run_preflight_checks(
    discord_token: str,
    channel_id: int,
    lockout_file: str,
    lm_studio_host: str = "127.0.0.1",
    lm_studio_port: int = 1234,
) -> tuple[bool, list[str]]:
    """
    Validates critical prerequisites before connecting to Discord.
    Returns (is_healthy, list_of_status_or_error_messages).
    """
    messages: list[str] = []

    locked, reason = is_lockout_active(lockout_file)
    if locked:
        messages.append(f"[LOTO LOCKOUT] Startup blocked: {reason}")
        return False, messages

    if not discord_token or channel_id == 0:
        messages.append("[CONFIG ERROR] Missing DISCORD_TOKEN or CHANNEL_ID in .env.")
        return False, messages

    # Non-blocking check to see if LM Studio is already listening (warn only, since cold-start recovery exists)
    try:
        with socket.create_connection((lm_studio_host, lm_studio_port), timeout=1.0):
            messages.append(f"[PREFLIGHT] LM Studio reachable on {lm_studio_host}:{lm_studio_port}.")
    except OSError:
        messages.append(
            f"[PREFLIGHT WARN] LM Studio not yet listening on port {lm_studio_port} (will auto-wake on demand)."
        )

    return True, messages


# ==============================================================================
# 3. UPS POWER MONITOR (DISABLED / MOCK / NUT TCP SOCKET)
# ==============================================================================
def _query_nut_var(
    host: str, port: int, ups_name: str, var_name: str, timeout: float = 2.0
) -> str | None:
    """Queries a single variable from NUT upsd over TCP (e.g., ups.status)."""
    cmd = f"GET VAR {ups_name} {var_name}\n".encode("ascii")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(cmd)
            response = sock.recv(512).decode("utf-8", errors="ignore").strip()
            # Expected NUT format: VAR <ups_name> <var_name> "<value>"
            if response.startswith("VAR ") and '"' in response:
                parts = response.split('"')
                if len(parts) >= 2:
                    return parts[1]
    except OSError:
        return None
    return None


def check_ups_status(
    mode: str = "disabled",
    mock_file: str = "mock_ups.json",
    ups_name: str = "ups",
    host: str = "127.0.0.1",
    port: int = 3493,
    min_battery_pct: int = 25,
) -> tuple[str, int, bool]:
    """
    Checks UPS power state across three modes: 'disabled', 'mock', or 'nut'.
    Returns (status_code, battery_charge_pct, should_trigger_loto_shutdown).
    """
    clean_mode = mode.strip().lower()
    if clean_mode in ("", "disabled", "off", "none"):
        return "DISABLED", 100, False

    status = "OL"
    charge = 100

    if clean_mode == "mock":
        if not os.path.exists(mock_file):
            return "MOCK_MISSING", 100, False
        try:
            with open(mock_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            status = str(data.get("status", "OL")).strip().upper()
            charge = int(data.get("battery_charge", 100))
        except (OSError, json.JSONDecodeError, ValueError):
            return "MOCK_ERROR", 100, False

    elif clean_mode == "nut":
        raw_status = _query_nut_var(host, port, ups_name, "ups.status")
        if raw_status is None:
            return "UNREACHABLE", 100, False
        status = raw_status.strip().upper()

        raw_charge = _query_nut_var(host, port, ups_name, "battery.charge")
        if raw_charge is not None:
            try:
                charge = int(float(raw_charge))
            except ValueError:
                charge = 100
    else:
        return f"UNKNOWN_MODE:{clean_mode}", 100, False

    # Trigger LOTO shutdown if Low Battery (LB) or On Battery (OB) at/below threshold
    tokens = set(status.split())
    on_battery = "OB" in tokens
    low_battery = "LB" in tokens
    critical = low_battery or (on_battery and charge <= min_battery_pct)

    return status, charge, critical


# ==============================================================================
# 4. GOODNIGHT PROTOCOL (ACTIVE-GAME GUARD + GRACEFUL OS SHUTDOWN)
# ==============================================================================
def build_os_shutdown_command(
    delay_sec: int = 15, platform_name: str | None = None
) -> list[str]:
    """Returns the cross-platform OS shutdown command for Windows or Linux."""
    target_os = platform_name if platform_name is not None else os.name
    if target_os == "nt":
        return ["shutdown", "/s", "/t", str(max(1, int(delay_sec)))]
    # Linux fallback (shutdown -h +1 schedules a 1-minute halt)
    return ["shutdown", "-h", "+1"]


def execute_goodnight_protocol(
    offloaded: bool,
    offload_reason: str = "",
    delay_sec: int = 15,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """
    Validates that no game is active before initiating a system shutdown.
    Returns (shutdown_initiated, status_message).
    """
    if offloaded and not force:
        detail = offload_reason or "an active game/workload is running"
        return (
            False,
            f"🛡️ **Goodnight Protocol Deferred:** Active session detected ({detail}). "
            "Close your game first or use `!goodnight force` to override.",
        )

    cmd = build_os_shutdown_command(delay_sec=delay_sec)
    if dry_run:
        return True, f"[DRY RUN] Would execute: {' '.join(cmd)}"

    try:
        subprocess.run(
            cmd,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
  
        abort_cmd = "shutdown /a" if os.name == "nt" else "shutdown -c"
        return (
            True,
            f"🌙 **Goodnight Protocol Initiated:** GPU VRAM unloaded. System power-down "
            f"scheduled in **{delay_sec}s** (`{abort_cmd}` to abort).",
        )

    except Exception as e:
        return False, f"⚠️ **Goodnight Shutdown Failed:** Could not invoke OS shutdown ({e})."
