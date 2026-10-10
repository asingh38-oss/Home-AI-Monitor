# Sing Security — Claude Code Context

## Project
Home AI security monitor. Python async stack (Flask + SocketIO dashboard).
Deploys to a headless Ubuntu 24.04 host as a systemd service. Developed on multiple machines.
Machine-specific details (hosts, IPs, paths) live in CLAUDE.local.md, which is not committed.

## File tree (excludes venv, __pycache__, recordings, known_faces)
.gitignore
.vscode/settings.json
README.md
ai_analyzer.py        — Claude Vision scene analysis (model must be configurable; default to a cheap model)
alarm.py              — Alarm FSM: DISARMED / ARMED_HOME / ARMED_AWAY
camera_manager.py     — RTSP streams, MOG2 motion, face recognition, clips
chat.py               — Claude Haiku chat, alarm tools, entity registration
config.example.yaml   — Safe config template (commit this, never config.yaml)
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
Reolink RLC-810A, direct RTSP (no NVR). Each camera has its own `rtsp_url` in config;
the shared `nvr:` block is optional.
Format: rtsp://USER:PASSWORD@CAMERA_IP:554/h264Preview_01_main  (placeholders only, never real values)

## Config
config.yaml is local to each machine and holds secrets. Never commit it.
Sections: cameras, recording.output_dir, face_recognition.known_faces_dir, ai.api_key.
Keep config.example.yaml in sync whenever a new config key is added, using placeholder values only.

## Public repo rules (this repo is PUBLIC)
- Treat every commit and push as publishing to the internet.
- Never commit or paste: API keys, passwords, camera credentials, real IPs/hostnames, usernames,
  local file paths, camera images/clips, face data, events.db, recordings.
- Use placeholders in code, docs, tests, and commit messages (e.g. 192.0.2.10, example.local).
- Test fixtures must be synthetic or generated. No real home footage.
- If a secret appears in output or a file, stop and tell me instead of continuing.

## Edit rules
- Python files are Linux-ready. No macOS-specific code outside setup_mac.sh.
- NEVER read, print, edit, or commit config.yaml or .env. Edit config.example.yaml only.
- setup_mac.sh is dead. Ubuntu setup = new setup_ubuntu.sh from scratch.
- events.db stays local until the NAS mount is confirmed.
- Work on a feature branch. Small commits. Show the diff before committing.
- Never push to main, force-push, or rewrite history.
- Prefer editing existing files. One file per prompt where possible.

## Do not do without asking
- ssh/scp to any host, or any systemctl/sudo command.
- Delete or move anything under recordings/, known_faces/, or the NAS mount.
- Install system packages or run curl-pipe-to-shell installers.
- Upload files or text to paste/gist/sharing services.
- Change .gitignore to un-ignore a sensitive path.

## Known issues / planned work
- Dashboard crashes under Werkzeug dev server → run under gunicorn/waitress, not allow_unsafe_werkzeug.
- requirements.txt must keep paho-mqtt and setuptools<81 (face_recognition_models needs pkg_resources).
- No tests yet. Plan: pytest + fake RTSP stream (ffmpeg or mediamtx looping a synthetic test video).
- Add a model provider interface (Claude / Ollama) behind ai_analyzer.py.
- Read the API key from an env var first, falling back to config.yaml.

## Token saving
- /clear between unrelated tasks; /compact when context grows long
- /usage to monitor burn rate

# Compact instructions
When compacting, keep test output, code changes, and config decisions.
