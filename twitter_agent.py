"""
twitter_agent.py — Larry's Twitter/X presence
Runs on a loop every 15 minutes:
  1. Check if it's time to tweet (3-8x per day, min 45min gaps)
  2. Ask Claude for a tweet if needed
  3. Check mentions — reply at 1:4 ratio (1 reply per 4 own tweets)
  4. Dead man's switch: auto-tweet if silent for 48h
  5. Friday pizza tweet scheduler
"""

import sys
import time
import json
import signal
import random
import logging
import threading
import requests
import tweepy
from datetime import datetime, timedelta
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
from larry_brain import ask_larry_for_tweet, ask_larry_to_reply

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TWITTER] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ─── REPLY RATIO: 1 reply per 4 own tweets ───────────────────────────────────
REPLY_RATIO = 4

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

# ─── TWITTER CLIENT SINGLETON ─────────────────────────────────────────────────
# Creating a new tweepy.Client per call = new HTTP session + SSL handshake every time.
# With 8-10 Twitter actions per cycle, that's 8-10 unnecessary handshakes.
# Singleton is safe — tweepy.Client is stateless (no persistent connection to close).
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


# ─── POST TWEET ───────────────────────────────────────────────────────────────

def post_tweet(text: str, tweet_type: str = "RANDOM", bet_id: int = None,
               reply_to_id: str = None) -> str:
    """Post a tweet as Larry. Optionally reply to another tweet."""
    if len(text) > 280:
        text = text[:277] + "..."

    try:
        client = get_twitter_client()

        if reply_to_id:
            response = client.create_tweet(
                text=text,
                in_reply_to_tweet_id=reply_to_id
            )
        else:
            response = client.create_tweet(text=text)

        tweet_id = str(response.data["id"])
        save_tweet(tweet_id=tweet_id, content=text, tweet_type=tweet_type, bet_id=bet_id)
        log.info(f"✅ Tweeted [{tweet_type}]: {text[:80]}...")
        return tweet_id

    except tweepy.TweepyException as e:
        log.error(f"Twitter error: {e}")
        raise


# ─── MENTIONS & REPLIES ──────────────────────────────────────────────────────

def get_today_tweet_stats() -> dict:
    """Single DB query for all today's tweet counts — was 3 separate connections before."""
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT
                SUM(CASE WHEN tweet_type NOT IN ('REPLY','WHITELIST_REPLY','VIP_REPLY','RETWEET')
                         THEN 1 ELSE 0 END) as own_count,
                SUM(CASE WHEN tweet_type IN ('REPLY','WHITELIST_REPLY','VIP_REPLY')
                         THEN 1 ELSE 0 END) as reply_count
            FROM tweets
            WHERE DATE(posted_at) = DATE('now')
        """).fetchone()
    finally:
        conn.close()
    return {
        "own":     (row["own_count"]   or 0) if row else 0,
        "replies": (row["reply_count"] or 0) if row else 0,
    }

def get_today_own_tweet_count() -> int:
    return get_today_tweet_stats()["own"]

def get_today_reply_count() -> int:
    return get_today_tweet_stats()["replies"]

def should_reply_now() -> bool:
    """Reply only if: own_tweets_today / REPLY_RATIO > replies_today (1 reply per 4 own tweets)."""
    stats = get_today_tweet_stats()
    own, replies = stats["own"], stats["replies"]
    allowed = own // REPLY_RATIO
    if replies < allowed:
        log.info(f"Reply allowed: {own} own tweets → {allowed} replies allowed, {replies} sent")
        return True
    return False


def _get_larry_id(client: tweepy.Client) -> int:
    """Get Larry's user ID, using a module-level cache to avoid repeated API calls."""
    global _larry_user_id
    if _larry_user_id is None:
        me = client.get_me()
        _larry_user_id = me.data.id
        log.info(f"Cached Larry's user ID: {_larry_user_id}")
    return _larry_user_id


