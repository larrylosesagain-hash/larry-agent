"""
main.py — Larry's entry point
Starts Twitter and Betting agents in separate threads.
SIGTERM/SIGINT are handled here (main thread only) and propagated to both agents.
"""

import sys
import time
import signal
import threading
import logging

from twitter_agent import run_twitter_agent, set_twitter_shutdown, is_twitter_shutdown
from betting_agent import run_betting_agent, set_betting_shutdown, is_betting_shutdown

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MAIN] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)


def _handle_sigterm(signum, frame):
    """
    Railway sends SIGTERM before killing the container.
    Signal handlers MUST be registered in the main thread — that's why this
    lives here and not inside the agent threads.
    """
    log.info("🛑 SIGTERM received — gracefully stopping both agents...")
    set_twitter_shutdown()
    set_betting_shutdown()


def run_twitter():
    """Wraps run_twitter_agent with automatic restart on crash."""
    while True:
        try:
            run_twitter_agent()
            return  # clean exit — shutdown was requested
        except Exception as e:
            if is_twitter_shutdown():
                return
            log.error(f"Twitter agent crashed: {type(e).__name__}: {e} — restarting in 60s")
            time.sleep(60)


def run_betting():
    """Wraps run_betting_agent with automatic restart on crash.
    Railway containers start before the network is fully up — the CLOB client
    may fail with a PolyApiException on the very first connect attempt.
    Without this loop that crash would kill the betting thread permanently.
    """
    while True:
        try:
            run_betting_agent()
            return  # clean exit — shutdown was requested
        except Exception as e:
            if is_betting_shutdown():
                return
            log.error(f"Betting agent crashed: {type(e).__name__}: {e} — restarting in 60s")
            time.sleep(60)


if __name__ == "__main__":
    log.info("🚀 Larry is waking up...")

    # Register graceful shutdown — MUST happen in main thread
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)   # Ctrl+C in local dev

    twitter_thread = threading.Thread(target=run_twitter, name="Twitter", daemon=True)
    betting_thread = threading.Thread(target=run_betting, name="Betting", daemon=True)

    twitter_thread.start()
    log.info("🐦 Twitter agent started")

    betting_thread.start()
    log.info("🎰 Betting agent started")

    # Block main thread — agents run until SIGTERM sets their shutdown flags
    twitter_thread.join()
    betting_thread.join()

    log.info("✅ Larry shut down cleanly")
