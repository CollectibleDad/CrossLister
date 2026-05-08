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
        new_images = []
    else:
        new_images = [
            f for f in INPUT_FOLDER.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS
        ]
        logger.info("Found %d image(s) to process", len(new_images))

    if new_images and not identifier.check_ollama_available():
        logger.error(
            "Ollama is not running or llava model not found. "
            "Run: ollama serve  then: ollama pull llava"
        )
        new_images = []

    identified_items = []
    for image_path in new_images:
        logger.info("Processing: %s", image_path.name)
        try:
            card_info = identifier.identify_card(image_path)
            card_info["title"] = identifier.build_title(card_info)
            identified_items.append((image_path, card_info))
            logger.info("  → %s [%s]", card_info["card_name"], card_info["card_type"])
        except Exception as e:
            logger.error("Failed to identify %s: %s", image_path.name, e)
            shutil.move(str(image_path), str(FAILED_DIR / image_path.name))

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
            listing_stats["attempted"] += 1

            any_success = False

            # Mercari
            try:
                mid = mercari.list_on_mercari(item_id)
                if mid:
                    listing_stats["mercari_ok"] += 1
                    any_success = True
                    logger.info("  Mercari OK: %s", mid)
            except Exception as e:
                logger.error("  Mercari FAILED for item %d: %s", item_id, e)

            time.sleep(2)

            # Depop
            try:
                did = depop.list_on_depop(item_id)
                if did:
                    listing_stats["depop_ok"] += 1
                    any_success = True
                    logger.info("  Depop OK: %s", did)
            except Exception as e:
                logger.error("  Depop FAILED for item %d: %s", item_id, e)

            time.sleep(2)

            # eBay
            try:
                eid = ebay.list_on_ebay(item_id)
                if eid:
                    listing_stats["ebay_ok"] += 1
                    any_success = True
                    logger.info("  eBay OK: %s", eid)
            except Exception as e:
                logger.error("  eBay FAILED for item %d: %s", item_id, e)

            if any_success:
                db.update_item(item_id, {"status": "active"})
                listing_stats["succeeded"].append(card_info["title"])
                dest = PROCESSED_DIR / image_path.name
                shutil.move(str(image_path), str(dest))
                logger.info("  Image archived to Processed/")
            else:
                listing_stats["failed"].append(card_info["title"])
                db.update_item(item_id, {"status": "error"})
                shutil.move(str(image_path), str(FAILED_DIR / image_path.name))

        except Exception as e:
            logger.error("Listing pipeline error for %s: %s", card_info.get("card_name"), e)
            if item_id:
                db.update_item(item_id, {"status": "error"})
            shutil.move(str(image_path), str(FAILED_DIR / image_path.name))

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
