# 🏠 Home AI Monitor

An AI-powered home security system built on macOS with computer vision, natural language event querying, and local IoT sensor integration.

Built as both a functional home security setup and a portfolio demonstration of practical AI engineering — custom intelligence layers on top of real infrastructure, not pre-built solutions.

---

## Architecture

```mermaid
flowchart TD
    CAM["📷 IP Cameras\nRTSP via Night Owl NVR"] --> CM
    SENSORS["🚪 Zigbee Contact Sensors\ndoors · windows"] --> ZB

    subgraph mac["MacBook Pro"]
        CM["CameraManager\nasync · multiprocessing per stream"]
        CM --> MOG2["Per-Zone MOG2\nMotion Detection"]
        MOG2 --> CG["Confidence Gating\nfilters shadows · noise · lighting changes"]
        CG --> VISION["Claude Vision API\nscene analysis · threat level · subject ID"]
        VISION --> ES
        VISION --> HAIKU["Claude Haiku\nconversational chat engine"]
        HAIKU --> ES

        ZB["SONOFF ZBDongle-E\nZigbee Coordinator"] --> Z2M["Zigbee2MQTT → MQTT Broker"]
        Z2M --> ES["EventStore · SQLite\ncamera_events · sensor_events · 3-month TTL"]

        ES --> DASH["Web Dashboard + AI Chat\nFlask · SocketIO · real-time"]
    end

    DASH --> ALERTS["📱 Push Notifications\nntfy.sh"]
```

---

## Key Engineering Decisions

**Two-model AI strategy**
Motion detection (MOG2) runs locally at zero cost. Claude Vision is only called when motion clears a confidence threshold — a typical active day costs $0.01–$0.10 in API usage. A separate Haiku instance handles conversational queries against the event history, keeping inference costs low while preserving full reasoning capability for scene analysis.

**Per-zone MOG2 sensitivity**
Each camera zone has independent sensitivity parameters: minimum contour area, MOG2 history length, variance threshold, and blur kernel. A street-facing zone can be set to `low` to ignore passing cars while a doorstep zone is set to `very_high` to trigger on any presence.

**Confidence gating**
Frames are only forwarded to the Vision API if local motion analysis exceeds a confidence threshold. This eliminates API calls for lighting changes, shadows, and noise — the most common source of false positives in naive implementations.

**Event citation system**
When the chat engine answers a query ("were there any deliveries today?"), it cites specific event IDs inline. The dashboard resolves these citations into clickable thumbnail cards linked to the original video clip and snapshot.

**Local IoT stack**
Zigbee contact sensors feed through SONOFF ZBDongle-E → Zigbee2MQTT → MQTT broker, keeping all sensor data on-device with no cloud dependency for door/window state.

**Camera-agnostic RTSP layer**
The `rtsp_path` config value is the only thing that changes between camera brands. NVR setups and direct-connect cameras both work without code changes.

---

## Stack

| Layer | Technology |
|---|---|
| Language | Python (async, multiprocessing) |
| Motion detection | OpenCV — MOG2 background subtraction |
| Scene analysis | Claude Vision API |
| Chat engine | Claude Haiku |
| Event storage | SQLite with automatic TTL pruning |
| IoT sensors | Zigbee2MQTT + MQTT broker |
| Dashboard | Flask + SocketIO |
| Remote access | Tailscale (recommended) |

---

## AI Processing Split

| Task | Where | Reason |
|---|---|---|
| Motion detection | Local (MOG2) | Zero latency, zero cost |
| Confidence gating | Local | Eliminates false-positive API calls |
| Scene analysis | Cloud (Claude Vision) | Requires vision model |
| Natural language queries | Cloud (Claude Haiku) | Conversational reasoning over event history |
| Sensor state | Local (Zigbee2MQTT) | Privacy-sensitive, works offline |

---

## Configuration

Copy `config.example.yaml` to `config.yaml` and fill in your values:

```yaml
cameras:
  - id: front_door
    name: "Front Door"
    rtsp_url: "rtsp://USERNAME:PASSWORD@CAMERA_IP:554"
    rtsp_path: "/h264Preview_01_main"   # adjust per camera brand
    zones:
      - name: "doorstep"
        sensitivity: very_high
      - name: "street"
        sensitivity: low

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

dashboard:
  host: "0.0.0.0"
  port: 8080
  secret_key: "CHANGE-THIS-TO-A-RANDOM-STRING"
```

**Sensitivity levels:** `very_low` → `low` → `medium` → `high` → `very_high`

Each level independently adjusts minimum contour area, MOG2 history length, variance threshold, and blur kernel.

---

## Example Chat Queries

Once running, the dashboard's AI chat panel can answer natural language questions about your event history:

- *"Were there any deliveries today?"*
- *"How many times was the front door opened this week?"*
- *"Did anything happen in the driveway after midnight?"*
- *"Show me all high-priority events from yesterday"*

Responses cite specific event IDs that link directly to the video clip and snapshot.

---

## File Structure

```
home_monitor/
├── main.py                  # Entry point — starts all services
├── config.yaml              # Your configuration (gitignored)
├── config.example.yaml      # Safe template to copy from
├── camera_manager.py        # RTSP streams, zone motion detection, confidence gating
├── ai_analyzer.py           # Claude Vision API integration
├── chat.py                  # Haiku-powered conversational query engine
├── event_store.py           # SQLite event storage with TTL pruning
├── zigbee_sensors.py        # Zigbee2MQTT / MQTT sensor integration
├── dashboard.py             # Flask + SocketIO web server + API routes
├── templates/
│   └── index.html           # Real-time dashboard UI with AI chat panel
├── requirements.txt
└── config.example.yaml      # Safe config template (no secrets)
```

---

## Privacy

- All video processing runs locally — frames are only sent to the Claude API when motion is detected, with a configurable cooldown per camera
- Zigbee sensor data never leaves your network
- Recordings and the event database are stored locally
- Dashboard is LAN-only by default; Tailscale recommended for secure remote access
- No accounts, no subscriptions, no third-party cloud storage
