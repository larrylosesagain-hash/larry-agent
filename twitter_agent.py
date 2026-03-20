"""
twitter_agent.py — Larry's Twitter/X presence
Runs on a loop every 15 minutes:
  1. Check if it's time to tweet (3-8x per day, min 45min gaps)
  2. Ask Claude for a tweet if needed
  3. Dead man's switch: auto-tweet if silent for 48h
  4. Friday pizza tweet scheduler
  5. Retweet whitelisted accounts (~2-3/day)
"""

import sys
import time
import json
import signal
import random
import logging
import requests
import tweepy
from datetime import datetime, timedelta, timezone
from config import (
    TWITTER_API_KEY, TWITTER_API_SECRET,
    TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET,
    TWITTER_BEARER_TOKEN,
    MIN_TWEETS_PER_DAY, MAX_TWEETS_PER_DAY,
    MIN_MINUTES_BETWEEN_TWEETS, DEAD_MAN_SWITCH_HOURS,
    LARRY_TWITTER_HANDLE, POLYMARKET_GAMMA_API,
    CLAUDE_MODEL,
)
from database import (
    save_tweet, get_last_tweet_time, get_today_tweet_count,
    get_bankroll, get_state, set_state, init_db, get_connection,
    get_pending_bets,
)
from larry_brain import ask_larry_for_tweet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TWITTER] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Return current UTC time as naive datetime. Replaces datetime.utcnow() (deprecated Python 3.12+)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── CONTENT SAFETY FILTER ───────────────────────────────────────────────────
# Two-layer filter: fast keyword blacklist — never engage with this content
_SCAM_KEYWORDS = [
    "airdrop", "presale", "whitelist", "mint now", "free nft", "send eth",
    "send bnb", "send usdt", "send sol", "dm for", "guaranteed profit",
    "100x", "1000x", "get rich", "passive income", "copy trade", "signal group",
    "pump incoming", "giveaway", "retweet to win", "follow to win",
    "click link in bio", "limited offer", "buy now before", "next 100x",
    "join our group", "free crypto", "earn daily",
]
_HARMFUL_KEYWORDS = [
    "kill yourself", "kys", "how to make bomb", "suicide method",
]

def _is_safe_to_engage(text: str) -> bool:
    """
    Fast safety check — returns False if content looks like scam or harmful.
    No Claude call needed — pure keyword matching.
    """
    text_lower = text.lower()
    for kw in _SCAM_KEYWORDS + _HARMFUL_KEYWORDS:
        if kw in text_lower:
            return False
    # Spam signals
    if text.count("#") > 4:       return False  # hashtag spam
    if text.count("@") > 3:       return False  # mention spam
    if text.lower().count("http") > 2: return False  # link spam
    return True

# Cached Larry's user ID — avoid get_me() every 15 minutes
_larry_user_id = None

# ─── ANTI-BURST TWEET THROTTLE ────────────────────────────────────────────────
# Prevents bot-like bursts (e.g. 5 tweets in 3 seconds when 5 bets fire at once).
# Shared by twitter_agent loop AND betting_agent (which imports post_tweet).
# Both run in separate threads inside the same process — so this global is shared.
_last_tweet_at: datetime | None = None
_TWEET_MIN_GAP_SECS = 65  # ≥1 minute between any two tweets Larry posts

# ─── TWITTER CLIENT SINGLETONS ────────────────────────────────────────────────
# v2 client — for posting tweets, replies, retweets
_twitter_client: tweepy.Client | None = None

def get_twitter_client() -> tweepy.Client:
    global _twitter_client
    if _twitter_client is None:
        _twitter_client = tweepy.Client(
            bearer_token=TWITTER_BEARER_TOKEN,
            consumer_key=TWITTER_API_KEY,
            consumer_secret=TWITTER_API_SECRET,
            access_token=TWITTER_ACCESS_TOKEN,
            access_token_secret=TWITTER_ACCESS_SECRET,
            wait_on_rate_limit=True,
        )
    return _twitter_client

# v1.1 API client — for media upload (v2 doesn't support media_upload)
_twitter_v1_api: tweepy.API | None = None

def get_twitter_v1_api() -> tweepy.API:
    """Return a tweepy v1.1 API client for media uploads."""
    global _twitter_v1_api
    if _twitter_v1_api is None:
        auth = tweepy.OAuth1UserHandler(
            consumer_key=TWITTER_API_KEY,
            consumer_secret=TWITTER_API_SECRET,
            access_token=TWITTER_ACCESS_TOKEN,
            access_token_secret=TWITTER_ACCESS_SECRET,
        )
        _twitter_v1_api = tweepy.API(auth, wait_on_rate_limit=True)
    return _twitter_v1_api


