# Local AI Companion & Hardware Telemetry Suite

A modular, locally hosted Discord AI companion powered by **LM Studio**, paired with an automated game-aware VRAM offloader and a real-time hardware spike monitor.

## Why This Architecture?
Running a local LLM on a daily-driver gaming PC usually creates a conflict: either the model consumes GPU VRAM and System RAM while you game, or you have to manually open LM Studio and unload models every time you launch a heavy title.

This suite splits responsibilities across three lightweight Python services that communicate through a shared atomic JSON state file (`offloader_state.json`):
* **`ai_bot.py` (Core Discord Companion):** Handles natural conversation, live DuckDuckGo web search, real-time weather reports, scheduled Cycle Start/End check-ins, and dynamic context trimming when GPU VRAM crosses 80%.
* **`offloader.py` (Game-Aware VRAM Watchdog):** Polls active processes every second against your configured game library (`games_config.json`). When a non-whitelisted game launches, it automatically executes `lms unload --all` so your GPU and RAM are immediately freed—and notifies `ai_bot.py` to reply with a clean standby message instead of timing out.
* **`overseer.py` (Hardware Spike Telemetry):** Monitors sustained GPU VRAM and System RAM spikes (default: ≥ 85% for 10+ seconds), sends Discord webhook alerts listing the top resource-consuming processes, and triggers an emergency model unload if System RAM reaches critical levels.

---

## Project Structure

```text
ai-bot/
├── ai_bot.py                   # Core Discord bot & LM Studio client
├── offloader.py                # Automated game detector & VRAM unloader
├── overseer.py                 # Sustained RAM/VRAM spike alert daemon
├── Toggle_AI_Bot.bat           # One-click Windows start/stop background toggle
├── requirements.txt            # Python dependencies
├── .env.example                # Environment variable template
├── games_config.example.json   # Game library & whitelist template
├── presets/
│   └── default.json            # Default "Atlas" character preset
└── Sounds/
    └── .gitkeep                # Optional folder for local voice-channel audio clips
```

---

## Prerequisites
* **Python 3.10+**
* **[LM Studio](https://lmstudio.ai)** installed with the `lms` CLI available in your system PATH and the local server enabled (default port `1234`).
* **Discord Bot Token** (with **Message Content Intent** enabled in the [Discord Developer Portal](https://discord.com)).
* **FFmpeg** (optional—only required if you use voice-channel audio commands).

---

## Quick Setup

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env` and fill in your credentials and preferences:
```bash
copy .env.example .env
```
* `DISCORD_TOKEN`: Your Discord bot token.
* `CHANNEL_ID`: The numeric ID of the Discord channel where the bot should listen and post updates.
* `CHARACTER_PRESET`: Name of the JSON file inside `presets/` to load (defaults to `default` for **Atlas**).
* `DEFAULT_LOCATION` & `TIMEZONE_LABEL`: City/region and timezone label used for live weather reports.
* `LM_STUDIO_URL` & `LLM_MODEL`: Your local LM Studio endpoint and loaded model identifier.
* `OVERSEER_WEBHOOK_URL`: Discord channel webhook URL for hardware spike alerts.

### 3. Configure Game Watchdog (Optional)
Copy `games_config.example.json` to `games_config.json` and customize your Steam/Xbox install directories, whitelisted lightweight games, and standalone heavy executables:
```bash
copy games_config.example.json games_config.json
```

### 4. Run the Suite
* **One-Click Background Toggle (Windows):** Double-click `Toggle_AI_Bot.bat` to launch `offloader.py`, `overseer.py`, and `ai_bot.py` silently via `pythonw.exe`. Double-click it again to stop all three processes cleanly.
* **Manual / Terminal Mode:** Run each script in its own terminal window (`python ai_bot.py`, `python offloader.py`, `python overseer.py`).

---

## Discord Commands
* **`!status`**: Displays a live hardware manifest (CPU load, System RAM, GPU VRAM, free SSD space, and AI backend state).
* **`!transmit`**: Generates an atmospheric 2-paragraph dispatch using your active preset's prompt scenarios (falls back to cached logs if VRAM is constrained).
* **`!screenoff`**: Powers down connected Windows monitors via `WM_SYSCOMMAND`.
* **Custom Audio Commands**: Any command mapped under `"audio_commands"` in your active preset (e.g., `!chime` in `default.json`) joins your active voice channel and plays the corresponding `.mp3` from `Sounds/`.
* **Natural Chat, Weather & Web Search**: Mention words like `weather` or `forecast` for live telemetry, or ask the bot to `search` / `look up` current topics via DuckDuckGo.

---

## Creating Custom Character Presets
You can create a new AI persona without modifying Python code:
1. Duplicate `presets/default.json` (e.g., `presets/my_character.json`).
2. Customize `bot_name`, `user_title`, `system_prompt`, `transmit_prompt`, `transmit_scenarios`, `fallback_fragments`, and `audio_commands`.
3. Set `CHARACTER_PRESET=my_character` in your `.env` file and restart the bot.
