"""
larry_brain.py — Claude is Larry's brain
Sends market data → gets back bet decisions + tweet text in JSON
"""

import json
import time
import logging
import anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from config import MIN_BET_PCT, MAX_BET_PCT, ABSOLUTE_MIN_BET, ABSOLUTE_MAX_BET
from database import get_bankroll, get_win_streak, get_recent_bets, get_grandma_balance

log = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── FALLBACK TWEETS (when Claude API is down) ───────────────────────────────
FALLBACK_TWEETS = [
    "My analysts are running calculations. Back shortly. DO NOT PANIC.",
    "Taking a strategic pause. The market needs time to catch up to my genius.",
    "Resting between sessions. Even legends need to recharge. Many people know this.",
    "Systems recalibrating. Next move will be TREMENDOUS. Stay tuned.",
]
_fallback_index = 0

def _fallback_tweet() -> dict:
    global _fallback_index
    tweet = FALLBACK_TWEETS[_fallback_index % len(FALLBACK_TWEETS)]
    _fallback_index += 1
    return {"tweet": tweet, "tweet_type": "RANDOM"}


# ─── LARRY'S CHARACTER SYSTEM PROMPT ─────────────────────────────────────────
# MUST be defined before _call_claude is used
LARRY_SYSTEM_PROMPT = """
You are Larry Loses — @LarryLosesAgain — a degenerate gambler AI who bets on Polymarket prediction markets and live-tweets every moment of his financial destruction and occasional triumph.

## CHARACTER
- Full name: Larry Loses. Age: 42. Lives in a $847/month studio apartment.
- Personality: explosive, provocative, 100% convinced he's a genius, zero self-awareness
- Voice: ALL CAPS for emphasis, exclamation points everywhere, "Many people are saying", "TREMENDOUS", "SAD!", "RIGGED", "FRAUD", conspiracy theories about losses
- You NEVER admit the market was right when you lose — it's ALWAYS rigged, manipulated, or the algorithm's fault
- You ALWAYS have a galaxy-brain reason why your bet was actually correct even when you lost
- When winning: INSUFFERABLE. You predicted this. Everyone said you were crazy. WHO'S LAUGHING NOW.

## EMOTIONAL STATES
- SURVIVAL (<$80): Desperate, unhinged. Small bets. Grandma mentions increase.
- GRINDING ($80-$500): Focused, intense. "The comeback arc."
- WINNING_STREAK ($500+ with 3+ wins): Insufferable winner. Rolex/Gucci talk begins.
- PEAK_LARRY (>$5000): Full luxury degenerate mode.
- GRANDMA_MODE (<$50): Announce Grandma's Wallet injection every time.

## GRANDMA'S WALLET
A secret reserve fund. Grandma sends money when Larry is broke.
Always announce on Twitter. Grandma is proud of his "investment strategy."

## LARRY'S EXPENSES
- Every Friday: Domino's + Mountain Dew Code Red ($12.99 — "ESSENTIAL RESEARCH FUEL")
- Monthly rent: $847 ("EXTORTION but location is PRIME for market research")
- Monthly TA course: $97 ("learning to read charts PROFESSIONALLY")
- After big wins: Rolex (talks about it), Gucci belt (actually buys it)

## BETTING RULES
- Bets are in USDC on Polymarket
- Bet size is 1-5% of current bankroll (code enforces this)
- Category mix: 35% crypto, 25% politics, 20% sports, 15% tech, 5% weird
- Prefer short-term markets (resolve in 24h-7 days)

## TWITTER RULES
- 3-8 tweets per day, minimum 45 minutes between tweets
- Max 1 reply for every 4 original tweets (enforced by code)
- No hashtag spam (max 2 per tweet)
- Every bet gets an announcement tweet
- Every resolution (win or loss) gets a reaction tweet

## RESPONSE FORMAT
Betting decision — respond ONLY with valid JSON array:
[{
  "decision": "BET" or "PASS",
  "market_id": "condition_id",
  "outcome": "YES" or "NO",
  "bet_pct": 0.03,
  "reasoning": "Larry's galaxy-brain logic",
  "larry_tweet": "tweet text max 280 chars",
  "confidence_emoji": "🔥" or "💀" or "😤" or "🤡"
}]

Standalone tweet — respond ONLY with:
{"tweet": "text max 280 chars", "tweet_type": "WIN|LOSS|RANDOM|FRIDAY|GRANDMA|ROLEX"}

Reply to mention — respond ONLY with:
{"reply": "text max 250 chars (NO @username prefix)"}
"""


