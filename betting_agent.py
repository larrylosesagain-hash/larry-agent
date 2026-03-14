"""
betting_agent.py — Larry places bets on Polymarket
Runs on a loop every 30 minutes:
  1. Fetch open markets from Polymarket
  2. Ask Claude (larry_brain) which ones to bet on
  3. Place bets via CLOB API
  4. Check pending bets for resolutions
  5. Update database + trigger Twitter announcements
"""

import sys
import time
import json
import signal
import random
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    get_grandma_balance, update_grandma, init_db, get_state, set_state
)
from larry_brain import ask_larry_to_bet, ask_larry_for_tweet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BETTING] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# Token-not-found blacklist: maps condition_id → expiry datetime (6h TTL)
# Markets with no tradeable tokens are skipped until TTL expires — avoids wasting
# Claude tokens on them, but allows retry in case tokens are added later.
_token_not_found_blacklist: dict = {}
_TOKEN_BLACKLIST_TTL_HOURS = 6

def _blacklist_token(condition_id: str):
    _token_not_found_blacklist[condition_id] = datetime.utcnow() + timedelta(hours=_TOKEN_BLACKLIST_TTL_HOURS)

def _is_token_blacklisted(condition_id: str) -> bool:
    expiry = _token_not_found_blacklist.get(condition_id)
    if expiry is None:
        return False
    if datetime.utcnow() > expiry:
        del _token_not_found_blacklist[condition_id]  # expired — remove and allow retry
        return False
    return True

# PASS cache: markets Claude already decided to skip this session.
# Re-sends the market only if something meaningful changed:
#   - price moved >5% (new information)
#   - <4 hours left (urgency spike)
#   - 6h TTL expired (market may have evolved)
# Saves ~30-40% of Claude tokens by not re-analyzing identical markets every cycle.
_pass_cache: dict = {}  # condition_id → {"passed_at": datetime, "price": float, "hours_to_end": int}


def _cache_pass(condition_id: str, yes_price: float, hours_to_end: int):
    _pass_cache[condition_id] = {
        "passed_at": datetime.utcnow(),
        "price": yes_price,
        "hours_to_end": hours_to_end,
    }


def _is_pass_cached(market: dict) -> bool:
    """Return True if Claude already passed on this market and nothing meaningful changed."""
    cid = market["condition_id"]
    entry = _pass_cache.get(cid)
    if not entry:
        return False

    # TTL expired — allow retry
    if datetime.utcnow() - entry["passed_at"] > timedelta(hours=6):
        del _pass_cache[cid]
        return False

    # Price moved >5% — new information, worth re-analysing
    current_price = market.get("yes_price", 0.5)
    if abs(current_price - entry["price"]) > 0.05:
        del _pass_cache[cid]
        return False

    # Market became urgent since last PASS — re-examine
    if market.get("hours_to_end", 24) <= 4 and entry["hours_to_end"] > 4:
        del _pass_cache[cid]
        return False

    return True  # nothing changed — skip


# Rotating page counter — persisted in DB so restarts continue where they left off
# (otherwise Larry always restarts at page 0 and misses pages 1-9)
_scan_page: int = 0

def _load_scan_page():
    global _scan_page
    try:
        val = get_state("scan_page")
        _scan_page = int(val) if val else 0
    except Exception:
        _scan_page = 0

def _save_scan_page():
    try:
        set_state("scan_page", str(_scan_page))
    except Exception:
        pass

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

def _fetch_gamma_raw(order: str, ascending: bool, limit: int, offset: int = 0,
                     end_min: str = None, end_max: str = None) -> list:
    """
    Single Gamma API call — returns raw market list or [] on failure.
    end_min / end_max: ISO8601 strings for server-side date filtering.
    Using these means ALL returned markets are already within the window —
    no wasted quota on far-future markets.
    """
    try:
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": order,
            "ascending": "true" if ascending else "false",
        }
        if offset > 0:
            params["offset"] = offset
        if end_min:
            params["endDateMin"] = end_min
        if end_max:
            params["endDateMax"] = end_max
        resp = requests.get(f"{POLYMARKET_GAMMA_API}/markets", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f"Gamma fetch failed ({order}, asc={ascending}, offset={offset}): {e}")
        return []


