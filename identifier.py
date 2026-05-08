"""
Uses local Ollama llava model to identify cards/collectibles from photos.
Ollama must be running: `ollama serve` and model pulled: `ollama pull llava`
"""
import base64
import json
import logging
import re
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llava"
TIMEOUT = 120
MAX_RETRIES = 3

IDENTIFICATION_PROMPT = """You are an expert collectibles identifier specializing in trading cards and collectibles.
Analyze this image and identify the item. Return ONLY valid JSON, no explanation, no markdown.

Extract these fields:
- card_name: The name of the card or item (e.g. "Charizard", "Michael Jordan", "Black Lotus", "Hot Wheels Ferrari")
- card_number: Card number if visible (e.g. "4/102", "PSA 10", or null)
- set_name: The set or series name (e.g. "Base Set", "1986 Fleer", "Alpha Edition", "Speed Machines")
- rarity: Rarity level (e.g. "Holo Rare", "Common", "Mythic Rare", "Rare", or null)
- card_type: ONE of: Pokemon, Sports, MTG, YuGiOh, HotWheels, Other
- condition: ONE of: Mint, Near Mint, Lightly Played, Moderately Played, Heavily Played, Poor
- brand: The manufacturer or publisher printed on the card (e.g. "Pokemon/Nintendo", "Topps", "Panini", "Upper Deck", "Bowman", "Fleer", "Donruss", "Score", "Leaf", "Wizards of the Coast", "Konami", or the actual brand name visible on the card). Use null if not identifiable.
- year: The 4-digit year the card was printed or issued. Search EVERY part of the image — front AND back — using these rules:
    * Sports cards: check the copyright line on the card back (e.g. "© 1986 Fleer" → "1986"), the set name if it contains a year (e.g. "1952 Topps" → "1952"), and any 4-digit year printed anywhere on front or back. Rookie year cards are extremely valuable so year accuracy matters.
    * Pokemon / Yu-Gi-Oh / other TCG: check the copyright line at the very bottom of the card back (e.g. "©1999 Nintendo" → "1999", "©2002 Wizards" → "2002").
    * MTG: check the fine-print copyright line at the bottom of the card (e.g. "© 1993 Wizards" → "1993").
    * Any card type: if a 4-digit number between 1900 and 2099 appears anywhere visible on the card, extract it as the year.
    * Return the year as a plain 4-digit string (e.g. "1986", "2023"). If no year is visible anywhere, return "unknown".
- description: Brief 1-sentence description for listing

If a field is unknown, use null (except year — use "unknown" for year). Always respond with valid JSON only.

Example response (sports card):
{"card_name":"Ken Griffey Jr.","card_number":"336","set_name":"Upper Deck","rarity":"Rookie Card","card_type":"Sports","condition":"Near Mint","brand":"Upper Deck","year":"1989","description":"1989 Upper Deck Ken Griffey Jr. rookie card #336, Near Mint condition."}

Example response (Pokemon):
{"card_name":"Charizard","card_number":"4/102","set_name":"Base Set","rarity":"Holo Rare","card_type":"Pokemon","condition":"Near Mint","brand":"Pokemon/Nintendo","year":"1999","description":"Charizard Holo Rare from the original Pokemon Base Set, 1999."}"""


def _encode_image(image_path: Path) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    return json.loads(text)