# ─── DAILY TWEET CAP ──────────────────────────────────────────────────────────
# Hard cap: Larry posts at most MAX_DAILY_ORIGINAL_TWEETS original tweets per day.
# Separate from config.MAX_TWEETS_PER_DAY which was used for organic spacing only.
MAX_DAILY_ORIGINAL_TWEETS = 15

def _is_daily_cap_reached() -> bool:
    """Return True if Larry has already hit MAX_DAILY_ORIGINAL_TWEETS today."""
    count = get_today_own_tweet_count()
    if count >= MAX_DAILY_ORIGINAL_TWEETS:
        log.info(f"🚫 Daily tweet cap reached ({count}/{MAX_DAILY_ORIGINAL_TWEETS}) — skipping")
        return True
    return False

# GM tweet interval — every 8h to catch different time zones
_GM_INTERVAL_SECS = 8 * 3600

# Path to Larry's GM image (stick figure PFP)
import os as _os
_LARRY_GM_IMAGE = _os.path.join(_os.path.dirname(__file__), "larry_gm.png")


# ─── POST TWEET ───────────────────────────────────────────────────────────────

def post_tweet(text: str, tweet_type: str = "RANDOM", bet_id: int = None) -> str:
    """Post an original tweet as Larry.

    Anti-burst: enforces _TWEET_MIN_GAP_SECS minimum gap between tweets.
    Daily cap: refuses to post if MAX_DAILY_ORIGINAL_TWEETS reached.
    """
    global _last_tweet_at

    # Hard daily cap — prevents runaway tweet storms
    if _is_daily_cap_reached():
        return ""

    # Anti-burst: sleep if we tweeted too recently
    if _last_tweet_at is not None:
        elapsed = (_utcnow() - _last_tweet_at).total_seconds()
        wait = _TWEET_MIN_GAP_SECS - elapsed
        if wait > 0:
            log.info(f"⏳ Tweet throttle: waiting {wait:.0f}s (anti-burst gap, last tweet {elapsed:.0f}s ago)")
            time.sleep(wait)

    if len(text) > 280:
        text = text[:277] + "..."

    try:
        client = get_twitter_client()
        response = client.create_tweet(text=text)
        tweet_id = str(response.data["id"])
        save_tweet(tweet_id=tweet_id, content=text, tweet_type=tweet_type, bet_id=bet_id)
        _last_tweet_at = _utcnow()  # update AFTER success
        log.info(f"✅ Tweeted [{tweet_type}]: {text[:80]}...")
        return tweet_id

    except tweepy.TweepyException as e:
        log.error(f"Twitter error: {e}")
        raise


def post_tweet_with_image(text: str, image_path: str, tweet_type: str = "GM", bet_id: int = None) -> str:
    """Post a tweet with an image attachment (e.g. GM tweets with Larry's PFP).

    Uses v1.1 API for media upload, then v2 API to create the tweet with media_ids.
    Falls back to text-only tweet if media upload fails.
    """
    global _last_tweet_at

    if _is_daily_cap_reached():
        return ""

    # Anti-burst gap
    if _last_tweet_at is not None:
        elapsed = (_utcnow() - _last_tweet_at).total_seconds()
        wait = _TWEET_MIN_GAP_SECS - elapsed
        if wait > 0:
            log.info(f"⏳ Tweet throttle: waiting {wait:.0f}s")
            time.sleep(wait)

    if len(text) > 280:
        text = text[:277] + "..."

    # Upload media via v1.1
    media_id = None
    try:
        v1_api = get_twitter_v1_api()
        media = v1_api.media_upload(filename=image_path)
        media_id = media.media_id
        log.info(f"📷 Media uploaded: id={media_id}")
    except Exception as e:
        log.warning(f"Media upload failed ({e}) — falling back to text-only tweet")

    try:
        client = get_twitter_client()
        kwargs = {"text": text}
        if media_id:
            kwargs["media_ids"] = [media_id]
        response = client.create_tweet(**kwargs)
        tweet_id = str(response.data["id"])
        save_tweet(tweet_id=tweet_id, content=text, tweet_type=tweet_type, bet_id=bet_id)
        _last_tweet_at = _utcnow()
        log.info(f"✅ Tweeted with image [{tweet_type}]: {text[:80]}...")
        return tweet_id

    except tweepy.TweepyException as e:
        log.error(f"Twitter error (image tweet): {e}")
        raise


def get_today_own_tweet_count() -> int:
    """Count original tweets posted today (excludes retweets)."""
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT COUNT(*) as cnt FROM tweets
            WHERE DATE(posted_at) = DATE('now')
              AND tweet_type != 'RETWEET'
        """).fetchone()
    finally:
        conn.close()
    return (row["cnt"] or 0) if row else 0


def _get_larry_id(client: tweepy.Client) -> int:
    """Get Larry's user ID, using a module-level cache to avoid repeated API calls."""
    global _larry_user_id
    if _larry_user_id is None:
        me = client.get_me()
        _larry_user_id = me.data.id
        log.info(f"Cached Larry's user ID: {_larry_user_id}")
    return _larry_user_id


