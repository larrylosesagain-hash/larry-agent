"""
config.py — Larry's configuration
All secrets come from environment variables (.env file on server)
NEVER hardcode API keys here!
"""

import os
from dotenv import load_dotenv

load_dotenv()  # loads .env file from project root

# ─── ANTHROPIC ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = "claude-opus-4-5"  # upgrade to opus for better Larry personality

# ─── TWITTER / X ─────────────────────────────────────────────────────────────
TWITTER_API_KEY        = os.environ["TWITTER_API_KEY"]
TWITTER_API_SECRET     = os.environ["TWITTER_API_SECRET"]
TWITTER_ACCESS_TOKEN   = os.environ["TWITTER_ACCESS_TOKEN"]
TWITTER_ACCESS_SECRET  = os.environ["TWITTER_ACCESS_SECRET"]
TWITTER_BEARER_TOKEN   = os.environ["TWITTER_BEARER_TOKEN"]

# ─── POLYMARKET ───────────────────────────────────────────────────────────────
POLYMARKET_API_KEY     = os.environ.get("POLYMARKET_API_KEY", "")  # optional
POLYMARKET_PRIVATE_KEY = os.environ["POLYMARKET_PRIVATE_KEY"]      # wallet private key (from magic.link)
POLYMARKET_FUNDER      = os.environ["POLYMARKET_FUNDER"]            # wallet address (0x...)

# ─── DATABASE ────────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "/home/larry/larry.db")

# ─── LARRY'S LIMITS (HARDCODED — do NOT move to prompt) ──────────────────────
MIN_BET_USDC      = 1.0    # minimum bet in USDC
MAX_BET_USDC      = 25.0   # HARD CAP per bet — no matter what Claude says
MAX_BANKROLL_BET  = 0.15   # max 15% of bankroll per single bet
MIN_ODDS          = 0.10   # never bet on >90% favorites (boring)
MAX_ODDS          = 0.90   # never bet on <10% longshots (too risky)

# ─── BANKROLL RULES ───────────────────────────────────────────────────────────
GRANDMA_INJECT_THRESHOLD = 50.0    # inject when bankroll drops below $50
GRANDMA_INJECT_AMOUNT    = 200.0   # inject this much from grandma wallet
SURVIVAL_MODE_THRESHOLD  = 80.0    # survival mode below $80
WINNING_STREAK_THRESHOLD = 500.0   # winning streak mode above $500

# ─── TWITTER SCHEDULE ─────────────────────────────────────────────────────────
MIN_TWEETS_PER_DAY   = 3
MAX_TWEETS_PER_DAY   = 8
MIN_MINUTES_BETWEEN_TWEETS = 45   # don't spam
DEAD_MAN_SWITCH_HOURS = 48        # tweet if silent for 48h

# ─── BETTING SCHEDULE ─────────────────────────────────────────────────────────
BET_CHECK_INTERVAL_MINUTES = 30   # check for new markets every 30 min
MAX_OPEN_BETS = 5                 # max simultaneous open bets
BET_CATEGORY_MIX = {
    "crypto":    0.35,
    "politics":  0.25,
    "sports":    0.20,
    "tech":      0.15,
    "weird":     0.05,
}

# ─── LARRY'S EXPENSES (for grandma wallet accumulation) ───────────────────────
WEEKLY_PIZZA_COST   = 12.99  # Domino's + Mountain Dew Code Red, every Friday
MONTHLY_RENT        = 847.0
MONTHLY_TA_COURSE   = 97.0
MONTHLY_REDBULL     = 45.0   # estimated monthly Red Bull budget

# Luxury thresholds (Larry buys these after big wins)
ROLEX_THRESHOLD     = 3000.0  # bankroll needs to hit $3000 for Rolex talk
GUCCI_THRESHOLD     = 2000.0  # $2000 for Gucci talk

# ─── LARRY TWITTER HANDLE ────────────────────────────────────────────────────
LARRY_TWITTER_HANDLE = "@LarryLosesAgain"
LARRY_WEBSITE_URL    = "https://larrylosesmoney.com"  # update when live

# ─── POLYMARKET ENDPOINTS ────────────────────────────────────────────────────
POLYMARKET_HOST = "https://clob.polymarket.com"
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
