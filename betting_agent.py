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
import os
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON

def _utcnow() -> datetime:
    """Return current UTC time as naive datetime. Replaces datetime.utcnow() (deprecated Python 3.12+)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── USDC ADDRESS ─────────────────────────────────────────────────────────────
# USDC.e on Polygon (bridged) — what Polymarket settles in
_USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

# ─── CTF CONTRACT (Conditional Token Framework) ───────────────────────────────
# Standard Gnosis CTF deployed at same address on all EVM chains via CREATE2.
# payoutDenominator(conditionId) > 0 means condition is resolved on-chain.
# This is the authoritative source — no API lag, no garbage data.
_CTF_ADDRESS = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
_CTF_ABI_MINIMAL = [
    {
        "inputs": [{"type": "bytes32", "name": "conditionId"}],
        "name": "payoutDenominator",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"type": "address", "name": "collateralToken"},
            {"type": "bytes32", "name": "parentCollectionId"},
            {"type": "bytes32", "name": "conditionId"},
            {"type": "uint256[]", "name": "indexSets"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]
_POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",   # no auth, reliable
    "https://rpc.ankr.com/polygon",              # no auth, high rate limit
    "https://polygon.meowrpc.com",               # no auth, fallback
    "https://polygon-rpc.com",                   # original (401s on some IPs)
]
_w3: Web3 | None = None  # cached Web3 instance

def _get_w3() -> Web3:
    global _w3
    if _w3 is not None and _w3.is_connected():
        return _w3
    for rpc in _POLYGON_RPCS:
        try:
            candidate = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
            if candidate.is_connected():
                log.info(f"⛓️  Connected to Polygon RPC: {rpc}")
                _w3 = candidate
                return _w3
        except Exception:
            continue
    # Last resort: return the first one anyway and let it fail loudly
    _w3 = Web3(Web3.HTTPProvider(_POLYGON_RPCS[0], request_kwargs={"timeout": 8}))
    return _w3

def _ctf_payout_denominator(condition_id: str) -> int:
    """Call CTF.payoutDenominator on-chain. Returns 0 if not resolved or on error."""
    try:
        w3 = _get_w3()
        ctf = w3.eth.contract(address=_CTF_ADDRESS, abi=_CTF_ABI_MINIMAL)
        cid_hex = condition_id.strip().lstrip("0x").strip()
        if len(cid_hex) % 2 != 0:
            cid_hex = "0" + cid_hex
        cid_bytes = bytes.fromhex(cid_hex)
        return ctf.functions.payoutDenominator(cid_bytes).call()
    except Exception as e:
        log.debug(f"CTF check error for {condition_id[:16]}...: {e}")
        return 0

# ─── BUILDER RELAYER (gasless claims) ─────────────────────────────────────────
_RELAYER_URL       = "https://relayer-v2.polymarket.com"
_BUILDER_API_KEY   = os.getenv("POLYMARKET_BUILDER_API_KEY", "")
_BUILDER_SECRET    = os.getenv("POLYMARKET_BUILDER_SECRET", "")
_BUILDER_PASSPHRASE = os.getenv("POLYMARKET_BUILDER_PASSPHRASE", "")

from config import (
    POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER,
    POLYMARKET_HOST, POLYMARKET_GAMMA_API,
    BET_CHECK_INTERVAL_MINUTES,
    ABSOLUTE_MIN_BET, ABSOLUTE_MAX_BET
)
from database import (
    get_bankroll, set_bankroll, get_pending_bets, save_bet, resolve_bet,
    init_db, get_state, set_state, get_connection
)
from larry_brain import ask_larry_to_bet, ask_larry_for_tweet, ask_larry_to_sell

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
    _token_not_found_blacklist[condition_id.lower()] = _utcnow() + timedelta(hours=_TOKEN_BLACKLIST_TTL_HOURS)

def _is_token_blacklisted(condition_id: str) -> bool:
    cid = condition_id.lower()
    expiry = _token_not_found_blacklist.get(cid)
    if expiry is None:
        return False
    if _utcnow() > expiry:
        _token_not_found_blacklist.pop(cid, None)  # .pop avoids KeyError if two threads race here
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
    _pass_cache[condition_id.lower()] = {
        "passed_at": _utcnow(),
        "price": yes_price,
        "hours_to_end": hours_to_end,
    }


def _is_pass_cached(market: dict) -> bool:
    """Return True if Claude already passed on this market and nothing meaningful changed."""
    cid = market["condition_id"].lower()
    entry = _pass_cache.get(cid)
    if not entry:
        return False

    # TTL expired — allow retry
    if _utcnow() - entry["passed_at"] > timedelta(hours=6):
        _pass_cache.pop(cid, None)  # .pop avoids KeyError if two code paths race
        return False

    # Price moved >5% — new information, worth re-analysing
    current_price = market.get("yes_price", 0.5)
    if abs(current_price - entry["price"]) > 0.05:
        _pass_cache.pop(cid, None)
        return False

    # Market became urgent since last PASS — re-examine
    if market.get("hours_to_end", 24) <= 4 and entry["hours_to_end"] > 4:
        _pass_cache.pop(cid, None)
        return False

    return True  # nothing changed — skip


# Rotating page counter — persisted in DB so restarts continue where they left off
# (otherwise Larry always restarts at page 0 and misses pages 1-9)
_scan_page: int = 0

# Sell-position cooldown: prevent hammering the sell function every cycle when GTC
# orders haven't filled yet and bankroll stays below $5.
_last_sell_attempt_at: datetime | None = None
_SELL_COOLDOWN_MINUTES = 15  # wait at least 15 min between sell attempts

# Positions that previously threw "not enough balance / allowance" — skip these
# so they don't block every sell cycle.  Persisted to DB so survives restarts.
_unsellable_positions: set = set()

def _load_unsellable():
    """Load persisted unsellable position blacklist from DB on startup."""
    global _unsellable_positions
    try:
        raw = get_state("unsellable_positions")
        if raw:
            _unsellable_positions = set(json.loads(raw))
            if _unsellable_positions:
                log.info(f"💸 Loaded {len(_unsellable_positions)} unsellable position(s) from DB: "
                         f"{', '.join(p[:16] for p in _unsellable_positions)}")
    except Exception:
        _unsellable_positions = set()

def _save_unsellable():
    """Persist unsellable position blacklist to DB."""
    try:
        set_state("unsellable_positions", json.dumps(list(_unsellable_positions)))
    except Exception:
        pass

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

    # Ensure USDC + CTF token allowances are set so both buying and selling work
    _ensure_allowances(client)

    return client


def _ensure_allowances(client: ClobClient) -> None:
    """
    Call update_balance_allowance for COLLATERAL on startup.
    CONDITIONAL allowance is set per token_id before each sell attempt.
    Safe to call every restart — idempotent if allowance is already sufficient.
    """
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        collateral = getattr(AssetType, "COLLATERAL", None) or getattr(AssetType, "USDC", None)
        if collateral:
            try:
                client.update_balance_allowance(BalanceAllowanceParams(asset_type=collateral))
                log.info("✅ COLLATERAL allowance updated (USDC approved for CLOB)")
            except Exception as e:
                log.warning(f"⚠️  COLLATERAL allowance update failed: {e}")
    except Exception as e:
        log.warning(f"⚠️  _ensure_allowances failed: {type(e).__name__}: {e}")


def sync_bankroll_from_clob(client: ClobClient):
    """
    Sync DB bankroll with actual USDC balance from CLOB.
    Called at startup so Larry knows his real balance, not just what the DB thinks.
    NOTE: This reflects trading allowance (approved for CLOB), not total wallet balance.
    Unclaimed winnings may not appear here until claimed on polymarket.com.
    Retries up to 3 times with 5s backoff — CLOB occasionally returns 5xx on cold start.
    """
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    asset = getattr(AssetType, "COLLATERAL", None) or getattr(AssetType, "USDC", None)

    for attempt in range(3):
        try:
            result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=asset))
            raw = result.get("balance", "0")
            real_balance = float(raw) / 1_000_000  # microUSDC → USDC
            if real_balance >= 0:
                db_balance = get_bankroll()
                if abs(real_balance - db_balance) > 0.01:
                    log.info(f"💰 Balance sync: DB=${db_balance:.2f} → CLOB=${real_balance:.2f}")
                    set_bankroll(real_balance, real_balance - db_balance, "SYNC")
                else:
                    log.info(f"💰 Balance OK: DB=${db_balance:.2f}, CLOB=${real_balance:.2f}")
            return  # success
        except Exception as e:
            if attempt < 2:
                log.warning(f"Balance sync attempt {attempt+1}/3 failed ({type(e).__name__}: {e}) — retrying in 5s")
                time.sleep(5)
            else:
                log.warning(f"Balance sync failed after 3 attempts — using DB balance (last error: {type(e).__name__}: {e})")


# ─── PORTFOLIO VALUATION ──────────────────────────────────────────────────────

_POLYMARKET_DATA_API = "https://data-api.polymarket.com"

def get_positions_value() -> tuple[float, int]:
    """
    Fetch current market value of all open positions.
    Returns (total_current_value, position_count).

    Uses Polymarket Data API (data-api.polymarket.com/positions) —
    NOT the Gamma API. Gamma is for market listings; Data API is for
    user portfolio data. This is the same source Polymarket's UI uses.

    Falls back to (0.0, 0) on any error — caller uses DB cost sum instead.
    """
    try:
        resp = requests.get(
            f"{_POLYMARKET_DATA_API}/positions",
            params={"user": POLYMARKET_FUNDER, "sizeThreshold": "0.1"},
            timeout=10,
        )
        if not resp.ok:
            log.warning(f"Data API positions failed: HTTP {resp.status_code}")
            return 0.0, 0
        positions = resp.json()
        if not isinstance(positions, list):
            log.warning(f"Data API positions: unexpected response type {type(positions)}")
            return 0.0, 0
        total = sum(float(p.get("currentValue") or 0) for p in positions)
        return round(total, 2), len(positions)
    except Exception as e:
        log.warning(f"Portfolio valuation failed: {type(e).__name__}: {e}")
        return 0.0, 0




def _get_all_bet_market_ids() -> set:
    """
    Return the set of ALL condition_ids Larry has EVER bet on (any status).
    Used to prevent re-betting on resolved markets that are no longer in pending_bets.
    Falls back to empty set on any DB error so filtering degrades gracefully.
    """
    try:
        conn = get_connection()
        rows = conn.execute("SELECT polymarket_id FROM bets").fetchall()
        conn.close()
        return {(r["polymarket_id"] or "").lower() for r in rows if r["polymarket_id"]}
    except Exception:
        return set()


# ─── AUTO-CLAIM WINNINGS ──────────────────────────────────────────────────────


def _build_relay_service():
    """
    Build a (PolyWeb3Service, relay_client) pair using the Builder Relayer.
    Shared by claim_winnings() and sweep_unclaimed_winnings().

    BuilderConfig + BuilderApiKeyCreds live in py_builder_signing_sdk.config
    (confirmed from production logs 2026-03-19).
    Returns (service, svc_methods) or raises on failure.
    """
    import inspect as _insp
    from poly_web3 import PolyWeb3Service
    from py_builder_relayer_client.client import RelayClient

    # ── 1. Build BuilderConfig ─────────────────────────────────────────────────
    builder_cfg = None
    try:
        from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds
        _creds = BuilderApiKeyCreds(
            key=_BUILDER_API_KEY,
            secret=_BUILDER_SECRET,
            passphrase=_BUILDER_PASSPHRASE,
        )
        builder_cfg = BuilderConfig(local_builder_creds=_creds)
        log.info(f"⛽ Gasless claim enabled via Builder Relayer")
    except ImportError:
        log.warning("🔧 py_builder_signing_sdk not installed — relay disabled")
    except Exception as _e:
        log.warning(f"🔧 BuilderConfig init failed ({_e}) — relay will run without auth")

    # ── 2. Build RelayClient with dynamic param filtering ─────────────────────
    _sig = _insp.signature(RelayClient.__init__)
    _valid = set(_sig.parameters.keys()) - {"self"}
    _has_var_kw = any(p.kind == _insp.Parameter.VAR_KEYWORD for p in _sig.parameters.values())
    _candidates = dict(
        relayer_url=_RELAYER_URL, host=_RELAYER_URL,
        chain_id=137,
        private_key=POLYMARKET_PRIVATE_KEY, key=POLYMARKET_PRIVATE_KEY,
        funder=POLYMARKET_FUNDER, funder_address=POLYMARKET_FUNDER,
        wallet_address=POLYMARKET_FUNDER,
        builder_config=builder_cfg,
    )
    _filtered = _candidates if _has_var_kw else {k: v for k, v in _candidates.items() if k in _valid}
    log.info(f"🔧 Passing to RelayClient: {sorted(_filtered.keys())}")
    relay_client = RelayClient(**_filtered)

    # ── 3. Wrap in PolyWeb3Service ─────────────────────────────────────────────
    clob = get_clob_client()
    service = PolyWeb3Service(clob_client=clob, relayer_client=relay_client)
    svc_methods = [m for m in dir(service) if not m.startswith("_")]
    log.info(f"🔧 PolyWeb3Service methods: {svc_methods}")
    return service, svc_methods


def sweep_unclaimed_winnings(client: ClobClient) -> bool:
    """
    Proactive sweep: call redeem_all() via Builder Relayer every cycle.
    Catches winnings that were PREVIOUSLY marked WON in DB but never actually
    claimed (e.g. because prior redeem attempts silently failed).
    Does NOT require any specific condition_id — the relayer finds all redeemable positions.
    Returns True if any winnings were successfully claimed.
    """
    if not _BUILDER_API_KEY:
        return False

    last_sweep = getattr(sweep_unclaimed_winnings, "_last_ran", None)
    now_ts = time.time()
    # Throttle: sweep at most once per 30 minutes (matches betting cycle sleep)
    if last_sweep and (now_ts - last_sweep) < 1800:
        return False
    sweep_unclaimed_winnings._last_ran = now_ts

    try:
        service, _ = _build_relay_service()
        if not hasattr(service, "redeem_all"):
            return False
        log.info("🧹 Sweeping unclaimed winnings via redeem_all()...")
        result = service.redeem_all()
        if result:
            log.info(f"✅ Sweep claimed winnings! result={str(result)[:200]}")
            sync_bankroll_from_clob(client)
            return True
        else:
            log.info("🧹 Sweep: redeem_all returned [] — nothing redeemable yet (or builder_config still broken)")
            return False
    except Exception as e:
        log.warning(f"🧹 Sweep failed ({type(e).__name__}: {e})")
        return False


def claim_winnings(condition_id: str, outcome: str, payout: float) -> bool:
    """
    Claim winning position. Tries gasless (Builder Relayer) first, then direct on-chain.

    Gasless path (preferred, no MATIC needed):
      Uses poly-web3 library which handles signing + relayer submission.
      Requires POLYMARKET_BUILDER_* env vars.

    Direct on-chain fallback (needs ~0.01 MATIC in funder wallet):
      Calls CTF.redeemPositions() directly via Web3.
    """
    log.info(f"⛽ Claiming | condition={condition_id[:16]}... | outcome={outcome} | ${payout:.2f}")

    # ── Path 1: Gasless via Builder Relayer ───────────────────────────────────
    if _BUILDER_API_KEY:
        try:
            service, svc_methods = _build_relay_service()
            amounts = [payout, 0.0] if outcome.upper() == "YES" else [0.0, payout]

            # Try redeem_all() first — catches this bet AND any other unclaimed wins
            if hasattr(service, "redeem_all"):
                try:
                    result = service.redeem_all()
                    if result:
                        log.info(f"✅ Gasless redeem_all! ${payout:.2f} → {str(result)[:120]}")
                        return True
                    else:
                        log.warning("⚠️  redeem_all returned [] (builder_config broken or CTF not resolved yet)")
                except Exception as _e_all:
                    log.debug(f"redeem_all raised ({type(_e_all).__name__}: {_e_all})")

            # Try redeem() with introspected param names
            import inspect as _insp_redeem
            redeem_fn = getattr(service, "redeem", None)
            if redeem_fn is not None:
                try:
                    sig = _insp_redeem.signature(redeem_fn)
                    param_names = [p for p in sig.parameters.keys() if p != "self"]
                    log.info(f"🔧 redeem() params: {param_names}")
                    call_kwargs = {}
                    for pname in param_names:
                        plow = pname.lower()
                        if "condition" in plow or plow == "cid":
                            call_kwargs[pname] = condition_id
                        elif "amount" in plow:
                            call_kwargs[pname] = amounts
                        elif "neg" in plow or "risk" in plow:
                            call_kwargs[pname] = False
                    result = redeem_fn(**call_kwargs) if call_kwargs else redeem_fn(condition_id, amounts, False)
                    if result:
                        log.info(f"✅ Gasless redeem! ${payout:.2f} → {str(result)[:120]}")
                        return True
                    else:
                        log.warning("⚠️  redeem() returned empty (library swallowed error)")
                except Exception as _e_redeem:
                    log.warning(f"redeem() failed ({type(_e_redeem).__name__}: {_e_redeem})")

            raise AttributeError(f"No working redeem method. Available: {svc_methods}")
        except Exception as e:
            log.warning(f"⚠️  Gasless claim failed ({type(e).__name__}: {e}) — trying direct on-chain")

        # ── Path 2: Direct on-chain via CTF.redeemPositions() ────────────────────
    # Requires small amount of MATIC in funder wallet (~0.01 MATIC = ~$0.005)
    # To enable: send 0.1 MATIC to POLYMARKET_FUNDER address (check Railway env vars)
    try:
        from eth_account import Account
        w3 = _get_w3()
        account = Account.from_key(POLYMARKET_PRIVATE_KEY)
        ctf = w3.eth.contract(address=_CTF_ADDRESS, abi=_CTF_ABI_MINIMAL)
        cid_hex = condition_id.strip().lstrip("0x").strip()
        # Pad to 64 chars if somehow truncated (defensive)
        if len(cid_hex) % 2 != 0:
            cid_hex = "0" + cid_hex
        cid_bytes = bytes.fromhex(cid_hex)

        # Verify resolved before spending gas
        denom = ctf.functions.payoutDenominator(cid_bytes).call()
        if denom == 0:
            log.warning(
                f"⚠️  CTF not resolved yet for {condition_id[:16]}... — "
                f"winnings will appear in Polymarket UI as 'Получить'. "
                f"Funder wallet: {POLYMARKET_FUNDER}"
            )
            return False

        matic_balance = w3.eth.get_balance(POLYMARKET_FUNDER)
        matic_eth = w3.from_wei(matic_balance, 'ether')
        log.info(f"⛽ Funder wallet: {POLYMARKET_FUNDER} | MATIC balance: {matic_eth:.4f}")
        if matic_balance < w3.to_wei("0.005", "ether"):
            log.warning(
                f"⚠️  Insufficient MATIC ({matic_eth:.4f}) for direct claim. "
                f"Send 0.1 MATIC to {POLYMARKET_FUNDER} on Polygon network to enable auto-claim."
            )
            return False

        nonce = w3.eth.get_transaction_count(POLYMARKET_FUNDER)
        gas_price = min(w3.eth.gas_price, w3.to_wei("50", "gwei"))

        # Try indexSets [1] and [2] — one corresponds to YES, one to NO
        # Polymarket binary: indexSet=1 → outcome0, indexSet=2 → outcome1
        index_sets = [1, 2] if outcome.upper() == "YES" else [2, 1]
        for idx_set in index_sets:
            try:
                tx = ctf.functions.redeemPositions(
                    Web3.to_checksum_address(_USDC_ADDRESS),
                    b"\x00" * 32,   # parentCollectionId = zero
                    cid_bytes,
                    [idx_set],
                ).build_transaction({
                    "from": POLYMARKET_FUNDER,
                    "gas": 200000,
                    "gasPrice": gas_price,
                    "nonce": nonce,
                    "chainId": 137,
                })
                signed = account.sign_transaction(tx)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

                # Polygon can be slow under congestion — retry receipt up to 3×60s
                receipt = None
                for _receipt_attempt in range(3):
                    try:
                        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                        break
                    except Exception as timeout_err:
                        log.debug(f"Receipt attempt {_receipt_attempt+1}/3 timed out: {timeout_err}")
                        time.sleep(5)

                if receipt and receipt["status"] == 1:
                    log.info(f"✅ Direct on-chain claim success! tx={tx_hash.hex()[:16]}...")
                    return True
                elif receipt:
                    log.warning(f"❌ TX mined but status=0 (reverted) for indexSet={idx_set}")
                else:
                    log.warning(f"❌ Could not get receipt for indexSet={idx_set} tx after 3 attempts — chain may be congested")
                nonce += 1
            except Exception as tx_err:
                log.debug(f"indexSet={idx_set} failed: {tx_err}")
                nonce += 1

        log.warning(f"❌ Direct claim failed for both indexSets — claim manually on polymarket.com")
        return False

    except Exception as e:
        log.warning(f"❌ Direct on-chain claim error ({type(e).__name__}: {e}) — claim manually on polymarket.com")
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
            params["end_date_min"] = end_min
        if end_max:
            params["end_date_max"] = end_max
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
    now = _utcnow()
    cutoff = now + timedelta(hours=24)   # TODAY only — must resolve within 24h
    min_time = now + timedelta(minutes=30)  # skip markets resolving in under 30min (too late to fill)

    scan_offset = _scan_page * 500
    # Offset=0 duplicates anchor (which also uses offset=0) — dedup would kill all scan results.
    # Use offset=5000 (page 10) on that cycle so we still cover extra markets instead of wasting it.
    if scan_offset == 0:
        scan_offset = 5000
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

    # 48h fallback — if 24h window is completely empty (Sunday morning gaps, overnight
    # market resolution with no new day markets yet), expand to 48h so Larry still has
    # something to bet on instead of spinning idle for hours.
    if not raw_anchor and not raw_scan and not raw_fresh:
        cutoff = now + timedelta(hours=48)
        end_max = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        log.info("⚠️  24h window returned 0 markets — expanding to 48h fallback")
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_anchor = ex.submit(_fetch_gamma_raw, "volume24hr", False, 500, 0,           end_min, end_max)
            f_scan   = ex.submit(_fetch_gamma_raw, "volume24hr", False, 500, scan_offset, end_min, end_max)
            f_fresh  = ex.submit(_fetch_gamma_raw, "createdAt",  False, 200, 0,           end_min, end_max)
            raw_anchor = f_anchor.result()
            raw_scan   = f_scan.result()
            raw_fresh  = f_fresh.result()

    def parse_strict(raw):
        """Parse with tighter time filter: resolves within 24h AND not in the next 30min."""
        out = []
        for m in raw:
            # Try all known Gamma API field name variations for end date
            end_date_str = (m.get("endDate") or m.get("end_date") or
                            m.get("end_date_iso") or m.get("endDateIso") or
                            m.get("end_date_utc") or m.get("endDateUtc"))
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
                    cond_id = (m.get("conditionId") or m.get("condition_id") or "").lower()
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

                # Build YES/NO price map — Gamma API changed format (March 2026):
                # OLD: tokens=[{outcome:"Yes",price:0.6},{outcome:"No",price:0.4}]
                # NEW: tokens=None, outcomes=["Yes","No"], outcomePrices=["0.6","0.4"]
                # Support both formats for forward-compat.
                gamma_tokens = m.get("tokens") or []
                token_map = {}

                if gamma_tokens:
                    # Old Gamma format: list of {outcome, price} dicts
                    token_map = {
                        (t.get("outcome") or "").lower(): float(t.get("price", 0.5))
                        for t in gamma_tokens if isinstance(t, dict)
                    }
                else:
                    # New Gamma format: separate outcomes + outcomePrices arrays
                    outcomes_raw = m.get("outcomes")
                    prices_raw   = m.get("outcomePrices")
                    if outcomes_raw and prices_raw:
                        try:
                            # Gamma returns these as JSON strings or already-parsed lists
                            import json as _json
                            outcomes_list = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                            prices_list   = _json.loads(prices_raw)   if isinstance(prices_raw,   str) else prices_raw
                            token_map = {
                                str(o).lower(): float(p)
                                for o, p in zip(outcomes_list, prices_list)
                            }
                        except Exception:
                            continue  # unparseable price data — skip
                    else:
                        continue  # no price data at all — skip

                if "yes" not in token_map or "no" not in token_map:
                    continue  # non-binary market or data missing — CLOB bet will fail

                yes_price = token_map["yes"]
                # Skip nearly-resolved markets (CLOB removes liquidity when price ~1 or ~0)
                if yes_price >= 0.97 or yes_price <= 0.03:
                    continue

                out.append({
                    "condition_id": (m.get("conditionId") or m.get("condition_id") or "").lower(),
                    "question": m.get("question"),
                    "end_date": end_date_str,
                    "days_to_end": days_to_end,
                    "hours_to_end": hours_to_end,
                    "yes_price": round(yes_price, 4),
                    "volume_24h": float(m.get("volume24hr", 0)),
                    "category": _guess_category(m.get("question", "")),
                })
            except (ValueError, TypeError):
                continue
        return out

    log.debug(f"🌐 Raw Gamma counts: anchor={len(raw_anchor)}, scan={len(raw_scan)}, fresh={len(raw_fresh)}")
    anchor = parse_strict(raw_anchor)
    scan   = parse_strict(raw_scan)
    fresh  = parse_strict(raw_fresh)

    # 48h fallback — also triggers if parse_strict filtered everything out
    # (e.g. Sunday afternoon: Gamma returned data but all markets nearly settled/resolved)
    if not anchor and not scan and not fresh and cutoff <= now + timedelta(hours=25):
        cutoff = now + timedelta(hours=48)
        end_max = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        log.info(f"⚠️  parse_strict returned 0 markets (raw: a={len(raw_anchor)}/s={len(raw_scan)}/f={len(raw_fresh)}) — expanding to 48h fallback")
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_anchor = ex.submit(_fetch_gamma_raw, "volume24hr", False, 500, 0,           end_min, end_max)
            f_scan   = ex.submit(_fetch_gamma_raw, "volume24hr", False, 500, scan_offset, end_min, end_max)
            f_fresh  = ex.submit(_fetch_gamma_raw, "createdAt",  False, 200, 0,           end_min, end_max)
            raw_anchor = f_anchor.result()
            raw_scan   = f_scan.result()
            raw_fresh  = f_fresh.result()
        log.debug(f"🌐 48h raw counts: anchor={len(raw_anchor)}, scan={len(raw_scan)}, fresh={len(raw_fresh)}")
        anchor = parse_strict(raw_anchor)
        scan   = parse_strict(raw_scan)
        fresh  = parse_strict(raw_fresh)

    # 7-day broad fallback — if 48h still empty, fetch without any date filter
    # and let parse_strict handle the wider window. Catches market cycles gaps.
    if not anchor and not scan and not fresh:
        cutoff = now + timedelta(days=7)
        log.info(f"⚠️  48h fallback still empty (raw: a={len(raw_anchor)}/s={len(raw_scan)}/f={len(raw_fresh)}) — trying 7-day broad fetch (no server-side date filter)")
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_anchor = ex.submit(_fetch_gamma_raw, "volume24hr", False, 500, 0,   None, None)
            f_fresh  = ex.submit(_fetch_gamma_raw, "createdAt",  False, 200, 0,   None, None)
            raw_anchor = f_anchor.result()
            raw_fresh  = f_fresh.result()
        log.info(f"🌐 7-day raw counts: anchor={len(raw_anchor)}, fresh={len(raw_fresh)}")
        anchor = parse_strict(raw_anchor)
        fresh  = parse_strict(raw_fresh)
        scan   = []

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

    if combined:
        log.info(
            f"🔎 Scan page {_scan_page-1 if _scan_page > 0 else 9}/10 (offset={scan_offset}) | "
            f"anchor={len(anchor)} scan={len(scan)} fresh={len(fresh)} → "
            f"sending all {len(combined)} to Claude"
        )
    else:
        log.warning(
            f"🔎 Scan page {_scan_page-1 if _scan_page > 0 else 9}/10 | "
            f"anchor={len(anchor)} scan={len(scan)} fresh={len(fresh)} → "
            f"no markets in 24h window right now (Gamma may be between market cycles)"
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


# ─── COLLECT NEAR-RESOLVED POSITIONS ──────────────────────────────────────────

_NEAR_RESOLVED_THRESHOLD = 0.99   # sell positions trading at ≥ 99¢ — market has effectively resolved

def collect_near_resolved_positions(client: ClobClient) -> float:
    """
    Proactively sell positions that are already trading at ≥ 0.95.
    These are effectively won — selling early collects ~95% of the value
    immediately rather than waiting days for formal resolution.

    Runs every betting cycle regardless of bankroll.
    Returns estimated USDC collected.
    """
    pending = get_pending_bets()
    if not pending:
        return 0.0

    # Fetch current prices and token IDs for all pending bets
    near_won = []
    token_lookup: dict = {}
    price_lookup: dict = {}

    for bet in pending:
        mid = bet.get("polymarket_id", "")
        if not mid or mid in _unsellable_positions:
            continue
        try:
            md = client.get_market(mid)
            tokens = md.get("tokens", []) if md else []
            for token in tokens:
                if token.get("outcome", "").lower() == bet.get("outcome", "").lower():
                    current_price = float(token.get("price", 0))
                    token_id = token.get("token_id")
                    token_lookup[mid] = token_id
                    price_lookup[mid] = current_price
                    if current_price >= _NEAR_RESOLVED_THRESHOLD:
                        shares = float(bet.get("amount_usdc", 5.0))
                        near_won.append({
                            "market_id": mid,
                            "question": bet.get("question", "?")[:80],
                            "outcome": bet.get("outcome", "YES"),
                            "current_price": current_price,
                            "paid": shares,
                            "token_id": token_id,
                        })
                    break
        except Exception as e:
            log.debug(f"⛏️  Price fetch failed for {mid[:16]}: {e}")

    if not near_won:
        return 0.0

    log.info(
        f"⛏️  Found {len(near_won)} near-resolved position(s) at ≥{_NEAR_RESOLVED_THRESHOLD:.0%} — collecting early: "
        + ", ".join(f"{p['question'][:30]} ({p['current_price']:.2f})" for p in near_won)
    )

    total_freed = 0.0

    for p in near_won:
        mid = p["market_id"]
        token_id = token_lookup.get(mid)
        current_price = price_lookup.get(mid, 0)
        if not token_id or not current_price:
            continue

        bet = next((b for b in pending if b.get("polymarket_id") == mid), None)
        if not bet:
            continue

        # Sell at a small discount to ensure fill
        sell_price = max(0.90, round(current_price - 0.02, 4))
        shares = round(float(bet.get("amount_usdc", 5.0)), 2)

        try:
            # Per-sell CONDITIONAL allowance
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                conditional = getattr(AssetType, "CONDITIONAL", None)
                if conditional:
                    client.update_balance_allowance(
                        BalanceAllowanceParams(asset_type=conditional, token_id=str(token_id))
                    )
            except Exception as _ae:
                log.warning(f"⛏️  CONDITIONAL allowance update failed for {mid[:16]}: {_ae}")

            order_args = OrderArgs(token_id=token_id, price=sell_price, size=shares, side="SELL")
            signed_order = client.create_order(order_args)
            resp = client.post_order(signed_order, OrderType.GTC)

            if resp.get("success"):
                estimated = round(shares * sell_price, 2)
                total_freed += estimated
                log.info(
                    f"⛏️  COLLECTED: {bet.get('outcome')} on \"{bet.get('question','?')[:50]}\" "
                    f"@ {sell_price:.3f} → ~${estimated:.2f}"
                )
                resolve_bet(mid, False, 0.0)

                # Tweet the win
                try:
                    from larry_brain import ask_larry_for_tweet
                    from twitter_agent import post_tweet
                    ctx = {
                        "question": bet.get("question", ""),
                        "outcome": bet.get("outcome", ""),
                        "price": sell_price,
                        "pnl": estimated - float(bet.get("amount_usdc", 5.0)),
                    }
                    tweet = ask_larry_for_tweet("NEAR_WIN_COLLECT", ctx)
                    if tweet:
                        post_tweet(tweet, tweet_type="COLLECTED_WIN")
                except Exception as te:
                    log.debug(f"Collect tweet failed: {te}")
            else:
                log.warning(
                    f"⛏️  Collect order rejected for {mid[:16]}: "
                    f"{resp.get('errorMsg', resp.get('error', 'unknown'))}"
                )
        except Exception as e:
            err_str = str(e).lower()
            log.warning(f"⛏️  Exception collecting {mid[:16]}: {type(e).__name__}: {e}")
            if "not enough balance" in err_str or "allowance" in err_str:
                _unsellable_positions.add(mid)
                _save_unsellable()

    if total_freed > 0:
        time.sleep(4)
        sync_bankroll_from_clob(client)

    return total_freed


# ─── SELL POSITIONS FOR CAPITAL ───────────────────────────────────────────────

def try_sell_positions_for_capital(client: ClobClient, needed: float = 5.0) -> float:
    """
    When Larry has < $5 free cash, ask him which open positions to sell early
    to free capital for new same-day bets.

    Flow:
    1. Fetch current prices for all pending bets via CLOB
    2. Ask Claude/Larry which ones to sell (larry_brain.ask_larry_to_sell)
    3. Place SELL orders (GTC) for chosen positions
    4. Mark those bets as resolved in DB (they'll disappear from pending)
    5. Sleep 4s then re-sync bankroll from CLOB — sell proceeds should appear

    Cooldown: _SELL_COOLDOWN_MINUTES between attempts so GTC orders have time to fill
    before we try to sell another position (avoids hammering every 30-min cycle).

    Returns: estimated USDC freed (sum of sell values; 0 if nothing sold / cooldown active).
    """
    global _last_sell_attempt_at, _unsellable_positions
    now = _utcnow()

    # Load sell cooldown from DB if in-memory is empty (e.g. after restart)
    if _last_sell_attempt_at is None:
        raw_ts = get_state("last_sell_attempt_at")
        if raw_ts:
            try:
                _last_sell_attempt_at = datetime.fromisoformat(raw_ts)
            except Exception:
                pass

    if (_last_sell_attempt_at is not None and
            (now - _last_sell_attempt_at).total_seconds() < _SELL_COOLDOWN_MINUTES * 60):
        remaining = int(_SELL_COOLDOWN_MINUTES - (now - _last_sell_attempt_at).total_seconds() / 60)
        log.info(f"💸 Sell cooldown active ({remaining}m left) — skipping sell attempt")
        return 0.0
    _last_sell_attempt_at = now
    set_state("last_sell_attempt_at", now.isoformat())

    pending = get_pending_bets()
    if not pending:
        log.info("💸 No open positions to sell")
        return 0.0

    # Build position info with current market prices
    positions = []
    token_lookup: dict = {}   # market_id → token_id (needed for sell order)
    price_lookup: dict = {}   # market_id → current price

    if _unsellable_positions:
        log.info(f"💸 Skipping {len(_unsellable_positions)} blacklisted unsellable position(s): "
                 f"{', '.join(p[:16] for p in _unsellable_positions)}")

    for bet in pending:
        mid = bet.get("polymarket_id", "")
        if not mid:
            continue
        if mid in _unsellable_positions:
            log.debug(f"💸 Skipping blacklisted position {mid[:16]}")
            continue
        try:
            md = client.get_market(mid)
            tokens = md.get("tokens", []) if md else []
            current_price = None
            token_id = None
            for token in tokens:
                if token.get("outcome", "").lower() == bet.get("outcome", "").lower():
                    current_price = float(token.get("price", 0.5))
                    token_id = token.get("token_id")
                    break

            if current_price is None or not token_id:
                continue

            shares = float(bet.get("amount_usdc", 5.0))  # same convention as place_bet
            current_value = round(shares * current_price, 2)
            bought_at = float(bet.get("odds", current_price))

            # Extract end date from CLOB market data — critical for "long-dated" detection
            end_date_raw = (
                md.get("endDateIso") or
                md.get("end_date_iso") or
                md.get("endDate") or
                md.get("end_date") or
                ""
            )
            # Trim to just the date portion (YYYY-MM-DD) to keep JSON compact
            end_date = end_date_raw[:10] if end_date_raw else "unknown"

            token_lookup[mid] = token_id
            price_lookup[mid] = current_price

            positions.append({
                "market_id":    mid,
                "question":     bet.get("question", "?")[:80],
                "outcome":      bet.get("outcome", "YES"),
                "end_date":     end_date,       # when this market resolves
                "placed_at":    (bet.get("placed_at") or "")[:10],  # when Larry placed it
                "bought_at":    round(bought_at, 3),
                "current_price": round(current_price, 3),
                "paid":         round(shares, 2),
                "current_value": current_value,
                "pnl_usdc":     round(current_value - shares, 2),
            })
        except Exception as e:
            log.debug(f"Could not get market data for {mid[:16]}: {type(e).__name__}")

    if not positions:
        log.info("💸 Could not fetch prices for any open position — skipping sell cycle")
        return 0.0

    log.info(f"💸 Bankroll insufficient (need ${needed:.2f}) — asking Larry which of {len(positions)} positions to sell...")
    decisions = ask_larry_to_sell(positions)

    sells = [d for d in decisions if d.get("action") == "SELL"]
    keeps = [d for d in decisions if d.get("action") == "KEEP"]
    log.info(f"💸 Larry decided: {len(sells)} SELL, {len(keeps)} KEEP")

    if not sells:
        return 0.0

    total_freed = 0.0
    for decision in sells:
        mid = decision.get("market_id", "")
        token_id = token_lookup.get(mid)
        current_price = price_lookup.get(mid)

        if not token_id or not current_price:
            log.warning(f"💸 No token_id/price for {mid[:16]} — skipping sell")
            continue

        bet = next((b for b in pending if b.get("polymarket_id") == mid), None)
        if not bet:
            continue

        # Sell at a slight discount to current price to ensure liquidity
        sell_price = max(0.02, round(current_price - 0.02, 4))
        shares = round(float(bet.get("amount_usdc", 5.0)), 2)

        try:
            # Ensure CONDITIONAL allowance for this specific token before selling
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                conditional = getattr(AssetType, "CONDITIONAL", None)
                if conditional:
                    client.update_balance_allowance(
                        BalanceAllowanceParams(asset_type=conditional, token_id=str(token_id))
                    )
            except Exception as _ae:
                log.warning(f"💸 Pre-sell CONDITIONAL allowance failed for {mid[:16]}: {type(_ae).__name__}: {_ae}")

            order_args = OrderArgs(
                token_id=token_id,
                price=sell_price,
                size=shares,
                side="SELL",
            )
            signed_order = client.create_order(order_args)
            resp = client.post_order(signed_order, OrderType.GTC)

            if resp.get("success"):
                estimated_proceeds = round(shares * sell_price, 2)
                total_freed += estimated_proceeds
                log.info(
                    f"💸 SOLD: {bet.get('outcome')} on \"{bet.get('question','?')[:50]}\" "
                    f"— placed sell order at {sell_price:.3f}, ~${estimated_proceeds:.2f} proceeds"
                )
                # Remove from pending bets — DB entry replaced by SOLD status
                # Bankroll NOT updated here — sync_bankroll_from_clob runs after and captures CLOB truth
                resolve_bet(mid, False, 0.0)

                # Tweet about the sale if Larry had something to say
                larry_tweet = decision.get("larry_tweet", "")
                if larry_tweet:
                    try:
                        from twitter_agent import post_tweet
                        post_tweet(larry_tweet, tweet_type="SOLD_POSITION")
                    except Exception as te:
                        log.debug(f"Sell tweet failed: {te}")
            else:
                log.warning(
                    f"💸 Sell order rejected for {mid[:16]}: "
                    f"{resp.get('errorMsg', resp.get('error', 'unknown'))}"
                )
        except Exception as e:
            err_str = str(e).lower()
            log.error(f"💸 Exception selling {mid[:16]}: {type(e).__name__}: {e}")
            if "not enough balance" in err_str or "allowance" in err_str:
                _unsellable_positions.add(mid)
                _save_unsellable()  # persist so survives container restart
                log.warning(
                    f"💸 Blacklisted {mid[:16]}... — repeated 'not enough balance/allowance' "
                    f"(total blacklisted: {len(_unsellable_positions)})"
                )

    if total_freed > 0:
        # Wait briefly for GTC orders to fill, then re-sync bankroll from CLOB
        log.info(f"💸 Sell orders placed — waiting 4s then syncing balance (est. ${total_freed:.2f} freed)")
        time.sleep(4)
        sync_bankroll_from_clob(client)

    return total_freed


# ─── CHECK RESOLVED BETS ──────────────────────────────────────────────────────

def _resolve_from_tokens(tokens: list, outcome: str, bet: dict) -> dict | None:
    """Helper: scan tokens list for matching outcome, return resolution dict."""
    for token in tokens:
        if token.get("outcome", "").upper() == outcome.upper():
            try:
                price = float(token.get("price", 0))
            except (TypeError, ValueError):
                price = 0.0
            won = price >= 0.99
            return {"bet": bet, "won": won, "payout": bet["potential_payout"] if won else 0.0}
    return None


def _check_gamma_for_resolution(cid: str, bet: dict) -> dict | None:
    """
    Fallback: ask Gamma API whether this market has resolved.
    Used when CLOB hasn't updated 'closed' flag yet or returns 404.
    """
    try:
        gm_resp = requests.get(
            f"{POLYMARKET_GAMMA_API}/markets",
            params={"conditionIds": cid},
            timeout=10,
        )
        if not gm_resp.ok:
            return None
        gm_list = gm_resp.json()
        if not isinstance(gm_list, list) or not gm_list:
            return None
        gm = gm_list[0]

        end_date_raw = str(gm.get("endDate") or gm.get("end_date_iso") or "")
        log.info(
            f"🔍 Gamma {cid[:16]}... | resolved={gm.get('resolved')} "
            f"active={gm.get('active')} closed={gm.get('closed')} "
            f"endDate={end_date_raw[:10]}"
        )

        # Sanity check: endDate before 2024 = Gamma returned garbage/stale data.
        # CLOB condition_ids don't always map to Gamma market IDs — Gamma returns
        # a null-ish record with closed=True and a 2020 date. Ignore it.
        if end_date_raw and end_date_raw[:4].isdigit() and int(end_date_raw[:4]) < 2024:
            log.warning(
                f"⚠️  Gamma garbage data (endDate={end_date_raw[:10]}) for {cid[:16]}... — ignoring"
            )
            return None

        # Only trust explicit resolved=True
        if not gm.get("resolved"):
            return None

        # Gamma resolved=True — find our outcome's token price
        tokens = gm.get("tokens") or []
        result = _resolve_from_tokens(tokens, bet["outcome"], bet)
        if result:
            return result
        # resolved=True but no matching token → only call LOST if tokens list is non-empty
        # (empty = Gamma doesn't actually know this market)
        if not tokens:
            log.warning(
                f"⚠️  Gamma resolved=True but no tokens for {cid[:16]}... — skipping"
            )
            return None
        log.info(f"Gamma resolved (no token match for {bet['outcome']}) on {cid[:16]}... — treating as LOST")
        return {"bet": bet, "won": False, "payout": 0.0}
    except Exception as e:
        log.warning(f"Gamma check error for {cid[:16]}...: {e}")
        return None


def _check_single_bet(bet: dict) -> dict | None:
    """
    Check one pending bet. Returns resolution dict or None if still open.
    Runs in a thread — no shared state written here, only reads.

    Resolution priority (most reliable first):
      1. On-chain CTF.payoutDenominator > 0  → authoritative, no lag
      2. CLOB market.closed=True             → reliable when available
      3. CLOB token price >= 0.99            → catches CLOB lag before closed flag
      4. CLOB 404                            → market purged = phantom bet
    """
    cid = bet["polymarket_id"]
    try:
        resp = requests.get(f"{POLYMARKET_HOST}/markets/{cid}", timeout=10)

        # ── CLOB 404: market purged entirely ─────────────────────────────────
        if resp.status_code == 404:
            # Check on-chain first before treating as phantom
            denom = _ctf_payout_denominator(cid)
            if denom > 0:
                log.info(f"🔗 CTF resolved (CLOB 404 but on-chain settled): {cid[:16]}...")
                # Can't determine price from CLOB — use Gamma as backup
                gamma_result = _check_gamma_for_resolution(cid, bet)
                if gamma_result:
                    return gamma_result
                # CTF resolved but can't determine winner — treat as won (payout known)
                log.info(f"🔗 Assuming WIN based on CTF resolution: {cid[:16]}...")
                return {"bet": bet, "won": True, "payout": bet["potential_payout"]}
            log.warning(f"Bet {cid[:16]}... not found on CLOB and not resolved on-chain — removing as phantom")
            return {"bet": bet, "won": False, "payout": 0.0}

        resp.raise_for_status()
        market = resp.json()
        tokens = market.get("tokens", [])

        # ── PRIMARY: On-chain CTF check ───────────────────────────────────────
        # payoutDenominator > 0 = condition resolved on blockchain.
        # Zero API lag, zero garbage data. This is ground truth.
        denom = _ctf_payout_denominator(cid)
        if denom > 0:
            # Find our outcome token's current price — winning token will be > 0.5
            for token in tokens:
                if token.get("outcome", "").upper() == bet["outcome"].upper():
                    try:
                        price = float(token.get("price", 0))
                    except (TypeError, ValueError):
                        price = 0.0
                    won = price > 0.5
                    log.info(
                        f"🔗 CTF resolved: {cid[:16]}... | "
                        f"bet={bet['outcome']} price={price:.3f} → {'WON' if won else 'LOST'}"
                    )
                    return {"bet": bet, "won": won, "payout": bet["potential_payout"] if won else 0.0}
            # No token found for our outcome — fall through to other checks
            log.info(f"🔗 CTF resolved but no price data for {bet['outcome']} on {cid[:16]}...")

        # ── SECONDARY: CLOB closed flag ───────────────────────────────────────
        if market.get("closed", False):
            result = _resolve_from_tokens(tokens, bet["outcome"], bet)
            if result:
                return result
            return {"bet": bet, "won": False, "payout": 0.0}

        # ── TERTIARY: Token price near 1.0 (CLOB lagging closed flag) ─────────
        for token in tokens:
            if token.get("outcome", "").upper() == bet["outcome"].upper():
                try:
                    price = float(token.get("price", 0))
                except (TypeError, ValueError):
                    price = 0.0
                if price >= 0.99:
                    log.info(f"💡 Token price={price:.3f} → WIN (CLOB closed flag lagging): {cid[:16]}...")
                    return {"bet": bet, "won": True, "payout": bet["potential_payout"]}
                break

    except Exception as e:
        log.error(f"Error checking bet {cid}: {e}")
    return None  # still open


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
        try:
            for future in as_completed(futures, timeout=90):  # 90s — 30 bets × ~3s each
                try:
                    result = future.result()
                    if result:
                        resolved.append(result)
                except Exception as worker_err:
                    bet_id = futures[future].get("polymarket_id", "?")[:16]
                    log.warning(f"check_pending_bets: worker failed for {bet_id}...: {type(worker_err).__name__}: {worker_err}")
        except TimeoutError:
            done_count = sum(1 for f in futures if f.done())
            log.warning(
                f"check_pending_bets: timed out after 90s "
                f"({done_count}/{len(futures)} futures finished) — processing what we have"
            )
            # as_completed yields futures as they finish, so resolved[] already
            # contains all results from futures that completed before the timeout.
            # Nothing extra to collect — just continue with what we have.

    for r in resolved:
        bet = r["bet"]
        won = r["won"]
        payout = r["payout"]

        if won:
            # Try on-chain claim (needs MATIC gas). If no MATIC — user claims manually on polymarket.com,
            # which is gasless (Polymarket pays gas through their relayer).
            # IMPORTANT: only update bankroll if claim actually succeeded.
            # If claim fails, bankroll stays unchanged — sync_bankroll_from_clob at next
            # startup will reconcile once user claims manually.
            claimed = claim_winnings(
                condition_id=bet["polymarket_id"],
                outcome=bet["outcome"],
                payout=payout,
            )
            resolve_bet(bet["polymarket_id"], True, payout)
            if claimed:
                bankroll = get_bankroll()
                new_balance = bankroll + payout
                set_bankroll(new_balance, payout, "WIN")
                log.info(f"🎉 WON ${payout:.2f} + auto-claimed! New bankroll: ${new_balance:.2f}")
            else:
                # Don't inflate bankroll — CLOB doesn't have the money yet.
                # polymarket.com → claim manually (gasless) → bankroll syncs on next restart.
                log.info(f"🎉 WON ${payout:.2f} — claim on polymarket.com (bankroll syncs on next restart)")
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


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

_betting_shutdown = False


def set_betting_shutdown():
    """Called by main.py SIGTERM handler (must run in main thread)."""
    global _betting_shutdown
    _betting_shutdown = True
    log.info("🛑 Betting agent shutdown requested — will exit after current cycle")

def is_betting_shutdown() -> bool:
    return _betting_shutdown


def reconcile_pending_bets():
    """
    Startup reconciliation: clear DB pending bets that have already resolved.
    Handles zombie bets from: manual claims on polymarket.com, failed check cycles,
    CLOB lag, phantom GTC orders that never filled, 404s, etc.

    IMPORTANT — does NOT adjust bankroll.
    sync_bankroll_from_clob() runs AFTER this and sets bankroll from CLOB truth.
    Touching bankroll here would double-count manually claimed wins:
      - User claims win manually → money already in CLOB balance
      - sync sets DB bankroll = CLOB balance (correct, includes that payout)
      - If reconcile ALSO does bankroll += payout → double-counted, Larry bets too much
    Solution: reconcile only cleans up the bets table. sync owns the bankroll number.
    """
    # get_pending_bets and resolve_bet are already imported at the top of this file
    pending = get_pending_bets()
    if not pending:
        return
    log.info(f"🔍 Startup reconciliation: checking {len(pending)} pending bets for stale entries...")
    resolved_count = 0

    # Phase 1: parallel status checks (read-only API calls — safe to run concurrently)
    resolved_results = []
    with ThreadPoolExecutor(max_workers=min(len(pending), 8)) as ex:
        futures = {ex.submit(_check_single_bet, bet): bet for bet in pending}
        try:
            for future in as_completed(futures, timeout=90):
                try:
                    result = future.result()
                    if result:
                        resolved_results.append(result)
                except Exception as _worker_err:
                    log.debug(f"reconcile worker error: {type(_worker_err).__name__}: {_worker_err}")
        except TimeoutError:
            done_count = sum(1 for f in futures if f.done())
            log.warning(
                f"reconcile_pending_bets: timed out after 90s "
                f"({done_count}/{len(futures)} futures finished) — processing what we have"
            )

    # Phase 2: sequential claims — MUST be outside ThreadPoolExecutor so each
    # claim_winnings call completes (including wait_for_transaction_receipt) before
    # the next one starts. Running claims concurrently causes nonce collisions:
    # all threads call get_transaction_count() simultaneously, get the same nonce,
    # and every TX after the first fails with "replacement transaction underpriced".
    for result in resolved_results:
        bet    = result["bet"]
        won    = result["won"]
        payout = result["payout"]
        if won:
            # Try to claim — wrap so a single failed claim doesn't abort all reconciliation
            try:
                claim_winnings(
                    condition_id=bet["polymarket_id"],
                    outcome=bet["outcome"],
                    payout=payout,
                )
            except Exception as claim_err:
                log.warning(
                    f"⚠️  Claim failed during reconcile for {bet.get('polymarket_id','?')[:16]}... "
                    f"({type(claim_err).__name__}: {claim_err}) — marking resolved anyway, claim manually on polymarket.com"
                )
        # Mark resolved in DB — bankroll NOT modified here (sync_bankroll runs after)
        try:
            resolve_bet(bet["polymarket_id"], won, payout if won else 0.0)
        except Exception as db_err:
            log.warning(f"⚠️  resolve_bet failed ({type(db_err).__name__}: {db_err}) — will retry next startup")
            continue
        label = f"WIN +${payout:.2f}" if won else f"LOSS/PHANTOM ${bet.get('amount_usdc', 0):.2f}"
        log.info(f"🗑️  Reconciled {label}: {bet.get('question', '?')[:50]}")
        resolved_count += 1
    remaining = len(get_pending_bets())
    if resolved_count:
        log.info(f"✅ Reconciliation: cleared {resolved_count} stale bets → {remaining} open remain")
    else:
        log.info(f"✅ Reconciliation: all {len(pending)} bets still genuinely open")


def run_betting_agent():
    log.info("🎰 Larry's Betting Agent starting up...")
    init_db()

    # Enable WAL mode for SQLite — allows concurrent reads+writes from multiple threads
    # (twitter agent + betting agent both write to the same DB simultaneously)
    try:
        import sqlite3
        with sqlite3.connect("/app/data/larry.db") as _conn:
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=5000")  # wait up to 5s if locked
        log.info("✅ SQLite WAL mode enabled")
    except Exception as e:
        log.debug(f"WAL mode setup skipped: {e}")  # non-critical

    # Restore scan page from DB so rotation continues across restarts
    _load_scan_page()
    log.info(f"📖 Scan page restored: {_scan_page}/10")

    # Restore unsellable position blacklist from DB
    _load_unsellable()
    # Clear blacklist on startup — CONDITIONAL allowance is now set per token_id
    # before each sell attempt, so previously-failing positions should work now.
    global _unsellable_positions
    if _unsellable_positions:
        log.info(f"🔓 Clearing {len(_unsellable_positions)} blacklisted positions — allowance now set per-sell")
        _unsellable_positions = set()
        _save_unsellable()

    client = get_clob_client()

    if _BUILDER_API_KEY:
        log.info("⛽ Gasless claim enabled via Builder Relayer")
    else:
        log.warning("⚠️  POLYMARKET_BUILDER_API_KEY not set — auto-claim disabled, claim manually on polymarket.com")

    # Step 1: Clear zombie bets (resolved/phantom bets stuck in DB)
    # Must run BEFORE sync so that cleared bets don't inflate "exposure" after sync.
    # Does NOT touch bankroll — sync owns that number.
    reconcile_pending_bets()

    # Step 2: Sync bankroll from CLOB AFTER reconcile.
    # CLOB balance is ground truth: it already reflects all wins (including manually
    # claimed ones) and any phantom bets that never actually consumed USDC.
    sync_bankroll_from_clob(client)

    while not _betting_shutdown:
        try:
            log.info("--- Betting cycle starting ---")

            # Sync CLOB balance every cycle — keeps DB accurate after manual claims,
            # failed bets, or any external USDC movement. One API call per 30 min.
            log.info("🔄 Syncing balance with CLOB...")
            sync_bankroll_from_clob(client)

            # 1. Proactive sweep — claim any unclaimed winnings via Builder Relayer.
            # Catches winnings from previous cycles that never got claimed (e.g. $14.25 stuck in "Получить").
            sweep_unclaimed_winnings(client)

            # 2. Check if pending bets resolved — claims newly resolved positions
            check_pending_bets(client)

            # 2. Check free bankroll — bet until it hits zero (no exposure cap)
            open_bets = get_pending_bets()
            bankroll = get_bankroll()
            positions_value, n_positions = get_positions_value()
            if positions_value > 0:
                total_portfolio = bankroll + positions_value
                log.info(
                    f"💼 Portfolio: ${total_portfolio:.2f} total "
                    f"(${bankroll:.2f} free + ${positions_value:.2f} in {n_positions} positions)"
                )
            else:
                # Gamma unavailable — fall back to DB cost sum
                open_exposure = sum(float(b.get("amount_usdc", 0)) for b in open_bets)
                log.info(f"💼 Bankroll: ${bankroll:.2f} free | ~${open_exposure:.2f} cost in {len(open_bets)} open bets (Gamma unavailable)")
            markets = None  # always initialize before bankroll branches so `if markets:` below never raises UnboundLocalError
            if bankroll <= 0:
                log.info("No free bankroll remaining — waiting for open bets to resolve")
            else:
                # 3b. If bankroll too low to bet ($5 min), wait for open bets to resolve.
                #     Larry only sells positions he's genuinely changed his mind on — not
                #     just to free capital for the next bet.  Forcing a sell-to-bet cycle
                #     destroys value: you exit a position you believe in just to enter a
                #     new one.  Better to be patient and let winners resolve.
                if bankroll < 5.0 and open_bets:
                    log.info(
                        f"💤 Bankroll ${bankroll:.2f} < $5 — waiting for open positions to resolve. "
                        f"Larry holds his convictions."
                    )

                # Guard: if under $5, skip Claude entirely.
                if bankroll < 5.0:
                    log.info(f"💤 Bankroll ${bankroll:.2f} < $5 minimum — skipping market scan until capital frees up")
                else:
                    # 4. Fetch markets — three parallel Gamma batches, 24h window
                    markets = fetch_active_markets()

                if markets:
                    # Filter out markets Larry already has open bets on, token-blacklisted,
                    # or already passed on this cycle (price/urgency unchanged).
                    # NOTE: use ALL-time bet IDs (not just pending) so resolved markets don't
                    #       trigger an IntegrityError from the UNIQUE constraint on polymarket_id.
                    pending_bets_now = get_pending_bets()
                    all_ever_bet_ids  = _get_all_bet_market_ids()  # includes WON/LOST
                    open_bet_ids  = {(b.get("polymarket_id") or "").lower() for b in pending_bets_now}
                    open_questions = {(b.get("question") or "").lower().strip() for b in pending_bets_now}
                    combined_bet_ids = open_bet_ids | all_ever_bet_ids
                    fresh_markets = [
                        m for m in markets
                        if m["condition_id"].lower() not in combined_bet_ids
                        and m.get("question", "").lower().strip() not in open_questions
                        and not _is_token_blacklisted(m["condition_id"])
                        and not _is_pass_cached(m)
                    ]
                    skipped_open  = sum(1 for m in markets if m["condition_id"].lower() in combined_bet_ids or m.get("question","").lower().strip() in open_questions)
                    skipped_pass  = sum(1 for m in markets if _is_pass_cached(m))
                    skipped_token = len(markets) - len(fresh_markets) - skipped_open - skipped_pass
                    log.info(
                        f"Filter: {len(markets)} total → {len(fresh_markets)} fresh "
                        f"(open_bets={skipped_open}, pass_cache={skipped_pass} [{len(_pass_cache)} cached], blacklist/token={skipped_token})"
                    )
                    markets = fresh_markets

                if markets:
                    # 5. Ask Claude / Larry which ones to bet on
                    decisions = ask_larry_to_bet(markets)
                    n_bets  = sum(1 for d in decisions if d.get("decision") == "BET")
                    n_pass  = len(decisions) - n_bets
                    log.info(f"Larry made {len(decisions)} decisions — {n_bets} BETs, {n_pass} PASSes")

                    # Build market lookup dict once — avoids O(N×M) searches in the decision loop
                    market_by_id = {m["condition_id"]: m for m in markets}

                    # Snapshot pending bets at decision-loop start — refreshed only when
                    # a new bet is placed (avoiding 1 DB query per decision).
                    mid_loop_bets = get_pending_bets()
                    mid_loop_ids  = {(b.get("polymarket_id") or "").lower() for b in mid_loop_bets}
                    mid_loop_qs   = {(b.get("question") or "").lower().strip() for b in mid_loop_bets}

                    for decision in decisions:
                        if decision.get("decision") != "BET":
                            log.info(f"PASS: {decision.get('reasoning', 'no reason given')}")
                            # Cache PASS so we don't re-send same market next cycle
                            mid = (decision.get("market_id") or "").lower()
                            if mid:
                                m_info = market_by_id.get(mid)
                                if m_info:
                                    _cache_pass(mid, m_info.get("yes_price", 0.5), m_info.get("hours_to_end", 24))
                            continue

                        if get_bankroll() <= 0:
                            log.info("Bankroll empty — stopping mid-loop")
                            break

                        # Resolve market_info once — reused for DB save and odds
                        # For neg-risk markets multiple entries share condition_id,
                        # so also match on outcome_name
                        market_id = (decision.get("market_id") or "").lower()
                        decision_outcome = decision.get("outcome", "")

                        # Guard: Claude sometimes returns empty/malformed market_id
                        if not market_id:
                            log.warning(f"Decision missing market_id — skipping (outcome={decision_outcome}, reasoning={decision.get('reasoning','')[:60]})")
                            continue

                        # Skip if we already have an open bet on this exact market.
                        # Use cached sets from the snapshot — updated immediately after each new bet.
                        already_open = market_id in mid_loop_ids
                        if not already_open:
                            # Fallback: same question text (catches ID format mismatches)
                            decision_question = (market_by_id.get(market_id) or {}).get("question", "").lower().strip()
                            if decision_question:
                                already_open = decision_question in mid_loop_qs
                        if already_open:
                            log.info(f"Already have open bet on {market_id[:20]}..., skipping")
                            continue
                        market_info = next(
                            (m for m in markets if m["condition_id"] == market_id
                             and (not m.get("neg_risk") or
                                  m.get("outcome_name", "").lower() == decision_outcome.lower())),
                            {}
                        )

                        # 6. Determine bet amount with safety clamps
                        bankroll = get_bankroll()
                        try:
                            raw_amount = float(decision.get("amount_usdc") or ABSOLUTE_MIN_BET)
                        except (TypeError, ValueError):
                            raw_amount = ABSOLUTE_MIN_BET
                        # CLOB minimum is $5; cap at ABSOLUTE_MAX_BET AND 50% bankroll
                        # (even if Larry's Kelly suggests more — prevents a single bad
                        # bet from wiping the bankroll if something goes wrong).
                        actual_amount = max(raw_amount, 5.0)
                        actual_amount = min(actual_amount, ABSOLUTE_MAX_BET, bankroll * 0.5)
                        if actual_amount < ABSOLUTE_MIN_BET:
                            log.info(
                                f"Skipping bet — ${actual_amount:.2f} after bankroll cap "
                                f"< ${ABSOLUTE_MIN_BET} CLOB minimum (bankroll=${bankroll:.2f})"
                            )
                            continue
                        if actual_amount > bankroll:
                            log.warning(f"Not enough bankroll (${bankroll:.2f}) for ${actual_amount:.2f} bet")
                            continue

                        # 7. Place the bet
                        success = place_bet(client, decision)

                        if success:
                            # Deduct from bankroll — use actual_amount ($5 min), not Kelly amount,
                            # so DB stays in sync with what CLOB actually reserved.
                            new_balance = bankroll - actual_amount
                            set_bankroll(new_balance, -actual_amount, "BET_PLACED")

                            # market_info already resolved above — no duplicate lookup
                            # no_price was removed from market dict; derive it
                            outcome_for_odds = decision.get("outcome", "YES")
                            raw_yes = market_info.get("yes_price", 0.5)
                            if outcome_for_odds == "YES":
                                odds = max(0.01, min(raw_yes, 0.99))
                            else:
                                odds = max(0.01, min(round(1 - raw_yes, 4), 0.99))
                            potential_payout = actual_amount / odds

                            bet_id = None
                            q_text = market_info.get("question", "Unknown market")
                            for _db_attempt in range(4):
                                try:
                                    bet_id = save_bet(
                                        polymarket_id=market_id,
                                        question=q_text,
                                        outcome=outcome_for_odds,
                                        amount=actual_amount,
                                        odds=odds,
                                        potential_payout=potential_payout,
                                        category=market_info.get("category", "weird"),
                                        larry_comment=decision.get("reasoning", ""),
                                    )
                                    break  # success
                                except Exception as db_err:
                                    err_str = str(db_err).lower()
                                    if "locked" in err_str and _db_attempt < 3:
                                        time.sleep(0.5 * (_db_attempt + 1))  # 0.5s, 1s, 1.5s
                                        continue
                                    # UNIQUE or non-recoverable error — bet on-chain but not tracked
                                    log.warning(f"⚠️  Could not save bet to DB ({type(db_err).__name__}): {market_id[:16]}... — bet placed but not tracked")
                                    break

                            # Update in-loop dedup cache so next decision in this same
                            # batch sees this bet as already open (avoids re-betting).
                            mid_loop_ids.add(market_id)
                            mid_loop_qs.add(q_text.lower().strip())

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
            err_str = str(e).lower()
            # SECURITY: no exc_info (traceback can expose env vars/keys)
            log.error(f"Unexpected error in betting loop: {type(e).__name__}: {e}")
            # DB lock at cycle level: short backoff then continue normally
            if "locked" in err_str or "operationalerror" in type(e).__name__.lower():
                log.warning("DB locked at cycle level — sleeping 10s then retrying")
                time.sleep(10)

        if _betting_shutdown:
            break

        # Wait before next cycle
        log.info(f"💤 Sleeping {BET_CHECK_INTERVAL_MINUTES} minutes...")
        time.sleep(BET_CHECK_INTERVAL_MINUTES * 60)

    log.info("✅ Betting agent exited cleanly")


if __name__ == "__main__":
    run_betting_agent()