# ─── TIMING LOGIC ────────────────────────────────────────────────────────────

def should_tweet_now() -> bool:
    """Decide if it's time for a new organic tweet.
    No daily cap — only constraint is MIN_MINUTES_BETWEEN_TWEETS gap.
    25% chance per 15-min cycle when gap has elapsed = ~4-6 tweets/day naturally.
    """
    now = _utcnow()
    last_tweet = get_last_tweet_time()

    if last_tweet:
        minutes_since = (now - last_tweet).total_seconds() / 60
        if minutes_since < MIN_MINUTES_BETWEEN_TWEETS:
            return False

    hour = now.hour
    if hour < 7 or hour >= 23:
        return False

    if random.random() < 0.25:
        log.info(f"Rolling to tweet (today: {get_today_own_tweet_count()})")
        return True

    return False


def is_friday_pizza_time() -> bool:
    now = _utcnow()
    return now.weekday() == 4 and 17 <= now.hour <= 19


def check_dead_man_switch() -> bool:
    last_tweet = get_last_tweet_time()
    if last_tweet is None:
        return False
    hours_silent = (_utcnow() - last_tweet).total_seconds() / 3600
    return hours_silent >= DEAD_MAN_SWITCH_HOURS


# ─── GM TWEET ────────────────────────────────────────────────────────────────

def maybe_tweet_gm() -> bool:
    """Post a GM tweet with Larry's stick-figure image every ~8 hours.

    Uses last_gm_tweet_time in state DB as throttle.
    No time-of-day restriction — Larry is active around the clock,
    and 'gm' hits different time zones at different local times.
    Returns True if a GM was posted.
    """
    if _is_daily_cap_reached():
        return False

    if not _os.path.exists(_LARRY_GM_IMAGE):
        log.warning(f"GM image not found at {_LARRY_GM_IMAGE} — skipping GM tweet")
        return False

    now_ts = time.time()
    last_gm_str = get_state("last_gm_tweet_time")
    last_gm_ts  = float(last_gm_str) if last_gm_str else 0.0

    if (now_ts - last_gm_ts) < _GM_INTERVAL_SECS:
        return False

    try:
        gm_data = ask_larry_for_tweet("GM")
        tweet_text = gm_data.get("tweet", "")
        if not tweet_text:
            return False
        tweet_id = post_tweet_with_image(tweet_text, _LARRY_GM_IMAGE, tweet_type="GM")
        if tweet_id:
            set_state("last_gm_tweet_time", str(now_ts))
            log.info(f"🌅 GM tweet posted with image: {tweet_text[:60]}...")
            return True
    except Exception as e:
        log.error(f"GM tweet failed: {e}")
    return False


# ─── FRIDAY PIZZA ────────────────────────────────────────────────────────────

# FIX: persist pizza flag in DB instead of module-level variable,
# which was lost on every process restart (causing duplicate Friday tweets)
def maybe_tweet_pizza():
    now = _utcnow()
    # Reset flag on Monday via DB
    if now.weekday() == 0:
        set_state("pizza_tweeted_this_week", "false")

    if is_friday_pizza_time() and get_state("pizza_tweeted_this_week") != "true":
        log.info("🍕 IT'S FRIDAY PIZZA TIME!")
        tweet_data = ask_larry_for_tweet("FRIDAY")
        post_tweet(tweet_data["tweet"], tweet_type="FRIDAY")
        set_state("pizza_tweeted_this_week", "true")


# ─── WEEKLY RECAP (Sunday) ───────────────────────────────────────────────────

def maybe_tweet_weekly_recap():
    """Every Sunday between 6-8pm UTC, Larry posts his weekly recap."""
    now = _utcnow()
    if now.weekday() != 6 or not (18 <= now.hour <= 20):
        return

    last_recap = get_state("last_weekly_recap_date")
    today_str = now.strftime("%Y-%m-%d")
    if last_recap == today_str:
        return  # already done this Sunday

    # Build weekly stats from database
    # FIX: use try/finally to guarantee connection is always closed
    from database import get_connection
    conn = get_connection()
    try:
        week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        row = conn.execute("""
            SELECT
                COUNT(*) as total_bets,
                SUM(CASE WHEN status='WON' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN status='LOST' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN status='WON' THEN result_amount ELSE 0 END) as total_won,
                SUM(CASE WHEN status='LOST' THEN amount_usdc ELSE 0 END) as total_lost
            FROM bets
            WHERE DATE(placed_at) >= ?
        """, (week_ago,)).fetchone()
    finally:
        conn.close()

    stats = {
        "total_bets": row["total_bets"] or 0,
        "wins": row["wins"] or 0,
        "losses": row["losses"] or 0,
        "total_won": round(row["total_won"] or 0, 2),
        "total_lost": round(row["total_lost"] or 0, 2),
        "net": round((row["total_won"] or 0) - (row["total_lost"] or 0), 2),
        "current_bankroll": round(get_bankroll(), 2),
    }

    log.info(f"📊 Posting weekly recap: {stats}")
    tweet_data = ask_larry_for_tweet("WEEKLY_RECAP", extra_data=stats)
    post_tweet(tweet_data["tweet"], tweet_type="WEEKLY_RECAP")
    set_state("last_weekly_recap_date", today_str)


