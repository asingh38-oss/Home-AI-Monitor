"""
chat.py
AI chat interface over the event database, with full alarm system control.

Two capabilities:
  1. Event queries — answers questions about camera/sensor history with [EVENT:id] citations
  2. Alarm control — arms, disarms, sets PIN, configures schedule via Anthropic tool use

Tool use loop:
  Claude may call tools (arm, disarm, set_pin, etc.) mid-response.
  We execute each tool call, feed results back, and continue until Claude
  returns a final text answer.
"""

import base64
import json
import logging
import re
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import anthropic

from event_store import EventStore
from known_entities import KnownEntities

log = logging.getLogger("chat")

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a home security assistant with two roles:

━━━ ROLE 1: EVENT ANALYST ━━━
You have access to a database of security camera and door/window sensor events.
When answering questions about activity, cite events with [EVENT:id] inline.
Example: "A dog was spotted at 2:34pm [EVENT:47] and again at 5:10pm [EVENT:52]."

Rules for event queries:
- Be specific: include times, counts, camera names
- Cite every specific event with [EVENT:id] so the user can click to watch footage
- Never fabricate events — if data is absent, say so
- Format times in friendly language ("Tuesday at 2:34pm")
- Group similar events with bullet points

━━━ ROLE 2: ALARM CONTROLLER ━━━
You can control the alarm system using tools. You can:
- Check alarm status
- Arm in HOME or AWAY mode
- Disarm (requires PIN — always ask the user for it first)
- Change the alarm PIN (requires current PIN — always ask first)
- Set the auto-arm/disarm schedule
- Set the entry delay

Security rules you must follow:
- NEVER disarm without first asking the user "Please enter your alarm PIN"
- NEVER change the PIN without first asking for the current PIN
- If a PIN is wrong, tell the user clearly and do not retry automatically
- For arming, no PIN is needed — just confirm the mode with the user first

━━━ ROLE 3: SETUP GUIDE ━━━
If the user asks to set up or configure the alarm, guide them step by step:
1. First check current status with get_alarm_status
2. Ask what PIN they want (explain requirements: 4-8 digits)
3. Set it with set_alarm_pin
4. Ask about auto-arm schedule preferences
5. Configure with set_auto_arm_schedule
6. Ask about entry delay (explain: how long they have to disarm when arriving home)
7. Confirm everything looks right

Be conversational and helpful. Explain what each setting does in plain English.
Always confirm what you're about to change before doing it.

Today's date and time will be provided in the event context."""

# ── Alarm tools definition ────────────────────────────────────────────────────

