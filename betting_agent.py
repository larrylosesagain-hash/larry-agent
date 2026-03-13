"""
betting_agent.py — Larry places bets on Polymarket
Runs on a loop every 30 minutes:
  1. Fetch open markets from Polymarket
  2. Ask Claude (larry_brain) which ones to bet on
  3. Place bets via CLOB API
  4. Check pending bets for resolutions
  5. Update database + trigger Twitter announcements
"""

import time
import json
import logging
import requests
from datetime import datetime, timedelta
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON

from config import (
    POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER,
    POLYMARKET_HOST, POLYMARKET_GAMMA_API,
    BET_CHECK_INTERVAL_MINUTES, MAX_OPEN_BETS,
    GRANDMA_INJECT_THRESHOLD, GRANDMA_INJECT_AMOUNT,
    BET_CATEGORY_MIX, ABSOLUTE_MIN_BET
)
from database import (
    get_bankroll, set_bankroll, get_pending_bets, save_bet, resolve_bet,
    get_grandma_balance, update_grandma, get_today_tweet_count, init_db
)
from larry_brain import ask_larry_to_bet, ask_larry_for_tweet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BETTING] %(message)s",
    handlers=[logging.StreamHandler()]  # Railway captures stdout automatically
)
log = logging.getLogger(__name__)

# Import twitter agent's post function (shared module)
from twitter_agent import post_tweet


# ─── POLYMARKET CLIENT ────────────────────────────────────────────────────────

def get_clob_client() -> ClobClient:
    # L1 init — needed to derive L2 credentials
    # signature_type=1 for proxy wallet (funder != signer address)
    client = ClobClient(
        host=POLYMARKET_HOST,
        chain_id=POLYGON,
        key=POLYMARKET_PRIVATE_KEY,
        funder=POLYMARKET_FUNDER,
        signature_type=1,
    )
    # Derive L2 creds automatically (required for placing orders)
    # create_or_derive_api_creds() is idempotent — safe to call every startup
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    return client


# ─── FETCH MARKETS ────────────────────────────────────────────────────────────

def fetch_active_markets(limit=20) -> list:
    """Get active markets from Polymarket Gamma API, filtered for Larry."""
    try:
        resp = requests.get(
            f"{POLYMARKET_GAMMA_API}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            },
            timeout=10
        )
        resp.raise_for_status()
        markets = resp.json()

        # Filter: only markets resolving within 7 days (Larry loves short-term)
        filtered = []
        cutoff = datetime.utcnow() + timedelta(days=7)
        for m in markets:
            end_date_str = m.get("endDate") or m.get("end_date_iso")
            if not end_date_str:
                continue
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                if end_date.replace(tzinfo=None) <= cutoff:
                    best_ask = float(m.get("bestAsk", 0.5))
                    best_bid = float(m.get("bestBid", best_ask - 0.02))
                    last_price = float(m.get("lastTradePrice") or best_ask)
                    filtered.append({
                        "condition_id": m.get("conditionId") or m.get("condition_id"),
                        "question": m.get("question"),
                        "end_date": end_date_str,
                        "yes_price": round(best_ask, 4),
                        "no_price": round(1 - best_ask, 4),
                        "spread": round(best_ask - best_bid, 4),      # narrow = liquid market
                        "price_vs_last": round(best_ask - last_price, 4),  # positive = rising
                        "volume_24h": float(m.get("volume24hr", 0)),
                        "category": _guess_category(m.get("question", "")),
                    })
            except (ValueError, TypeError):
                continue

        log.info(f"Fetched {len(filtered)} markets (filtered from {len(markets)})")
        return filtered[:10]  # send max 10 to Claude at once

    except Exception as e:
        log.error(f"Failed to fetch markets: {e}")
        return []


def _guess_category(question: str) -> str:
    """Rough category detection from question text."""
    q = question.lower()
    if any(w in q for w in ["bitcoin", "eth", "crypto", "btc", "sol", "token", "defi"]):
        return "crypto"
    if any(w in q for w in ["trump", "election", "president", "senate", "congress", "vote", "poll"]):
        return "politics"
    if any(w in q for w in ["nba", "nfl", "game", "match", "championship", "league", "score"]):
        return "sports"
    if any(w in q for w in ["ai", "openai", "apple", "google", "microsoft", "launch", "gpt"]):
        return "tech"
    return "weird"


