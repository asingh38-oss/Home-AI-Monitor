"""
camera_manager.py
Manages RTSP streams from the Night Owl PoE NVR.
Per-zone MOG2 motion detection, face recognition, snapshot capture, clip recording.
"""

import asyncio
import base64
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger("camera")

# ── Sensitivity presets ───────────────────────────────────────────────────────
SENSITIVITY_PRESETS = {
    "very_low":  dict(min_area=8000, history=800, var_threshold=40, blur=9),
    "low":       dict(min_area=4000, history=600, var_threshold=30, blur=7),
    "medium":    dict(min_area=1500, history=400, var_threshold=20, blur=5),
    "high":      dict(min_area=600,  history=250, var_threshold=14, blur=5),
    "very_high": dict(min_area=200,  history=150, var_threshold=10, blur=3),
}


@dataclass
class MotionEvent:
    camera_name: str
    zone_name: str
    timestamp: datetime
    confidence: float
    frame: np.ndarray
    frame_b64: str = ""           # JPEG base64 for AI / dashboard
    ai_result: Optional[dict] = None
    alert_priority: str = "LOW"


@dataclass
class CameraState:
    name: str
    channel: int
    rtsp_url: str
    latest_frame: Optional[np.ndarray] = None
    latest_snapshot_b64: str = ""
    last_motion_time: float = 0.0
    last_ai_time: float = 0.0
    is_connected: bool = False
    zone_subtractors: dict = field(default_factory=dict)
    recent_events: deque = field(default_factory=lambda: deque(maxlen=50))


def _build_rtsp_url(nvr_cfg: dict, channel: int) -> str:
    ip   = nvr_cfg["ip"]
    user = nvr_cfg["username"]
    pwd  = nvr_cfg["password"]
    port = nvr_cfg.get("rtsp_port", 554)
    path_tpl = nvr_cfg.get("rtsp_path", "ch{channel:02d}/main/av_stream")
    path = path_tpl.format(channel=channel)
    return f"rtsp://{user}:{pwd}@{ip}:{port}/{path}"


def _frame_to_b64(frame: np.ndarray, quality: int = 70) -> str:
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf.tobytes()).decode()