ALARM_TOOLS = [
    {
        "name": "get_alarm_status",
        "description": (
            "Get the current alarm state (DISARMED, ARMED_HOME, ARMED_AWAY), "
            "whether it has been triggered, the current PIN length (not the PIN itself), "
            "and the auto-arm schedule configuration."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "arm_alarm",
        "description": (
            "Arm the alarm. HOME mode adds an entry delay before triggering (good for sleeping). "
            "AWAY mode triggers instantly (good for when everyone leaves the house)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["home", "away"],
                    "description": "'home' = ARMED_HOME with entry delay. 'away' = ARMED_AWAY instant trigger.",
                }
            },
            "required": ["mode"],
        },
    },
    {
        "name": "disarm_alarm",
        "description": (
            "Disarm the alarm. Requires the user's PIN. "
            "Always ask the user for their PIN before calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pin": {
                    "type": "string",
                    "description": "The alarm PIN provided by the user.",
                }
            },
            "required": ["pin"],
        },
    },
    {
        "name": "set_alarm_pin",
        "description": (
            "Change the alarm PIN. Requires the current PIN for verification. "
            "Always ask the user for their current PIN and desired new PIN before calling this. "
            "New PIN must be 4-8 digits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "current_pin": {
                    "type": "string",
                    "description": "The current alarm PIN for verification.",
                },
                "new_pin": {
                    "type": "string",
                    "description": "The new PIN to set (4-8 digits, numbers only).",
                },
            },
            "required": ["current_pin", "new_pin"],
        },
    },
    {
        "name": "set_auto_arm_schedule",
        "description": (
            "Enable or disable the automatic arm/disarm schedule. "
            "When enabled, the alarm arms at arm_at time and disarms at disarm_at time every day."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "Whether to enable the auto-arm schedule.",
                },
                "arm_at": {
                    "type": "string",
                    "description": "Time to arm each night in HH:MM (24hr) format, e.g. '23:00'.",
                },
                "disarm_at": {
                    "type": "string",
                    "description": "Time to disarm each morning in HH:MM (24hr) format, e.g. '07:00'.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["ARMED_HOME", "ARMED_AWAY"],
                    "description": "Which mode to use when auto-arming. ARMED_HOME recommended for overnight.",
                },
            },
            "required": ["enabled"],
        },
    },
    {
        "name": "set_entry_delay",
        "description": (
            "Set the entry delay in seconds for ARMED_HOME mode. "
            "This is the grace period after a door/window opens before the siren fires — "
            "giving you time to disarm when you arrive home."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "integer",
                    "description": "Delay in seconds. Recommended: 20-60. Maximum: 120.",
                }
            },
            "required": ["seconds"],
        },
    },
    {
        "name": "register_face",
        "description": (
            "Register a known person's face from a photo the user has uploaded. "
            "Saves the photo to known_faces/ so the face recognizer identifies them on camera. "
            "Always call this when the user uploads a photo of a person they want recognised."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The person's name. Used as the display name when recognised on camera.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "register_vehicle",
        "description": (
            "Register a known vehicle from a photo the user has uploaded. "
            "Stores the vehicle name, description, and photo so the AI recognises it on camera. "
            "Call this when the user uploads a photo of a car, van, or truck they own or expect."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "A short name for the vehicle, e.g. 'My Tesla' or 'Wife\'s Honda'.",
                },
                "description": {
                    "type": "string",
                    "description": "Colour, make, model, any distinguishing features. E.g. 'Red Tesla Model 3, 2022'.",
                },
            },
            "required": ["name", "description"],
        },
    },
    {
        "name": "register_license_plate",
        "description": (
            "Register a known licence plate from a photo the user has uploaded, or from plate text they type. "
            "The AI will flag matching plates seen on camera as known and lower their threat level."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plate_text": {
                    "type": "string",
                    "description": "The licence plate string, e.g. 'ABC 1234'. Will be stored in uppercase.",
                },
                "owner_name": {
                    "type": "string",
                    "description": "Who the plate belongs to, e.g. 'My car' or 'John\'s van'.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional: vehicle make/model/colour.",
                },
            },
            "required": ["plate_text", "owner_name"],
        },
    },
    {
        "name": "register_animal",
        "description": (
            "Register a known animal (pet or regular wildlife visitor) from a photo the user uploads. "
            "The AI will recognise this animal on camera and report it by name instead of 'unknown animal'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The animal's name or identifier, e.g. 'Max the dog' or 'neighbourhood fox'.",
                },
                "description": {
                    "type": "string",
                    "description": "Species, breed, colour, size — e.g. 'Golden retriever, large, fluffy'.",
                },
            },
            "required": ["name", "description"],
        },
    },
    {
        "name": "list_known_entities",
        "description": "List all registered known faces, vehicles, licence plates, and animals.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "remove_known_entity",
        "description": "Remove a registered entity by its ID. Get IDs from list_known_entities.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "integer",
                    "description": "The numeric ID of the entity to remove.",
                },
                "entity_type": {
                    "type": "string",
                    "description": "Type for confirmation: 'face', 'vehicle', 'license_plate', or 'animal'.",
                },
            },
            "required": ["entity_id"],
        },
    },
]


# ── Event context helpers ─────────────────────────────────────────────────────