# ─── MILESTONE TWEETS ────────────────────────────────────────────────────────

MILESTONES = [200, 500, 1000, 2000, 5000, 10000]

def check_milestones():
    """Tweet when Larry hits a bankroll milestone for the first time.
    Uses total net worth (free cash + in-play) — otherwise milestone never fires
    when most money is locked in open bets."""
    bankroll = get_bankroll()
    try:
        in_play = sum(float(b.get("amount_usdc", 0)) for b in get_pending_bets())
    except Exception:
        in_play = 0.0
    total = bankroll + in_play
    for milestone in MILESTONES:
        key = f"milestone_{milestone}_tweeted"
        if total >= milestone and get_state(key) != "true":
            log.info(f"🏆 MILESTONE HIT: ${milestone}!")
            tweet_data = ask_larry_for_tweet(
                "MILESTONE",
                extra_data={"milestone": f"${milestone} bankroll", "current": bankroll}
            )
            post_tweet(tweet_data["tweet"], tweet_type="MILESTONE")
            set_state(key, "true")
            break  # only one milestone per cycle


# ─── LIKES ───────────────────────────────────────────────────────────────────

def like_tweet(tweet_id: str):
    """Larry likes a tweet. Silent fail — likes are nice-to-have."""
    try:
        client = get_twitter_client()
        larry_id = _get_larry_id(client)
        client.like(larry_id, tweet_id)
        log.info(f"❤️ Liked tweet {tweet_id}")
    except Exception as e:
        log.debug(f"Like failed: {type(e).__name__}")


# ─── QUOTE TWEETS ─────────────────────────────────────────────────────────────

# Tweet IDs where quote tweet returned 403 — session-level, avoids retrying same tweet twice
_quote_blocked_ids: set = set()

# Cycle-level candidate cache — _find_quote_tweet_candidate() is called by 3 functions per cycle.
# Without caching: 3 Twitter search API calls per 15-min cycle = wasteful.
# With caching: 1 search call, result shared across maybe_quote_tweet / maybe_retweet / maybe_reply_to_whitelist.
# Cache expires after 14 minutes so next cycle gets a fresh candidate.
_candidate_cache: dict = {"candidate": None, "expires_at": datetime.min}

def _get_cycle_candidate() -> dict | None:
    """Return cached candidate or fetch a fresh one. Shared across all engagement functions."""
    global _candidate_cache
    now = _utcnow()
    if now < _candidate_cache["expires_at"]:
        return _candidate_cache["candidate"]
    candidate = _find_quote_tweet_candidate()
    _candidate_cache = {"candidate": candidate, "expires_at": now + timedelta(minutes=14)}
    return candidate

# ACCOUNT-LEVEL blacklist: maps username → UTC datetime until which we skip them for quote tweets
# Problem: Trump/Elon restrict quote tweets from accounts they haven't mentioned.
# Tweet-level blacklist doesn't help — next cycle finds a new tweet from the same account.
# This account-level blacklist + DB persistence solves it across restarts.
_quote_account_blacklist: dict = {}


def _init_quote_blacklist():
    """Load persisted account-level quote tweet blacklist from DB on startup."""
    global _quote_account_blacklist
    raw = get_state("quote_account_blacklist")
    if not raw:
        return
    try:
        data = json.loads(raw)
        now = _utcnow()
        # Load only entries that haven't expired yet
        _quote_account_blacklist = {
            k: datetime.fromisoformat(v)
            for k, v in data.items()
            if datetime.fromisoformat(v) > now
        }
        if _quote_account_blacklist:
            log.info(f"🚫 Loaded quote account blacklist: {list(_quote_account_blacklist.keys())}")
    except Exception:
        _quote_account_blacklist = {}

