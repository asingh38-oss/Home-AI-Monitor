"""
storage_manager.py
Monitors disk usage of the recordings directory.
Notifies via dashboard + ntfy when storage is getting full.
Handles deletion requests from the dashboard (by age or amount).
"""

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("storage")


@dataclass
class RecordingFile:
    path: Path
    size_bytes: int
    created: datetime
    camera: str

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    @property
    def age_days(self) -> float:
        return (datetime.now() - self.created).total_seconds() / 86400


class StorageManager:
    def __init__(self, config: dict, notifier):
        self.config = config
        self.notifier = notifier
        rec_cfg = config.get("recording", {})
        storage_cfg = config.get("storage", {})

        self.recordings_dir = Path(rec_cfg.get("output_dir", "recordings"))
        self.warn_threshold_pct = storage_cfg.get("warn_at_percent", 80)
        self.critical_threshold_pct = storage_cfg.get("critical_at_percent", 90)
        self.check_interval_seconds = storage_cfg.get("check_interval_seconds", 300)  # every 5 min

        self._state_callbacks = []
        self._last_warn_level: Optional[str] = None  # "warn" or "critical"
        self._pending_deletion: Optional[dict] = None  # holds a pending delete job

    def add_state_callback(self, cb):
        self._state_callbacks.append(cb)

    def _emit(self, event: str, data: dict):
        for cb in self._state_callbacks:
            try:
                cb(event, data)
            except Exception as e:
                log.debug("Storage callback error: %s", e)

    # ── Disk stats ────────────────────────────────────────────────────────────

    def get_disk_stats(self) -> dict:
        """Returns disk usage stats for the partition holding recordings."""
        usage = shutil.disk_usage(self.recordings_dir if self.recordings_dir.exists() else Path.home())
        total_gb   = usage.total / (1024 ** 3)
        used_gb    = usage.used  / (1024 ** 3)
        free_gb    = usage.free  / (1024 ** 3)
        used_pct   = (usage.used / usage.total) * 100

        rec_size = self._recordings_size()

        return {
            "total_gb":    round(total_gb, 1),
            "used_gb":     round(used_gb, 1),
            "free_gb":     round(free_gb, 1),
            "used_pct":    round(used_pct, 1),
            "recordings_gb": round(rec_size / (1024 ** 3), 2),
            "recordings_mb": round(rec_size / (1024 ** 2), 1),
            "recording_count": len(self._list_recordings()),
        }

    def _recordings_size(self) -> int:
        if not self.recordings_dir.exists():
            return 0
        return sum(f.stat().st_size for f in self.recordings_dir.glob("*.mp4"))

    def _list_recordings(self) -> list[RecordingFile]:
        if not self.recordings_dir.exists():
            return []
        files = []
        for p in self.recordings_dir.glob("*.mp4"):
            try:
                stat = p.stat()
                # Parse camera name from filename: CameraName_YYYYMMDD_HHMMSS.mp4
                parts = p.stem.split("_")
                camera = parts[0].replace("-", " ") if parts else "Unknown"
                created = datetime.fromtimestamp(stat.st_ctime)
                files.append(RecordingFile(
                    path=p,
                    size_bytes=stat.st_size,
                    created=created,
                    camera=camera,
                ))
            except Exception:
                pass
        return sorted(files, key=lambda f: f.created)  # oldest first

    # ── Deletion ──────────────────────────────────────────────────────────────

    def preview_delete(self, older_than_days: Optional[float] = None,
                       free_up_gb: Optional[float] = None) -> dict:
        """
        Returns a preview of what would be deleted without actually deleting.
        Pass either older_than_days OR free_up_gb.
        """
        recordings = self._list_recordings()
        to_delete = []

        if older_than_days is not None:
            to_delete = [r for r in recordings if r.age_days >= older_than_days]

        elif free_up_gb is not None:
            target_bytes = free_up_gb * (1024 ** 3)
            accumulated = 0
            for r in recordings:  # oldest first
                if accumulated >= target_bytes:
                    break
                to_delete.append(r)
                accumulated += r.size_bytes

        total_size_mb = sum(r.size_mb for r in to_delete)
        return {
            "count": len(to_delete),
            "total_mb": round(total_size_mb, 1),
            "total_gb": round(total_size_mb / 1024, 2),
            "oldest": to_delete[0].created.isoformat() if to_delete else None,
            "newest": to_delete[-1].created.isoformat() if to_delete else None,
            "files": [str(r.path.name) for r in to_delete[:5]],  # first 5 as sample
            "older_than_days": older_than_days,
            "free_up_gb": free_up_gb,
        }

    def execute_delete(self, older_than_days: Optional[float] = None,
                       free_up_gb: Optional[float] = None) -> dict:
        """Actually deletes files. Returns summary."""
        recordings = self._list_recordings()
        to_delete = []

        if older_than_days is not None:
            to_delete = [r for r in recordings if r.age_days >= older_than_days]
        elif free_up_gb is not None:
            target_bytes = free_up_gb * (1024 ** 3)
            accumulated = 0
            for r in recordings:
                if accumulated >= target_bytes:
                    break
                to_delete.append(r)
                accumulated += r.size_bytes

        deleted = 0
        freed_bytes = 0
        errors = 0

        for r in to_delete:
            try:
                r.path.unlink()
                deleted += 1
                freed_bytes += r.size_bytes
                log.info("Deleted recording: %s (%.1f MB)", r.path.name, r.size_mb)
            except Exception as e:
                log.error("Failed to delete %s: %s", r.path.name, e)
                errors += 1

        freed_gb = freed_bytes / (1024 ** 3)
        log.info("Storage cleanup: deleted %d files, freed %.2f GB (%d errors)", deleted, freed_gb, errors)

        result = {
            "deleted": deleted,
            "freed_gb": round(freed_gb, 2),
            "freed_mb": round(freed_bytes / (1024 ** 2), 1),
            "errors": errors,
            "timestamp": datetime.now().isoformat(),
        }

        self._emit("storage_deleted", result)
        return result

    # ── Monitoring loop ───────────────────────────────────────────────────────

    async def run(self, stop_event: asyncio.Event):
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        log.info("Storage monitor started (checking every %ds)", self.check_interval_seconds)

        while not stop_event.is_set():
            try:
                await self._check_storage()
            except Exception as e:
                log.error("Storage check error: %s", e)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.check_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def _check_storage(self):
        stats = self.get_disk_stats()
        pct = stats["used_pct"]
        free_gb = stats["free_gb"]

        # Always emit current stats to dashboard
        self._emit("storage_stats", stats)

        if pct >= self.critical_threshold_pct:
            level = "critical"
        elif pct >= self.warn_threshold_pct:
            level = "warn"
        else:
            level = "ok"
            self._last_warn_level = None
            return

        # Only notify if level changed (avoid spamming)
        if level == self._last_warn_level:
            return

        self._last_warn_level = level
        log.warning("Storage %s: %.1f%% used, %.1f GB free", level.upper(), pct, free_gb)

        # Build suggested deletion options
        suggestions = self._build_suggestions(stats)

        # Push to dashboard (prompts the user)
        self._emit("storage_warning", {
            "level": level,
            "used_pct": pct,
            "free_gb": free_gb,
            "total_gb": stats["total_gb"],
            "recordings_gb": stats["recordings_gb"],
            "recording_count": stats["recording_count"],
            "suggestions": suggestions,
            "timestamp": datetime.now().isoformat(),
        })

        # Also send push notification
        emoji = "🚨" if level == "critical" else "⚠️"
        await self.notifier.send(
            f"{emoji} Storage {pct:.0f}% full — {free_gb:.1f} GB remaining. "
            f"Open dashboard to free up space.",
            priority="HIGH" if level == "critical" else "MEDIUM",
            title="Storage Warning",
        )

    def _build_suggestions(self, stats: dict) -> list[dict]:
        """Pre-calculate deletion options to show the user."""
        suggestions = []
        recordings = self._list_recordings()
        if not recordings:
            return suggestions

        oldest_days = recordings[0].age_days if recordings else 0

        options = [
            {"label": "Older than 7 days",  "older_than_days": 7},
            {"label": "Older than 14 days", "older_than_days": 14},
            {"label": "Older than 30 days", "older_than_days": 30},
            {"label": "Free up 10 GB",      "free_up_gb": 10},
            {"label": "Free up 50 GB",      "free_up_gb": 50},
            {"label": "Free up 100 GB",     "free_up_gb": 100},
        ]

        for opt in options:
            preview = self.preview_delete(
                older_than_days=opt.get("older_than_days"),
                free_up_gb=opt.get("free_up_gb"),
            )
            if preview["count"] > 0:
                opt["preview"] = preview
                suggestions.append(opt)

        return suggestions