def _format_events_for_context(
    camera_events: list[dict],
    sensor_events: list[dict],
    stats: dict,
    now: datetime,
) -> str:
    lines = [f"Current date/time: {now.strftime('%A %d %B %Y, %H:%M')}\n"]

    if stats.get("by_subject"):
        lines.append("=== EVENT SUMMARY (requested period) ===")
        for subject, count in stats["by_subject"].items():
            lines.append(f"  {subject}: {count} events")

    if stats.get("by_sensor"):
        lines.append("\n=== SENSOR SUMMARY ===")
        for s in stats["by_sensor"]:
            lines.append(f"  {s['sensor_name']} {s['state']}: {s['cnt']} times")

    if camera_events:
        lines.append(f"\n=== CAMERA EVENTS ({len(camera_events)} records) ===")
        lines.append("Format: [id] time | camera / zone | subject | description")
        for e in camera_events[:200]:
            ts = datetime.fromisoformat(e["timestamp"])
            time_str = ts.strftime("%a %d %b, %H:%M")
            faces = f" [{e['known_faces']}]" if e.get("known_faces") else ""
            unusual = " ⚠️UNUSUAL" if e.get("unusual") else ""
            has_clip = " 🎬" if e.get("clip_path") else ""
            has_snap = " 📷" if e.get("snapshot_path") else ""
            lines.append(
                f"  [ID:{e['id']}] {time_str} | {e['camera']} / {e['zone']} | "
                f"{e['subject']}{faces} | {e['description']}{unusual}{has_clip}{has_snap}"
            )
        if len(camera_events) > 200:
            lines.append(f"  ... and {len(camera_events) - 200} more events")

    if sensor_events:
        lines.append(f"\n=== SENSOR EVENTS ({len(sensor_events)} records) ===")
        for e in sensor_events[:200]:
            ts = datetime.fromisoformat(e["timestamp"])
            time_str = ts.strftime("%a %d %b, %H:%M")
            lines.append(
                f"  {time_str} | {e['sensor_name']} ({e['sensor_type']}) → {e['state'].upper()}"
            )

    if not camera_events and not sensor_events:
        lines.append("\nNo events found for the requested period.")

    return "\n".join(lines)


def _parse_time_range(question: str, now: datetime) -> tuple[datetime, datetime]:
    q = question.lower()
    if "today" in q:
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        until = now
    elif "yesterday" in q:
        yesterday = now - timedelta(days=1)
        since = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        until = yesterday.replace(hour=23, minute=59, second=59)
    elif any(w in q for w in ["this week", "past week", "last week"]):
        since, until = now - timedelta(days=7), now
    elif any(w in q for w in ["this month", "past month", "last month"]):
        since, until = now - timedelta(days=30), now
    elif "3 months" in q or "past 3 months" in q:
        since, until = now - timedelta(days=90), now
    else:
        m = re.search(r"past\s+(\d+)\s+(day|hour|week)", q)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            delta = (timedelta(days=n) if "day" in unit else
                     timedelta(hours=n) if "hour" in unit else
                     timedelta(weeks=n))
            since, until = now - delta, now
        else:
            since, until = now - timedelta(days=7), now
    return since, until


def _extract_subject_filter(question: str) -> Optional[str]:
    q = question.lower()
    if any(w in q for w in ["dog", "cat", "animal", "fox", "bird", "pet", "wildlife"]):
        return "animal"
    if any(w in q for w in ["car", "vehicle", "van", "truck", "lorry"]):
        return "vehicle"
    if any(w in q for w in ["person", "people", "human", "someone", "visitor", "stranger", "unknown"]):
        return "human"
    return None


def _extract_event_ids(text: str) -> list[int]:
    return [int(m) for m in re.findall(r"\[EVENT:(\d+)\]", text)]


# ── ChatEngine ────────────────────────────────────────────────────────────────

