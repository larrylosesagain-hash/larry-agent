"""
twitter_agent.py — Larry's Twitter/X presence
Runs on a loop every 15 minutes:
  1. Check if it's time to tweet (3-8x per day, min 45min gaps)
  2. Ask Claude for a tweet if needed
  3. Post it
  4. Dead man's switch: auto-tweet if silent for 48h
  5. Friday pizza tweet scheduler
"""

import time
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
    get_bankroll, init_db
)
from larry_brain import ask_larry_for_tweet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TWITTER] %(message)s",
    handlers=[
        logging.FileHandler("/home/larry/logs/twitter.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


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

def post_tweet(text: str, tweet_type: str = "RANDOM", bet_id: int = None) -> str:
    """
    Post a tweet as Larry. Returns tweet_id string.
    This function is called both by twitter_agent and betting_agent.
    """
    # Safety: truncate to 280
    if len(text) > 280:
        text = text[:277] + "..."

    try:
        client = get_twitter_client()
        response = client.create_tweet(text=text)
        tweet_id = str(response.data["id"])

        save_tweet(tweet_id=tweet_id, content=text, tweet_type=tweet_type, bet_id=bet_id)
        log.info(f"✅ Tweeted [{tweet_type}]: {text[:80]}...")
        return tweet_id

    except tweepy.TweepyException as e:
        log.error(f"Twitter error posting tweet: {e}")
        raise


# ─── TIMING LOGIC ────────────────────────────────────────────────────────────

def should_tweet_now() -> bool:
    """Decide if it's time for a new organic tweet."""
    now = datetime.utcnow()
    today_count = get_today_tweet_count()
    last_tweet = get_last_tweet_time()

    # Max tweets per day reached
    if today_count >= MAX_TWEETS_PER_DAY:
        log.info(f"Max tweets today reached ({today_count}/{MAX_TWEETS_PER_DAY})")
        return False

    # Too soon since last tweet
    if last_tweet:
        minutes_since = (now - last_tweet).total_seconds() / 60
        if minutes_since < MIN_MINUTES_BETWEEN_TWEETS:
            log.info(f"Only {minutes_since:.1f}min since last tweet, need {MIN_MINUTES_BETWEEN_TWEETS}min")
            return False

    # At least MIN_TWEETS_PER_DAY — distribute over waking hours (7am-11pm UTC)
    hour = now.hour
    if hour < 7 or hour > 23:
        return False

    # Simple probability: if we have room for more tweets, tweet ~every 2-3 hours
    # This creates organic-looking timing
    waking_hours = 16  # 7am to 11pm
    tweets_remaining = MAX_TWEETS_PER_DAY - today_count
    hours_remaining = max(1, 23 - hour)

    # Chance to tweet this 15-min window
    expected_interval_hours = hours_remaining / max(1, tweets_remaining)
    chance_per_check = (0.25) / expected_interval_hours  # 0.25 = 15min / 60min

    import random
    if random.random() < chance_per_check:
        log.info(f"Rolling to tweet: {today_count}/{MAX_TWEETS_PER_DAY} today")
        return True

    return False


def is_friday_pizza_time() -> bool:
    """Friday between 5pm-7pm UTC = pizza time."""
    now = datetime.utcnow()
    return now.weekday() == 4 and 17 <= now.hour <= 19  # Friday


def check_dead_man_switch() -> bool:
    """Return True if Larry has been silent for 48+ hours."""
    last_tweet = get_last_tweet_time()
    if last_tweet is None:
        return False  # Never tweeted, don't trigger
    hours_silent = (datetime.utcnow() - last_tweet).total_seconds() / 3600
    return hours_silent >= DEAD_MAN_SWITCH_HOURS


# ─── FRIDAY PIZZA CHECK ──────────────────────────────────────────────────────

_pizza_tweeted_this_week = False  # module-level flag

def maybe_tweet_pizza():
    global _pizza_tweeted_this_week
    now = datetime.utcnow()

    if now.weekday() == 0:  # Monday reset
        _pizza_tweeted_this_week = False

    if is_friday_pizza_time() and not _pizza_tweeted_this_week:
        log.info("🍕 IT'S FRIDAY PIZZA TIME!")
        tweet_data = ask_larry_for_tweet("FRIDAY")
        post_tweet(tweet_data["tweet"], tweet_type="FRIDAY")
        _pizza_tweeted_this_week = True


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def run_twitter_agent():
    log.info(f"🐦 Larry's Twitter Agent starting up as {LARRY_TWITTER_HANDLE}...")
    init_db()

    while True:
        try:
            # 1. Dead man's switch check
            if check_dead_man_switch():
                log.warning("⚠️ DEAD MAN'S SWITCH TRIGGERED — Larry has been silent 48h!")
                tweet_data = ask_larry_for_tweet("DEAD_MAN_SWITCH")
                post_tweet(tweet_data["tweet"], tweet_type="DEAD_MAN_SWITCH")

            # 2. Friday pizza check
            maybe_tweet_pizza()

            # 3. Organic tweet if timing is right
            if should_tweet_now():
                # Pick tweet type based on recent activity
                bankroll = get_bankroll()

                if bankroll < 80:
                    tweet_type = "RANDOM"  # desperate vibes
                else:
                    tweet_type = "RANDOM"

                tweet_data = ask_larry_for_tweet(tweet_type)
                post_tweet(tweet_data["tweet"], tweet_type=tweet_type)

        except KeyboardInterrupt:
            log.info("👋 Twitter agent stopped")
            break
        except Exception as e:
            log.error(f"Error in twitter loop: {e}", exc_info=True)

        # Check every 15 minutes
        log.info("💤 Twitter agent sleeping 15 minutes...")
        time.sleep(15 * 60)


if __name__ == "__main__":
    run_twitter_agent()
