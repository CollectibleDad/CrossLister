import sqlite3
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "crosslister.db"

VALID_CARD_TYPES = ("Pokemon", "Sports", "MTG", "YuGiOh", "HotWheels", "Other")
VALID_STATUSES = ("pending", "active", "sold", "deleted", "error")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                filename        TEXT NOT NULL,
                image_path      TEXT,
                title           TEXT,
                card_name       TEXT,
                card_number     TEXT,
                set_name        TEXT,
                rarity          TEXT,
                brand           TEXT,
                year            TEXT DEFAULT 'unknown',
                card_type       TEXT DEFAULT 'Other',
                condition       TEXT DEFAULT 'Near Mint',
                mercari_id      TEXT,
                depop_id        TEXT,
                ebay_id         TEXT,
                date_listed     TEXT,
                date_sold       TEXT,
                platform_sold   TEXT,
                asking_price    REAL,
                sold_price      REAL,
                status          TEXT DEFAULT 'pending',
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        for col in ("status", "mercari_id", "depop_id", "ebay_id"):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{col} ON items({col})")
        # Migrate existing databases that predate these columns
        for col, definition in (("brand", "TEXT"), ("year", "TEXT DEFAULT 'unknown'")):
            try:
                conn.execute(f"ALTER TABLE items ADD COLUMN {col} {definition}")
                logger.info("Migration: added column '%s'", col)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()
    logger.info("Database ready at %s", DB_PATH)


def insert_item(data: dict) -> int:
    data.setdefault("status", "pending")
    data.setdefault("date_listed", datetime.now().date().isoformat())
    with get_connection() as conn:
        cur = conn.execute("""
            INSERT INTO items
                (filename, image_path, title, card_name, card_number, set_name,
                 rarity, brand, year, card_type, condition, asking_price, status, date_listed)
            VALUES
                (:filename, :image_path, :title, :card_name, :card_number, :set_name,
                 :rarity, :brand, :year, :card_type, :condition, :asking_price, :status, :date_listed)
        """, data)
        conn.commit()
        logger.info("Inserted item id=%d  file=%s", cur.lastrowid, data.get("filename"))
        return cur.lastrowid


def update_item(item_id: int, updates: dict):
    updates = {k: v for k, v in updates.items() if k != "id"}
    updates["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["id"] = item_id
    with get_connection() as conn:
        conn.execute(f"UPDATE items SET {set_clause} WHERE id = :id", updates)
        conn.commit()


def mark_sold(item_id: int, platform: str, sold_price: float):
    update_item(item_id, {
        "status": "sold",
        "platform_sold": platform,
        "sold_price": sold_price,
        "date_sold": datetime.now().date().isoformat(),
    })
    logger.info("Marked item %d sold on %s for $%.2f", item_id, platform, sold_price)


def mark_deleted(item_id: int, platform: str):
    item = get_item_by_id(item_id)
    if not item:
        return
    updates = {f"{platform}_id": None}
    if not any([
        platform != "mercari" and item["mercari_id"],
        platform != "depop"   and item["depop_id"],
        platform != "ebay"    and item["ebay_id"],
    ]):
        updates["status"] = "deleted"
    update_item(item_id, updates)


def get_item_by_id(item_id: int):
    with get_connection() as conn:
        return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def get_active_items():
    with get_connection() as conn:
        return conn.execute("SELECT * FROM items WHERE status = 'active'").fetchall()


def get_pending_items():
    with get_connection() as conn:
        return conn.execute("SELECT * FROM items WHERE status = 'pending'").fetchall()


def get_items_by_status(status: str):
    with get_connection() as conn:
        return conn.execute("SELECT * FROM items WHERE status = ?", (status,)).fetchall()


def get_all_items():
    with get_connection() as conn:
        return conn.execute("SELECT * FROM items ORDER BY created_at DESC").fetchall()


def get_item_by_platform_id(platform: str, platform_id: str):
    col = f"{platform}_id"
    with get_connection() as conn:
        return conn.execute(f"SELECT * FROM items WHERE {col} = ?", (platform_id,)).fetchone()


def get_daily_stats(date_str: str = None) -> dict:
    date_str = date_str or datetime.now().date().isoformat()
    with get_connection() as conn:
        sold = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(sold_price),0) FROM items WHERE date_sold = ?",
            (date_str,)
        ).fetchone()
        listed = conn.execute(
            "SELECT COUNT(*) FROM items WHERE date_listed = ?", (date_str,)
        ).fetchone()
        active = conn.execute(
            "SELECT COUNT(*) FROM items WHERE status = 'active'"
        ).fetchone()
        by_type = conn.execute(
            "SELECT card_type, COUNT(*) FROM items WHERE status = 'active' GROUP BY card_type"
        ).fetchall()
    return {
        "date": date_str,
        "sold_count": sold[0],
        "revenue": sold[1],
        "listed_today": listed[0],
        "active_count": active[0],
        "by_type": {row[0]: row[1] for row in by_type},
    }
