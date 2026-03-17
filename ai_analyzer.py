import asyncio, base64, json, logging, re
from typing import Optional
import anthropic

log = logging.getLogger("ai")

SYSTEM_PROMPT = """You are a home security AI. Analyse the CCTV frame and respond with ONLY a JSON object — no markdown, no explanation outside the JSON.

Required fields:
{
  "subject": "human|animal|vehicle|object|empty",
  "description": "brief one-sentence description of what you see",
  "threat_level": "none|low|medium|high",
  "priority": "LOW|MEDIUM|HIGH",
  "unusual": true|false,
  "notes": "any relevant detail or empty string",
  "matched_entities": []
}

Priority rules:
- HIGH: unknown person acting suspiciously, forced entry attempt, weapon visible
- MEDIUM: unknown person loitering, unattended package, vehicle idling
- LOW: known resident, known vehicle, registered animal, normal activity
- Use context (time of day, zone name, known entities) provided in the user message."""

class AIAnalyzer:
    def __init__(self, config: dict, notifier, known_entities=None):
        self.config = config
        self.notifier = notifier
        self.known_entities = known_entities
        ai_cfg = config.get("ai", {})
        self.model = ai_cfg.get("model", "claude-opus-4-5")
        self.client = anthropic.AsyncAnthropic(api_key=ai_cfg["api_key"])

    async def analyze(self, frame_b64, camera_name, zone_name, known_faces, is_quiet_hours):
        try:
            context_parts = [
                f"Camera: {camera_name}",
                f"Zone: {zone_name}",
                f"Time: {'quiet hours (night)' if is_quiet_hours else 'daytime'}",
            ]
            if known_faces:
                context_parts.append(f"Face recognition: {', '.join(known_faces)}")
            else:
                context_parts.append("Face recognition: no known faces detected")
            if self.known_entities:
                entity_ctx = self.known_entities.get_context_string()
                if entity_ctx:
                    context_parts.append(entity_ctx)
            user_text = "\n".join(context_parts) + "\n\nAnalyse this security camera frame."
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame_b64}},
                    {"type": "text", "text": user_text},
                ]}],
            )
            raw = re.sub(r"```json|```", "", message.content[0].text.strip()).strip()
            result = json.loads(raw)
            log.info("AI [%s/%s]: %s — priority=%s", camera_name, zone_name,
                     result.get("description", "?"), result.get("priority", "?"))
            return result
        except json.JSONDecodeError as e:
            log.warning("AI returned invalid JSON: %s", e)
        except anthropic.APIError as e:
            log.error("Anthropic API error: %s", e)
        except Exception as e:
            log.error("AI analyzer error: %s", e)
        return None