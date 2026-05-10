"""
Uses local Ollama llava model to identify cards from photos.
Ollama must be running: `ollama serve` and model pulled: `ollama pull llava`

Naming convention for input files:
  Sports cards  → filename must contain _Front (e.g. Jordan_Front.jpg + Jordan_Back.jpg)
  TCG cards     → plain filename, no suffix (e.g. Charizard.jpg)
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
TIMEOUT = 300
MAX_RETRIES = 3

VALID_CARD_TYPES = {"Pokemon", "OnePiece", "Lorcana", "Sports", "MTG", "YuGiOh", "HotWheels", "Other"}
VALID_CONDITIONS = {"Mint", "Near Mint", "Lightly Played", "Moderately Played", "Heavily Played", "Poor"}

CARD_GAME_TO_TYPE = {
    "pokemon":               "Pokemon",
    "one piece":             "OnePiece",
    "onepiece":              "OnePiece",
    "lorcana":               "Lorcana",
    "disney lorcana":        "Lorcana",
    "mtg":                   "MTG",
    "magic":                 "MTG",
    "magic: the gathering":  "MTG",
    "magic the gathering":   "MTG",
    "yugioh":                "YuGiOh",
    "yu-gi-oh":              "YuGiOh",
    "yu gi oh":              "YuGiOh",
}

SPORTS_PROMPT = """\
This is the front and back of a sports card. Carefully examine both sides.

Extract the following and return ONLY valid JSON with exactly these keys — no markdown, no explanation:
{
  "player_name": "full player name as printed on card",
  "year": "4-digit year from copyright line on back (e.g. from '© 1986 Topps' extract '1986')",
  "brand": "manufacturer name (Topps / Panini / Donruss / Bowman / Upper Deck / Fleer / Score / Leaf / Prizm / etc.)",
  "card_number": "card number as printed on back (just the number, e.g. '57' or '123/200')",
  "sport": "Basketball / Football / Baseball / Hockey / Soccer / Other",
  "team": "team name as printed on card",
  "rookie_card": "yes or no — is RC or Rookie Card printed on the card?",
  "parallel_type": "parallel or insert name if any (e.g. 'Prizm', 'Refractor', 'Gold', null if base card)"
}

Example:
{"player_name":"Michael Jordan","year":"1986","brand":"Fleer","card_number":"57","sport":"Basketball","team":"Chicago Bulls","rookie_card":"yes","parallel_type":null}\
"""

TCG_PROMPT = """\
This is a trading card front. Carefully examine the card.

Extract the following and return ONLY valid JSON with exactly these keys — no markdown, no explanation:
{
  "card_name": "name of the card character or spell",
  "card_number": "card number exactly as printed (e.g. '4/102' for Pokemon or 'OP01-001' for One Piece)",
  "set_name": "full set or expansion name",
  "set_abbreviation": "short set code if visible (e.g. 'SVI', 'OP01', '1ED'), null if not visible",
  "rarity": "rarity as printed (Common / Uncommon / Rare / Holo Rare / Ultra Rare / Secret Rare / Leader / etc.)",
  "card_game": "Pokemon / One Piece / Lorcana / MTG / YuGiOh"
}

Example (Pokemon):
{"card_name":"Charizard","card_number":"4/102","set_name":"Base Set","set_abbreviation":"BS","rarity":"Holo Rare","card_game":"Pokemon"}

