"""
main.py — Launch both Larry agents in parallel threads
Railway runs this single file, which starts betting + twitter agents.
"""

import threading
import logging
import time
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

RESTART_DELAY_SECONDS = 30  # wait before restarting a dead thread


def run_twitter():
    from twitter_agent import run_twitter_agent
    run_twitter_agent()


def run_betting():
    from betting_agent import run_betting_agent
    run_betting_agent()


def seed_bankroll():
    """Seed $100 bankroll on very first startup."""
    from database import init_db, get_bankroll, set_bankroll, get_state, set_state
    init_db()
    if get_state("bankroll_seeded") != "true":
        if get_bankroll() == 0.0:
            set_bankroll(100.0, 100.0, "INITIAL_DEPOSIT")
            set_state("bankroll_seeded", "true")
            log.info("💰 Bankroll seeded: $100.00 — Larry is ready to lose money!")
        else:
            set_state("bankroll_seeded", "true")  # bankroll already exists


if __name__ == "__main__":
    log.info("🚀 Larry is waking up...")

    # Seed bankroll once before agents start
    seed_bankroll()

    t_twitter = threading.Thread(target=run_twitter, name="Twitter", daemon=False)
    t_betting = threading.Thread(target=run_betting, name="Betting", daemon=False)

    t_twitter.start()
    log.info("🐦 Twitter agent started")

    t_betting.start()
    log.info("🎰 Betting agent started")

    # Watchdog: restart any thread that dies, with delay to avoid crash loops
    while True:
        time.sleep(60)

        if not t_twitter.is_alive():
            log.error(f"⚠️ Twitter agent died — restarting in {RESTART_DELAY_SECONDS}s...")
            time.sleep(RESTART_DELAY_SECONDS)
            t_twitter = threading.Thread(target=run_twitter, name="Twitter", daemon=False)
            t_twitter.start()
            log.info("🐦 Twitter agent restarted")

        if not t_betting.is_alive():
            log.error(f"⚠️ Betting agent died — restarting in {RESTART_DELAY_SECONDS}s...")
            time.sleep(RESTART_DELAY_SECONDS)
            t_betting = threading.Thread(target=run_betting, name="Betting", daemon=False)
            t_betting.start()
            log.info("🎰 Betting agent restarted")
