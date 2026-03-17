# 🏠 Home AI Monitor

A Raspberry Pi 5 home security system with AI-powered analysis, per-zone motion sensitivity, facial recognition, and real-time push notifications.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Raspberry Pi 5                           │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │   Camera 1   │  │   Camera 2   │  │   Camera 3/4         │  │
│  │  (Kitchen)   │  │ (Front Door) │  │ (Garden / Hallway)   │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬───────────┘  │
│         │                 │                     │               │
│         └─────────────────┼─────────────────────┘               │
│                           │                                     │
│                 ┌─────────▼─────────┐                           │
│                 │  CameraManager    │                           │
│                 │  ┌─────────────┐  │                           │
│                 │  │ ZoneMotion  │  │ ← Per-zone MOG2           │
│                 │  │  Detector   │  │   sensitivity levels      │
│                 │  └─────────────┘  │                           │
│                 │  ┌─────────────┐  │                           │
│                 │  │    Face     │  │ ← face_recognition        │
│                 │  │ Recognition │  │   (local, on-device)      │
│                 │  └─────────────┘  │                           │
│                 └─────────┬─────────┘                           │
│                           │                                     │
│  ┌────────────────────┐   │   ┌──────────────────────────────┐  │
│  │ MotionSensorMgr    │   │   │       AIAnalyzer             │  │
│  │ (PIR via GPIO)     │   │   │  Sends frame to Claude API   │  │
│  └─────────┬──────────┘   │   │  → human/animal/object       │  │
│            │              │   │  → threat level              │  │
│            └──────────────┤   │  → unusual activity flag     │  │
│                           │   └──────────────┬───────────────┘  │
│                           │                  │                  │
│                 ┌─────────▼──────────────────▼──────────┐       │
│                 │           Notifier + Dashboard         │       │
│                 └────────────────────────────────────────┘       │
└─────────────────────────────┬───────────────────────────────────┘
                              │
              ┌───────────────┼────────────────────┐
              │               │                    │
     ┌────────▼──────┐  ┌─────▼──────┐   ┌────────▼────────┐
     │  ntfy.sh App  │  │  Web       │   │  Claude Vision  │
     │  (Phone Push) │  │  Dashboard │   │  API (cloud AI) │
     └───────────────┘  └────────────┘   └─────────────────┘
```

---

## AI Processing Strategy: Hybrid (Recommended)

| Task | Where | Why |
|---|---|---|
| Motion detection | **Local (Pi)** | Zero latency, zero cost, no network needed |
| Face recognition | **Local (Pi)** | Privacy-sensitive, works offline |
| Human vs animal classification | **Cloud (Claude API)** | Requires vision model, too heavy for Pi |
| Unusual activity description | **Cloud (Claude API)** | Natural language, nuanced understanding |
| PIR sensor alerts | **Local (Pi)** | Instant, hardware-level |

The Pi handles ~95% of processing. Claude API is only called when significant motion is detected, with a configurable cooldown (default: 20 seconds per camera). A typical active day might cost **$0.01–$0.10** in API usage.

---

## Per-Zone Motion Sensitivity

Each camera can have multiple named zones, each with independent sensitivity:

```
┌─────────────────────────────────────┐
│         FRONT DOOR CAMERA           │
│  ┌───────────────────────────────┐  │
│  │    street_distant (LOW)       │  │ ← Ignores cars, people on pavement
│  └───────────────────────────────┘  │
│                                     │
│  ┌───────────────────────────────┐  │
│  │                               │  │
│  │       doorstep (VERY HIGH)    │  │ ← Triggers on any presence
│  │                               │  │
│  └───────────────────────────────┘  │
└─────────────────────────────────────┘
```

Sensitivity levels: `very_low` → `low` → `medium` → `high` → `very_high`

Each level adjusts:
- **Minimum contour area** — how large must movement be
- **MOG2 history** — how many frames to build background model
- **Variance threshold** — how much pixel change counts as motion
- **Blur kernel** — smoothing to reduce noise

---

## File Structure

```
home_monitor/
├── main.py                  # Entry point — starts all services
├── config.yaml              # All configuration (edit this first)
├── camera_manager.py        # Cameras, zone motion detection, face recognition
├── ai_analyzer.py           # Claude Vision API integration
├── motion_sensor_manager.py # PIR sensors via GPIO
├── notifier.py              # Push notifications (ntfy.sh / Pushover)
├── dashboard.py             # Flask + SocketIO web server
├── templates/
│   └── index.html           # Real-time web dashboard UI
├── known_faces/             # Add name.jpg files here for face recognition
├── recordings/              # Auto-saved motion clips
├── requirements.txt
└── install.sh               # One-shot setup script
```

---

## Quick Start

### 1. Install
```bash
git clone <this-repo> home_monitor && cd home_monitor
bash install.sh
```

### 2. Configure
Edit `config.yaml`:
- Set your `anthropic_api_key`
- Set a unique `ntfy.topic` (e.g. `my-house-abc123`)
- Set camera `source` values (0, 1, 2… or RTSP URLs)
- Adjust zone `coordinates` to match your camera views
- Set GPIO pin numbers for your PIR sensors

### 3. Add known faces
```bash
# Put clear face photos in known_faces/
cp john.jpg known_faces/John.jpg
cp jane.jpg known_faces/Jane.jpg
```

### 4. Test run
```bash
source venv/bin/activate
python main.py
```
Open dashboard: `http://<pi-ip>:5000`

### 5. Get push notifications on phone
- Install the **ntfy** app (iOS / Android — free)
- Subscribe to your topic (the one set in config.yaml)
- Alerts will arrive instantly when triggered

### 6. Run on boot
```bash
sudo systemctl start home-monitor
sudo systemctl status home-monitor
```

---

## Hardware Shopping List

| Item | Notes |
|---|---|
| Raspberry Pi 5 (8GB) | 8GB recommended for face recognition |
| USB/CSI cameras ×3–4 | Night vision (IR) models e.g. Reolink, Arducam |
| PIR sensors ×3–4 | HC-SR501, ~£2 each |
| MicroSD 64GB+ | Class 10 / A2 for recording |
| Heatsink + fan | Pi 5 runs warm under load |
| PoE hat (optional) | Powers cameras over ethernet |

---

## Useful Commands

```bash
# View live logs
sudo journalctl -u home-monitor -f

# Restart service
sudo systemctl restart home-monitor

# Check camera sources
v4l2-ctl --list-devices

# Test a camera
python3 -c "import cv2; c=cv2.VideoCapture(0); print(c.isOpened())"

# Check GPIO pins
gpio readall
```

---

## Privacy Notes

- Face recognition runs **entirely on the Pi** — no face data leaves your network
- Camera frames are only sent to the Claude API when motion is detected, with configurable cooldown
- All recordings are stored locally in `./recordings/`
- The dashboard is only accessible on your local network (no external exposure by default)
