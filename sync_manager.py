"""
When an item sells on one platform, automatically deletes it from all other platforms.
Prevents double-selling and keeps inventory in sync.
"""
import logging

import database as db

logger = logging.getLogger(__name__)


def sync_sold_items(sold_items: list[dict]) -> dict:
    """
    Takes list of sold items (from sales_checker) and deletes them from other platforms.
    Returns stats on what was deleted.
    """
    stats = {"processed": 0, "deleted_mercari": 0, "deleted_depop": 0, "deleted_ebay": 0, "errors": []}

    for sold in sold_items:
        item_id = sold["id"]
        sold_platform = sold["platform"]
        item = db.get_item_by_id(item_id)
        if not item:
            continue

        stats["processed"] += 1
        title = item["title"] or f"Item #{item_id}"
        logger.info("Syncing sold item: %s (sold on %s)", title, sold_platform)

        if sold_platform != "mercari" and item["mercari_id"]:
            success = _delete_from_mercari(item["mercari_id"])
            if success:
                db.update_item(item_id, {"mercari_id": None})
                stats["deleted_mercari"] += 1
            else:
                stats["errors"].append(f"item {item_id} mercari delete failed")

        if sold_platform != "depop" and item["depop_id"]:
            success = _delete_from_depop(item["depop_id"])
            if success:
                db.update_item(item_id, {"depop_id": None})
                stats["deleted_depop"] += 1
            else:
                stats["errors"].append(f"item {item_id} depop delete failed")

        if sold_platform != "ebay" and item["ebay_id"]:
            success = _delete_from_ebay(item["ebay_id"])
            if success:
                db.update_item(item_id, {"ebay_id": None})
                stats["deleted_ebay"] += 1
            else:
                stats["errors"].append(f"item {item_id} ebay delete failed")

    logger.info(
        "Sync complete: %d items processed, mercari=%d depop=%d ebay=%d errors=%d",
        stats["processed"],
        stats["deleted_mercari"],
        stats["deleted_depop"],
        stats["deleted_ebay"],
        len(stats["errors"]),
    )
    return stats


def _delete_from_mercari(mercari_id: str) -> bool:
    from mercari import delete_mercari_listing
    try:
        result = delete_mercari_listing(mercari_id)
        if result:
            logger.info("Removed from Mercari: %s", mercari_id)
        return result
    except Exception as e:
        logger.error("Mercari delete error for %s: %s", mercari_id, e)
        return False


def _delete_from_depop(depop_id: str) -> bool:
    from depop import delete_depop_listing
    try:
        result = delete_depop_listing(depop_id)
        if result:
            logger.info("Removed from Depop: %s", depop_id)
        return result
    except Exception as e:
        logger.error("Depop delete error for %s: %s", depop_id, e)
        return False


def _delete_from_ebay(ebay_id: str) -> bool:
    from ebay import delete_ebay_listing
    try:
        result = delete_ebay_listing(ebay_id)
        if result:
            logger.info("Removed from eBay: %s", ebay_id)
        return result
    except Exception as e:
        logger.error("eBay delete error for %s: %s", ebay_id, e)
        return False


def sync_all_active() -> dict:
    """Full sync pass: check every active item and ensure consistency."""
    active = db.get_active_items()
    stats = {"checked": 0, "already_sold_elsewhere": []}

    for item in active:
        stats["checked"] += 1
        item_id = item["id"]
        fresh = db.get_item_by_id(item_id)
        if fresh and fresh["status"] == "sold":
            sold_info = {"id": item_id, "platform": fresh["platform_sold"]}
            stats["already_sold_elsewhere"].append(sold_info)
            sync_sold_items([sold_info])

    logger.info("Full sync pass complete. Checked %d items.", stats["checked"])
    return stats
