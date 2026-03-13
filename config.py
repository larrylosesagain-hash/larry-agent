"""
config.py — Larry's configuration
All secrets from environment variables — NEVER hardcode here!
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── ANTHROPIC ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = "claude-sonnet-4-6"

# ─── TWITTER / X ─────────────────────────────────────────────────────────────
TWITTER_API_KEY       = os.environ["TWITTER_API_KEY"]
TWITTER_API_SECRET    = os.environ["TWITTER_API_SECRET"]
TWITTER_ACCESS_TOKEN  = os.environ["TWITTER_ACCESS_TOKEN"]
TWITTER_ACCESS_SECRET = os.environ["TWITTER_ACCESS_SECRET"]
TWITTER_BEARER_TOKEN  = os.environ["TWITTER_BEARER_TOKEN"]

# ─── POLYMARKET ───────────────────────────────────────────────────────────────
POLYMARKET_PRIVATE_KEY = os.environ["POLYMARKET_PRIVATE_KEY"]
POLYMARKET_FUNDER      = os.environ["POLYMARKET_FUNDER"]
POLYMARKET_HOST        = "https://clob.polymarket.com"
POLYMARKET_GAMMA_API   = "https://gamma-api.polymarket.com"

# ─── DATABASE ────────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "/app/larry.db")

# ─── BET SIZING — PERCENTAGE BASED (not fixed amounts) ───────────────────────
# Larry bets 1%-5% of his bankroll per bet
MIN_BET_PCT      = 0.01   # 1% of bankroll minimum
MAX_BET_PCT      = 0.05   # 5% of bankroll maximum
ABSOLUTE_MIN_BET = 1.0    # never bet less than $1 (dust protection)
ABSOLUTE_MAX_BET = 50.0   # hard cap regardless of bankroll size

# ─── BANKROLL RULES ───────────────────────────────────────────────────────────
GRANDMA_INJECT_THRESHOLD = 50.0
GRANDMA_INJECT_AMOUNT    = 200.0
SURVIVAL_MODE_THRESHOLD  = 80.0
WINNING_STREAK_THRESHOLD = 500.0

# ─── TWITTER SCHEDULE ─────────────────────────────────────────────────────────
MIN_TWEETS_PER_DAY         = 3
MAX_TWEETS_PER_DAY         = 8
MIN_MINUTES_BETWEEN_TWEETS = 45
DEAD_MAN_SWITCH_HOURS      = 48

# ─── BETTING SCHEDULE ─────────────────────────────────────────────────────────
BET_CHECK_INTERVAL_MINUTES = 30
MAX_OPEN_BETS = 5
BET_CATEGORY_MIX = {
    "crypto":   0.35,
    "politics": 0.25,
    "sports":   0.20,
    "tech":     0.15,
    "weird":    0.05,
}

# ─── LARRY'S EXPENSES ─────────────────────────────────────────────────────────
WEEKLY_PIZZA_COST  = 12.99
MONTHLY_RENT       = 847.0
MONTHLY_TA_COURSE  = 97.0
MONTHLY_REDBULL    = 45.0
ROLEX_THRESHOLD    = 3000.0
GUCCI_THRESHOLD    = 2000.0

# ─── LARRY IDENTITY ──────────────────────────────────────────────────────────
LARRY_TWITTER_HANDLE = "@LarryLosesAgain"
LARRY_WEBSITE_URL    = "https://larrylosesmoney.com"