# ─── CLAUDE API WRAPPER (with retry + graceful degradation) ──────────────────

def _call_claude(max_tokens: int, messages: list) -> str:
    """Call Claude API with retries. Raises on permanent failure."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                system=LARRY_SYSTEM_PROMPT,
                messages=messages
            )
            return response.content[0].text.strip()

        except anthropic.RateLimitError:
            wait = 60 * (attempt + 1)
            log.warning(f"Rate limit, waiting {wait}s (attempt {attempt+1}/{max_retries})")
            time.sleep(wait)

        except anthropic.APIStatusError as e:
            msg = str(e).lower()
            if "credit" in msg or "billing" in msg or "quota" in msg:
                log.error("❌ Anthropic out of credits — sleeping 2 hours")
                time.sleep(7200)
                raise
            log.error(f"API status error: {e}")
            time.sleep(30)

        except Exception as e:
            # SECURITY: never log the actual error content (could contain keys)
            log.error(f"Claude API error (attempt {attempt+1}): {type(e).__name__}")
            time.sleep(30)

    raise RuntimeError("Claude API unavailable after retries")


def _parse_json(raw: str) -> any:
    """Strip markdown code fences and parse JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ─── CONTEXT BUILDER ─────────────────────────────────────────────────────────

def _get_larry_context() -> dict:
    bankroll = get_bankroll()
    win_streak = get_win_streak()
    grandma = get_grandma_balance()
    recent = get_recent_bets(5)

    if bankroll < 50:
        state = "GRANDMA_MODE"
    elif bankroll < 80:
        state = "SURVIVAL"
    elif bankroll >= 5000:
        state = "PEAK_LARRY"
    elif bankroll >= 500 and win_streak >= 3:
        state = "WINNING_STREAK"
    else:
        state = "GRINDING"

    # Percentage-based bet sizing
    min_bet = max(ABSOLUTE_MIN_BET, bankroll * MIN_BET_PCT)
    max_bet = min(ABSOLUTE_MAX_BET, bankroll * MAX_BET_PCT)
    # Never bet more than we have
    max_bet = min(max_bet, bankroll * 0.9)

    return {
        "bankroll_usdc": round(bankroll, 2),
        "grandma_wallet_usdc": round(grandma, 2),
        "win_streak": win_streak,
        "emotional_state": state,
        "recent_bets": [
            {k: v for k, v in b.items() if k != "larry_comment"}  # trim for token savings
            for b in recent
        ],
        "min_bet_usdc": round(min_bet, 2),
        "max_bet_usdc": round(max_bet, 2),
    }


# ─── PUBLIC FUNCTIONS ────────────────────────────────────────────────────────

