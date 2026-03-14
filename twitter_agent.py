"""
twitter_agent.py — Larry's Twitter/X presence
Runs on a loop every 15 minutes:
  1. Check if it's time to tweet (3-8x per day, min 45min gaps)
  2. Ask Claude for a tweet if needed
  3. Check mentions — reply at 1:4 ratio (1 reply per 4 own tweets)
  4. Dead man's switch: auto-tweet if silent for 48h
  5. Friday pizza tweet scheduler
"""

import time
import random
import logging
import tweepy
from datetime import datetime, timedelta

from config import (
    TWITTER_API_KEY, TWITTER_API_SECRET,
    TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET,
    TWITTER_BEARER_TOKEN,
    MIN_TWEETS_PER_DAY, MAX_TWEETS_PER_DAY,
    MIN_MINUTES_BETWEEN_TWEETS, DEAD_MAN_SWITCH_HOURS,
    LARRY_TWITTER_HANDLE
)
from database import (
    save_tweet, get_last_tweet_time, get_today_tweet_count,
    get_bankroll, get_state, set_state, init_db
)
from larry_brain import ask_larry_for_tweet, ask_larry_to_reply

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TWITTER] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── REPLY RATIO: 1 reply per 4 own tweets ───────────────────────────────────
REPLY_RATIO = 4

# FIX: cache larry's user ID so we don't call get_me() every 15 minutes
_larry_user_id = None


# ─── TWITTER CLIENT ───────────────────────────────────────────────────────────

def get_twitter_client() -> tweepy.Client:
    return tweepy.Client(
        bearer_token=TWITTER_BEARER_TOKEN,
        consumer_key=TWITTER_API_KEY,
        consumer_secret=TWITTER_API_SECRET,
        access_token=TWITTER_ACCESS_TOKEN,
        access_token_secret=TWITTER_ACCESS_SECRET,
        wait_on_rate_limit=True,
    )


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

def get_today_reply_count() -> int:
    """Count how many replies Larry sent today."""
    from database import get_connection
    conn = get_connection()
    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM tweets
        WHERE tweet_type = 'REPLY' AND DATE(posted_at) = DATE('now')
    """).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def get_today_own_tweet_count() -> int:
    """Count own tweets today (not replies)."""
    from database import get_connection
    conn = get_connection()
    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM tweets
        WHERE tweet_type != 'REPLY' AND DATE(posted_at) = DATE('now')
    """).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def should_reply_now() -> bool:
    """
    Reply only if: own_tweets_today / REPLY_RATIO > replies_today
    i.e. 1 reply per 4 own tweets.
    """
    own = get_today_own_tweet_count()
    replies = get_today_reply_count()
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

    # Save latest mention ID
    latest_id = max(m["tweet_id"] for m in mentions)
    set_state("last_mention_id", latest_id)

    # Pick the best one
    best = pick_best_mention(mentions)
    if not best:
        return

    log.info(f"Replying to @{best['username']}: {best['text'][:60]}...")

    # Ask Claude to generate Larry's reply
    try:
        reply_data = ask_larry_to_reply(best)
        reply_text = reply_data.get("reply", "")

        if reply_text:
            # Prepend @username
            full_reply = f"@{best['username']} {reply_text}"
            if len(full_reply) > 280:
                full_reply = full_reply[:277] + "..."

            post_tweet(full_reply, tweet_type="REPLY", reply_to_id=best["tweet_id"])
            log.info(f"✅ Replied to @{best['username']}")

    except Exception as e:
        log.error(f"Failed to generate/post reply: {e}")


# ─── TIMING LOGIC ────────────────────────────────────────────────────────────

def should_tweet_now() -> bool:
    """Decide if it's time for a new organic tweet."""
    now = datetime.utcnow()

    # FIX: use own-tweet count only — replies shouldn't eat into the daily limit
    today_count = get_today_own_tweet_count()
    last_tweet = get_last_tweet_time()

    if today_count >= MAX_TWEETS_PER_DAY:
        return False

    if last_tweet:
        minutes_since = (now - last_tweet).total_seconds() / 60
        if minutes_since < MIN_MINUTES_BETWEEN_TWEETS:
            return False

    hour = now.hour
    # FIX: hour > 23 is always False (hours are 0–23); use >= 23 to block late-night tweets
    if hour < 7 or hour >= 23:
        return False

    tweets_remaining = MAX_TWEETS_PER_DAY - today_count
    hours_remaining = max(1, 23 - hour)
    expected_interval_hours = hours_remaining / max(1, tweets_remaining)
    chance_per_check = 0.25 / expected_interval_hours

    if random.random() < chance_per_check:
        log.info(f"Rolling to tweet: {today_count}/{MAX_TWEETS_PER_DAY} today")
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
    """Tweet when Larry hits a bankroll milestone for the first time."""
    bankroll = get_bankroll()
    for milestone in MILESTONES:
        key = f"milestone_{milestone}_tweeted"
        if bankroll >= milestone and get_state(key) != "true":
            log.info(f"🏆 MILESTONE HIT: ${milestone}!")
            tweet_data = ask_larry_for_tweet(
                "MILESTONE",
                extra_data={"milestone": f"${milestone} bankroll", "current": bankroll}
            )
            post_tweet(tweet_data["tweet"], tweet_type="MILESTONE")
            set_state(key, "true")
            break  # only one milestone per cycle


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def run_twitter_agent():
    log.info(f"🐦 Larry's Twitter Agent starting up as {LARRY_TWITTER_HANDLE}...")
    init_db()

    while True:
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

            # 5. Weekly recap (Sundays)
            maybe_tweet_weekly_recap()

            # 6. Milestone tweets
            check_milestones()

        except KeyboardInterrupt:
            log.info("👋 Twitter agent stopped")
            break
        except Exception as e:
            log.error(f"Error in twitter loop: {type(e).__name__}: {e}")

        log.info("💤 Twitter agent sleeping 15 minutes...")
        time.sleep(15 * 60)


if __name__ == "__main__":
    run_twitter_agent()