# Whitelist of accounts Larry can quote tweet — high engagement, relevant to betting/markets/politics
_QUOTE_ACCOUNTS = [
    # ── Prediction markets & forecasting ──
    "polymarket",       # Larry's home turf
    "Kalshi",           # competitor — prediction market context
    "NateSilver538",    # Larry thinks he's better than Nate Silver
    "AriDavidPaul",     # prediction markets OG, active

    # ── Crypto ──
    "elonmusk",         # Elon — Larry has opinions on everything
    "saylor",           # Bitcoin maximalist — Larry bets on BTC
    "cz_binance",       # crypto, massive following
    "APompliano",       # macro + crypto, open replies
    "WatcherGuru",      # crypto news, fast posts
    "coindesk",         # crypto news
    "CryptoBanter",     # crypto commentary

    # ── Macro / markets ──
    "realDonaldTrump",  # Trump — Larry bets on politics
    "unusual_whales",   # tracks market activity
    "KobeissiLetter",   # macro commentary
    "balajis",          # loves predictions and bets

    # ── Sports (NBA, NFL, general) ── Larry bets on sports constantly
    "NBA",              # official NBA — huge reach, open replies
    "wojespn",          # Woj — breaks NBA news, everyone piles on
    "ShamsCharania",    # The Athletic NBA reporter
    "ESPNStatsInfo",    # stats tweets — perfect for Larry to "analyze"
    "BleacherReport",   # sports highlights, massive audience
    "TheAthletic",      # in-depth sports coverage
    "DraftKings",       # sports betting brand — same audience as Larry
    "FanDuel",          # sports betting — same audience
    "ActionNetworkHQ",  # sports betting analytics — very on-brand
    "espn",             # ESPN — huge reach
]

def _search_tweets_from_accounts(account_list: list, sort_by_recency: bool = False) -> dict | None:
    """
    Core search: find a tweet from the given account list.

    sort_by_recency=False (default): returns highest-engagement tweet — good for quote tweets/retweets.
    sort_by_recency=True: returns the FRESHEST valid tweet — good for proactive replies
                          so Larry comments on something that just happened, not yesterday's news.
    """
    if not account_list:
        return None
    try:
        client = get_twitter_client()
        # For recency search: include ALL whitelist accounts to find freshest across all of them.
        # For engagement search: sample 5 to stay within query length limits.
        accounts = account_list if sort_by_recency else random.sample(account_list, min(5, len(account_list)))
        from_query = " OR ".join(f"from:{a}" for a in accounts)
        query = f"({from_query}) -is:retweet -is:reply lang:en"

        api_kwargs = dict(
            query=query,
            max_results=20,
            tweet_fields=["author_id", "text", "public_metrics", "reply_settings", "created_at"],
            expansions=["author_id"],
            user_fields=["username", "public_metrics"],
        )
        if sort_by_recency:
            api_kwargs["sort_order"] = "recency"  # Twitter returns newest first

        response = client.search_recent_tweets(**api_kwargs)
        if not response.data:
            return None

        users = {}
        if response.includes and "users" in response.includes:
            for u in response.includes["users"]:
                followers = (u.public_metrics or {}).get("followers_count", 0)
                users[u.id] = {"username": u.username, "followers": followers}

        candidates = []
        for tweet in response.data:
            text = tweet.text
            if str(tweet.id) in _quote_blocked_ids:
                continue
            if not _is_safe_to_engage(text):
                continue
            # Skip tweets where replies are restricted (kept for quote-tweet safety check).
            reply_settings = getattr(tweet, "reply_settings", None)
            if reply_settings and reply_settings != "everyone":
                _quote_blocked_ids.add(str(tweet.id))
                continue
            user = users.get(tweet.author_id, {})
            metrics = tweet.public_metrics or {}
            score = (
                metrics.get("like_count", 0) * 2 +
                metrics.get("retweet_count", 0) * 3 +
                min(user.get("followers", 0), 500000) / 50000
            )
            # Tweet ID is a snowflake — higher ID = more recent.
            candidates.append({
                "tweet_id": str(tweet.id),
                "text": text,
                "username": user.get("username", ""),
                "score": score,
                "created_at": getattr(tweet, "created_at", None),
            })

        if not candidates:
            return None

        if sort_by_recency:
            # Sort by tweet ID descending — snowflake IDs are monotonically increasing.
            # Twitter already returns recency order but explicit sort is safer.
            return sorted(candidates, key=lambda x: int(x["tweet_id"]), reverse=True)[0]
        else:
            return max(candidates, key=lambda x: x["score"])

    except tweepy.errors.Unauthorized:
        log.warning("Tweet search: Unauthorized — Basic tier required")
        return None
    except Exception as e:
        log.warning(f"Tweet search failed: {type(e).__name__}: {e}")
        return None


def _find_quote_tweet_candidate() -> dict | None:
    """
    Search Twitter for a recent tweet from whitelisted accounts worth retweeting.
    Returns the best candidate or None if nothing safe/interesting found.
    NOTE: requires Twitter Basic API ($100/month). Silently returns None on free tier.
    """
    now_dt = _utcnow()
    available_accounts = [
        a for a in _QUOTE_ACCOUNTS
        if _quote_account_blacklist.get(a, datetime.min) < now_dt
    ]
    if not available_accounts:
        log.debug("All quote accounts currently blacklisted — skipping")
        return None

    return _search_tweets_from_accounts(available_accounts)


