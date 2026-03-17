"""
dashboard.py
Real-time web dashboard using Flask + SocketIO.
"""

import asyncio
import logging
from datetime import datetime
from threading import Thread

from flask import Flask, render_template, jsonify, request, send_file, abort
import os
import mimetypes
from flask_socketio import SocketIO

log = logging.getLogger("dashboard")


class Dashboard:
    def __init__(self, config: dict, camera_manager, zigbee_manager, alarm_manager, storage_manager, chat_engine=None):
        self.config = config.get("dashboard", {})
        self.camera_manager = camera_manager
        self.zigbee_manager = zigbee_manager
        self.alarm_manager = alarm_manager
        self.storage_manager = storage_manager
        self.chat_engine = chat_engine

        self.app = Flask(__name__, template_folder="templates")
        self.app.config["SECRET_KEY"] = "home-monitor-secret"
        self.socketio = SocketIO(
            self.app,
            async_mode="threading",
            cors_allowed_origins="*",
            logger=False,
            engineio_logger=False,
        )

        self._snapshot_interval = config.get("dashboard", {}).get("snapshot_interval_seconds", 2)
        self._register_routes()
        self._register_callbacks()

    def _register_routes(self):
        @self.app.route("/")
        def index():
            return render_template("index.html")

        @self.app.route("/manifest.json")
        def manifest():
            return jsonify({
                "name": "Home AI Monitor",
                "short_name": "HomeAI",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#0d0f14",
                "theme_color": "#0d0f14",
                "icons": [
                    {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"},
                ]
            })

        @self.app.route("/sw.js")
        def service_worker():
            sw = "self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));"
            from flask import Response
            return Response(sw, mimetype="application/javascript")

        @self.app.route("/api/status")
        def status():
            cameras = []
            for name, state in self.camera_manager.states.items():
                cameras.append({
                    "name": name,
                    "connected": state.is_connected,
                    "last_motion": state.last_motion_time,
                })
            sensors = []
            for name, state in self.zigbee_manager.sensors.items():
                sensors.append({
                    "name": name,
                    "type": state.sensor_type,
                    "is_open": state.is_open,
                    "battery": state.battery_pct,
                    "last_changed": state.last_changed.isoformat() if state.last_changed else None,
                })
            return jsonify({
                "cameras": cameras,
                "sensors": sensors,
                "alarm": self.alarm_manager.status,
            })

        @self.app.route("/api/events")
        def events():
            all_events = []
            for state in self.camera_manager.states.values():
                for ev in state.recent_events:
                    all_events.append({
                        "camera": ev.camera_name,
                        "zone": ev.zone_name,
                        "time": ev.timestamp.isoformat(),
                        "priority": ev.alert_priority,
                        "description": ev.ai_result.get("description", "Motion detected") if ev.ai_result else "Motion detected",
                        "confidence": round(ev.confidence, 2),
                    })
            all_events.sort(key=lambda x: x["time"], reverse=True)
            return jsonify(all_events[:50])

        @self.app.route("/api/alarm/arm", methods=["POST"])
        def alarm_arm():
            from flask import request
            from alarm import AlarmState
            data = request.get_json(silent=True) or {}
            mode_str = data.get("mode", "ARMED_HOME")
            try:
                mode = AlarmState[mode_str]
            except KeyError:
                return jsonify({"error": "invalid mode"}), 400
            self.alarm_manager.arm(mode, source="dashboard")
            return jsonify({"state": self.alarm_manager.state.value})

        @self.app.route("/api/alarm/disarm", methods=["POST"])
        def alarm_disarm():
            from flask import request
            data = request.get_json(silent=True) or {}
            pin = data.get("pin")
            success = self.alarm_manager.disarm(pin=pin, source="dashboard")
            if success:
                return jsonify({"state": "DISARMED"})
            return jsonify({"error": "wrong PIN"}), 403

        @self.app.route("/api/storage/stats")
        def storage_stats():
            return jsonify(self.storage_manager.get_disk_stats())

        @self.app.route("/api/storage/preview", methods=["POST"])
        def storage_preview():
            from flask import request
            data = request.get_json(silent=True) or {}
            preview = self.storage_manager.preview_delete(
                older_than_days=data.get("older_than_days"),
                free_up_gb=data.get("free_up_gb"),
            )
            return jsonify(preview)

        @self.app.route("/api/storage/delete", methods=["POST"])
        def storage_delete():
            from flask import request
            data = request.get_json(silent=True) or {}
            result = self.storage_manager.execute_delete(
                older_than_days=data.get("older_than_days"),
                free_up_gb=data.get("free_up_gb"),
            )
            return jsonify(result)


        @self.app.route("/api/chat", methods=["POST"])
        def api_chat():
            if not self.chat_engine:
                return jsonify({"answer": "Chat is not configured.", "events": []}), 503
            data = request.get_json(force=True)
            question = (data.get("question") or "").strip()
            if not question:
                return jsonify({"answer": "Please ask a question.", "events": []}), 400
            import asyncio
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(self.chat_engine.ask(question))
            finally:
                loop.close()

            # If the chat took any alarm actions, broadcast updated alarm state
            # so the dashboard panel updates live without a page refresh
            if result.get("actions"):
                self.socketio.emit("alarm_state", {
                    "state": self.alarm_manager.state.value,
                    "triggered": self.alarm_manager._triggered,
                    "timestamp": __import__("datetime").datetime.now().isoformat(),
                    "source": "chat",
                })

            return jsonify(result)

        @self.app.route("/api/chat/clear", methods=["POST"])
        def api_chat_clear():
            if self.chat_engine:
                self.chat_engine.clear_history()
            return jsonify({"ok": True})

        @self.app.route("/api/chat/upload", methods=["POST"])
        def api_chat_upload():
            """
            Multipart upload endpoint. Accepts:
              - file: the image (JPEG, PNG, WEBP, GIF)
              - question: the accompanying text message (optional)
            Stores the image on the ChatEngine, then runs ask() with the question.
            """
            if not self.chat_engine:
                return jsonify({"answer": "Chat is not configured.", "events": []}), 503

            uploaded = request.files.get("file")
            question  = request.form.get("question", "").strip()

            if not uploaded:
                return jsonify({"error": "No file provided"}), 400

            allowed = {"image/jpeg", "image/png", "image/webp", "image/gif"}
            media_type = uploaded.mimetype or "image/jpeg"
            if media_type not in allowed:
                return jsonify({"error": f"Unsupported file type: {media_type}"}), 400

            image_bytes = uploaded.read()
            if len(image_bytes) > 5 * 1024 * 1024:  # 5MB limit
                return jsonify({"error": "Image too large (max 5MB)"}), 400

            if not question:
                question = "What is this? Help me register it."

            # Attach image to chat engine for the next ask() call
            self.chat_engine.set_pending_image(image_bytes, media_type)

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(self.chat_engine.ask(question))
            finally:
                loop.close()

            if result.get("actions"):
                self.socketio.emit("alarm_state", {
                    "state": self.alarm_manager.state.value,
                    "triggered": self.alarm_manager._triggered,
                    "timestamp": __import__("datetime").datetime.now().isoformat(),
                    "source": "chat",
                })

            return jsonify(result)

        @self.app.route("/api/snapshot/<int:event_id>")
        def api_snapshot(event_id):
            if not self.chat_engine:
                abort(503)
            event = self.chat_engine.store.get_event_by_id(event_id)
            if not event or not event.get("snapshot_path"):
                abort(404)
            path = event["snapshot_path"]
            if not os.path.isfile(path):
                abort(404)
            return send_file(path, mimetype="image/jpeg")

        @self.app.route("/api/clip/<int:event_id>")
        def api_clip(event_id):
            if not self.chat_engine:
                abort(503)
            event = self.chat_engine.store.get_event_by_id(event_id)
            if not event or not event.get("clip_path"):
                abort(404)
            path = event["clip_path"]
            if not os.path.isfile(path):
                abort(404)
            mime = mimetypes.guess_type(path)[0] or "video/mp4"
            return send_file(path, mimetype=mime, conditional=True)

    def _register_callbacks(self):
        # Camera motion events → broadcast to all dashboard clients
        def on_camera_event(event):
            self.socketio.emit("motion_event", {
                "camera": event.camera_name,
                "zone": event.zone_name,
                "time": event.timestamp.isoformat(),
                "priority": event.alert_priority,
                "description": event.ai_result.get("description", "Motion detected") if event.ai_result else "Motion detected",
                "confidence": round(event.confidence, 2),
                "snapshot": event.frame_b64,
            })

        self.camera_manager.add_event_callback(on_camera_event)

        # Sensor events → broadcast
        def on_sensor_event(sensor_name, is_open):
            sensor = self.zigbee_manager.sensors.get(sensor_name, {})
            self.socketio.emit("sensor_event", {
                "name": sensor_name,
                "is_open": is_open,
                "time": datetime.now().isoformat(),
                "type": getattr(sensor, "sensor_type", "door"),
                "battery": getattr(sensor, "battery_pct", None),
            })

        self.zigbee_manager.add_event_callback(on_sensor_event)

        # Alarm state → broadcast
        def on_alarm_event(event: str, data: dict):
            self.socketio.emit(event, data)

        self.alarm_manager.add_state_callback(on_alarm_event)

        # Storage events → broadcast
        def on_storage_event(event: str, data: dict):
            self.socketio.emit(event, data)

        self.storage_manager.add_state_callback(on_storage_event)

    def _snapshot_broadcaster(self):
        """Periodically push camera snapshots to connected clients."""
        import time
        while True:
            time.sleep(self._snapshot_interval)
            snapshots = {}
            for name, state in self.camera_manager.states.items():
                if state.latest_snapshot_b64:
                    snapshots[name] = {
                        "b64": state.latest_snapshot_b64,
                        "connected": state.is_connected,
                    }
            if snapshots:
                self.socketio.emit("snapshots", snapshots)

    async def run(self, stop_event: asyncio.Event):
        host = self.config.get("host", "0.0.0.0")
        port = self.config.get("port", 5000)

        # Snapshot broadcaster in background thread
        Thread(target=self._snapshot_broadcaster, daemon=True).start()

        # Run Flask in a thread so we can await stop_event
        def _serve():
            self.socketio.run(
                self.app,
                host=host,
                port=port,
                use_reloader=False,
                log_output=False,
            )

        thread = Thread(target=_serve, daemon=True)
        thread.start()
        log.info("Dashboard running at http://%s:%d", host, port)

        await stop_event.wait()
        log.info("Dashboard shutting down")