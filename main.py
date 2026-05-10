"""
CrossLister — Master Orchestration Script
Runs the full pipeline:
  1. Check sales on Mercari/Depop/eBay
  2. Sync deletions when items sell
  3. Identify new card photos with Ollama llava
  4. Price new items via eBay completed listings
  5. List new items on all three platforms
  6. Generate daily report
"""
import logging
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import database as db
import identifier
import pricing
import mercari
import depop
import ebay
import sales_checker
import sync_manager
import report

# ── Configuration ──────────────────────────────────────────────────────────────
INPUT_FOLDER   = Path(r"C:\Users\mrozo\OneDrive\Desktop\CardsToList")
PROCESSED_DIR  = INPUT_FOLDER / "Processed"
FAILED_DIR     = INPUT_FOLDER / "Failed"
LOG_DIR        = Path(__file__).parent / "logs"
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}
# ───────────────────────────────────────────────────────────────────────────────

LOG_DIR.mkdir(exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
FAILED_DIR.mkdir(parents=True, exist_ok=True)

log_file = LOG_DIR / f"crosslister_{datetime.now():%Y%m%d}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")


def _pair_card_images(
    folder: Path, supported_exts: set
) -> list[tuple[Path, Path | None]]:
    """
    Scan folder and return (front, back_or_None) pairs.
      Sports cards  → filename contains _Front  →  paired with matching _Back file
      TCG cards     → no _Front suffix           →  standalone (back=None)
    Matching is case-insensitive on the base name with _Front/_Back stripped.
    """
    all_files = sorted(
        [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in supported_exts],
        key=lambda p: p.name.lower(),
    )

    fronts = [f for f in all_files if "_front" in f.stem.lower()]
    backs  = {
        re.sub(r"_back", "", f.stem, flags=re.IGNORECASE).lower(): f
        for f in all_files if "_back" in f.stem.lower()
    }
    standalone = [
        f for f in all_files
        if "_front" not in f.stem.lower() and "_back" not in f.stem.lower()
    ]

    pairs: list[tuple[Path, Path | None]] = []

    for front in fronts:
        base = re.sub(r"_front", "", front.stem, flags=re.IGNORECASE).lower()
        back = backs.get(base)
        pairs.append((front, back))
        if back:
            logger.info("Paired: %s  +  %s", front.name, back.name)
        else:
            logger.warning("Sports front has no matching _Back file: %s", front.name)

    for f in standalone:
        pairs.append((f, None))

    return pairs


def step_banner(step: int, title: str):
    logger.info("")
    logger.info("━━━ STEP %d: %s ━━━", step, title)


