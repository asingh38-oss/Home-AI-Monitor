# 🏠 Home AI Monitor

An AI-powered home security system built on macOS with computer vision, 
natural language event querying, and local IoT sensor integration.

Built as both a functional home security setup and a portfolio demonstration 
of practical AI engineering — custom intelligence layers on top of real infrastructure, 
not pre-built solutions.

---

## Architecture

\```
┌─────────────────────────────────────────────────────────┐
│                      MacBook Pro                        │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │              CameraManager                      │   │
│  │  ┌──────────────────┐  ┌─────────────────────┐  │   │
│  │  │  Per-Zone MOG2   │  │  Confidence Gating  │  │   │
│  │  │  Motion Detect   │  │  (skip low-conf     │  │   │
│  │  └────────┬─────────┘  │   frames)           │  │   │
│  │           │            └─────────────────────┘  │   │
│  └───────────┼─────────────────────────────────────┘   │
│              │                                          │
│  ┌───────────▼─────────────────────────────────────┐   │
│  │              Two-Model AI Pipeline               │   │
│  │  ┌──────────────────────┐                       │   │
│  │  │  Claude Vision API   │ ← Scene analysis,     │   │
│  │  │  (scene analysis)    │   threat level,       │   │
│  │  └──────────┬───────────┘   subject ID          │   │
│  │             │                                   │   │
│  │  ┌──────────▼───────────┐                       │   │
│  │  │  Claude Haiku        │ ← Conversational      │   │
│  │  │  (chat engine)       │   queries over        │   │
│  │  └──────────────────────┘   event history       │   │
│  └─────────────────────────────────────────────────┘   │
│              │                                          │
│  ┌───────────▼─────────────────────────────────────┐   │
│  │              EventStore (SQLite)                 │   │
│  │  camera_events | sensor_events | 3-month TTL    │   │
│  └───────────┬─────────────────────────────────────┘   │
│              │                                          │
│  ┌───────────▼─────────────────────────────────────┐   │
│  │  Zigbee2MQTT  ←  SONOFF ZBDongle-E              │   │
│  │  Contact sensors (doors/windows)                │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
          │                    │
  ┌───────▼──────┐    ┌────────▼────────┐
  │  Web Dashboard│    │  Push Alerts    │
  │  + AI Chat   │    │  (ntfy.sh)      │
  └──────────────┘    └─────────────────┘
\```

---

## Key Engineering Decisions

**Two-model AI strategy**
Motion detection (MOG2) runs locally at zero cost. Claude Vision is only 
called when motion clears a confidence threshold — a typical active day 
costs $0.01–$0.10 in API usage. A separate Haiku instance handles 
conversational queries against the event history, keeping costs low 
while preserving full reasoning capability for scene analysis.

**Per-zone MOG2 sensitivity**
Each camera zone has independent sensitivity parameters: minimum contour 
area, MOG2 history length, variance threshold, and blur kernel. A 
street-facing zone can be set to `low` to ignore passing cars while a 
doorstep zone is set to `very_high` to trigger on any presence.

**Confidence gating**
Frames are only forwarded to the Vision API if local motion analysis 
exceeds a confidence threshold. This eliminates API calls for lighting 
changes, shadows, and noise — the most common source of false positives 
in naive implementations.

**Event citation system**
When the chat engine answers a query ("were there any deliveries today?"), 
it cites specific event IDs inline. The dashboard resolves these citations 
into clickable thumbnail cards linked to the original video clip and snapshot.

**Local IoT stack**
Zigbee contact sensors feed through SONOFF ZBDongle-E → Zigbee2MQTT → 
MQTT broker, keeping all sensor data on-device. No cloud dependency for 
door/window state.

**Camera-agnostic RTSP layer**
The `rtsp_path` config value is the only thing that changes between camera 
brands. NVR setups (Night Owl, Hikvision, etc.) and direct-connect cameras 
(Reolink) both work without code changes.

---

## Stack

- **Python** — async architecture, multiprocessing per camera stream
- **OpenCV** — MOG2 background subtraction, per-zone motion analysis
- **Claude Vision API** — scene analysis, subject classification, threat assessment
- **Claude Haiku** — conversational event querying
- **SQLite** — event store with automatic TTL pruning
- **Zigbee2MQTT + MQTT** — local IoT sensor integration
- **Flask + SocketIO** — real-time web dashboard
- **Tailscale** — recommended for secure remote access

---

## Configuration

Copy `config.example.yaml` to `config.yaml` and fill in your values:

\```yaml
cameras:
  - id: front_door
    name: "Front Door"
    rtsp_url: "rtsp://USERNAME:PASSWORD@CAMERA_IP:554"
    rtsp_path: "/h264Preview_01_main"
    zones:
      - name: "doorstep"
        sensitivity: very_high

ai:
  anthropic_api_key: "YOUR_KEY_HERE"
  vision_model: "claude-opus-4-20250514"
  chat_model: "claude-haiku-4-5"
  analyze_cooldown_seconds: 20

storage:
  db_path: "./home_monitor.db"

zigbee:
  mqtt_broker: "localhost"
  mqtt_port: 1883
\```

---

## Privacy

- All video processing runs locally — frames are only sent to the Claude API 
  when motion is detected, with a configurable cooldown
- Zigbee sensor data never leaves your network
- Recordings and event database are stored locally
- Dashboard is LAN-only by default; Tailscale recommended for remote access