def fetch_mentions(since_id: str = None) -> list:
    """Fetch recent mentions of @LarryLosesAgain."""
    try:
        client = get_twitter_client()

        # FIX: use cached user ID instead of calling get_me() every 15 minutes
        larry_id = _get_larry_id(client)

        kwargs = {
            "max_results": 10,
            "tweet_fields": ["author_id", "text", "created_at", "public_metrics"],
            "expansions": ["author_id"],
            "user_fields": ["username", "public_metrics"],
        }
        if since_id:
            kwargs["since_id"] = since_id

        response = client.get_users_mentions(larry_id, **kwargs)

        if not response.data:
            return []

        # Build users lookup
        users = {}
        if response.includes and "users" in response.includes:
            for u in response.includes["users"]:
                users[u.id] = u.username

        mentions = []
        for tweet in response.data:
            mentions.append({
                "tweet_id": str(tweet.id),
                "author_id": str(tweet.author_id),
                "username": users.get(tweet.author_id, "unknown"),
                "text": tweet.text,
                "likes": tweet.public_metrics.get("like_count", 0) if tweet.public_metrics else 0,
                "replies": tweet.public_metrics.get("reply_count", 0) if tweet.public_metrics else 0,
            })

        log.info(f"Fetched {len(mentions)} mentions")
        return mentions

    except tweepy.TweepyException as e:
        log.error(f"Error fetching mentions: {e}")
        return []


def pick_best_mention(mentions: list) -> dict | None:
    """
    Pick the most interesting mention to reply to.
    Prioritize: high engagement > challenges Larry > insults Larry (gold content).
    """
    if not mentions:
        return None

    # Score each mention
    def score(m):
        s = m["likes"] * 2 + m["replies"] * 3
        text_lower = m["text"].lower()
        # Larry loves confrontation
        if any(w in text_lower for w in ["wrong", "idiot", "loser", "bad", "fraud", "scam"]):
            s += 50  # insults = must reply
        if any(w in text_lower for w in ["bet", "win", "lose", "market", "prediction"]):
            s += 20  # on-topic = relevant
        if "?" in m["text"]:
            s += 15  # questions = reply bait
        return s

    sorted_mentions = sorted(mentions, key=score, reverse=True)
    return sorted_mentions[0]


def check_and_reply_to_mentions():
    """Check mentions and reply to the best one if ratio allows."""
    if not should_reply_now():
        return

    # Track last processed mention ID to avoid duplicates
    since_id = get_state("last_mention_id")
    mentions = fetch_mentions(since_id=since_id)

    if not mentions:
        return

    # FIX: don't advance last_mention_id until reply succeeds.
    # Old: set_state first → if reply fails, mention is lost forever.
    # New: save ID only after successful post.
    latest_id = max(m["tweet_id"] for m in mentions)

    best = pick_best_mention(mentions)
    if not best:
        set_state("last_mention_id", latest_id)  # still advance past uninteresting mentions
        return

    log.info(f"Replying to @{best['username']}: {best['text'][:60]}...")

    try:
        reply_data = ask_larry_to_reply(best)
        reply_text = reply_data.get("reply", "")

        if reply_text:
            full_reply = f"@{best['username']} {reply_text}"
            if len(full_reply) > 280:
                full_reply = full_reply[:277] + "..."

            post_tweet(full_reply, tweet_type="REPLY", reply_to_id=best["tweet_id"])
            set_state("last_mention_id", latest_id)  # ← only advance AFTER success
            log.info(f"✅ Replied to @{best['username']}")
            like_tweet(best["tweet_id"])

    except Exception as e:
        log.error(f"Failed to generate/post reply: {e}")
        # Don't advance last_mention_id — will retry this mention next cycle


# ─── TIMING LOGIC ────────────────────────────────────────────────────────────

def should_tweet_now() -> bool:
    """Decide if it's time for a new organic tweet.
    No daily cap — only constraint is MIN_MINUTES_BETWEEN_TWEETS gap.
    25% chance per 15-min cycle when gap has elapsed = ~4-6 tweets/day naturally.
    """
    now = datetime.utcnow()
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
    now = datetime.utcnow()
    return now.weekday() == 4 and 17 <= now.hour <= 19