def maybe_quote_tweet():
    """
    Larry quote-tweets whitelisted accounts with his take.
    Throttled: min 2 hours between quote tweets. 70% chance per check when eligible.
    Gives ~5-7 quote tweets per day — active but not spammy.
    """
    now = _utcnow()
    if now.hour < 8 or now.hour >= 23:
        return  # only active hours

    # Throttle: min 2 hours between quote tweets
    last_qt = get_state("last_quote_tweet_time")
    if last_qt:
        try:
            last_dt = datetime.fromisoformat(last_qt)
            if (now - last_dt).total_seconds() < 2 * 3600:
                return
        except Exception:
            pass

    if random.random() > 0.70:
        return  # 70% chance when eligible — more active

    candidate = _get_cycle_candidate()
    if not candidate:
        return

    try:
        tweet_data = ask_larry_for_tweet(
            "QUOTE_TWEET",
            extra_data={
                "original_tweet": candidate["text"][:200],
                "username": candidate["username"],
            }
        )
        comment = tweet_data.get("tweet", "")
        if not comment:
            return

        client = get_twitter_client()
        response = client.create_tweet(
            text=comment,
            quote_tweet_id=candidate["tweet_id"]
        )
        qt_id = str(response.data["id"])
        save_tweet(tweet_id=qt_id, content=comment, tweet_type="QUOTE_TWEET")
        log.info(f"✅ Quote-tweeted @{candidate['username']}: {comment[:60]}...")
        set_state("last_quote_tweet_time", now.isoformat())

        # Like the original too
        like_tweet(candidate["tweet_id"])

    except tweepy.Forbidden:
        # Account restricts quote tweets — blacklist at ACCOUNT level, not just tweet level
        # (tweet-level blacklist doesn't help: next cycle finds a new tweet from same account)
        username = candidate["username"]
        _quote_blocked_ids.add(candidate["tweet_id"])
        blocked_until = now + timedelta(hours=24)
        _quote_account_blacklist[username] = blocked_until
        # Persist so it survives container restarts
        try:
            set_state("quote_account_blacklist", json.dumps(
                {k: v.isoformat() for k, v in _quote_account_blacklist.items()}
            ))
        except Exception:
            pass
        # FIX: advance throttle even on 403 — otherwise next cycle fires again,
        # burns another Claude call, and blacklists another account. 3h pause.
        set_state("last_quote_tweet_time", now.isoformat())
        log.info(f"🚫 Quote tweet 403 for @{username} — blacklisted 24h, retrying quote tweets in 3h")
    except Exception as e:
        log.error(f"Quote tweet failed: {type(e).__name__}: {e}")


# ─── RETWEETS ─────────────────────────────────────────────────────────────────

def maybe_retweet():
    """
    Larry retweets something from a whitelisted account.
    No text needed — pure retweet. ~2-3 per day.
    Throttled: min 6 hours between retweets. 50% chance when eligible.
    """
    now = _utcnow()
    if now.hour < 8 or now.hour >= 23:
        return

    last_rt = get_state("last_retweet_time")
    if last_rt:
        try:
            if (now - datetime.fromisoformat(last_rt)).total_seconds() < 6 * 3600:
                return
        except Exception:
            pass

    if random.random() > 0.50:
        return

    candidate = _get_cycle_candidate()
    if not candidate:
        return

    try:
        client = get_twitter_client()
        # FIX: Tweepy 4.x client.retweet(tweet_id) — user_id is NOT passed,
        # it's inferred automatically from the bearer/access token.
        # Old call: client.retweet(larry_id, tweet_id) → TypeError: too many args
        client.retweet(candidate["tweet_id"])
        save_tweet(tweet_id=candidate["tweet_id"], content=f"RT @{candidate['username']}: {candidate['text'][:100]}", tweet_type="RETWEET")
        log.info(f"🔁 Retweeted @{candidate['username']}: {candidate['text'][:60]}...")
        set_state("last_retweet_time", now.isoformat())
    except Exception as e:
        log.warning(f"Retweet failed: {type(e).__name__}: {e}")


# ─── PROACTIVE WHITELIST REPLIES ──────────────────────────────────────────────