def run_pipeline():
    start = datetime.now()
    logger.info("════ CrossLister Starting %s ════", start.strftime("%Y-%m-%d %H:%M:%S"))

    db.init_db()

    # ── STEP 1: Check Sales ─────────────────────────────────────────────────
    step_banner(1, "Checking Sales")
    sold_results = {"sold": [], "errors": []}
    try:
        sold_results = sales_checker.check_all_sales()
        if sold_results["sold"]:
            logger.info(
                "%d items sold: %s",
                len(sold_results["sold"]),
                [f"{s['title'][:30]} @ ${s['price']:.2f}" for s in sold_results["sold"]],
            )
        else:
            logger.info("No new sales found.")
    except Exception as e:
        logger.error("Sales check failed: %s", e)

    # ── STEP 2: Sync Deletions ──────────────────────────────────────────────
    step_banner(2, "Syncing Cross-Platform Deletions")
    if sold_results["sold"]:
        try:
            sync_stats = sync_manager.sync_sold_items(sold_results["sold"])
            logger.info("Sync complete: %s", sync_stats)
        except Exception as e:
            logger.error("Sync failed: %s", e)
    else:
        logger.info("No sold items to sync.")

    # ── STEP 3: Identify New Cards ──────────────────────────────────────────
    step_banner(3, "Identifying New Cards")
    if not INPUT_FOLDER.exists():
        logger.warning("Input folder not found: %s", INPUT_FOLDER)
        card_pairs = []
    else:
        card_pairs = _pair_card_images(INPUT_FOLDER, SUPPORTED_EXTS)
        sports_count = sum(1 for _, b in card_pairs if b is not None)
        tcg_count    = sum(1 for _, b in card_pairs if b is None)
        logger.info(
            "Found %d item(s) to process: %d sports pair(s), %d TCG single(s)",
            len(card_pairs), sports_count, tcg_count,
        )

    if card_pairs and not identifier.check_ollama_available():
        logger.error(
            "Ollama is not running or llava model not found. "
            "Run: ollama serve  then: ollama pull llava"
        )
        card_pairs = []

    identified_items = []
    for front_path, back_path in card_pairs:
        label = f"{front_path.name} + {back_path.name}" if back_path else front_path.name
        logger.info("Processing: %s", label)
        try:
            card_info = identifier.identify_card(front_path, back_path)
            card_info["title"]       = identifier.build_title(card_info)
            card_info["front_photo"] = str(front_path)
            card_info["back_photo"]  = str(back_path) if back_path else None
            identified_items.append((front_path, card_info))
            logger.info("  → %s [%s]", card_info.get("card_name"), card_info.get("card_type"))
        except Exception as e:
            logger.error("Failed to identify %s: %s", front_path.name, e)
            shutil.move(str(front_path), str(FAILED_DIR / front_path.name))
            if back_path and back_path.exists():
                shutil.move(str(back_path), str(FAILED_DIR / back_path.name))

    # ── STEP 4: Price New Cards ─────────────────────────────────────────────
    step_banner(4, "Pricing New Cards")
    priced_items = []
    for image_path, card_info in identified_items:
        try:
            price_data = pricing.get_smart_price(card_info)
            card_info["asking_price"] = price_data["asking_price"]
            card_info["price_data"] = price_data
            priced_items.append((image_path, card_info))
            logger.info(
                "  %s → $%.2f (from %d data points)",
                card_info["card_name"],
                price_data["asking_price"],
                price_data["data_points"],
            )
        except Exception as e:
            logger.error("Pricing failed for %s: %s", card_info.get("card_name"), e)
            card_info["asking_price"] = pricing.FLOOR_PRICE
            priced_items.append((image_path, card_info))

    # ── STEP 5: List on All Platforms ───────────────────────────────────────
    step_banner(5, "Listing on Mercari / Depop / eBay")
    listing_stats = {
        "attempted": 0, "mercari_ok": 0, "depop_ok": 0, "ebay_ok": 0,
        "failed": [], "succeeded": []
    }

    for image_path, card_info in priced_items:
        item_id = None
        try:
            item_id = db.insert_item({
                "filename":     card_info["filename"],
                "image_path":   str(image_path),
                "front_photo":  card_info.get("front_photo"),
                "back_photo":   card_info.get("back_photo"),
                "title":        card_info["title"],
                "card_name":    card_info.get("card_name"),
                "card_number":  card_info.get("card_number"),
                "set_name":     card_info.get("set_name"),
                "rarity":       card_info.get("rarity"),
                "brand":        card_info.get("brand"),
                "year":         card_info.get("year", "unknown"),
                "card_type":    card_info.get("card_type", "Other"),
                "condition":    card_info.get("condition", "Near Mint"),
                "asking_price": card_info["asking_price"],
                "status":       "pending",
            })
        except Exception as e:
            logger.error("DB insert failed for %s: %s", card_info.get("card_name"), e)
            shutil.move(str(image_path), str(FAILED_DIR / image_path.name))
            continue

        listing_stats["attempted"] += 1
        any_success = False

        # ── [1/3] Mercari ─────────────────────────────────────────────────
        logger.info("  [1/3] Starting Mercari — waiting for full completion...")
        mid = None
        try:
            mid = mercari.list_on_mercari(item_id)
        except Exception as e:
            logger.error("  Mercari raised an exception for item %d: %s", item_id, e)
        if mid:
            listing_stats["mercari_ok"] += 1
            any_success = True
            logger.info("  [1/3] Mercari complete — listing ID: %s", mid)
        else:
            logger.warning("  [1/3] Mercari finished without a confirmed listing ID")

        logger.info("  Waiting 5s before Depop...")
        time.sleep(5)

        # ── [2/3] Depop ───────────────────────────────────────────────────
        logger.info("  [2/3] Starting Depop — waiting for full completion...")
        did = None
        try:
            did = depop.list_on_depop(item_id)
        except Exception as e:
            logger.error("  Depop raised an exception for item %d: %s", item_id, e)
        if did:
            listing_stats["depop_ok"] += 1
            any_success = True
            logger.info("  [2/3] Depop complete — listing ID: %s", did)
        else:
            logger.warning("  [2/3] Depop finished without a confirmed listing ID")

        # ── [3/3] eBay — coming soon ──────────────────────────────────────
        # eBay listing is disabled until eBay automation is properly configured.
        # eid = None
        # try:
        #     logger.info("  Waiting 5s before eBay...")
        #     time.sleep(5)
        #     logger.info("  [3/3] Starting eBay — waiting for full completion...")
        #     eid = ebay.list_on_ebay(item_id)
        # except Exception as e:
        #     logger.error("  eBay raised an exception for item %d: %s", item_id, e)
        # if eid:
        #     listing_stats["ebay_ok"] += 1
        #     any_success = True
        #     logger.info("  [3/3] eBay complete — listing ID: %s", eid)
        # else:
        #     logger.warning("  [3/3] eBay finished without a confirmed listing ID")

        # ── Archive ───────────────────────────────────────────────────────
        back_photo = card_info.get("back_photo")
        if any_success:
            db.update_item(item_id, {"status": "active"})
            listing_stats["succeeded"].append(card_info["title"])
            shutil.move(str(image_path), str(PROCESSED_DIR / image_path.name))
            if back_photo and Path(back_photo).exists():
                shutil.move(back_photo, str(PROCESSED_DIR / Path(back_photo).name))
            logger.info("  Image(s) archived to Processed/")
        else:
            db.update_item(item_id, {"status": "error"})
            listing_stats["failed"].append(card_info["title"])
            shutil.move(str(image_path), str(FAILED_DIR / image_path.name))
            if back_photo and Path(back_photo).exists():
                shutil.move(back_photo, str(FAILED_DIR / Path(back_photo).name))
            logger.warning("  All platforms failed — image(s) moved to Failed/")

    # ── STEP 6: Daily Report ────────────────────────────────────────────────
    step_banner(6, "Generating Daily Report")
    try:
        report.generate_daily_report()
    except Exception as e:
        logger.error("Report generation failed: %s", e)

    # ── Summary ─────────────────────────────────────────────────────────────
    elapsed = (datetime.now() - start).total_seconds()
    logger.info("")
    logger.info("════ CrossLister Complete in %.0fs ════", elapsed)
    logger.info("  New cards processed:  %d", listing_stats["attempted"])
    logger.info("  Mercari listings:     %d", listing_stats["mercari_ok"])
    logger.info("  Depop listings:       %d", listing_stats["depop_ok"])
    logger.info("  eBay listings:        %d", listing_stats["ebay_ok"])
    logger.info("  Sales found today:    %d", len(sold_results.get("sold", [])))
    if listing_stats["failed"]:
        logger.warning("  Failed (moved to Failed/): %s", listing_stats["failed"])
    logger.info("")


if __name__ == "__main__":
    run_pipeline()