def check_dead_man_switch() -> bool:
    last_tweet = get_last_tweet_time()
    if last_tweet is None:
        return False
    hours_silent = (datetime.utcnow() - last_tweet).total_seconds() / 3600
    return hours_silent >= DEAD_MAN_SWITCH_HOURS


# ─── FRIDAY PIZZA ────────────────────────────────────────────────────────────

# FIX: persist pizza flag in DB instead of module-level variable,
# which was lost on every process restart (causing duplicate Friday tweets)
def maybe_tweet_pizza():
    now = datetime.utcnow()
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
    now = datetime.utcnow()
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
    now = datetime.utcnow()
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
        now = datetime.utcnow()
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
    "polymarket",       # prediction markets — Larry's home turf
    "elonmusk",         # Elon — Larry has opinions on everything he says
    "realDonaldTrump",  # Trump — Larry bets on politics
    "NateSilver538",    # forecasting legend — Larry thinks he's better than Nate
    "unusual_whales",   # tracks market activity — relevant
    "KobeissiLetter",   # macro commentary — Larry will have a take
    "Kalshi",           # Polymarket competitor — prediction market context
    "saylor",           # Bitcoin maximalist — Larry bets on BTC
    "cz_binance",       # crypto, massive following
]

def _search_tweets_from_accounts(account_list: list) -> dict | None:
    """
    Core search: find a recent tweet from the given account list.
    Returns best candidate by engagement score, or None.
    """
    if not account_list:
        return None
    try:
        client = get_twitter_client()
        accounts = random.sample(account_list, min(5, len(account_list)))
        from_query = " OR ".join(f"from:{a}" for a in accounts)
        query = f"({from_query}) -is:retweet -is:reply lang:en"

        response = client.search_recent_tweets(
            query=query,
            max_results=20,
            tweet_fields=["author_id", "text", "public_metrics", "reply_settings"],
            expansions=["author_id"],
            user_fields=["username", "public_metrics"],
        )
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
            # Skip tweets where replies are restricted — Larry isn't mentioned/followed
            # by these authors so he'll always get a 403. Check BEFORE calling Claude.
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
            candidates.append({
                "tweet_id": str(tweet.id),
                "text": text,
                "username": user.get("username", ""),
                "score": score,
            })
        return max(candidates, key=lambda x: x["score"]) if candidates else None
    except tweepy.errors.Unauthorized:
        log.warning("Tweet search: Unauthorized — Basic tier required")
        return None
    except Exception as e:
        log.warning(f"Tweet search failed: {type(e).__name__}: {e}")
        return None


