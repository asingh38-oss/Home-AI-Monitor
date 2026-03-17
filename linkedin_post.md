---
LINKEDIN POST — Home AI Monitor
---

I wanted to deeply understand how to build a real AI vision pipeline end to end — so I built one for my house.

The result: a fully custom home security system running on my MacBook Pro, with 8 PoE cameras, Zigbee door/window sensors, and two Claude models doing different jobs.

Here's how it works:

🎥 **Computer vision layer**
Every camera stream gets processed with OpenCV MOG2 background subtraction — split into named zones with independent sensitivity levels. A street zone runs nearly deaf to ignore traffic. A doorstep zone catches any movement at all. Only events above a confidence threshold get escalated to the AI.

🧠 **AI classification (Claude Opus)**
Each motion event sends a JPEG frame to Claude Vision API. It classifies the subject (human, animal, vehicle, object), assigns a threat level, flags anything unusual, and writes a one-sentence description. All of this gets written to SQLite with the clip path and snapshot.

💬 **Natural language search (Claude Haiku)**
A separate chat engine sits on top of that event database. Ask it anything: *"any unknown visitors this week?"*, *"how many times was the back door opened yesterday?"*, *"what cars came to the driveway today?"*

It returns a cited answer — every claim is tagged with the specific event ID. The dashboard renders those as clickable thumbnail cards that open the actual video clip.

🚪 **IoT layer**
SONOFF Zigbee sensors on every door and window report state changes in under a second via a local MQTT broker. No cloud dependency — works even when the internet is down. The same event store captures everything.

🔔 **Alarm + notifications**
Full alarm state machine (ARMED_HOME / ARMED_AWAY / DISARMED) with PIN disarm, entry delay countdown, auto-arm schedule, and a browser siren. Push notifications via ntfy.sh include inline JPEG snapshots.

---

A few things I found interesting building this:

→ Running two different models for two different jobs (Opus for vision, Haiku for text chat) cut the chat cost by ~15x with no quality loss — the right model for the right task matters

→ The hardest part wasn't the AI. It was making the motion detection not constantly fire on tree branches. Per-zone sensitivity presets and a confidence threshold solved it

→ Grounding the AI chat in real cited events (rather than just returning text) was the most useful UX decision — you can actually verify what it's telling you

---

Full source on GitHub: [link]

Built with: Python · asyncio · OpenCV · dlib · Anthropic API · Flask · SocketIO · SQLite · Zigbee2MQTT · Mosquitto

#Python #MachineLearning #ComputerVision #AI #IoT #SoftwareEngineering #BuildInPublic