Example (One Piece):
{"card_name":"Monkey D. Luffy","card_number":"OP01-001","set_name":"Romance Dawn","set_abbreviation":"OP01","rarity":"Leader","card_game":"One Piece"}\
"""


def _encode_image(image_path: Path) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    return json.loads(text)


def _call_ollama(prompt: str, image_paths: list[Path]) -> dict:
    images_b64 = [_encode_image(p) for p in image_paths]
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "images": images_b64,
        "stream": False,
        "options": {"temperature": 0.1},
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    raw = resp.json().get("response", "")
    logger.debug("Ollama raw response: %s", raw[:300])
    return _parse_json_response(raw)


def identify_card(front_path: str | Path, back_path: str | Path | None = None) -> dict:
    front_path = Path(front_path)
    if not front_path.exists():
        raise FileNotFoundError(f"Image not found: {front_path}")

    if back_path:
        back_path = Path(back_path)
        if not back_path.exists():
            logger.warning("Back image not found, proceeding with front only: %s", back_path)
            back_path = None

    # Determine card category from filename convention
    is_sports = "_front" in front_path.stem.lower() or back_path is not None
    prompt = SPORTS_PROMPT if is_sports else TCG_PROMPT
    image_paths = [front_path] if not back_path else [front_path, back_path]
    card_label = "sports" if is_sports else "TCG"
    logger.info("Identifying %s (%s)", front_path.name, card_label)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw_result = _call_ollama(prompt, image_paths)
            if is_sports:
                result = _sanitize_sports_result(raw_result, front_path)
            else:
                result = _sanitize_tcg_result(raw_result, front_path)
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
            logger.error("Cannot connect to Ollama at %s — is 'ollama serve' running?", OLLAMA_URL)
            raise
        except Exception as e:
            logger.warning("Attempt %d failed: %s", attempt, e)
        if attempt < MAX_RETRIES:
            time.sleep(5 * attempt)

    logger.error("All %d attempts failed for %s", MAX_RETRIES, front_path.name)
    return _fallback_result(front_path, is_sports)


def _sanitize_sports_result(result: dict, front_path: Path) -> dict:
    # Normalize player_name → card_name
    if not result.get("card_name") and result.get("player_name"):
        result["card_name"] = result["player_name"]
    result.pop("player_name", None)

    result["card_type"] = "Sports"
    result["condition"] = "Near Mint"  # sports prompt doesn't assess condition

    result["year"] = _sanitize_year(result.get("year"))
    result.setdefault("card_name", front_path.stem)
    result.setdefault("card_number", None)
    result.setdefault("set_name", None)
    result.setdefault("rarity", None)
    result.setdefault("brand", None)
    result.setdefault("sport", None)
    result.setdefault("team", None)
    result.setdefault("rookie_card", "no")
    result.setdefault("parallel_type", None)
    result.setdefault("description", "")
    result["filename"] = front_path.name
    result["image_path"] = str(front_path)
    return result


def _sanitize_tcg_result(result: dict, front_path: Path) -> dict:
    # Map card_game string → internal card_type
    if "card_game" in result:
        game = (result.pop("card_game") or "").lower().strip()
        result["card_type"] = CARD_GAME_TO_TYPE.get(game, "Other")

    if result.get("card_type") not in VALID_CARD_TYPES:
        result["card_type"] = "Other"
    if result.get("condition") not in VALID_CONDITIONS:
        result["condition"] = "Near Mint"

    result["year"] = _sanitize_year(result.get("year"))
    result.setdefault("card_name", front_path.stem)
    result.setdefault("card_number", None)
    result.setdefault("set_name", None)
    result.setdefault("set_abbreviation", None)
    result.setdefault("rarity", None)
    result.setdefault("brand", None)
    result.setdefault("description", "")
    result["filename"] = front_path.name
    result["image_path"] = str(front_path)
    return result


def _sanitize_year(raw) -> str:
    if raw is None:
        return "unknown"
    text = str(raw).strip()
    m = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    return m.group(1) if m else "unknown"


def _fallback_result(front_path: Path, is_sports: bool = False) -> dict:
    base = {
        "filename":   front_path.name,
        "image_path": str(front_path),
        "card_name":  front_path.stem,
        "card_number": None,
        "set_name":   None,
        "rarity":     None,
        "brand":      None,
        "year":       "unknown",
        "condition":  "Near Mint",
        "description": "Could not auto-identify — please update manually.",
    }
    if is_sports:
        base.update({
            "card_type":    "Sports",
            "sport":        None,
            "team":         None,
            "rookie_card":  "no",
            "parallel_type": None,
        })
    else:
        base.update({
            "card_type":        "Other",
            "set_abbreviation": None,
        })
    return base


def _join_unique(*parts) -> str | None:
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
        year_brand = _join_unique(year, brand)
        rc = "RC" if (card_info.get("rookie_card") or "").lower() == "yes" else None
        parts = [name, year_brand, number, rc, condition]

    elif card_type == "Pokemon":
        year_set = _join_unique(year, set_name)
        parts = [name, number, year_set, rarity, condition]

    elif card_type == "OnePiece":
        # "Name - OP##-### - Rarity - Condition"
        parts = [name, number, rarity, condition]

    elif card_type == "Lorcana":
        # "Name - Number - Set - Rarity - Condition"
        parts = [name, number, set_name, rarity, condition]

    elif card_type in ("MTG", "YuGiOh"):
        year_set = _join_unique(year, set_name)
        parts = [name, number, year_set, rarity, condition]

    elif card_type == "HotWheels":
        year_brand_set = _join_unique(year, brand, set_name)
        parts = [name, year_brand_set, condition]

    else:
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
