# Sing Security — Claude Code Context

## Project
Home AI security monitor. Python async stack.
Target: Alienware x17 R1 → Ubuntu 24.04 headless NVR.
Dev machine: HP OMEN (C:\Users\fires\Documents\Home-AI-Monitor).
NAS: UGREEN DXP2800 @ TBD IP — update when confirmed.

## File tree (excludes venv, __pycache__, recordings, known_faces)
.gitignore
.vscode/settings.json
README.md
ai_analyzer.py        — Claude Vision API (Opus) scene analysis
alarm.py              — Alarm FSM: DISARMED / ARMED_HOME / ARMED_AWAY
camera_manager.py     — RTSP streams, MOG2 motion, face recognition, clips
chat.py               — Claude Haiku chat, alarm tools, entity registration
config.example.yaml   — Safe config template (commit this, not config.yaml)
dashboard.py          — Flask + SocketIO server, all API routes
event_store.py        — SQLite: camera_events + sensor_events, 3mo retention
known_entities.py     — SQLite: vehicles, plates, animals
linkedin_post.md      — Launch post draft
main.py               — Async entry point, starts all subsystems
notifier.py           — ntfy.sh push notifications
requirements.txt      — Python deps
setup_mac.sh          — DEPRECATED. Do not patch. Write setup_ubuntu.sh fresh.
storage_manager.py    — Disk usage monitor
templates/index.html  — Dashboard UI: camera grid, chat panel, alarm

## Cameras
Reolink RLC-810A x4, direct RTSP (no NVR):
rtsp://admin:PASSWORD@CAMERA_IP:554/h264Preview_01_main
RTSP must be enabled per camera: Reolink app → Device Settings → Network → Advanced → Port 554

## Edit rules
- Python files are Linux-ready. No macOS-specific code outside setup_mac.sh.
- Never edit config.yaml. Edit config.example.yaml only.
- setup_mac.sh is dead. Ubuntu setup = new setup_ubuntu.sh from scratch.
- events.db stays local until NAS SMB mount is confirmed, then update db_path in config.
- recordings/ will point to NAS mount — update output_dir in config when NAS is live.

## Token saving
- /compact when context grows long
- /clear between unrelated tasks
- /cost to monitor burn rate
- Target one file per prompt, not broad sweeps
