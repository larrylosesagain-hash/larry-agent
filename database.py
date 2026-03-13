"""
database.py — Larry's SQLite database
All data lives here: bets, tweets, bankroll history, expenses
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "larry.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create all tables if they don't exist."""
    conn = get_connection()
    c = conn.cursor()

    # ─── BETS ────────────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            polymarket_id   TEXT UNIQUE,          -- market condition_id
            question        TEXT NOT NULL,
            outcome         TEXT NOT NULL,        -- "YES" or "NO"
            amount_usdc     REAL NOT NULL,
            odds_at_bet     REAL NOT NULL,         -- e.g. 0.65 = 65¢
            potential_payout REAL NOT NULL,
            status          TEXT DEFAULT 'PENDING', -- PENDING / WON / LOST / CANCELLED
            result_amount   REAL DEFAULT 0,        -- actual payout received
            category        TEXT,                  -- crypto / politics / sports / tech / weird
            larry_comment   TEXT,                  -- what Larry said when placing bet
            placed_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at     TIMESTAMP,
            tweet_id        TEXT                   -- tweet announcing this bet
        )
    """)

    # ─── BANKROLL ────────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS bankroll_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            balance     REAL NOT NULL,
            change      REAL NOT NULL,             -- positive = deposit/win, negative = bet/loss
            reason      TEXT,                      -- "WIN", "LOSS", "DEPOSIT", "GRANDMA", "TAX_INCOME"
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ─── TWEETS ──────────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS tweets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tweet_id    TEXT UNIQUE,               -- Twitter's id
            content     TEXT NOT NULL,
            tweet_type  TEXT,                      -- NEW_BET / WIN / LOSS / FRIDAY / GRANDMA / ROLEX / RANDOM
            bet_id      INTEGER REFERENCES bets(id),
            posted_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ─── EXPENSES (grandma wallet tracking) ──────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT NOT NULL,             -- pizza / rent / courses / rolex / gucci / redbull
            amount      REAL NOT NULL,
            description TEXT,
            grandma_wallet_balance REAL,           -- balance after this entry
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tweet_id    TEXT
        )
    """)

    # ─── GRANDMA WALLET ──────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS grandma_wallet (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT NOT NULL,             -- DEPOSIT / INJECT / LUXURY_SAVE
            amount      REAL NOT NULL,
            balance_after REAL NOT NULL,
            note        TEXT,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ─── AGENT STATE ─────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS agent_state (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()
    print("✅ Database initialized at", DB_PATH)


# ─── BANKROLL HELPERS ─────────────────────────────────────────────────────────

def get_bankroll() -> float:
    """Current bankroll balance."""
    conn = get_connection()
    row = conn.execute(
        "SELECT value FROM agent_state WHERE key = 'bankroll'"
    ).fetchone()
    conn.close()
    if row:
        return float(row["value"])
    return 0.0


def set_bankroll(new_balance: float, change: float, reason: str):
    """Update bankroll and record history."""
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO agent_state (key, value) VALUES ('bankroll', ?)",
        (str(new_balance),)
    )
    conn.execute(
        "INSERT INTO bankroll_history (balance, change, reason) VALUES (?, ?, ?)",
        (new_balance, change, reason)
    )
    conn.commit()
    conn.close()


# ─── GRANDMA WALLET HELPERS ───────────────────────────────────────────────────

def get_grandma_balance() -> float:
    conn = get_connection()
    row = conn.execute(
        "SELECT value FROM agent_state WHERE key = 'grandma_balance'"
    ).fetchone()
    conn.close()
    return float(row["value"]) if row else 0.0


def update_grandma(event_type: str, amount: float, note: str = ""):
    balance = get_grandma_balance()
    if event_type in ("DEPOSIT", "LUXURY_SAVE"):
        new_balance = balance + amount
    else:  # INJECT
        new_balance = balance - amount

    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO agent_state (key, value) VALUES ('grandma_balance', ?)",
        (str(new_balance),)
    )
    conn.execute(
        "INSERT INTO grandma_wallet (event_type, amount, balance_after, note) VALUES (?, ?, ?, ?)",
        (event_type, amount, new_balance, note)
    )
    conn.commit()
    conn.close()
    return new_balance


# ─── BET HELPERS ─────────────────────────────────────────────────────────────

def save_bet(polymarket_id, question, outcome, amount, odds, potential_payout,
             category, larry_comment, tweet_id=None) -> int:
    conn = get_connection()
    cursor = conn.execute("""
        INSERT INTO bets
            (polymarket_id, question, outcome, amount_usdc, odds_at_bet,
             potential_payout, category, larry_comment, tweet_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (polymarket_id, question, outcome, amount, odds,
          potential_payout, category, larry_comment, tweet_id))
    bet_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return bet_id


def resolve_bet(polymarket_id: str, won: bool, payout: float):
    status = "WON" if won else "LOST"
    conn = get_connection()
    conn.execute("""
        UPDATE bets
        SET status = ?, result_amount = ?, resolved_at = CURRENT_TIMESTAMP
        WHERE polymarket_id = ?
    """, (status, payout, polymarket_id))
    conn.commit()
    conn.close()


def get_pending_bets() -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM bets WHERE status = 'PENDING' ORDER BY placed_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_bets(limit=10) -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM bets ORDER BY placed_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_win_streak() -> int:
    """Count consecutive wins from most recent resolved bets."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT status FROM bets
        WHERE status IN ('WON', 'LOST')
        ORDER BY resolved_at DESC
        LIMIT 20
    """).fetchall()
    conn.close()
    streak = 0
    for row in rows:
        if row["status"] == "WON":
            streak += 1
        else:
            break
    return streak


# ─── TWEET HELPERS ────────────────────────────────────────────────────────────

def save_tweet(tweet_id: str, content: str, tweet_type: str, bet_id=None):
    conn = get_connection()
    conn.execute("""
        INSERT OR IGNORE INTO tweets (tweet_id, content, tweet_type, bet_id)
        VALUES (?, ?, ?, ?)
    """, (tweet_id, content, tweet_type, bet_id))
    conn.commit()
    conn.close()


def get_last_tweet_time():
    conn = get_connection()
    row = conn.execute(
        "SELECT MAX(posted_at) as last FROM tweets"
    ).fetchone()
    conn.close()
    if row and row["last"]:
        return datetime.fromisoformat(row["last"])
    return None


def get_today_tweet_count() -> int:
    conn = get_connection()
    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM tweets
        WHERE DATE(posted_at) = DATE('now')
    """).fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ─── AGENT STATE ──────────────────────────────────────────────────────────────

def get_state(key: str, default=None):
    conn = get_connection()
    row = conn.execute(
        "SELECT value FROM agent_state WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_state(key: str, value: str):
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO agent_state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (key, str(value))
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    # Seed initial bankroll
    if get_bankroll() == 0:
        set_bankroll(100.0, 100.0, "INITIAL_DEPOSIT")
        print("💰 Bankroll seeded: $100.00")
    print("👵 Grandma wallet:", get_grandma_balance())
