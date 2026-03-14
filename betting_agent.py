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
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON

# ─── POLYGON / CTF CONSTANTS ──────────────────────────────────────────────────
# Gnosis Conditional Token Framework contract on Polygon (same address all chains)
_CTF_ADDRESS  = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
# USDC.e on Polygon (bridged) — what Polymarket settles in
_USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
_POLYGON_RPC  = "https://polygon-rpc.com"
_CTF_ABI = [{
    "inputs": [
        {"name": "collateralToken",    "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId",        "type": "bytes32"},
        {"name": "indexSets",          "type": "uint256[]"},
    ],
    "name": "redeemPositions",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function",
}]

from config import (
    POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER,
    POLYMARKET_HOST, POLYMARKET_GAMMA_API,
    BET_CHECK_INTERVAL_MINUTES,
    GRANDMA_INJECT_THRESHOLD, GRANDMA_INJECT_AMOUNT,
    ABSOLUTE_MIN_BET
)
from database import (
    get_bankroll, set_bankroll, get_pending_bets, save_bet, resolve_bet,
    get_grandma_balance, update_grandma, init_db
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


def sync_bankroll_from_clob(client: ClobClient):
    """
    Sync DB bankroll with actual USDC balance from CLOB.
    Called at startup so Larry knows his real balance, not just what the DB thinks.
    NOTE: This reflects trading allowance (approved for CLOB), not total wallet balance.
    Unclaimed winnings may not appear here until claimed on polymarket.com.
    """
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        # AssetType.USDC was renamed to COLLATERAL in newer versions of py_clob_client
        asset = getattr(AssetType, "COLLATERAL", None) or getattr(AssetType, "USDC", None)
        result = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=asset)
        )
        # balance is returned as a string of raw units (6 decimals for USDC)
        raw = result.get("balance", "0")
        real_balance = float(raw) / 1_000_000  # convert from microUSDC to USDC
        if real_balance > 0:
            db_balance = get_bankroll()
            if abs(real_balance - db_balance) > 1.0:  # only sync if diff > $1
                log.info(f"💰 Balance sync: DB=${db_balance:.2f} → CLOB=${real_balance:.2f}")
                set_bankroll(real_balance, real_balance - db_balance, "SYNC")
            else:
                log.info(f"💰 Balance OK: DB=${db_balance:.2f}, CLOB=${real_balance:.2f}")
    except Exception as e:
        log.warning(f"Balance sync failed ({type(e).__name__}: {e}) — using DB balance")


# ─── AUTO-CLAIM WINNINGS ──────────────────────────────────────────────────────

