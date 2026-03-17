"""
zigbee_sensors.py
Subscribes to Zigbee2MQTT topics via Mosquitto MQTT.
Reports door/window open-close events and fires notifications.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import paho.mqtt.client as mqtt

log = logging.getLogger("zigbee")


@dataclass
class SensorState:
    name: str
    sensor_id: str
    sensor_type: str          # "door" or "window"
    is_open: bool = False
    battery_pct: Optional[int] = None
    last_changed: Optional[datetime] = None
    last_seen: Optional[datetime] = None


class ZigbeeSensorManager:
    def __init__(self, config: dict, notifier, alarm_manager=None, event_store=None):
        self.config = config
        self.notifier = notifier
        self.alarm_manager = alarm_manager
        self.event_store = event_store
        mqtt_cfg = config.get("mqtt", {})
        self.host = mqtt_cfg.get("host", "localhost")
        self.port = mqtt_cfg.get("port", 1883)
        self.username = mqtt_cfg.get("username") or None
        self.password = mqtt_cfg.get("password") or None
        self.base_topic = mqtt_cfg.get("base_topic", "zigbee2mqtt")

        self.sensors: dict[str, SensorState] = {}
        self._id_to_name: dict[str, str] = {}

        for s in config.get("zigbee_sensors", []):
            sid = s["sensor_id"]
            state = SensorState(
                name=s["name"],
                sensor_id=sid,
                sensor_type=s.get("type", "door"),
            )
            self.sensors[s["name"]] = state
            self._id_to_name[sid] = s["name"]

        self._client: Optional[mqtt.Client] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._event_callbacks = []
        self._quiet_start, self._quiet_end = self._parse_quiet_hours()

    def _parse_quiet_hours(self):
        from datetime import time as dtime
        def pt(s):
            h, m = map(int, s.split(":"))
            return dtime(h, m)
        qh = self.config.get("quiet_hours", {})
        return pt(qh.get("start", "23:00")), pt(qh.get("end", "06:30"))

    def _is_quiet_hours(self) -> bool:
        from datetime import datetime as dt
        now = dt.now().time()
        s, e = self._quiet_start, self._quiet_end
        return now >= s or now <= e if s > e else s <= now <= e

    def add_event_callback(self, cb):
        self._event_callbacks.append(cb)

    def _emit(self, sensor_name: str, is_open: bool):
        for cb in self._event_callbacks:
            try:
                cb(sensor_name, is_open)
            except Exception as e:
                log.debug("Sensor event callback error: %s", e)

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected to %s:%d", self.host, self.port)
            for sid in self._id_to_name:
                topic = f"{self.base_topic}/{sid}"
                client.subscribe(topic)
                log.debug("Subscribed to %s", topic)
        else:
            log.error("MQTT connection failed, rc=%d", rc)

    def _on_message(self, client, userdata, msg):
        try:
            sid = msg.topic.split("/")[-1]
            name = self._id_to_name.get(sid)
            if not name:
                return

            payload = json.loads(msg.payload.decode())
            sensor = self.sensors[name]
            prev_open = sensor.is_open

            # Zigbee2MQTT: contact=true means CLOSED, contact=false means OPEN
            if "contact" in payload:
                sensor.is_open = not payload["contact"]

            if "battery" in payload:
                sensor.battery_pct = int(payload["battery"])

            sensor.last_seen = datetime.now()

            if sensor.is_open != prev_open:
                sensor.last_changed = datetime.now()
                self._emit(name, sensor.is_open)

                state_str = "opened" if sensor.is_open else "closed"
                emoji = "🚪" if sensor.sensor_type == "door" else "🪟"
                quiet = self._is_quiet_hours()

                # Trigger alarm if armed and sensor opened
                if sensor.is_open and self.alarm_manager and self.alarm_manager.is_armed:
                    from alarm import TriggerSource
                    self.alarm_manager.trigger(
                        TriggerSource.SENSOR,
                        f"{name} opened while alarm is armed",
                        self._loop,
                    )
                    priority = "HIGH"
                elif sensor.is_open and quiet:
                    priority = "HIGH"
                elif sensor.is_open:
                    priority = "MEDIUM"
                else:
                    priority = "LOW"

                log.info(
                    "Sensor [%s] %s — priority=%s%s",
                    name, state_str, priority,
                    " (quiet hours)" if quiet else "",
                )

                # Persist to event store for AI chat queries
                if self.event_store:
                    try:
                        self.event_store.record_sensor_event(
                            timestamp=sensor.last_changed,
                            sensor_name=name,
                            sensor_type=sensor.sensor_type,
                            state="open" if sensor.is_open else "closed",
                            priority=priority,
                        )
                    except Exception as db_e:
                        log.debug("Event store write error: %s", db_e)

                # Schedule async notification from sync callback
                if self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self.notifier.send(
                            f"{emoji} {name} {state_str}",
                            priority=priority,
                        ),
                        self._loop,
                    )

        except Exception as e:
            log.error("MQTT message error: %s", e)

    def _on_disconnect(self, client, userdata, rc):
        log.warning("MQTT disconnected (rc=%d), will auto-reconnect", rc)

    async def run(self, stop_event: asyncio.Event):
        if not self.sensors:
            log.info("No Zigbee sensors configured, skipping MQTT")
            await stop_event.wait()
            return

        self._loop = asyncio.get_event_loop()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        self._client = client

        if self.username:
            client.username_pw_set(self.username, self.password)

        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.reconnect_delay_set(min_delay=5, max_delay=60)

        try:
            client.connect(self.host, self.port, keepalive=60)
            client.loop_start()
            log.info("MQTT client started, connecting to %s:%d", self.host, self.port)
            await stop_event.wait()
        except Exception as e:
            log.error("MQTT startup error: %s — is Mosquitto running?", e)
            await stop_event.wait()
        finally:
            client.loop_stop()
            client.disconnect()
            log.info("MQTT client stopped")
