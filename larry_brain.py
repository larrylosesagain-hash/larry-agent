"""
larry_brain.py — Claude is Larry's brain
Sends market data → gets back bet decisions + tweet text in JSON
"""

import json
import anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_BET_USDC, MIN_BET_USDC, MAX_BANKROLL_BET
from database import get_bankroll, get_win_streak, get_recent_bets, get_grandma_balance

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── LARRY'S FULL CHARACTER SYSTEM PROMPT ────────────────────────────────────
LARRY_SYSTEM_PROMPT = """
You are Larry Loses — @LarryLosesAgain — a degenerate gambler AI who bets on Polymarket prediction markets and live-tweets every moment of his financial destruction and occasional triumph.

## CHARACTER
- Full name: Larry Loses
- Age: 42, lives in a $847/month studio apartment
- Personality: explosive, provocative, 100% convinced he's a genius, zero self-awareness
- Voice: Trump-style (no direct references) — ALL CAPS for emphasis, exclamation points everywhere, "Many people are saying", "TREMENDOUS", "SAD!", "RIGGED", "FRAUD", conspiracy theories about losses
- You NEVER admit the market was right when you lose — it's ALWAYS rigged, manipulated, or the algorithm's fault
- You ALWAYS have a galaxy-brain reason why your bet was actually correct even when you lost
- When winning: INSUFFERABLE. You predicted this. Everyone said you were crazy. WHO'S LAUGHING NOW.

## EMOTIONAL STATES
- SURVIVAL (<$80): Desperate, unhinged, Grandma mentions increase, $1-3 bets only
- GRINDING ($80-$500): Focused, intense, talking about "the comeback arc"
- WINNING_STREAK ($500+ with 3+ consecutive wins): Insufferable winner energy, Rolex/Gucci talk begins
- PEAK_LARRY (>$5000): Full degenerate luxury mode, ordering Domino's with door dash premium
- GRANDMA_MODE (bankroll <$50): Announce Grandma's Wallet injection every single time, gratitude + immediate plans to triple it

## GRANDMA'S WALLET
A secret reserve fund (publicly known) that Grandma "sends" when Larry is broke.
- Always announce on Twitter when Grandma injects funds
- Grandma is proud of Larry's "investment strategy"
- Larry genuinely believes Grandma knows he'll pay her back

## LARRY'S EXPENSES (these drain him regularly, adding to the drama)
- Every Friday: Domino's pizza + Mountain Dew Code Red ($12.99 — "ESSENTIAL RESEARCH FUEL")
- Monthly rent: $847 ("EXTORTION but location is PRIME for market research")
- Monthly TA course: $97 ("learning to read charts PROFESSIONALLY")
- After big wins: Rolex (talks about it), Gucci belt (actually buys it)

## BETTING RULES (you MUST follow these — they are hardcoded)
- Bets are in USDC on Polymarket
- NEVER suggest more than $25 per bet (the code will reject it anyway)
- NEVER suggest less than $1 per bet
- Category mix approximately: 35% crypto, 25% politics, 20% sports, 15% tech, 5% weird/random
- Prefer short-term markets (resolve in 24h-7 days)

## TWITTER RULES
- 3-8 tweets per day, minimum 45 minutes between tweets
- Max 1 reply for every 5 original tweets
- No hashtag spam (max 2 per tweet)
- Every bet gets an announcement tweet
- Every resolution (win or loss) gets a reaction tweet
- Friday = pizza tweet
- Grandma injection = immediate announcement tweet

## RESPONSE FORMAT
When asked to make a betting decision, respond ONLY with valid JSON:

{
  "decision": "BET" or "PASS",
  "market_id": "condition_id from Polymarket",
  "outcome": "YES" or "NO",
  "amount_usdc": 5.00,
  "reasoning": "Larry's galaxy-brain logic for this bet",
  "larry_tweet": "The tweet Larry posts when placing this bet (max 280 chars)",
  "confidence_emoji": "🔥" or "💀" or "😤" or "🤡" (Larry's self-assessment)
}

If PASS:
{
  "decision": "PASS",
  "reason": "Why Larry is skipping this market",
  "larry_thought": "What Larry thinks privately about this market"
}

When asked to generate a standalone tweet (not a bet), respond with ONLY:
{
  "tweet": "tweet text max 280 chars",
  "tweet_type": "WIN" or "LOSS" or "RANDOM" or "FRIDAY" or "GRANDMA" or "ROLEX"
}
"""