def claim_winnings(condition_id: str, outcome: str, payout: float) -> bool:
    """
    Redeem winning CTF positions on-chain via Polygon.
    Calls redeemPositions() on the Gnosis CTF contract — this is what
    'claiming' means on Polymarket. Works with the same private key used for trading.

    indexSets encoding (binary CTF):
      YES position = index set 1  (binary: 01)
      NO  position = index set 2  (binary: 10)
    """
    try:
        w3 = Web3(Web3.HTTPProvider(_POLYGON_RPC))
        if not w3.is_connected():
            log.error("Cannot connect to Polygon RPC for claim")
            return False

        account = w3.eth.account.from_key(POLYMARKET_PRIVATE_KEY)
        ctf = w3.eth.contract(address=_CTF_ADDRESS, abi=_CTF_ABI)

        # condition_id may have 0x prefix — strip it for bytes conversion
        cid_hex = condition_id.replace("0x", "").zfill(64)
        condition_bytes = bytes.fromhex(cid_hex)
        parent_collection = b"\x00" * 32  # parentCollectionId = bytes32(0)

        index_sets = [1] if outcome.upper() == "YES" else [2]

        tx = ctf.functions.redeemPositions(
            _USDC_ADDRESS,
            parent_collection,
            condition_bytes,
            index_sets,
        ).build_transaction({
            "from":     account.address,
            "nonce":    w3.eth.get_transaction_count(account.address),
            "gas":      250_000,
            "gasPrice": w3.eth.gas_price,
            "chainId":  137,  # Polygon mainnet
        })

        signed   = w3.eth.account.sign_transaction(tx, POLYMARKET_PRIVATE_KEY)
        tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt  = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status == 1:
            log.info(f"✅ Auto-claimed ${payout:.2f}! TX: {tx_hash.hex()}")
            return True
        else:
            log.error(f"❌ Claim tx reverted. TX: {tx_hash.hex()} — claim manually on polymarket.com")
            return False

    except Exception as e:
        log.warning(f"Auto-claim failed ({type(e).__name__}: {e}) — claim manually on polymarket.com")
        return False


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

        now = datetime.utcnow()
        cutoff = now + timedelta(days=21)  # 21 days — catches Oscars, big sports events, etc.
        filtered = []

        for m in markets:
            end_date_str = m.get("endDate") or m.get("end_date_iso")
            if not end_date_str:
                continue
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                end_date_naive = end_date.replace(tzinfo=None)

                # FIX: was `<= cutoff` which passed expired markets too (end_date < now)
                # Now: only markets that haven't expired yet AND are within 7 days
                if end_date_naive <= now:
                    continue  # already expired — skip before wasting Claude tokens
                if end_date_naive > cutoff:
                    continue  # too far in the future

                # Neg-risk = multi-outcome market (Oscars, championships etc)
                # Gamma API doesn't give per-outcome prices — fetch from CLOB instead
                if m.get("negRisk") or m.get("neg_risk"):
                    cond_id = m.get("conditionId") or m.get("condition_id")
                    vol = float(m.get("volume24hr", 0))
                    cat = _guess_category(m.get("question", ""))
                    try:
                        clob_resp = requests.get(
                            f"{POLYMARKET_HOST}/markets/{cond_id}", timeout=5
                        )
                        tokens = clob_resp.json().get("tokens", [])
                        for t in tokens:
                            if not isinstance(t, dict):
                                continue
                            t_name = t.get("outcome", "")
                            t_price = float(t.get("price", 0.5))
                            # Skip YES/NO tokens (binary markets can appear in neg-risk groups)
                            if not t_name or t_name.lower() in ("yes", "no"):
                                continue
                            if t_price >= 0.97 or t_price <= 0.03:
                                continue
                            filtered.append({
                                "condition_id": cond_id,
                                "question": m.get("question"),
                                "end_date": end_date_str,
                                "yes_price": round(t_price, 4),
                                "outcome_name": t_name,  # Claude uses this as the outcome field
                                "neg_risk": True,
                                "volume_24h": vol,
                                "category": cat,
                            })
                    except Exception:
                        pass  # if CLOB fetch fails, skip this neg-risk market silently
                    continue  # skip normal binary processing below

                best_ask = float(m.get("bestAsk", 0.5))
                best_bid = float(m.get("bestBid", best_ask - 0.02))
                last_price = float(m.get("lastTradePrice") or best_ask)

                # skip near-resolved markets (price at floor/ceiling = effectively resolved)
                if best_ask >= 0.97 or best_ask <= 0.03:
                    continue

                filtered.append({
                    "condition_id": m.get("conditionId") or m.get("condition_id"),
                    "question": m.get("question"),
                    "end_date": end_date_str,
                    "yes_price": round(best_ask, 4),
                    "spread": round(best_ask - best_bid, 4),
                    "price_vs_last": round(best_ask - last_price, 4),
                    "volume_24h": float(m.get("volume24hr", 0)),
                    "category": _guess_category(m.get("question", "")),
                })
            except (ValueError, TypeError):
                continue

        # Sort: entertainment/culture first (less efficient = more opportunities),
        # then by volume (liquid = easier to fill orders)
        def sort_key(m):
            cat_priority = 0 if m["category"] in ("entertainment", "sports", "weird") else 1
            return (cat_priority, -m["volume_24h"])
        filtered.sort(key=sort_key)

        log.info(f"Fetched {len(filtered)} live markets (filtered from {len(markets)})")
        return filtered[:15]  # top 15 after sorting

    except Exception as e:
        log.error(f"Failed to fetch markets: {e}")
        return []