class ZoneDetector:
    """Wraps a MOG2 background subtractor for one named zone."""

    def __init__(self, name: str, sensitivity: str):
        self.name = name
        p = SENSITIVITY_PRESETS.get(sensitivity, SENSITIVITY_PRESETS["medium"])
        self.min_area = p["min_area"]
        self.blur = p["blur"]
        self.subtractor = cv2.createBackgroundSubtractorMOG2(
            history=p["history"],
            varThreshold=p["var_threshold"],
            detectShadows=False,
        )

    def detect(self, frame: np.ndarray) -> float:
        """Returns motion confidence 0.0–1.0."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (self.blur, self.blur), 0)
        mask = self.subtractor.apply(blurred)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        motion_area = sum(cv2.contourArea(c) for c in contours if cv2.contourArea(c) > self.min_area)
        total_area = frame.shape[0] * frame.shape[1]
        return min(motion_area / total_area * 10, 1.0)


class FaceRecognizer:
    """Loads known faces and identifies people in frames."""

    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", True)
        self.tolerance = cfg.get("tolerance", 0.6)
        self.known_encodings = []
        self.known_names = []
        self._loaded = False

        if self.enabled:
            self._load(cfg.get("known_faces_dir", "known_faces"))

    def _load(self, faces_dir: str):
        try:
            import face_recognition as fr  # noqa
            self._fr = fr
            p = Path(faces_dir)
            for img_path in p.glob("*.jpg"):
                img = fr.load_image_file(str(img_path))
                encs = fr.face_encodings(img)
                if encs:
                    self.known_encodings.append(encs[0])
                    self.known_names.append(img_path.stem)
            log.info("Loaded %d known face(s)", len(self.known_names))
            self._loaded = True
        except ImportError:
            log.warning("face_recognition not installed — face ID disabled")
            self.enabled = False
        except Exception as e:
            log.warning("Face loading error: %s", e)

    def identify(self, frame: np.ndarray) -> list[str]:
        """Returns list of recognised names found in frame."""
        if not self.enabled or not self._loaded:
            return []
        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            locs = self._fr.face_locations(rgb)
            encs = self._fr.face_encodings(rgb, locs)
            names = []
            for enc in encs:
                matches = self._fr.compare_faces(self.known_encodings, enc, self.tolerance)
                name = "Unknown"
                if True in matches:
                    distances = self._fr.face_distance(self.known_encodings, enc)
                    name = self.known_names[int(np.argmin(distances))]
                names.append(name)
            return names
        except Exception as e:
            log.debug("Face recognition error: %s", e)
            return []


class ClipRecorder:
    """Writes short video clips around motion events."""

    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", True)
        self.output_dir = Path(cfg.get("output_dir", "recordings"))
        self.duration = cfg.get("clip_duration_seconds", 15)
        self._active: dict[str, dict] = {}

    def start(self, camera_name: str, fps: float, frame_size: tuple):
        if not self.enabled or camera_name in self._active:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = camera_name.replace(" ", "_")
        path = self.output_dir / f"{safe}_{ts}.mp4"
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(path), fourcc, fps, frame_size)
        self._active[camera_name] = {
            "writer": writer,
            "start": time.time(),
            "path": path,
        }
        log.debug("Recording started: %s", path)

    def write(self, camera_name: str, frame: np.ndarray):
        rec = self._active.get(camera_name)
        if not rec:
            return
        rec["writer"].write(frame)
        if time.time() - rec["start"] >= self.duration:
            self._stop(camera_name)

    def _stop(self, camera_name: str):
        rec = self._active.pop(camera_name, None)
        if rec:
            rec["writer"].release()
            log.info("Clip saved: %s", rec["path"])

    def stop_all(self):
        for name in list(self._active.keys()):
            self._stop(name)


class CameraManager:
    def __init__(self, config: dict, ai_analyzer, notifier, alarm_manager=None, event_store=None):
        self.config = config
        self.ai = ai_analyzer
        self.notifier = notifier
        self.alarm_manager = alarm_manager
        self.event_store = event_store
        self.nvr_cfg = config["nvr"]
        self.states: dict[str, CameraState] = {}
        self.face_recognizer = FaceRecognizer(config.get("face_recognition", {}))
        self.recorder = ClipRecorder(config.get("recording", {}))
        self._min_confidence = config["ai"].get("min_motion_confidence", 0.4)
        self._ai_cooldown = config["ai"].get("analysis_cooldown_seconds", 20)
        self._quiet_start, self._quiet_end = self._parse_quiet_hours()
        self._event_callbacks = []

        for cam_cfg in config.get("cameras", []):
            if not cam_cfg.get("enabled", True):
                continue
            channel = cam_cfg["channel"]
            url = _build_rtsp_url(self.nvr_cfg, channel)
            state = CameraState(
                name=cam_cfg["name"],
                channel=channel,
                rtsp_url=url,
            )
            for zone in cam_cfg.get("zones", [{"name": "default", "sensitivity": "medium"}]):
                state.zone_subtractors[zone["name"]] = ZoneDetector(
                    zone["name"], zone.get("sensitivity", "medium")
                )
            self.states[cam_cfg["name"]] = state

    def _parse_quiet_hours(self):
        from datetime import time as dtime
        def pt(s):
            h, m = map(int, s.split(":"))
            return dtime(h, m)
        qh = self.config.get("quiet_hours", {})
        return pt(qh.get("start", "23:00")), pt(qh.get("end", "06:30"))

    def _is_quiet_hours(self) -> bool:
        from datetime import datetime as dt, time as dtime
        now = dt.now().time()
        s, e = self._quiet_start, self._quiet_end
        return now >= s or now <= e if s > e else s <= now <= e

    def add_event_callback(self, cb):
        self._event_callbacks.append(cb)

    def _emit_event(self, event: MotionEvent):
        for cb in self._event_callbacks:
            try:
                cb(event)
            except Exception as e:
                log.debug("Event callback error: %s", e)

    async def run(self, stop_event: asyncio.Event):
        tasks = [
            asyncio.create_task(self._camera_loop(name, state, stop_event))
            for name, state in self.states.items()
        ]
        await stop_event.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.recorder.stop_all()

    async def _camera_loop(self, name: str, state: CameraState, stop_event: asyncio.Event):
        log.info("Camera loop starting: %s → %s", name, state.rtsp_url)
        loop = asyncio.get_event_loop()

        while not stop_event.is_set():
            try:
                cap = await loop.run_in_executor(None, lambda: cv2.VideoCapture(state.rtsp_url))
                if not cap.isOpened():
                    log.warning("Cannot open %s, retrying in 10s...", name)
                    await asyncio.sleep(10)
                    continue

                state.is_connected = True
                fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                log.info("%s connected: %dx%d @ %.1f fps", name, w, h, fps)

                while not stop_event.is_set():
                    ret, frame = await loop.run_in_executor(None, cap.read)
                    if not ret:
                        log.warning("%s: dropped frame", name)
                        await asyncio.sleep(0.1)
                        break

                    state.latest_frame = frame
                    state.latest_snapshot_b64 = _frame_to_b64(frame, quality=60)

                    # Motion detection across all zones
                    best_conf = 0.0
                    best_zone = "default"
                    for zone_name, detector in state.zone_subtractors.items():
                        conf = detector.detect(frame)
                        if conf > best_conf:
                            best_conf = conf
                            best_zone = zone_name

                    if best_conf >= self._min_confidence:
                        now = time.time()
                        if now - state.last_motion_time > 3.0:
                            state.last_motion_time = now
                            event = await self._handle_motion(state, frame, best_zone, best_conf, fps, (w, h))
                            if event:
                                self._emit_event(event)

                    self.recorder.write(name, frame)
                    await asyncio.sleep(1.0 / fps)

                cap.release()
                state.is_connected = False

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Camera %s error: %s — retrying in 15s", name, e)
                state.is_connected = False
                await asyncio.sleep(15)

    async def _handle_motion(
        self, state: CameraState, frame: np.ndarray,
        zone: str, confidence: float, fps: float, frame_size: tuple
    ) -> Optional[MotionEvent]:
        log.info("Motion: %s / %s  conf=%.2f", state.name, zone, confidence)

        # Face recognition (fast, local)
        known_faces = self.face_recognizer.identify(frame)

        frame_b64 = _frame_to_b64(frame)
        event = MotionEvent(
            camera_name=state.name,
            zone_name=zone,
            timestamp=datetime.now(),
            confidence=confidence,
            frame=frame,
            frame_b64=frame_b64,
        )

        # Start recording
        self.recorder.start(state.name, fps, frame_size)

        # Determine initial priority
        quiet = self._is_quiet_hours()
        if known_faces and "Unknown" not in known_faces:
            event.alert_priority = "LOW"
            await self.notifier.send(
                f"👤 {', '.join(known_faces)} detected at {state.name}",
                priority="LOW",
                image_b64=frame_b64,
            )
        elif quiet:
            event.alert_priority = "HIGH"
        else:
            event.alert_priority = "MEDIUM"

        # Claude Vision analysis (with cooldown)
        now = time.time()
        if now - state.last_ai_time >= self._ai_cooldown:
            state.last_ai_time = now
            ai_result = await self.ai.analyze(frame_b64, state.name, zone, known_faces, quiet)
            event.ai_result = ai_result
            if ai_result:
                event.alert_priority = ai_result.get("priority", event.alert_priority)
                await self.notifier.send(
                    f"🎥 {state.name}: {ai_result.get('description', 'Motion detected')}",
                    priority=event.alert_priority,
                    image_b64=frame_b64,
                )

        state.recent_events.appendleft(event)

        # Save snapshot JPEG to disk
        snapshot_path = None
        try:
            snap_dir = Path(self.config.get("recording", {}).get("output_dir", "recordings")) / "snapshots"
            snap_dir.mkdir(parents=True, exist_ok=True)
            ts_str = event.timestamp.strftime("%Y%m%d_%H%M%S")
            safe_cam = event.camera_name.replace(" ", "_")
            snap_file = snap_dir / f"{safe_cam}_{ts_str}.jpg"
            cv2.imwrite(str(snap_file), frame)
            snapshot_path = str(snap_file)
        except Exception as snap_e:
            log.debug("Snapshot save error: %s", snap_e)

        # Determine clip path from recorder
        clip_path = None
        rec = self.recorder._active.get(state.name)
        if rec:
            clip_path = str(rec["path"])

        # Persist to event store for AI chat queries
        if self.event_store:
            try:
                self.event_store.record_camera_event(
                    timestamp=event.timestamp,
                    camera=event.camera_name,
                    zone=event.zone_name,
                    confidence=event.confidence,
                    ai_result=event.ai_result,
                    known_faces=known_faces,
                    priority=event.alert_priority,
                    clip_path=clip_path,
                    snapshot_path=snapshot_path,
                )
            except Exception as e:
                log.debug("Event store write error: %s", e)

        return event