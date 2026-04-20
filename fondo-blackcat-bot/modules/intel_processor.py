"""Auto-processor for intel_memory items.

Runs every 30 min via scheduler. Picks unprocessed intel items and:
1. Parses them (entities, sentiment, tags) via Gemini 2.5 Flash (free)
2. Updates intel_memory row with parsed_summary + tags
3. Marks as processed_for_thesis=true

When /reporte or /tesis runs, the already-parsed intel feeds Sonnet
for the critical analysis (no re-parsing = token savings on expensive model).
"""
from __future__ import annotations

import json
import logging

from modules.llm_router import route_request, LLMError
from modules.intel_memory import get_unprocessed_intel, update_intel_item, mark_as_processed

log = logging.getLogger(__name__)


INTEL_PARSE_PROMPT = """Procesá este mensaje de intel crypto/macro.

Output ESTRICTO en JSON (sin markdown, sin backticks, solo JSON puro):
{
  "summary": "resumen en 1-2 líneas en español",
  "entities": ["ticker1", "ticker2", "whale_name", "event"],
  "sentiment": "bullish|bearish|neutral",
  "relevance": "high|medium|low",
  "tags": ["macro", "hype", "alt_shorts", "btc", "war", "fed", "whale", "liquidation", "onchain"]
}

Solo incluí tags relevantes de la lista. NO agregues explicaciones fuera del JSON.
"""


async def process_pending_intel(limit: int = 50) -> int:
    """Process unprocessed intel items via Gemini free tier.

    Returns number of items successfully processed.
    """
    pending = get_unprocessed_intel(limit=limit)
    if not pending:
        return 0

    processed_count = 0
    processed_ids: list[int] = []

    for item in pending:
        try:
            raw_text = item.get("raw_text", "")
            if not raw_text or len(raw_text) < 10:
                processed_ids.append(item["id"])
                continue

            # Truncate very long items to save tokens
            if len(raw_text) > 2000:
                raw_text = raw_text[:2000] + "... [truncado]"

            json_str, provider = await route_request(
                task_name="intel_parse",
                system_prompt=INTEL_PARSE_PROMPT,
                user_message=f"SOURCE: {item.get('source', 'unknown')}\n\nMENSAJE:\n{raw_text}",
                max_tokens=500,
            )

            parsed = _parse_json_safely(json_str)
            if parsed:
                update_intel_item(
                    item_id=item["id"],
                    parsed_summary=parsed.get("summary"),
                    tags=parsed.get("tags", []),
                )
                processed_ids.append(item["id"])
                processed_count += 1
            else:
                # JSON parse failed — mark processed to avoid infinite retry
                processed_ids.append(item["id"])
                log.warning("Intel parse returned invalid JSON for item %d", item["id"])

        except LLMError:
            log.warning("LLM providers unavailable for intel processing, stopping batch")
            break
        except Exception:  # noqa: BLE001
            log.exception("Intel parse failed for item %d", item.get("id", "?"))
            continue

    if processed_ids:
        mark_as_processed(processed_ids)

    log.info("Intel processor: %d/%d items processed", processed_count, len(pending))
    return processed_count


def _parse_json_safely(text: str) -> dict | None:
    """Extract JSON from LLM response, handling markdown wrapping."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            return None
    return None
