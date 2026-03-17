"""
alarm.py
Manages alarm state: DISARMED, ARMED_HOME, ARMED_AWAY.
Triggered by door/window sensors or cameras when armed.
Broadcasts alarm events to the dashboard via SocketIO.
"""

import asyncio
import logging
from datetime import datetime, time as dtime
from enum import Enum
from typing import Optional, Callable

log = logging.getLogger("alarm")


class AlarmState(str, Enum):
    DISARMED    = "DISARMED"
    ARMED_HOME  = "ARMED_HOME"   # Home / sleep mode — entry delay applies
    ARMED_AWAY  = "ARMED_AWAY"   # Away — instant trigger


class TriggerSource(str, Enum):
    SENSOR  = "sensor"
    CAMERA  = "camera"
    MANUAL  = "manual"


class AlarmManager:
    def __init__(self, config: dict, notifier):
        self.config = config
        self.notifier = notifier
        alarm_cfg = config.get("alarm", {})

        self.state: AlarmState = AlarmState.DISARMED
        self.pin: str = str(alarm_cfg.get("pin", "1234"))
        self.entry_delay_seconds: int = alarm_cfg.get("entry_delay_seconds", 30)
        self.siren_duration_seconds: int = alarm_cfg.get("siren_duration_seconds", 120)

        # Auto-arm schedule
        sched = alarm_cfg.get("auto_arm", {})
        self.auto_arm_enabled: bool = sched.get("enabled", False)
        self.auto_arm_time: Optional[dtime] = self._parse_time(sched.get("arm_at", "23:00"))
        self.auto_disarm_time: Optional[dtime] = self._parse_time(sched.get("disarm_at", "07:00"))
        self.auto_arm_mode: AlarmState = AlarmState[sched.get("mode", "ARMED_HOME")]

        # Callbacks — dashboard registers here to receive events
        self._state_callbacks: list[Callable] = []
        self._triggered: bool = False
        self._entry_delay_task: Optional[asyncio.Task] = None
        self._siren_task: Optional[asyncio.Task] = None
        self._trigger_log: list[dict] = []

        # Camera manager reference — set after init to avoid circular deps
        self._camera_manager = None

    def set_camera_manager(self, cm):
        self._camera_manager = cm

    def add_state_callback(self, cb: Callable):
        """Dashboard registers this to get live state updates via SocketIO."""
        self._state_callbacks.append(cb)

    def _emit(self, event: str, data: dict):
        for cb in self._state_callbacks:
            try:
                cb(event, data)
            except Exception as e:
                log.debug("Alarm state callback error: %s", e)

    # ── Arming / Disarming ────────────────────────────────────────────────────

    def arm(self, mode: AlarmState, source: str = "manual"):
        if self.state == mode:
            return
        prev = self.state
        self.state = mode
        self._triggered = False
        log.info("Alarm ARMED [%s] by %s", mode.value, source)
        self._emit("alarm_state", {
            "state": self.state.value,
            "triggered": False,
            "timestamp": datetime.now().isoformat(),
            "source": source,
        })
        self._boost_cameras(True)

    def disarm(self, pin: Optional[str] = None, source: str = "manual") -> bool:
        """Returns True if disarm succeeded (correct PIN or no PIN required)."""
        if pin is not None and pin != self.pin:
            log.warning("Disarm attempt with wrong PIN from %s", source)
            self._emit("alarm_wrong_pin", {"timestamp": datetime.now().isoformat()})
            return False

        if self._entry_delay_task:
            self._entry_delay_task.cancel()
            self._entry_delay_task = None
        if self._siren_task:
            self._siren_task.cancel()
            self._siren_task = None

        prev = self.state
        self.state = AlarmState.DISARMED
        self._triggered = False
        log.info("Alarm DISARMED by %s", source)
        self._emit("alarm_state", {
            "state": self.state.value,
            "triggered": False,
            "timestamp": datetime.now().isoformat(),
            "source": source,
        })
        self._boost_cameras(False)
        return True

    # ── Triggering ────────────────────────────────────────────────────────────

    def trigger(self, source: TriggerSource, detail: str, loop: asyncio.AbstractEventLoop):
        """Called by sensors or cameras when armed. Starts entry delay or fires immediately."""
        if self.state == AlarmState.DISARMED:
            return
        if self._triggered:
            return  # already triggered

        entry = self.entry_delay_seconds if self.state == AlarmState.ARMED_HOME else 0

        log.warning("Alarm TRIGGERED by %s: %s (entry delay=%ds)", source.value, detail, entry)

        self._trigger_log.append({
            "time": datetime.now().isoformat(),
            "source": source.value,
            "detail": detail,
        })

        self._emit("alarm_triggered", {
            "source": source.value,
            "detail": detail,
            "entry_delay": entry,
            "timestamp": datetime.now().isoformat(),
        })

        if entry > 0:
            # Emit countdown so dashboard can show it
            self._entry_delay_task = asyncio.run_coroutine_threadsafe(
                self._entry_delay(entry, source, detail), loop
            )
        else:
            asyncio.run_coroutine_threadsafe(
                self._fire_alarm(source, detail), loop
            )

    async def _entry_delay(self, delay: int, source: TriggerSource, detail: str):
        """Countdown before alarm fires — gives time to disarm."""
        try:
            log.info("Entry delay: %ds to disarm before alarm fires", delay)
            for remaining in range(delay, 0, -1):
                if self.state == AlarmState.DISARMED:
                    return
                self._emit("alarm_countdown", {"remaining": remaining})
                await asyncio.sleep(1)
            await self._fire_alarm(source, detail)
        except asyncio.CancelledError:
            log.info("Entry delay cancelled (disarmed in time)")

    async def _fire_alarm(self, source: TriggerSource, detail: str):
        """Fires the siren and sends urgent notification."""
        if self.state == AlarmState.DISARMED:
            return

        self._triggered = True
        log.warning("🚨 ALARM FIRING: %s — %s", source.value, detail)

        self._emit("alarm_siren", {
            "active": True,
            "detail": detail,
            "timestamp": datetime.now().isoformat(),
        })

        await self.notifier.send(
            f"🚨 ALARM: {detail}",
            priority="HIGH",
            title="⚠️ SECURITY ALERT",
        )

        # Keep siren active for configured duration
        self._siren_task = asyncio.create_task(self._siren_timeout())

    async def _siren_timeout(self):
        try:
            await asyncio.sleep(self.siren_duration_seconds)
            if self._triggered:
                self._emit("alarm_siren", {"active": False, "reason": "timeout"})
                log.info("Siren auto-stopped after %ds", self.siren_duration_seconds)
        except asyncio.CancelledError:
            self._emit("alarm_siren", {"active": False, "reason": "disarmed"})

    # ── Camera sensitivity boost ──────────────────────────────────────────────

    def _boost_cameras(self, boost: bool):
        if not self._camera_manager:
            return
        for state in self._camera_manager.states.values():
            for zone in state.zone_subtractors.values():
                if boost:
                    # Store original and override to very_high
                    if not hasattr(zone, "_original_min_area"):
                        zone._original_min_area = zone.min_area
                    zone.min_area = 200  # very_high preset
                    log.debug("Boosted sensitivity: %s / %s", state.name, zone.name)
                else:
                    # Restore original
                    if hasattr(zone, "_original_min_area"):
                        zone.min_area = zone._original_min_area
                        del zone._original_min_area

    # ── Auto-arm scheduler ────────────────────────────────────────────────────

    async def run(self, stop_event: asyncio.Event):
        if not self.auto_arm_enabled:
            log.info("Auto-arm schedule disabled")
            await stop_event.wait()
            return

        log.info(
            "Auto-arm schedule: arm=%s disarm=%s mode=%s",
            self.auto_arm_time, self.auto_disarm_time, self.auto_arm_mode.value
        )

        while not stop_event.is_set():
            now = datetime.now().time().replace(second=0, microsecond=0)

            if self.auto_arm_time and now == self.auto_arm_time:
                if self.state == AlarmState.DISARMED:
                    self.arm(self.auto_arm_mode, source="schedule")
                    await self.notifier.send(
                        f"🔒 Alarm armed automatically ({self.auto_arm_mode.value})",
                        priority="LOW",
                    )

            if self.auto_disarm_time and now == self.auto_disarm_time:
                if self.state != AlarmState.DISARMED:
                    self.disarm(source="schedule")
                    await self.notifier.send(
                        "🔓 Alarm disarmed automatically",
                        priority="LOW",
                    )

            await asyncio.sleep(30)  # check every 30s

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_time(s: str) -> Optional[dtime]:
        try:
            h, m = map(int, s.split(":"))
            return dtime(h, m)
        except Exception:
            return None

    @property
    def is_armed(self) -> bool:
        return self.state != AlarmState.DISARMED

    @property
    def status(self) -> dict:
        return {
            "state": self.state.value,
            "triggered": self._triggered,
            "trigger_log": self._trigger_log[-10:],
        }