# ─── PLACE BET ────────────────────────────────────────────────────────────────

def place_bet(client: ClobClient, decision: dict) -> bool:
    """
    Execute a bet on Polymarket.
    decision: dict with condition_id, outcome, amount_usdc, etc.
    Returns True if successful.
    """
    condition_id = decision.get("market_id")
    outcome = decision.get("outcome", "YES")
    amount = float(decision.get("amount_usdc", ABSOLUTE_MIN_BET))

    try:
        # Use CLOB client to get market data (more reliable than raw HTTP)
        market_data = client.get_market(condition_id)

        tokens = market_data.get("tokens", [])
        token_id = None
        price = None
        for token in tokens:
            # Case-insensitive: API returns "Yes"/"No", we store "YES"/"NO"
            if token.get("outcome", "").upper() == outcome.upper():
                token_id = token.get("token_id")
                price = float(token.get("price", 0.5))
                break

        if not token_id:
            log.error(f"Could not find token_id for {condition_id} {outcome}")
            return False

        # FIX: added side="BUY" — was missing, caused TypeError
        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=round(amount, 2),
            side="BUY",
        )

        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)  # Good Till Cancelled — FOK fails silently when Gamma prices are stale

        if resp.get("success"):
            log.info(f"✅ BET PLACED: {outcome} on {condition_id} for ${amount}")
            return True
        else:
            log.error(f"❌ Order failed: {resp.get('errorMsg', resp.get('error', 'unknown error'))}")
            return False

    except TypeError as te:
        # TypeError is safe to log (no sensitive data in type errors)
        log.error(f"TypeError placing bet: {te}")
        return False
    except Exception as e:
        # SECURITY: log only exception type, not message (may contain wallet data)
        log.error(f"Exception placing bet: {type(e).__name__}")
        return False


# ─── CHECK RESOLVED BETS ──────────────────────────────────────────────────────

def check_pending_bets(client: ClobClient):
    """Check if any pending bets have resolved."""
    pending = get_pending_bets()
    if not pending:
        return

    log.info(f"Checking {len(pending)} pending bets...")

    for bet in pending:
        try:
            resp = requests.get(
                f"{POLYMARKET_HOST}/markets/{bet['polymarket_id']}",
                timeout=10
            )
            market = resp.json()

            # Check if market is resolved
            if not market.get("closed", False):
                continue  # still open

            # Find our outcome's result
            tokens = market.get("tokens", [])
            for token in tokens:
                if token.get("outcome", "").upper() == bet["outcome"].upper():
                    price = float(token.get("price", 0))
                    won = price >= 0.99  # price goes to 1.0 on win

                    payout = bet["potential_payout"] if won else 0.0
                    resolve_bet(bet["polymarket_id"], won, payout)

                    # Update bankroll
                    bankroll = get_bankroll()
                    if won:
                        new_balance = bankroll + payout
                        set_bankroll(new_balance, payout, "WIN")
                        log.info(f"🎉 WON ${payout:.2f}! New bankroll: ${new_balance:.2f}")
                    else:
                        # Bet amount was already deducted when placing
                        new_balance = bankroll
                        log.info(f"💀 LOST ${bet['amount_usdc']:.2f}. Bankroll: ${new_balance:.2f}")

                    # Ask Larry to react and tweet
                    try:
                        tweet_data = ask_larry_for_tweet(
                            "WIN" if won else "LOSS",
                            extra_data=bet
                        )
                        post_tweet(
                            tweet_data["tweet"],
                            tweet_type="WIN" if won else "LOSS",
                            bet_id=bet["id"]
                        )
                    except Exception as e:
                        log.error(f"Failed to post resolution tweet: {e}")

                    break

        except Exception as e:
            log.error(f"Error checking bet {bet['polymarket_id']}: {e}")


# ─── GRANDMA WALLET CHECK ────────────────────────────────────────────────────

