"""
notifier.py
Sends push notifications via ntfy.sh and/or Pushover.
ntfy.sh is recommended — free, no account needed, great mobile app.
"""

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

PRIORITY_ICONS = {
    "low": "ℹ️",
    "medium": "⚠️",
    "high": "🚨",
    "critical": "🆘",
}


class Notifier:
    def __init__(self, config: dict):
        self._ntfy_cfg = config.get("notifications", {}).get("ntfy", {})
        self._pushover_cfg = config.get("notifications", {}).get("pushover", {})
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send_alert(self, alert: dict):
        """Dispatch alert to all enabled notification channels."""
        tasks = []

        if self._ntfy_cfg.get("enabled", False):
            tasks.append(self._send_ntfy(alert))

        if self._pushover_cfg.get("enabled", False):
            tasks.append(self._send_pushover(alert))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.error(f"Notification error: {r}")

    # ── ntfy.sh ───────────────────────────────────────────────────────────────

    async def _send_ntfy(self, alert: dict):
        server = self._ntfy_cfg.get("server", "https://ntfy.sh")
        topic = self._ntfy_cfg.get("topic", "home-monitor")
        priority = alert.get("priority", "low")
        ntfy_priority = self._ntfy_cfg.get("priority_map", {}).get(priority, 3)

        icon = PRIORITY_ICONS.get(priority, "📷")
        title = f"{icon} {alert.get('location', 'Camera')} — {priority.upper()} alert"
        body = alert.get("summary", "Motion detected")

        reasons = alert.get("reasons", [])
        if reasons:
            body += "\n• " + "\n• ".join(reasons)

        ts = alert.get("timestamp", "")[:19].replace("T", " ")
        body += f"\n\n🕐 {ts}"

        session = await self._get_session()
        url = f"{server}/{topic}"
        headers = {
            "Title": title,
            "Priority": str(ntfy_priority),
            "Tags": self._build_tags(alert),
        }

        try:
            async with session.post(url, data=body.encode("utf-8"), headers=headers) as resp:
                if resp.status == 200:
                    logger.debug(f"ntfy sent: {title}")
                else:
                    logger.warning(f"ntfy returned status {resp.status}")
        except aiohttp.ClientError as e:
            logger.error(f"ntfy error: {e}")

    def _build_tags(self, alert: dict) -> str:
        tags = ["house", "security"]
        subject = alert.get("subject_type", "")
        if subject == "human":
            tags.append("bust_in_silhouette")
        elif subject == "animal":
            tags.append("dog")
        if alert.get("is_quiet_hours"):
            tags.append("night_with_stars")
        if alert.get("priority") == "high":
            tags.append("rotating_light")
        return ",".join(tags)

    # ── Pushover ──────────────────────────────────────────────────────────────

    async def _send_pushover(self, alert: dict):
        token = self._pushover_cfg.get("api_token", "")
        user_key = self._pushover_cfg.get("user_key", "")
        if not token or not user_key:
            return

        priority_map = {"low": -1, "medium": 0, "high": 1, "critical": 2}
        p = priority_map.get(alert.get("priority", "low"), 0)

        title = f"[{alert.get('priority', 'low').upper()}] {alert.get('location', 'Camera')}"
        message = alert.get("summary", "Motion detected")
        reasons = alert.get("reasons", [])
        if reasons:
            message += "\n• " + "\n• ".join(reasons)

        payload = {
            "token": token,
            "user": user_key,
            "title": title,
            "message": message,
            "priority": p,
            "sound": "siren" if p >= 1 else "pushover",
        }
        # Pushover requires retry+expire for emergency priority
        if p == 2:
            payload["retry"] = 30
            payload["expire"] = 300

        session = await self._get_session()
        try:
            async with session.post(
                "https://api.pushover.net/1/messages.json", data=payload
            ) as resp:
                if resp.status == 200:
                    logger.debug(f"Pushover sent: {title}")
                else:
                    text = await resp.text()
                    logger.warning(f"Pushover status {resp.status}: {text[:100]}")
        except aiohttp.ClientError as e:
            logger.error(f"Pushover error: {e}")