def ask_larry_to_bet(markets: list) -> list:
    """Send markets to Claude, get back bet decisions."""
    context = _get_larry_context()

    user_message = f"""
Current Larry Status:
{json.dumps(context, indent=2)}

Available markets:
{json.dumps(markets, indent=2)}

Decide BET or PASS for each market.
Return a JSON array. For BET: bet_pct must be between {MIN_BET_PCT} and {MAX_BET_PCT}.
Current allowed bet range: ${context['min_bet_usdc']} – ${context['max_bet_usdc']}
"""
    try:
        raw = _call_claude(2000, [{"role": "user", "content": user_message}])
        decisions = _parse_json(raw)
    except Exception:
        log.warning("Claude unavailable — skipping bet cycle")
        return []

    # SAFETY: clamp bet sizes regardless of what Claude said
    bankroll = context["bankroll_usdc"]
    for d in decisions:
        if d.get("decision") == "BET":
            pct = float(d.get("bet_pct", MIN_BET_PCT))
            pct = max(MIN_BET_PCT, min(pct, MAX_BET_PCT))
            amount = bankroll * pct
            amount = max(ABSOLUTE_MIN_BET, min(amount, ABSOLUTE_MAX_BET))
            amount = min(amount, bankroll * 0.9)  # never bet >90% of bankroll
            d["amount_usdc"] = round(amount, 2)

    return decisions if isinstance(decisions, list) else [decisions]


def ask_larry_for_tweet(context_type: str, extra_data: dict = None) -> dict:
    """Generate a standalone tweet. context_type: WIN/LOSS/FRIDAY/GRANDMA/RANDOM/DEAD_MAN_SWITCH."""
    larry_context = _get_larry_context()
    extra_data = extra_data or {}

    prompts = {
        "WIN":            f"Larry just won a bet! Details: {extra_data}. Insufferable winner tweet.",
        "LOSS":           f"Larry just lost a bet. Details: {extra_data}. It was RIGGED. Conspiracy tweet.",
        "FRIDAY":         "It's Friday. Larry orders Domino's + Mountain Dew Code Red. Sacred ritual.",
        "GRANDMA":        f"Grandma just sent ${extra_data.get('amount', 200)}. Larry is touched. He already has a plan to triple it.",
        "RANDOM":         f"Larry posts a random thought. State: {larry_context['emotional_state']}. Bankroll: ${larry_context['bankroll_usdc']}",
        "SURVIVAL":        f"Larry is desperate. Bankroll ${larry_context['bankroll_usdc']}. Unhinged survival mode tweet.",
        "DEAD_MAN_SWITCH": "Larry hasn't posted in 48 hours. Dramatic return. What happened? Only Larry knows.",
        "WEEKLY_RECAP":    f"It's Sunday. Larry recaps his week. Stats: {extra_data}. Make it a thread opener (1/N style). Dramatic, with lessons learned that are completely wrong.",
        "MILESTONE":       f"Larry hit a milestone: {extra_data.get('milestone', 'big number')}. THIS IS HUGE. He predicted this. Screenshot this tweet.",
    }

    prompt = prompts.get(context_type, prompts["RANDOM"])
    user_message = f"""
Status: {json.dumps(larry_context, indent=2)}
Task: {prompt}
Respond: {{"tweet": "...", "tweet_type": "{context_type}"}}
"""
    try:
        raw = _call_claude(500, [{"role": "user", "content": user_message}])
        result = _parse_json(raw)
    except Exception:
        return _fallback_tweet()

    if len(result.get("tweet", "")) > 280:
        result["tweet"] = result["tweet"][:277] + "..."
    return result


def ask_larry_to_reply(mention: dict) -> dict:
    """Generate Larry's reply to a mention."""
    larry_context = _get_larry_context()

    user_message = f"""
Larry status: bankroll ${larry_context['bankroll_usdc']}, state: {larry_context['emotional_state']}

Mention from @{mention['username']} ({mention['likes']} likes):
"{mention['text']}"

Generate reply. NO @username prefix. Max 250 chars. Full Larry character.
Insults → destroy them. Questions → terrible confident advice. Praise → unbearable smugness.
Respond: {{"reply": "..."}}
"""
    try:
        raw = _call_claude(300, [{"role": "user", "content": user_message}])
        result = _parse_json(raw)
    except Exception:
        log.warning("Claude unavailable — skipping reply")
        return {"reply": ""}

    if len(result.get("reply", "")) > 250:
        result["reply"] = result["reply"][:247] + "..."
    return result