def fetch_active_markets() -> list:
    """
    FULL POLYMARKET SCAN — three parallel Gamma queries every cycle:

      1. ANCHOR  — top 200 by 24h volume, offset=0
                   Always the most liquid same-day markets (sports, crypto dailies)

      2. SCAN    — top 200 by volume, rotating offset (_scan_page × 200)
                   Walks through ALL of Polymarket over ~15 cycles (7.5 hours).
                   Cycle 0 = markets 0-200, cycle 1 = 200-400, ... cycle 14 = 2800-3000
                   Larry sees every corner of the platform daily.

      3. FRESH   — newest 100 by createdAt desc, offset=0
                   Brand-new markets often have mispriced odds (no one's bet on them yet).

    Window: ONLY markets resolving within 24 hours — same-day resolution only.
    Claude sees: 12 anchor + 8 scan + 5 fresh = 25 markets per cycle.
    """
    global _scan_page
    now = datetime.utcnow()
    cutoff = now + timedelta(hours=24)   # TODAY only — must resolve within 24h
    min_time = now + timedelta(minutes=30)  # skip markets resolving in under 30min (too late to fill)

    scan_offset = _scan_page * 500
    _scan_page = (_scan_page + 1) % 10   # 10 pages × 500 = 5000 markets per rotation (~5h)
    _save_scan_page()  # persist so restarts continue from current position

    # Server-side date filters — Gamma only returns markets within 24h window.
    # This means limit=500 quota is spent entirely on relevant markets, not far-future noise.
    end_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_max = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Three parallel fetches — wall time = slowest single request, not sum of all three
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_anchor = ex.submit(_fetch_gamma_raw, "volume24hr", False, 500, 0,            end_min, end_max)
        f_scan   = ex.submit(_fetch_gamma_raw, "volume24hr", False, 500, scan_offset,  end_min, end_max)
        f_fresh  = ex.submit(_fetch_gamma_raw, "createdAt",  False, 200, 0,            end_min, end_max)
        raw_anchor = f_anchor.result()
        raw_scan   = f_scan.result()
        raw_fresh  = f_fresh.result()

    def parse_strict(raw):
        """Parse with tighter time filter: resolves within 24h AND not in the next 30min."""
        out = []
        for m in raw:
            end_date_str = m.get("endDate") or m.get("end_date_iso")
            if not end_date_str:
                continue
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                end_date_naive = end_date.replace(tzinfo=None)
                if end_date_naive <= min_time:
                    continue  # already resolved or resolves too soon
                if end_date_naive > cutoff:
                    continue  # further than 24h out

                delta = end_date_naive - now
                days_to_end  = delta.days
                hours_to_end = int(delta.total_seconds() // 3600)  # FIX: .seconds only gives 0-86399s component

                if m.get("negRisk") or m.get("neg_risk"):
                    cond_id = m.get("conditionId") or m.get("condition_id")
                    vol = float(m.get("volume24hr", 0))
                    cat = _guess_category(m.get("question", ""))
                    try:
                        clob_resp = requests.get(f"{POLYMARKET_HOST}/markets/{cond_id}", timeout=5)
                        tokens = clob_resp.json().get("tokens", [])
                        for t in tokens:
                            if not isinstance(t, dict):
                                continue
                            t_name = t.get("outcome", "")
                            t_price = float(t.get("price", 0.5))
                            if not t_name or t_name.lower() in ("yes", "no"):
                                continue
                            if t_price >= 0.97 or t_price <= 0.03:
                                continue
                            out.append({
                                "condition_id": cond_id,
                                "question": m.get("question"),
                                "end_date": end_date_str,
                                "days_to_end": days_to_end,
                                "hours_to_end": hours_to_end,
                                "yes_price": round(t_price, 4),
                                "outcome_name": t_name,
                                "neg_risk": True,
                                "volume_24h": vol,
                                "category": cat,
                            })
                    except Exception:
                        pass
                    continue

                best_ask = float(m.get("bestAsk", 0.5))
                best_bid = float(m.get("bestBid", best_ask - 0.02))
                last_price = float(m.get("lastTradePrice") or best_ask)
                if best_ask >= 0.97 or best_ask <= 0.03:
                    continue

                out.append({
                    "condition_id": m.get("conditionId") or m.get("condition_id"),
                    "question": m.get("question"),
                    "end_date": end_date_str,
                    "days_to_end": days_to_end,
                    "hours_to_end": hours_to_end,
                    "yes_price": round(best_ask, 4),
                    "spread": round(best_ask - best_bid, 4),
                    "price_vs_last": round(best_ask - last_price, 4),
                    "volume_24h": float(m.get("volume24hr", 0)),
                    "category": _guess_category(m.get("question", "")),
                })
            except (ValueError, TypeError):
                continue
        return out

    anchor = parse_strict(raw_anchor)
    scan   = parse_strict(raw_scan)
    fresh  = parse_strict(raw_fresh)

    # Deduplicate: scan and fresh shouldn't repeat what anchor already has
    anchor_ids = {m["condition_id"] for m in anchor}
    scan  = [m for m in scan  if m["condition_id"] not in anchor_ids]
    all_ids = anchor_ids | {m["condition_id"] for m in scan}
    fresh = [m for m in fresh if m["condition_id"] not in all_ids]

    # Anchor: sort by hours_to_end (most urgent first), then sports/entertainment, then volume
    def sort_key(m):
        h = m.get("hours_to_end", 24)
        time_tier = 0 if h <= 4 else (1 if h <= 12 else 2)
        cat_priority = 0 if m["category"] in ("entertainment", "sports", "weird") else 1
        return (time_tier, cat_priority, -m["volume_24h"])
    anchor.sort(key=sort_key)

    # Scan + fresh: random shuffle — different obscure markets each cycle
    random.shuffle(scan)
    random.shuffle(fresh)

    combined = anchor + scan + fresh
    random.shuffle(combined)  # mix so Claude doesn't bias by list position

    log.info(
        f"🔎 Scan page {_scan_page-1 if _scan_page > 0 else 9}/10 (offset={scan_offset}) | "
        f"anchor={len(anchor)} scan={len(scan)} fresh={len(fresh)} → "
        f"sending all {len(combined)} to Claude"
    )
    return combined


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
            _blacklist_token(condition_id)  # 6h TTL — retried after expiry
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

def _check_single_bet(bet: dict) -> dict | None:
    """
    Check one pending bet against CLOB. Returns resolution dict or None if still open.
    Runs in a thread — no shared state written here, only reads.
    """
    try:
        resp = requests.get(
            f"{POLYMARKET_HOST}/markets/{bet['polymarket_id']}",
            timeout=10
        )
        resp.raise_for_status()
        market = resp.json()
        if not market.get("closed", False):
            return None  # still open

        tokens = market.get("tokens", [])
        for token in tokens:
            if token.get("outcome", "").upper() == bet["outcome"].upper():
                price = float(token.get("price", 0))
                won = price >= 0.99
                return {"bet": bet, "won": won, "payout": bet["potential_payout"] if won else 0.0}
    except Exception as e:
        log.error(f"Error checking bet {bet['polymarket_id']}: {e}")
    return None


def check_pending_bets(client: ClobClient):
    """
    Check all pending bets in parallel — N sequential requests → 1 parallel batch.
    FIX: claim_winnings now runs BEFORE resolve_bet + set_bankroll to prevent
    bankroll inflation when USDC hasn't actually been claimed yet.
    """
    pending = get_pending_bets()
    if not pending:
        return

    log.info(f"Checking {len(pending)} pending bets...")

    # Parallel CLOB checks — all reads, safe to run concurrently
    resolved = []
    with ThreadPoolExecutor(max_workers=min(len(pending), 8)) as ex:
        futures = {ex.submit(_check_single_bet, bet): bet for bet in pending}
        for future in as_completed(futures, timeout=30):
            result = future.result()
            if result:
                resolved.append(result)

    for r in resolved:
        bet = r["bet"]
        won = r["won"]
        payout = r["payout"]

        if won:
            # FIX: CLAIM FIRST — then resolve + update bankroll.
            # Old order: resolve → update bankroll → claim
            # Problem: if claim fails, DB says WON and bankroll shows money Larry can't spend.
            # New order: claim → resolve → update bankroll
            # If claim fails: still mark WON (it IS won), log prominently for manual claim.
            claimed = claim_winnings(
                condition_id=bet["polymarket_id"],
                outcome=bet["outcome"],
                payout=payout,
            )
            resolve_bet(bet["polymarket_id"], True, payout)
            bankroll = get_bankroll()
            new_balance = bankroll + payout
            set_bankroll(new_balance, payout, "WIN")
            if claimed:
                log.info(f"🎉 WON ${payout:.2f} + auto-claimed! New bankroll: ${new_balance:.2f}")
            else:
                log.warning(f"🎉 WON ${payout:.2f} but auto-claim failed — claim manually on polymarket.com. Bankroll: ${new_balance:.2f}")
        else:
            resolve_bet(bet["polymarket_id"], False, 0.0)
            bankroll = get_bankroll()
            log.info(f"💀 LOST ${bet['amount_usdc']:.2f}. Bankroll: ${bankroll:.2f}")

        # Tweet the result
        try:
            tweet_data = ask_larry_for_tweet("WIN" if won else "LOSS", extra_data=bet)
            post_tweet(tweet_data["tweet"], tweet_type="WIN" if won else "LOSS", bet_id=bet["id"])
        except Exception as e:
            log.error(f"Failed to post resolution tweet: {e}")


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

_betting_shutdown = False


def set_betting_shutdown():
    """Called by main.py SIGTERM handler (must run in main thread)."""
    global _betting_shutdown
    _betting_shutdown = True
    log.info("🛑 Betting agent shutdown requested — will exit after current cycle")


def run_betting_agent():
    log.info("🎰 Larry's Betting Agent starting up...")
    init_db()

    # Restore scan page from DB so rotation continues across restarts
    _load_scan_page()
    log.info(f"📖 Scan page restored: {_scan_page}/10")

    client = get_clob_client()

    # Sync real CLOB balance at startup so Larry knows his actual bankroll
    sync_bankroll_from_clob(client)

    while not _betting_shutdown:
        try:
            log.info("--- Betting cycle starting ---")

            # 1. Check if pending bets resolved
            check_pending_bets(client)

            # 2. Check if Grandma needs to intervene
            check_grandma_wallet()

            # 3. Check bankroll exposure — stop placing new bets if 80%+ of TOTAL is already in play
            # total = free cash + open bets (bankroll already has placed bets deducted)
            open_bets = get_pending_bets()
            bankroll = get_bankroll()
            open_exposure = sum(float(b.get("amount_usdc", 0)) for b in open_bets)
            total = bankroll + open_exposure
            max_exposure = total * 0.80
            if open_exposure >= max_exposure:
                log.info(f"Exposure limit reached: ${open_exposure:.2f} of ${total:.2f} total in play ({open_exposure/total*100:.0f}%), skipping new bets")
            else:
                # 4. Fetch markets — three parallel Gamma batches, 24h window
                markets = fetch_active_markets()

                if markets:
                    # Filter out markets Larry already has open bets on, token-blacklisted,
                    # or already passed on this cycle (price/urgency unchanged)
                    open_bet_ids = {b.get("polymarket_id") for b in get_pending_bets()}
                    fresh_markets = [
                        m for m in markets
                        if m["condition_id"] not in open_bet_ids
                        and not _is_token_blacklisted(m["condition_id"])
                        and not _is_pass_cached(m)
                    ]
                    skipped = len(markets) - len(fresh_markets)
                    if skipped:
                        log.info(f"Skipped {skipped} markets (open bets / no-token / already passed) — sending {len(fresh_markets)} fresh to Claude")
                    markets = fresh_markets

                if markets:
                    # 5. Ask Claude / Larry which ones to bet on
                    decisions = ask_larry_to_bet(markets)
                    log.info(f"Larry made {len(decisions)} decisions")

                    for decision in decisions:
                        if decision.get("decision") != "BET":
                            log.info(f"PASS: {decision.get('reasoning', 'no reason given')}")
                            # Cache PASS so we don't re-send same market next cycle
                            mid = decision.get("market_id")
                            if mid:
                                m_info = next((m for m in markets if m["condition_id"] == mid), None)
                                if m_info:
                                    _cache_pass(mid, m_info.get("yes_price", 0.5), m_info.get("hours_to_end", 24))
                            continue

                        pending_now = get_pending_bets()
                        current_exposure = sum(float(b.get("amount_usdc", 0)) for b in pending_now)
                        current_total = get_bankroll() + current_exposure
                        if current_exposure >= current_total * 0.80:
                            log.info(f"Reached exposure limit mid-loop (${current_exposure:.2f} of ${current_total:.2f}), stopping")
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

        if _betting_shutdown:
            break

        # Wait before next cycle
        log.info(f"💤 Sleeping {BET_CHECK_INTERVAL_MINUTES} minutes...")
        time.sleep(BET_CHECK_INTERVAL_MINUTES * 60)

    log.info("✅ Betting agent exited cleanly")


if __name__ == "__main__":
    run_betting_agent()