def maybe_reply_to_whitelist():
    """
    Larry drops a direct reply under a fresh tweet from a whitelisted account.
    Different from quote tweets: this is a reply IN the thread — more organic,
    gets Larry visible to the original poster's followers who are reading replies.

    Throttled: max 2 replies/day. 40% chance when eligible (less common than quotes).
    Uses recency sort — Larry comments on stuff that just happened, not yesterday's news.
    """
    now = _utcnow()
    if now.hour < 9 or now.hour >= 22:
        return  # active hours only

    # Max 2 whitelist replies per day
    today_str = now.strftime("%Y-%m-%d")
    reply_count_key = f"whitelist_reply_count_{today_str}"
    try:
        count = int(get_state(reply_count_key) or "0")
    except (ValueError, TypeError):
        count = 0
    if count >= 2:
        return  # daily cap hit

    # Throttle: min 3 hours between replies
    last_reply = get_state("last_whitelist_reply_time")
    if last_reply:
        try:
            if (now - datetime.fromisoformat(last_reply)).total_seconds() < 3 * 3600:
                return
        except Exception:
            pass

    if random.random() > 0.40:
        return  # 40% chance when eligible

    # Find the freshest tweet from whitelist — sort by recency (not engagement)
    candidate = _search_tweets_from_accounts(_QUOTE_ACCOUNTS, sort_by_recency=True)
    if not candidate:
        return

    # Skip if tweet is older than 2 hours — Larry replies to current events, not yesterday
    created_at = candidate.get("created_at")
    if created_at:
        try:
            age_hours = (now - created_at.replace(tzinfo=None)).total_seconds() / 3600
            if age_hours > 2:
                log.debug(f"Whitelist reply: skipping tweet {age_hours:.1f}h old (too stale)")
                return
        except Exception:
            pass  # if age check fails, proceed anyway

    try:
        tweet_data = ask_larry_for_tweet(
            "WHITELIST_REPLY",
            extra_data={
                "original_tweet": candidate["text"][:200],
                "username": candidate["username"],
            }
        )
        comment = tweet_data.get("tweet", "")
        if not comment:
            return

        client = get_twitter_client()
        response = client.create_tweet(
            text=comment,
            in_reply_to_tweet_id=candidate["tweet_id"],
        )
        reply_id = str(response.data["id"])
        save_tweet(tweet_id=reply_id, content=comment, tweet_type="WHITELIST_REPLY")
        log.info(f"💬 Replied to @{candidate['username']}: {comment[:60]}...")

        # Update throttle state
        set_state("last_whitelist_reply_time", now.isoformat())
        set_state(reply_count_key, str(count + 1))

        # Like the tweet we replied to (natural human behavior)
        like_tweet(candidate["tweet_id"])

    except tweepy.Forbidden:
        log.debug(f"Reply 403 for tweet {candidate['tweet_id'][:16]} — skipping")
        _quote_blocked_ids.add(candidate["tweet_id"])
    except Exception as e:
        log.warning(f"Whitelist reply failed: {type(e).__name__}: {e}")


# ─── FADE LARRY DETECTION ─────────────────────────────────────────────────────

def maybe_react_to_fade_larry():
    """
    Search for tweets that mention 'fade larry' or 'fading larry' — people publicly
    betting against Larry as a strategy. If found, Larry reacts in character.

    This creates an authentic narrative: the fade-Larry crowd becomes a recurring antagonist.
    Throttled: max once per day. Only fires ~40% of the time when triggered.
    """
    now = _utcnow()
    if now.hour < 10 or now.hour >= 22:
        return

    # Once per day max
    last_fade = get_state("last_fade_react_date")
    today_str = now.strftime("%Y-%m-%d")
    if last_fade == today_str:
        return

    if random.random() > 0.40:
        return  # 40% chance per cycle when eligible

    try:
        client = get_twitter_client()
        # Search for tweets mentioning fading Larry
        response = client.search_recent_tweets(
            query='("fade larry" OR "fading larry" OR "LarryLosesAgain" OR "fade @LarryLosesAgain") -is:retweet lang:en',
            max_results=10,
            tweet_fields=["text", "created_at", "public_metrics"],
        )
        if not response.data:
            return

        # Pick most liked / most recent
        candidates = [t for t in response.data if _is_safe_to_engage(t.text)]
        if not candidates:
            return

        best = max(candidates, key=lambda t: (t.public_metrics or {}).get("like_count", 0))
        fade_text = best.text[:200]

        tweet_data = ask_larry_for_tweet(
            "FADE_LARRY",
            extra_data={"fade_text": fade_text}
        )
        comment = tweet_data.get("tweet", "")
        if comment:
            post_tweet(comment, tweet_type="FADE_LARRY")
            set_state("last_fade_react_date", today_str)
            log.info(f"🎯 Reacted to fade-Larry tweet: {comment[:60]}...")

    except tweepy.errors.Unauthorized:
        log.debug("Fade Larry search: Unauthorized — Basic tier needed")
    except Exception as e:
        log.debug(f"Fade Larry detection failed: {type(e).__name__}: {e}")


# ─── PRICE MOVE REACTIONS ─────────────────────────────────────────────────────