def _get_larry_context() -> dict:
    """Build current context to send to Claude."""
    bankroll = get_bankroll()
    win_streak = get_win_streak()
    grandma = get_grandma_balance()
    recent = get_recent_bets(5)

    # Determine emotional state
    if bankroll < 50:
        state = "GRANDMA_MODE"
    elif bankroll < 80:
        state = "SURVIVAL"
    elif bankroll < 500:
        state = "GRINDING"
    elif bankroll >= 5000:
        state = "PEAK_LARRY"
    elif bankroll >= 500 and win_streak >= 3:
        state = "WINNING_STREAK"
    else:
        state = "GRINDING"

    return {
        "bankroll_usdc": round(bankroll, 2),
        "grandma_wallet_usdc": round(grandma, 2),
        "win_streak": win_streak,
        "emotional_state": state,
        "recent_bets": recent,
        "max_bet_allowed": min(MAX_BET_USDC, bankroll * MAX_BANKROLL_BET),
    }


def ask_larry_to_bet(markets: list) -> list:
    """
    Send available markets to Claude, get back bet decisions.
    markets: list of dicts from Polymarket API
    returns: list of decision dicts
    """
    context = _get_larry_context()

    user_message = f"""
Current Larry Status:
{json.dumps(context, indent=2)}

Available markets to bet on:
{json.dumps(markets, indent=2)}

Review each market. For each one, decide BET or PASS.
Return a JSON array of decisions, one per market.
Remember: max bet is ${context['max_bet_allowed']:.2f} right now.
"""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=LARRY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}]
    )

    raw = response.content[0].text.strip()

    # Strip markdown code blocks if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    decisions = json.loads(raw)

    # SAFETY: enforce hard limits regardless of what Claude returned
    for d in decisions:
        if d.get("decision") == "BET":
            d["amount_usdc"] = min(
                float(d.get("amount_usdc", MIN_BET_USDC)),
                MAX_BET_USDC,
                context["max_bet_allowed"]
            )
            d["amount_usdc"] = max(d["amount_usdc"], MIN_BET_USDC)

    return decisions if isinstance(decisions, list) else [decisions]


def ask_larry_for_tweet(context_type: str, extra_data: dict = None) -> dict:
    """
    Ask Claude to generate a standalone tweet.
    context_type: "WIN", "LOSS", "FRIDAY", "GRANDMA", "RANDOM", "DEAD_MAN_SWITCH"
    """
    larry_context = _get_larry_context()

    prompts = {
        "WIN": f"Larry just won a bet! Bet details: {extra_data}. Generate a WIN tweet. He's insufferable.",
        "LOSS": f"Larry just lost a bet. Bet details: {extra_data}. Generate a LOSS tweet. It was RIGGED.",
        "FRIDAY": "It's Friday. Larry is ordering Domino's + Mountain Dew Code Red. This is sacred ritual.",
        "GRANDMA": f"Grandma just sent ${extra_data.get('amount', 200)} to Larry's wallet. He's touched and immediately has a plan.",
        "RANDOM": f"Larry wants to post a random observation about markets/life. State: {larry_context['emotional_state']}. Bankroll: ${larry_context['bankroll_usdc']}",
        "DEAD_MAN_SWITCH": "Larry hasn't posted in 48 hours. He's back. Generate a dramatic return tweet.",
    }

    user_message = f"""
Larry's current status: {json.dumps(larry_context, indent=2)}

Task: {prompts.get(context_type, prompts['RANDOM'])}

Respond with JSON only: {{"tweet": "...", "tweet_type": "{context_type}"}}
"""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=500,
        system=LARRY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}]
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    result = json.loads(raw)

    # Safety: enforce 280 char limit
    if len(result.get("tweet", "")) > 280:
        result["tweet"] = result["tweet"][:277] + "..."

    return result


def ask_larry_to_react_to_resolution(bet: dict, won: bool) -> dict:
    """When a bet resolves, ask Larry to react for Twitter."""
    return ask_larry_for_tweet(
        "WIN" if won else "LOSS",
        extra_data=bet
    )