def _find_quote_tweet_candidate() -> dict | None:
    """
    Search Twitter for a recent tweet from whitelisted accounts worth Larry commenting on.
    Returns the best candidate or None if nothing safe/interesting found.
    NOTE: requires Twitter Basic API ($100/month). Silently returns None on free tier.

    Uses ALL _QUOTE_ACCOUNTS (including VIP). For quote tweets and retweets the VIP
    accounts are fine — it's only in maybe_reply_to_whitelist where we need to avoid them
    (to prevent double-replying with the VIP stream).
    """
    now_dt = datetime.utcnow()
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
    Throttled: min 3 hours between quote tweets. 50% chance per check when eligible.
    Gives ~3-5 quote tweets per day — active but not spammy.
    """
    now = datetime.utcnow()
    if now.hour < 8 or now.hour >= 23:
        return  # only active hours

    # Throttle: min 3 hours between quote tweets
    last_qt = get_state("last_quote_tweet_time")
    if last_qt:
        try:
            last_dt = datetime.fromisoformat(last_qt)
            if (now - last_dt).total_seconds() < 3 * 3600:
                return
        except Exception:
            pass

    if random.random() > 0.50:
        return  # 50% chance when eligible — natural variation

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
    now = datetime.utcnow()
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


# ─── REPLIES TO WHITELIST ──────────────────────────────────────────────────────

def maybe_reply_to_whitelist():
    """
    Larry drops a comment under a tweet from a whitelisted account.
    Different from quote tweet — appears as a reply thread under their post.
    ~4-5 per day. Throttled: min 2 hours. 70% chance when eligible.

    IMPORTANT: searches ONLY non-VIP accounts.
    VIP accounts (elonmusk, realDonaldTrump, polymarket) are covered by the real-time
    VIP stream. Using _get_cycle_candidate() here was wrong — those top accounts
    dominate the score ranking (millions of likes), so the shared candidate was always
    a VIP account, the skip-VIP check fired, and Larry NEVER replied to anyone.
    Fix: separate search for non-VIP accounts only.
    """
    now = datetime.utcnow()
    if now.hour < 8 or now.hour >= 23:
        return

    last_wr = get_state("last_whitelist_reply_time")
    if last_wr:
        try:
            if (now - datetime.fromisoformat(last_wr)).total_seconds() < 2 * 3600:
                return
        except Exception:
            pass

    if random.random() > 0.70:
        return

    # Search only non-VIP accounts — VIPs are handled by the real-time stream
    vip_lower = {a.lower() for a in _VIP_STREAM_ACCOUNTS}
    now_dt = datetime.utcnow()
    non_vip_accounts = [
        a for a in _QUOTE_ACCOUNTS
        if a.lower() not in vip_lower
        and _quote_account_blacklist.get(a, datetime.min) < now_dt
    ]
    if not non_vip_accounts:
        return

    candidate = _search_tweets_from_accounts(non_vip_accounts)
    if not candidate:
        return

    try:
        tweet_data = ask_larry_for_tweet(
            "WHITELIST_REPLY",
            extra_data={
                "original_tweet": candidate["text"][:200],
                "username": candidate["username"],
            }
        )
        reply_text = tweet_data.get("tweet", "")
        if not reply_text:
            return

        client = get_twitter_client()
        response = client.create_tweet(
            text=reply_text,
            in_reply_to_tweet_id=candidate["tweet_id"]
        )
        reply_id = str(response.data["id"])
        save_tweet(tweet_id=reply_id, content=reply_text, tweet_type="WHITELIST_REPLY")
        log.info(f"💬 Replied to @{candidate['username']}: {reply_text[:60]}...")
        set_state("last_whitelist_reply_time", now.isoformat())
        like_tweet(candidate["tweet_id"])
    except tweepy.Forbidden as e:
        # Tweet has reply restrictions ("not mentioned or engaged by author").
        # Blacklist this tweet_id so we never retry it.
        # Also advance the cooldown — no point retrying other tweets this cycle,
        # we just burned a Claude API call. Wait 2h before trying again.
        _quote_blocked_ids.add(candidate["tweet_id"])
        set_state("last_whitelist_reply_time", now.isoformat())
        log.warning(f"Whitelist reply forbidden for @{candidate['username']} (tweet restricted) — skipping 2h")
    except Exception as e:
        log.warning(f"Whitelist reply failed: {type(e).__name__}: {e}")


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
            if (datetime.utcnow() - datetime.fromisoformat(last_react)).total_seconds() < 6 * 3600:
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
            set_state("last_price_react_time", datetime.utcnow().isoformat())

    except Exception as e:
        log.warning(f"Price move react failed: {type(e).__name__}: {e}")


# ─── VIP STREAM (real-time engagement farming) ────────────────────────────────
#
# Uses Twitter Filtered Stream API — persistent connection, Twitter pushes tweets
# in real-time the moment they're posted. No polling. Latency: ~5-10 seconds.
# Requires Basic tier ($100/mo). Silently skips if unavailable.
#
# VIP accounts Larry monitors and replies to immediately via filtered stream.
# Only Elon — every tweet, 24/7, no per-account cooldown.
_VIP_STREAM_ACCOUNTS = ["elonmusk"]

# Anti-spam guard: minimum seconds between consecutive VIP replies.
# Elon sometimes posts 5 tweets in 2 minutes — without this Larry would fire
# 5 Claude calls + 5 Twitter API calls in a row, risking rate limits.
_VIP_REPLY_MIN_GAP_SECS = 300  # 5 minutes between replies max
_vip_last_reply_at: datetime | None = None


class LarryStreamClient(tweepy.StreamingClient):
    """Tweepy v4 streaming client — receives VIP tweets in real-time."""

    def on_response(self, response):
        """
        BUG FIX: Must override on_response, NOT on_tweet.

        In Tweepy 4.x, on_tweet(tweet) receives only the Tweet object built from
        data["data"]. The matching_rules field lives at the ROOT of the API response
        (data["matching_rules"]), NOT inside data["data"] — so tweet.matching_rules
        is ALWAYS None, username stays None, and the old handler returned silently
        on every single tweet without ever replying.

        on_response(response) receives the full StreamResponse which has:
          response.data           → the Tweet object
          response.matching_rules → list of matched stream rules (WITH tags)
        """
        try:
            global _vip_last_reply_at
            tweet = response.data
            if not tweet:
                return

            now = datetime.utcnow()

            # No active-hours restriction — Elon tweets 24/7, Larry replies 24/7.

            text = tweet.text or ""
            if not text or not _is_safe_to_engage(text):
                return

            # Figure out which VIP account this is from (via matching_rules in response)
            username = None
            if response.matching_rules:
                tag = response.matching_rules[0].tag  # tag = "vip_elonmusk"
                username = tag.replace("vip_", "")
            if not username:
                return

            # Anti-spam: if Elon posts a burst of tweets, don't fire on every one.
            # Reply to the first, then wait _VIP_REPLY_MIN_GAP_SECS before the next.
            if _vip_last_reply_at:
                secs_since = (now - _vip_last_reply_at).total_seconds()
                if secs_since < _VIP_REPLY_MIN_GAP_SECS:
                    log.info(f"⚡ VIP: skipping @{username} tweet (replied {secs_since:.0f}s ago, gap={_VIP_REPLY_MIN_GAP_SECS}s)")
                    return

            log.info(f"⚡ VIP stream: new tweet from @{username} — replying...")

            # Sonnet for Elon replies — high visibility, worth the cost.
            # A good reply under his tweet can get 50k+ impressions.
            tweet_data = ask_larry_for_tweet(
                "WHITELIST_REPLY",
                extra_data={"original_tweet": text[:200], "username": username},
                model=CLAUDE_MODEL,
            )
            reply_text = tweet_data.get("tweet", "")
            if not reply_text:
                return

            client = get_twitter_client()
            reply_resp = client.create_tweet(
                text=reply_text,
                in_reply_to_tweet_id=str(tweet.id),
            )
            reply_id = str(reply_resp.data["id"])
            save_tweet(tweet_id=reply_id, content=reply_text, tweet_type="VIP_REPLY")
            log.info(f"⚡ VIP reply to @{username}: {reply_text[:80]}...")

            _vip_last_reply_at = now
            like_tweet(str(tweet.id))

        except Exception as e:
            log.warning(f"VIP stream on_response error: {type(e).__name__}: {e}")

    def on_errors(self, errors):
        log.warning(f"VIP stream errors: {errors}")

    def on_closed(self, resp):
        log.warning("VIP stream connection closed — will reconnect")

    def on_exception(self, exception):
        log.warning(f"VIP stream exception: {exception}")


def _setup_stream_rules(stream: LarryStreamClient):
    """
    Sync stream filter rules: delete old VIP rules, add fresh ones.
    Rules are persistent on Twitter's side — need to clean up on startup.
    """
    try:
        existing = stream.get_rules()
        if existing.data:
            ids = [r.id for r in existing.data if r.tag and r.tag.startswith("vip_")]
            if ids:
                stream.delete_rules(ids)

        for username in _VIP_STREAM_ACCOUNTS:
            stream.add_rules(tweepy.StreamRule(
                value=f"from:{username} -is:retweet -is:reply lang:en",
                tag=f"vip_{username}",
            ))
        log.info(f"⚡ Stream rules set for: {', '.join('@' + a for a in _VIP_STREAM_ACCOUNTS)}")
    except Exception as e:
        log.warning(f"Failed to set stream rules: {type(e).__name__}: {e}")


def run_vip_stream():
    """
    Background thread — connects to Twitter Filtered Stream and listens forever.
    Auto-reconnects on disconnect with exponential backoff.
    Requires Basic API tier. Silently exits if unavailable.

    BUG FIXED: tweepy's stream.filter() catches ConnectTimeout internally, calls
    on_connection_error(), then returns normally — no exception raised. This caused
    the `else: backoff = 5` branch to fire on every timeout, resetting backoff and
    spinning at ~60s intervals for hours. Fix: track how long we were actually
    connected. If stream.filter() returns in under 30s, treat it as a failure and
    apply backoff rather than resetting to 5.
    """
    log.info("⚡ VIP stream starting...")
    backoff = 5

    while True:
        connected_at = None
        try:
            stream = LarryStreamClient(bearer_token=TWITTER_BEARER_TOKEN)
            _setup_stream_rules(stream)

            # NOTE: matching_rules is returned by Twitter at the ROOT of the stream response,
            # not inside tweet data. We access it via on_response(response).matching_rules.
            connected_at = datetime.utcnow()
            stream.filter(
                tweet_fields=["id", "text", "author_id"],
                expansions=["author_id"],
                threaded=False,  # blocking — runs in this thread
            )

        except tweepy.errors.TooManyRequests:
            # 429 "TooManyConnections" — Twitter still has a zombie connection from a
            # previous deploy registered. Must wait for Twitter to kill it (~90s).
            wait = 120
            log.warning(f"VIP stream: too many connections (zombie from prev deploy) — waiting {wait}s for Twitter to clean up")
            time.sleep(wait)
            # don't escalate backoff here — this is a one-time deploy artifact
        except tweepy.errors.TwitterServerError:
            log.warning(f"VIP stream server error — reconnecting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)  # cap at 5 min
        except tweepy.errors.Unauthorized:
            log.warning("VIP stream: unauthorized — Basic tier required, disabling")
            return  # give up — not available on free tier
        except Exception as e:
            log.warning(f"VIP stream error: {type(e).__name__}: {e} — reconnecting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            # stream.filter() returned without raising — but was it a real clean disconnect,
            # or an immediate ConnectTimeout handled silently inside tweepy?
            session_secs = (datetime.utcnow() - connected_at).total_seconds() if connected_at else 0
            if session_secs >= 30:
                backoff = 5  # genuinely connected for a while — reset backoff
                log.info(f"VIP stream disconnected after {session_secs:.0f}s — reconnecting")
            else:
                # Rapid disconnect — network was likely down, apply full backoff
                log.warning(f"VIP stream disconnected after only {session_secs:.0f}s — reconnecting in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)


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

    # Start VIP stream in background — real-time push from Twitter, no polling
    vip_thread = threading.Thread(target=run_vip_stream, daemon=True)
    vip_thread.start()

    while not _twitter_shutdown:
        try:
            # 1. Dead man's switch
            if check_dead_man_switch():
                log.warning("⚠️ DEAD MAN'S SWITCH TRIGGERED!")
                tweet_data = ask_larry_for_tweet("DEAD_MAN_SWITCH")
                post_tweet(tweet_data["tweet"], tweet_type="DEAD_MAN_SWITCH")

            # 2. Friday pizza
            maybe_tweet_pizza()

            # 3. Organic tweet
            if should_tweet_now():
                bankroll = get_bankroll()
                tweet_type = "SURVIVAL" if bankroll < 80 else "RANDOM"
                tweet_data = ask_larry_for_tweet(tweet_type)
                post_tweet(tweet_data["tweet"], tweet_type=tweet_type)

            # 4. Check mentions and maybe reply (1 reply per 4 own tweets)
            check_and_reply_to_mentions()

            # 5. Quote tweet something relevant
            maybe_quote_tweet()

            # 6. Retweet something from whitelist
            maybe_retweet()

            # 7. Reply under a whitelist account's tweet
            maybe_reply_to_whitelist()

            # 8. React to price moves on open bets (throttled, only big moves)
            maybe_react_to_price_moves()

            # 9. Weekly recap (Sundays)
            maybe_tweet_weekly_recap()

            # 10. Milestone tweets
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