class ChatEngine:
    def __init__(self, config: dict, event_store: EventStore, alarm_manager=None,
                 config_path: str = "config.yaml", known_entities=None):
        self.config = config
        self.store = event_store
        self.alarm_manager = alarm_manager
        self.config_path = config_path
        self.known_entities = known_entities  # KnownEntities instance
        self._pending_image: Optional[tuple[bytes, str]] = None  # (bytes, media_type)

        ai_cfg = config.get("ai", {})
        self.model = ai_cfg.get("chat_model", "claude-haiku-4-5-20251001")
        self.client = anthropic.AsyncAnthropic(api_key=ai_cfg["api_key"])
        self._history: list[dict] = []

    def clear_history(self):
        self._history = []

    # ── Tool execution ────────────────────────────────────────────────────────

    def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool call and return a JSON string result."""
        try:
            if tool_name == "get_alarm_status":
                return self._tool_get_status()
            elif tool_name == "arm_alarm":
                return self._tool_arm(tool_input["mode"])
            elif tool_name == "disarm_alarm":
                return self._tool_disarm(tool_input["pin"])
            elif tool_name == "set_alarm_pin":
                return self._tool_set_pin(tool_input["current_pin"], tool_input["new_pin"])
            elif tool_name == "set_auto_arm_schedule":
                return self._tool_set_schedule(tool_input)
            elif tool_name == "set_entry_delay":
                return self._tool_set_entry_delay(tool_input["seconds"])
            elif tool_name == "register_face":
                return self._tool_register_face(tool_input["name"])
            elif tool_name == "register_vehicle":
                return self._tool_register_vehicle(tool_input["name"], tool_input.get("description", ""))
            elif tool_name == "register_license_plate":
                return self._tool_register_plate(
                    tool_input["plate_text"], tool_input["owner_name"],
                    tool_input.get("description", ""),
                )
            elif tool_name == "register_animal":
                return self._tool_register_animal(tool_input["name"], tool_input.get("description", ""))
            elif tool_name == "list_known_entities":
                return self._tool_list_entities()
            elif tool_name == "remove_known_entity":
                return self._tool_remove_entity(tool_input["entity_id"])
            else:
                return json.dumps({"error": f"Unknown tool: {tool_name}"})
        except Exception as e:
            log.error("Tool %s error: %s", tool_name, e)
            return json.dumps({"error": str(e)})

    def _tool_get_status(self) -> str:
        if not self.alarm_manager:
            return json.dumps({"error": "Alarm manager not connected"})

        am = self.alarm_manager
        alarm_cfg = self.config.get("alarm", {})
        sched = alarm_cfg.get("auto_arm", {})

        return json.dumps({
            "state": am.state.value,
            "triggered": am._triggered,
            "entry_delay_seconds": am.entry_delay_seconds,
            "siren_duration_seconds": am.siren_duration_seconds,
            "pin_length": len(am.pin),
            "auto_arm": {
                "enabled": am.auto_arm_enabled,
                "arm_at": sched.get("arm_at", "23:00"),
                "disarm_at": sched.get("disarm_at", "07:00"),
                "mode": am.auto_arm_mode.value if am.auto_arm_enabled else "ARMED_HOME",
            },
            "recent_triggers": am._trigger_log[-3:],
        })

    def _tool_arm(self, mode: str) -> str:
        if not self.alarm_manager:
            return json.dumps({"error": "Alarm manager not connected"})

        from alarm import AlarmState
        mode_map = {"home": AlarmState.ARMED_HOME, "away": AlarmState.ARMED_AWAY}
        alarm_state = mode_map.get(mode.lower())
        if not alarm_state:
            return json.dumps({"error": f"Invalid mode: {mode}. Use 'home' or 'away'."})

        self.alarm_manager.arm(alarm_state, source="chat")
        log.info("Chat armed alarm: %s", alarm_state.value)
        return json.dumps({
            "success": True,
            "state": alarm_state.value,
            "message": f"Alarm armed in {alarm_state.value} mode.",
        })

    def _tool_disarm(self, pin: str) -> str:
        if not self.alarm_manager:
            return json.dumps({"error": "Alarm manager not connected"})

        success = self.alarm_manager.disarm(pin=str(pin), source="chat")
        if success:
            log.info("Chat disarmed alarm")
            return json.dumps({"success": True, "message": "Alarm disarmed successfully."})
        else:
            return json.dumps({"success": False, "message": "Incorrect PIN. Alarm remains armed."})

    def _tool_set_pin(self, current_pin: str, new_pin: str) -> str:
        if not self.alarm_manager:
            return json.dumps({"error": "Alarm manager not connected"})

        # Validate current PIN
        if str(current_pin) != self.alarm_manager.pin:
            return json.dumps({"success": False, "message": "Current PIN is incorrect."})

        # Validate new PIN format
        new_pin = str(new_pin).strip()
        if not new_pin.isdigit():
            return json.dumps({"success": False, "message": "PIN must contain only digits."})
        if not (4 <= len(new_pin) <= 8):
            return json.dumps({"success": False, "message": "PIN must be 4-8 digits."})

        # Apply to alarm manager
        self.alarm_manager.pin = new_pin

        # Persist to config.yaml
        self.config.setdefault("alarm", {})["pin"] = new_pin
        self._save_config()

        log.info("Alarm PIN changed via chat")
        return json.dumps({
            "success": True,
            "message": f"PIN changed successfully. New PIN is {len(new_pin)} digits.",
        })

    def _tool_set_schedule(self, params: dict) -> str:
        if not self.alarm_manager:
            return json.dumps({"error": "Alarm manager not connected"})

        from datetime import time as dtime
        from alarm import AlarmState

        enabled = params.get("enabled", False)
        self.alarm_manager.auto_arm_enabled = enabled

        alarm_cfg = self.config.setdefault("alarm", {})
        sched_cfg = alarm_cfg.setdefault("auto_arm", {})
        sched_cfg["enabled"] = enabled

        if enabled:
            arm_at   = params.get("arm_at", "23:00")
            disarm_at = params.get("disarm_at", "07:00")
            mode_str  = params.get("mode", "ARMED_HOME")

            # Validate times
            try:
                h, m = map(int, arm_at.split(":"))
                self.alarm_manager.auto_arm_time = dtime(h, m)
                sched_cfg["arm_at"] = arm_at
            except Exception:
                return json.dumps({"success": False, "message": f"Invalid arm_at time: {arm_at}"})

            try:
                h, m = map(int, disarm_at.split(":"))
                self.alarm_manager.auto_disarm_time = dtime(h, m)
                sched_cfg["disarm_at"] = disarm_at
            except Exception:
                return json.dumps({"success": False, "message": f"Invalid disarm_at time: {disarm_at}"})

            try:
                mode = AlarmState[mode_str]
                self.alarm_manager.auto_arm_mode = mode
                sched_cfg["mode"] = mode_str
            except KeyError:
                return json.dumps({"success": False, "message": f"Invalid mode: {mode_str}"})

            self._save_config()
            log.info("Auto-arm schedule set: arm=%s disarm=%s mode=%s", arm_at, disarm_at, mode_str)
            return json.dumps({
                "success": True,
                "message": (
                    f"Auto-arm enabled. Will arm at {arm_at} ({mode_str}) "
                    f"and disarm at {disarm_at} every day."
                ),
            })
        else:
            self._save_config()
            log.info("Auto-arm schedule disabled via chat")
            return json.dumps({"success": True, "message": "Auto-arm schedule disabled."})

    def _tool_set_entry_delay(self, seconds: int) -> str:
        if not self.alarm_manager:
            return json.dumps({"error": "Alarm manager not connected"})

        seconds = int(seconds)
        if not (5 <= seconds <= 120):
            return json.dumps({"success": False, "message": "Entry delay must be between 5 and 120 seconds."})

        self.alarm_manager.entry_delay_seconds = seconds
        self.config.setdefault("alarm", {})["entry_delay_seconds"] = seconds
        self._save_config()

        log.info("Entry delay set to %ds via chat", seconds)
        return json.dumps({
            "success": True,
            "message": f"Entry delay set to {seconds} seconds.",
        })

    # ── Entity registration tools ────────────────────────────────────────────

    def _tool_register_face(self, name: str) -> str:
        if not self._pending_image:
            return json.dumps({
                "success": False,
                "message": "No image attached to this message. Please attach a clear photo of the person's face.",
            })
        img_bytes, _ = self._pending_image
        path = Path("known_faces") / f"{name.replace(' ', '_')}.jpg"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(img_bytes)
        # Trigger face recognizer reload
        if hasattr(self, "_face_recognizer_reload_cb") and self._face_recognizer_reload_cb:
            try:
                self._face_recognizer_reload_cb()
            except Exception:
                pass
        log.info("Registered face: %s → %s", name, path)
        return json.dumps({
            "success": True,
            "message": f"Saved face photo for '{name}' to known_faces/. They will be identified on camera from now on.",
            "path": str(path),
        })

    def _tool_register_vehicle(self, name: str, description: str) -> str:
        if not self.known_entities:
            return json.dumps({"error": "Entity store not available"})
        photo_path = None
        if self._pending_image:
            img_bytes, _ = self._pending_image
            photo_path = self.known_entities.save_photo(name, "vehicle", img_bytes)
        eid = self.known_entities.register("vehicle", name, description, photo_path)
        return json.dumps({
            "success": True,
            "id": eid,
            "message": f"Vehicle '{name}' registered (id={eid}). The AI will now recognise it on camera.",
        })

    def _tool_register_plate(self, plate_text: str, owner_name: str, description: str) -> str:
        if not self.known_entities:
            return json.dumps({"error": "Entity store not available"})
        photo_path = None
        if self._pending_image:
            img_bytes, _ = self._pending_image
            photo_path = self.known_entities.save_photo(plate_text, "plate", img_bytes)
        eid = self.known_entities.register(
            "license_plate", owner_name, description, photo_path, plate_text
        )
        plate_upper = plate_text.upper().strip()
        return json.dumps({
            "success": True,
            "id": eid,
            "message": f"Plate '{plate_upper}' registered to '{owner_name}' (id={eid}). It will be flagged as known when seen on camera.",
        })

    def _tool_register_animal(self, name: str, description: str) -> str:
        if not self.known_entities:
            return json.dumps({"error": "Entity store not available"})
        photo_path = None
        if self._pending_image:
            img_bytes, _ = self._pending_image
            photo_path = self.known_entities.save_photo(name, "animal", img_bytes)
        eid = self.known_entities.register("animal", name, description, photo_path)
        return json.dumps({
            "success": True,
            "id": eid,
            "message": f"Animal '{name}' registered (id={eid}). The AI will identify them by name on camera.",
        })

    def _tool_list_entities(self) -> str:
        faces_dir = Path("known_faces")
        face_names = [p.stem.replace("_", " ") for p in faces_dir.glob("*.jpg")] if faces_dir.exists() else []
        entities = self.known_entities.get_all() if self.known_entities else []
        return json.dumps({
            "faces": face_names,
            "other_entities": [
                {"id": e["id"], "type": e["type"], "name": e["name"],
                 "description": e["description"], "plate_text": e.get("plate_text")}
                for e in entities
            ],
        })

    def _tool_remove_entity(self, entity_id: int) -> str:
        if not self.known_entities:
            return json.dumps({"error": "Entity store not available"})
        ok = self.known_entities.remove(entity_id)
        if ok:
            return json.dumps({"success": True, "message": f"Entity {entity_id} removed."})
        return json.dumps({"success": False, "message": f"No entity found with id={entity_id}."})

    def set_pending_image(self, image_bytes: bytes, media_type: str = "image/jpeg"):
        """Called by the dashboard before ask() when the user attaches an image."""
        self._pending_image = (image_bytes, media_type)

    def clear_pending_image(self):
        self._pending_image = None

    def _save_config(self):
        """Write the current in-memory config back to config.yaml."""
        try:
            path = Path(self.config_path)
            with open(path, "w") as f:
                yaml.dump(self.config, f, default_flow_style=False, sort_keys=False)
            log.info("Config saved to %s", path)
        except Exception as e:
            log.error("Failed to save config: %s", e)

    # ── Main ask method ───────────────────────────────────────────────────────

    async def ask(self, question: str) -> dict:
        """
        Process a question. May involve:
        - Querying the event database
        - One or more alarm tool calls (multi-turn tool use loop)

        Returns:
            answer  — final text response (may contain [EVENT:id] markers)
            events  — list of cited event dicts with clip/snapshot paths
            actions — list of alarm actions taken during this response
        """
        try:
            now = datetime.now()
            since, until = _parse_time_range(question, now)
            subject_filter = _extract_subject_filter(question)

            q = question.lower()
            wants_sensors = any(w in q for w in [
                "door", "window", "sensor", "open", "opened", "closed",
                "entry", "back door", "front door", "gate", "garage door",
            ])
            wants_camera = not wants_sensors or any(w in q for w in [
                "camera", "saw", "seen", "detected", "passed", "visited",
                "delivery", "animal", "dog", "cat", "car", "vehicle",
                "person", "people", "package", "parcel",
            ])

            camera_events, sensor_events = [], []
            if wants_camera:
                camera_events = self.store.query_camera_events(
                    since=since, until=until, subject=subject_filter, limit=300,
                )
            if wants_sensors:
                sensor_events = self.store.query_sensor_events(
                    since=since, until=until, limit=300,
                )

            stats = self.store.get_summary_stats(since=since)
            context = _format_events_for_context(camera_events, sensor_events, stats, now)

            # Build message for this turn (context + question)
            # Build user message — attach image bytes if one was uploaded
            if self._pending_image:
                img_bytes, media_type = self._pending_image
                img_b64 = base64.b64encode(img_bytes).decode()
                user_message_content = [
                    {"type": "text", "text": f"{context}\n\n---\nQuestion: {question}"},
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": img_b64,
                    }},
                ]
            else:
                user_message_content = f"{context}\n\n---\nQuestion: {question}"

            user_content = user_message_content
            self._history.append({"role": "user", "content": user_content})
            messages = self._history[-20:]

            # ── Tool use loop ─────────────────────────────────────────────────
            actions_taken = []
            final_answer = ""

            while True:
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    tools=ALARM_TOOLS,
                    messages=messages,
                )

                # Collect any text blocks
                text_blocks = [b.text for b in response.content if b.type == "text"]
                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

                if response.stop_reason == "end_turn" or not tool_use_blocks:
                    # No more tool calls — extract final answer
                    final_answer = " ".join(text_blocks).strip()
                    break

                # Execute all tool calls in this response
                tool_results = []
                for tool_block in tool_use_blocks:
                    log.info("Executing tool: %s(%s)", tool_block.name, tool_block.input)
                    result = self._execute_tool(tool_block.name, tool_block.input)
                    result_data = json.loads(result)

                    actions_taken.append({
                        "tool": tool_block.name,
                        "input": tool_block.input,
                        "result": result_data,
                    })

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": result,
                    })

                # Append assistant message (with tool_use blocks) + tool results to history
                messages = messages + [
                    {"role": "assistant", "content": response.content},
                    {"role": "user", "content": tool_results},
                ]

            # ── Finalize ──────────────────────────────────────────────────────
            # Update persistent history with clean question/answer (no context blob)
            self._history[-1] = {"role": "user", "content": question}
            self._history.append({"role": "assistant", "content": final_answer})

            # Extract cited event IDs
            cited_ids = _extract_event_ids(final_answer)
            cited_events = self.store.get_events_by_ids(cited_ids) if cited_ids else []

            log.info(
                "Chat Q: %s → %d chars, %d events cited, %d tools called",
                question[:60], len(final_answer), len(cited_events), len(actions_taken),
            )
            self.clear_pending_image()
            return {
                "answer": final_answer,
                "events": cited_events,
                "actions": actions_taken,
            }

        except anthropic.APIError as e:
            log.error("Chat API error: %s", e)
            return {"answer": "Sorry, I couldn't reach the AI service right now.", "events": [], "actions": []}
        except Exception as e:
            log.error("Chat error: %s", e)
            return {"answer": f"Sorry, something went wrong: {e}", "events": [], "actions": []}