def maybe_react_to_price_moves():
    """
    If a market where Larry has an open bet moved >5% since he bet,
    he tweets about it — panic, smugness, or confusion depending on direction.
    Max once per 6 hours.
    """
    last_react = get_state("last_price_react_time")
    if last_react:
        try:
            if (_utcnow() - datetime.fromisoformat(last_react)).total_seconds() < 6 * 3600:
                return
        except Exception:
            pass

    try:
        pending = get_pending_bets()
        if not pending:
            return

        # Check one random open bet for price movement
        bet = random.choice(pending)
        market_id = bet.get("polymarket_id", "")
        if not market_id:
            return

        # FIX: Gamma single-market lookup is /markets?conditionIds={id}, NOT /markets/{id}
        # The path-style endpoint returns 404; query-param style returns list with one item.
        resp = requests.get(
            f"{POLYMARKET_GAMMA_API}/markets",
            params={"conditionIds": market_id},
            timeout=5,
        )
        if resp.status_code != 200:
            return
        data = resp.json()
        market = data[0] if isinstance(data, list) and data else data
        if not market:
            return

        current_price = float(market.get("bestAsk") or market.get("lastTradePrice") or 0.5)
        original_odds = float(bet.get("odds", current_price))
        outcome = bet.get("outcome", "YES")

        # For NO bets, we care about YES price going down (good for us)
        if outcome == "NO":
            move = original_odds - current_price  # positive = price fell = good for NO
        else:
            move = current_price - original_odds  # positive = price rose = good for YES

        if abs(move) < 0.05:
            return  # less than 5% move — not interesting

        direction = "winning" if move > 0 else "losing"
        tweet_data = ask_larry_for_tweet(
            "PRICE_MOVE",
            extra_data={
                "question": bet.get("question", "")[:80],
                "outcome": outcome,
                "move_pct": round(abs(move) * 100),
                "direction": direction,
                "original_price": round(original_odds, 2),
                "current_price": round(current_price, 2),
            }
        )
        comment = tweet_data.get("tweet", "")
        if comment:
            post_tweet(comment, tweet_type="PRICE_MOVE")
            set_state("last_price_react_time", _utcnow().isoformat())

    except Exception as e:
        log.warning(f"Price move react failed: {type(e).__name__}: {e}")


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

_twitter_shutdown = False


def set_twitter_shutdown():
    """Called by main.py SIGTERM handler (must run in main thread)."""
    global _twitter_shutdown
    _twitter_shutdown = True
    log.info("🛑 Twitter agent shutdown requested — will exit after current cycle")

def is_twitter_shutdown() -> bool:
    return _twitter_shutdown


def run_twitter_agent():
    log.info(f"🐦 Larry's Twitter Agent starting up as {LARRY_TWITTER_HANDLE}...")
    init_db()

    # Load persisted state from previous session
    _init_quote_blacklist()

    while not _twitter_shutdown:
        try:
            # 1. Dead man's switch
            if check_dead_man_switch():
                log.warning("⚠️ DEAD MAN'S SWITCH TRIGGERED!")
                tweet_data = ask_larry_for_tweet("DEAD_MAN_SWITCH")
                post_tweet(tweet_data["tweet"], tweet_type="DEAD_MAN_SWITCH")

            # 2. GM tweet (every 8h, with Larry's image — cross-timezone reach)
            maybe_tweet_gm()

            # 3. Friday pizza
            maybe_tweet_pizza()

            # 4. Organic tweet
            if should_tweet_now():
                bankroll = get_bankroll()
                tweet_type = "SURVIVAL" if bankroll < 80 else "RANDOM"
                tweet_data = ask_larry_for_tweet(tweet_type)
                post_tweet(tweet_data["tweet"], tweet_type=tweet_type)

            # 4. Retweet something from whitelist (~2-3/day)
            maybe_retweet()

            # 5. Drop a reply in a whitelist account's thread (~2/day, freshest tweet only)
            maybe_reply_to_whitelist()

            # 6. React to price moves on open bets (throttled, only big moves)
            maybe_react_to_price_moves()

            # 7. Fade Larry detection (~1/day if people are publicly fading him)
            maybe_react_to_fade_larry()

            # 8. Weekly recap (Sundays)
            maybe_tweet_weekly_recap()

            # 9. Milestone tweets
            check_milestones()

        except KeyboardInterrupt:
            log.info("👋 Twitter agent stopped")
            break
        except Exception as e:
            log.error(f"Error in twitter loop: {type(e).__name__}: {e}")

        if _twitter_shutdown:
            break

        log.info("💤 Twitter agent sleeping 15 minutes...")
        time.sleep(15 * 60)

    log.info("✅ Twitter agent exited cleanly")


if __name__ == "__main__":
    run_twitter_agent()