def _guess_category(question: str) -> str:
    """Rough category detection from question text."""
    q = question.lower()
    if any(w in q for w in ["bitcoin", "eth", "crypto", "btc", "sol", "token", "defi", "coin"]):
        return "crypto"
    if any(w in q for w in ["trump", "election", "president", "senate", "congress", "vote", "poll", "biden", "harris"]):
        return "politics"
    if any(w in q for w in ["nba", "nfl", "nhl", "mlb", "game", "match", "championship", "league", "score", "cup", "tournament", "playoff", "soccer", "football", "basketball", "tennis", "ufc", "boxing"]):
        return "sports"
    if any(w in q for w in ["ai", "openai", "apple", "google", "microsoft", "launch", "gpt", "model", "nvidia"]):
        return "tech"
    if any(w in q for w in ["oscar", "emmy", "grammy", "golden globe", "award", "movie", "film", "actor", "actress", "director", "box office", "celebrity", "music", "album", "song", "billboard", "spotify", "netflix", "tv show"]):
        return "entertainment"
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
    amount = max(amount, 5.0)  # Polymarket enforces $5 minimum order size

    try:
        # Use CLOB client to get market data (more reliable than raw HTTP)
        market_data = client.get_market(condition_id)

        tokens = market_data.get("tokens", [])
        token_id = None
        price = None
        for token in tokens:
            token_outcome = token.get("outcome", "")
            # Works for both binary (YES/NO) and neg-risk (named outcomes like "Demi Moore")
            if token_outcome.lower() == outcome.lower():
                token_id = token.get("token_id")
                price = float(token.get("price", 0.5))
                break

        if not token_id:
            log.info(f"Skipping {condition_id[:16]}... — no '{outcome}' token found")
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
        log.error(f"TypeError placing bet: {te}")
        return False
    except Exception as e:
        # PolyApiException message is safe (HTTP error details, no wallet data)
        log.error(f"Exception placing bet ({type(e).__name__}): {e}")
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
            resp.raise_for_status()
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
                        # Auto-claim: call redeemPositions on Polygon CTF contract
                        claim_winnings(
                            condition_id=bet["polymarket_id"],
                            outcome=bet["outcome"],
                            payout=payout,
                        )
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

    # Sync real CLOB balance at startup so Larry knows his actual bankroll
    sync_bankroll_from_clob(client)

    while True:
        try:
            log.info("--- Betting cycle starting ---")

            # 1. Check if pending bets resolved
            check_pending_bets(client)

            # 2. Check if Grandma needs to intervene
            check_grandma_wallet()

            # 3. Check bankroll exposure — stop placing new bets if 80%+ is already in play
            open_bets = get_pending_bets()
            bankroll = get_bankroll()
            open_exposure = sum(float(b.get("amount_usdc", 0)) for b in open_bets)
            max_exposure = bankroll * 0.80
            if open_exposure >= max_exposure:
                log.info(f"Exposure limit reached: ${open_exposure:.2f} of ${bankroll:.2f} in play ({open_exposure/bankroll*100:.0f}%), skipping new bets")
            else:
                # 4. Fetch markets
                markets = fetch_active_markets(limit=100)

                if markets:
                    # 5. Ask Claude / Larry which ones to bet on
                    decisions = ask_larry_to_bet(markets)
                    log.info(f"Larry made {len(decisions)} decisions")

                    for decision in decisions:
                        if decision.get("decision") != "BET":
                            log.info(f"PASS: {decision.get('reasoning', 'no reason given')}")
                            continue

                        current_exposure = sum(float(b.get("amount_usdc", 0)) for b in get_pending_bets())
                        if current_exposure >= max_exposure:
                            log.info(f"Reached exposure limit mid-loop (${current_exposure:.2f}), stopping")
                            break

                        # Resolve market_info once — reused for DB save and odds
                        # For neg-risk markets multiple entries share condition_id,
                        # so also match on outcome_name
                        market_id = decision.get("market_id")
                        decision_outcome = decision.get("outcome", "")

                        # Skip if we already have an open bet on this exact market
                        already_open = any(
                            b.get("polymarket_id") == market_id
                            for b in get_pending_bets()
                        )
                        if already_open:
                            log.info(f"Already have open bet on {market_id[:16]}..., skipping")
                            continue
                        market_info = next(
                            (m for m in markets if m["condition_id"] == market_id
                             and (not m.get("neg_risk") or
                                  m.get("outcome_name", "").lower() == decision_outcome.lower())),
                            {}
                        )

                        # 6. Deduct bet amount from bankroll first
                        bankroll = get_bankroll()
                        amount = float(decision.get("amount_usdc", ABSOLUTE_MIN_BET))
                        if amount > bankroll:
                            log.warning(f"Not enough bankroll (${bankroll:.2f}) for ${amount:.2f} bet")
                            continue

                        # 7. Place the bet
                        success = place_bet(client, decision)

                        if success:
                            # Deduct from bankroll
                            new_balance = bankroll - amount
                            set_bankroll(new_balance, -amount, "BET_PLACED")

                            # market_info already resolved above — no duplicate lookup
                            # no_price was removed from market dict; derive it
                            if decision.get("outcome") == "YES":
                                odds = market_info.get("yes_price", 0.5)
                            else:
                                odds = round(1 - market_info.get("yes_price", 0.5), 4)
                            potential_payout = amount / odds if odds > 0 else amount * 2

                            bet_id = save_bet(
                                polymarket_id=market_id,
                                question=market_info.get("question", "Unknown market"),
                                outcome=decision.get("outcome", "YES"),
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