def _call_ollama(image_path: Path) -> dict:
    image_b64 = _encode_image(image_path)
    payload = {
        "model": MODEL,
        "prompt": IDENTIFICATION_PROMPT,
        "images": [image_b64],
        "stream": False,
        "options": {"temperature": 0.1},
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    raw = resp.json().get("response", "")
    logger.debug("Ollama raw response: %s", raw[:300])
    return _parse_json_response(raw)


def identify_card(image_path: str | Path) -> dict:
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    logger.info("Identifying %s", image_path.name)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = _call_ollama(image_path)
            result = _sanitize_result(result, image_path)
            logger.info(
                "Identified: %s | %s | %s",
                result.get("card_name"),
                result.get("card_type"),
                result.get("condition"),
            )
            return result
        except json.JSONDecodeError as e:
            logger.warning("Attempt %d: JSON parse error — %s", attempt, e)
        except requests.exceptions.ConnectionError:
            logger.error(
                "Cannot connect to Ollama at %s — is 'ollama serve' running?", OLLAMA_URL
            )
            raise
        except Exception as e:
            logger.warning("Attempt %d failed: %s", attempt, e)
        if attempt < MAX_RETRIES:
            time.sleep(2 * attempt)

    logger.error("All %d attempts failed for %s", MAX_RETRIES, image_path.name)
    return _fallback_result(image_path)


def _sanitize_result(result: dict, image_path: Path) -> dict:
    valid_types = {"Pokemon", "Sports", "MTG", "YuGiOh", "HotWheels", "Other"}
    valid_conditions = {
        "Mint", "Near Mint", "Lightly Played", "Moderately Played",
        "Heavily Played", "Poor"
    }
    if result.get("card_type") not in valid_types:
        result["card_type"] = "Other"
    if result.get("condition") not in valid_conditions:
        result["condition"] = "Near Mint"
    result["year"] = _sanitize_year(result.get("year"))
    result.setdefault("card_name", image_path.stem)
    result.setdefault("card_number", None)
    result.setdefault("set_name", None)
    result.setdefault("rarity", None)
    result.setdefault("brand", None)
    result.setdefault("description", "")
    result["filename"] = image_path.name
    result["image_path"] = str(image_path)
    return result


def _sanitize_year(raw) -> str:
    if raw is None:
        return "unknown"
    text = str(raw).strip()
    m = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    return m.group(1) if m else "unknown"


def _fallback_result(image_path: Path) -> dict:
    return {
        "filename": image_path.name,
        "image_path": str(image_path),
        "card_name": image_path.stem,
        "card_number": None,
        "set_name": None,
        "rarity": None,
        "brand": None,
        "year": "unknown",
        "card_type": "Other",
        "condition": "Near Mint",
        "description": "Could not auto-identify — please update manually.",
    }


def _join_unique(*parts) -> str | None:
    """Join non-empty parts with spaces, skipping exact duplicates (e.g. brand==set_name)."""
    seen: set[str] = set()
    tokens: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            tokens.append(p)
    return " ".join(tokens) or None


def build_title(card_info: dict) -> str:
    card_type = card_info.get("card_type", "Other")
    name      = (card_info.get("card_name") or "Card").strip()
    number    = card_info.get("card_number")
    set_name  = card_info.get("set_name")
    rarity    = card_info.get("rarity")
    condition = (card_info.get("condition") or "Near Mint").strip()
    brand     = card_info.get("brand")
    year      = card_info.get("year")
    year      = year if (year and year != "unknown") else None

    if card_type == "Sports":
        # "Player Name - Year Brand Set - Card Number - Rarity - Condition"
        year_brand_set = _join_unique(year, brand, set_name)
        parts = [name, year_brand_set, number, rarity, condition]

    elif card_type in ("Pokemon", "MTG", "YuGiOh"):
        # "Card Name - Number/Total - Year Set Name - Rarity - Condition"
        year_set = _join_unique(year, set_name)
        parts = [name, number, year_set, rarity, condition]

    elif card_type == "HotWheels":
        # "Name - Year Brand Series - Condition"
        year_brand_set = _join_unique(year, brand, set_name)
        parts = [name, year_brand_set, condition]

    else:
        # "Name - Year Brand Set - Number - Rarity - Condition"
        year_brand_set = _join_unique(year, brand, set_name)
        parts = [name, year_brand_set, number, rarity, condition]

    title = " - ".join(str(p) for p in parts if p)
    return title[:80]


def check_ollama_available() -> bool:
    try:
        resp = requests.get("http://localhost:11434/api/tags", timeout=5)
        models = [m["name"] for m in resp.json().get("models", [])]
        available = any("llava" in m for m in models)
        if not available:
            logger.warning("llava model not found. Run: ollama pull llava")
        return available
    except Exception:
        logger.error("Ollama not reachable. Run: ollama serve")
        return False