def check_grandma_wallet():
    """Inject from Grandma's Wallet if bankroll is critically low."""
    bankroll = get_bankroll()
    grandma_balance = get_grandma_balance()

    if bankroll < GRANDMA_INJECT_THRESHOLD and grandma_balance >= GRANDMA_INJECT_AMOUNT:
        log.info(f"👵 GRANDMA WALLET ACTIVATED! Bankroll ${bankroll:.2f} < ${GRANDMA_INJECT_THRESHOLD}")

        inject_amount = min(GRANDMA_INJECT_AMOUNT, grandma_balance)
        new_balance = bankroll + inject_amount
        set_bankroll(new_balance, inject_amount, "GRANDMA")
        update_grandma("INJECT", inject_amount, f"Bankroll was ${bankroll:.2f}")

        # Tweet about it immediately
        try:
            tweet_data = ask_larry_for_tweet("GRANDMA", extra_data={"amount": inject_amount})
            post_tweet(tweet_data["tweet"], tweet_type="GRANDMA")
        except Exception as e:
            log.error(f"Failed to post grandma tweet: {e}")

        log.info(f"✅ Grandma injected ${inject_amount:.2f}. New bankroll: ${new_balance:.2f}")


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def run_betting_agent():
    log.info("🎰 Larry's Betting Agent starting up...")
    init_db()

    client = get_clob_client()

    while True:
        try:
            log.info("--- Betting cycle starting ---")

            # 1. Check if pending bets resolved
            check_pending_bets(client)

            # 2. Check if Grandma needs to intervene
            check_grandma_wallet()

            # 3. Check how many open bets we have
            open_bets = get_pending_bets()
            if len(open_bets) >= MAX_OPEN_BETS:
                log.info(f"Max open bets reached ({MAX_OPEN_BETS}), skipping new bets")
            else:
                # 4. Fetch markets
                markets = fetch_active_markets(limit=20)

                if markets:
                    # 5. Ask Claude / Larry which ones to bet on
                    decisions = ask_larry_to_bet(markets)
                    log.info(f"Larry made {len(decisions)} decisions")

                    for decision in decisions:
                        if decision.get("decision") != "BET":
                            log.info(f"PASS: {decision.get('reason', 'no reason given')}")
                            continue

                        if len(get_pending_bets()) >= MAX_OPEN_BETS:
                            log.info("Reached max open bets mid-loop, stopping")
                            break

                        # 6. Deduct bet amount from bankroll first
                        bankroll = get_bankroll()
                        amount = float(decision["amount_usdc"])
                        if amount > bankroll:
                            log.warning(f"Not enough bankroll (${bankroll:.2f}) for ${amount:.2f} bet")
                            continue

                        # 7. Place the bet
                        success = place_bet(client, decision)

                        if success:
                            # Deduct from bankroll
                            new_balance = bankroll - amount
                            set_bankroll(new_balance, -amount, "BET_PLACED")

                            # Save to DB
                            market_info = next(
                                (m for m in markets if m["condition_id"] == decision["market_id"]),
                                {}
                            )
                            odds = market_info.get("yes_price" if decision["outcome"] == "YES" else "no_price", 0.5)
                            potential_payout = amount / odds if odds > 0 else amount * 2

                            bet_id = save_bet(
                                polymarket_id=decision["market_id"],
                                question=market_info.get("question", "Unknown market"),
                                outcome=decision["outcome"],
                                amount=amount,
                                odds=odds,
                                potential_payout=potential_payout,
                                category=market_info.get("category", "weird"),
                                larry_comment=decision.get("reasoning", ""),
                            )

                            # 8. Tweet the bet announcement
                            try:
                                tweet_text = decision.get("larry_tweet", "")
                                if tweet_text:
                                    tweet_id = post_tweet(tweet_text, tweet_type="NEW_BET", bet_id=bet_id)
                                    log.info(f"Tweeted bet announcement: {tweet_id}")
                            except Exception as e:
                                log.error(f"Failed to tweet bet: {e}")

        except KeyboardInterrupt:
            log.info("👋 Betting agent stopped by user")
            break
        except Exception as e:
            # SECURITY: no exc_info (traceback can expose env vars/keys)
            log.error(f"Unexpected error in betting loop: {type(e).__name__}: {e}")

        # Wait before next cycle
        log.info(f"💤 Sleeping {BET_CHECK_INTERVAL_MINUTES} minutes...")
        time.sleep(BET_CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    run_betting_agent()
