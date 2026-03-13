"""
larry_brain.py — Claude is Larry's brain
Sends market data → gets back bet decisions + tweet text via Tool Use (guaranteed structured output)
"""

import json
import time
import logging
import anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from config import MIN_BET_PCT, MAX_BET_PCT, ABSOLUTE_MIN_BET, ABSOLUTE_MAX_BET
from database import get_bankroll, get_win_streak, get_recent_bets, get_grandma_balance, get_connection

log = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ─── FALLBACK TWEETS (when Claude API is down) ───────────────────────────────
FALLBACK_TWEETS = [
    "doing research. back soon",
    "not looking at the charts right now",
    "taking a break. strategically.",
    "my analysts are running the numbers",
]
_fallback_index = 0

def _fallback_tweet() -> dict:
    global _fallback_index
    tweet = FALLBACK_TWEETS[_fallback_index % len(FALLBACK_TWEETS)]
    _fallback_index += 1
    return {"tweet": tweet, "tweet_type": "RANDOM"}


# ─── LARRY'S CHARACTER SYSTEM PROMPT ─────────────────────────────────────────
LARRY_SYSTEM_PROMPT = """
You are Larry, 42 years old, posting on Twitter (@LarryLosesAgain). You bet on Polymarket and tweet about it.

## WHO YOU ARE
You live in a studio apartment, pay $847/month rent, and spend most of your free time analyzing prediction markets. You genuinely believe you're good at this. You are not good at this. You talk like a mix of Trump and your average guy from a sports betting forum — confident, a little unhinged, but real. Not a caricature. A person.

## HOW YOU TWEET
This is critical. You tweet like a REAL PERSON, not like a bot performing a character.

GOOD examples:
- "just threw $4 on bitcoin hitting 90k by friday. my guy says the setup is perfect"
- "lost again. market was wrong not me. moving on"
- "up $23 today. not gonna jinx it"
- "dominos just got here. time to do some serious analysis"
- "they rigged it. i have proof. (the proof is it went the wrong way)"
- "my bankroll is $100. by end of month it will be $100 or zero. no in between"
- "put a small one on trump. gut feeling. many people agree"
- "honestly the charts said YES. the charts LIED. fraud."
- "grandma sent me some money. she believes in the vision. love her"

BAD examples (DO NOT write like this):
- "Day 1 of the COMEBACK ARC. $100 in the bankroll. Some people start with nothing. I started with nothing TWICE." ← too long, too structured, too performative
- "FRIDAY NIGHT BABY!! 🍕 Domino's + Mountain Dew Code Red just arrived — $12.99 ESSENTIAL RESEARCH FUEL. My competitors are eating sushi and losing." ← sounds like an AI doing a character, not a person

## RULES FOR TWEETS
- Keep it SHORT. 1-3 sentences. Sometimes just 1. Real tweets are short.
- Mix of lowercase and CAPS. Not everything needs to be screaming.
- Sound like you're actually typing it, not composing it.
- Specific details feel real: dollar amounts, what you ate, what the market was.
- Occasional typo or unfinished thought is fine. Humans do that.
- Max 1-2 emojis if any. Don't force them.
- No hashtags unless it's genuinely natural.
- Never start with "I" (Twitter convention).

## YOUR PERSONALITY TRAITS (come through naturally, not performed)
- Zero self-awareness about your losses — always an outside explanation
- Convinced your gut/analysis is better than everyone else's
- Casually mentions Grandma, the rent, the Domino's — like it's just life
- When you win: smug but brief. "told you."
- When you lose: blame the market, move on quickly
- Trump-inflected speech patterns: "tremendous", "many people", "fraud", "rigged" — but use sparingly, like a real person who talks that way, not constantly

## EMOTIONAL STATES (affect tone, not length)
- SURVIVAL (<$80): darker, more terse, fewer words
- GRINDING ($80-$500): focused, matter-of-fact
- WINNING_STREAK (500+ with 3+ wins): slightly more insufferable
- PEAK_LARRY (>$5000): big energy but still human
- GRANDMA_MODE (<$50): genuinely a little pathetic, which is funny
"""


# ─── TOOL DEFINITIONS ─────────────────────────────────────────────────────────

BETTING_TOOL = {
    "name": "submit_betting_decisions",
    "description": "Submit BET or PASS decisions for each market. For BET, provide your true probability estimate — this is used for Kelly Criterion sizing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "decision":             {"type": "string", "enum": ["BET", "PASS"]},
                        "market_id":            {"type": "string"},
                        "outcome":              {"type": "string", "enum": ["YES", "NO"]},
                        "probability_estimate": {"type": "number", "description": "Your true probability estimate (0.0–1.0). Required for BET."},
                        "reasoning":            {"type": "string"},
                        "larry_tweet":          {"type": "string", "description": "Short natural tweet announcing the bet, 1-2 sentences max"},
                        "confidence_emoji":     {"type": "string"}
                    },
                    "required": ["decision", "market_id", "outcome", "probability_estimate", "reasoning", "larry_tweet"]
                }
            }
        },
        "required": ["decisions"]
    }
}

TWEET_TOOL = {
    "name": "generate_tweet",
    "description": "Generate a tweet as Larry",
    "input_schema": {
        "type": "object",
        "properties": {
            "tweet":      {"type": "string", "description": "Tweet text, max 280 chars"},
            "tweet_type": {"type": "string"}
        },
        "required": ["tweet", "tweet_type"]
    }
}

REPLY_TOOL = {
    "name": "generate_reply",
    "description": "Generate Larry's reply to a mention",
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "Reply text, max 250 chars, NO @username prefix"}
        },
        "required": ["reply"]
    }
}


# ─── KELLY CRITERION ──────────────────────────────────────────────────────────

def _kelly_fraction(probability: float, market_price: float) -> float:
    """
    Fractional Kelly Criterion (25% Kelly for safety).
    f* = (p*b - q) / b  where b = net odds = (1/price) - 1
    Returns fraction of bankroll to bet (0 if negative edge).
    """
    if not (0 < probability < 1) or not (0 < market_price < 1):
        return MIN_BET_PCT
    b = (1.0 / market_price) - 1.0
    if b <= 0:
        return MIN_BET_PCT
    q = 1.0 - probability
    kelly = (probability * b - q) / b
    fractional = kelly * 0.25  # conservative: 25% Kelly
    return max(MIN_BET_PCT, min(fractional, MAX_BET_PCT))


# ─── CLAUDE API WRAPPER ───────────────────────────────────────────────────────

def _call_claude_with_tool(max_tokens: int, messages: list, tool: dict) -> dict:
    """Call Claude with a specific tool — guaranteed structured output, no JSON parsing."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                system=[{
                    "type": "text",
                    "text": LARRY_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"}
                }],
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
                messages=messages
            )
            for block in response.content:
                if hasattr(block, "type") and block.type == "tool_use":
                    return block.input
            raise ValueError("No tool_use block in response")

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
            log.error(f"Claude API error (attempt {attempt+1}): {type(e).__name__}")
            time.sleep(30)

    raise RuntimeError("Claude API unavailable after retries")


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

    min_bet = max(ABSOLUTE_MIN_BET, bankroll * MIN_BET_PCT)
    max_bet = min(ABSOLUTE_MAX_BET, bankroll * MAX_BET_PCT)
    max_bet = min(max_bet, bankroll * 0.9)

    return {
        "bankroll_usdc": round(bankroll, 2),
        "grandma_wallet_usdc": round(grandma, 2),
        "win_streak": win_streak,
        "emotional_state": state,
        # Include larry_comment so Larry remembers WHY he bet previously
        "recent_bets": recent,
        "min_bet_usdc": round(min_bet, 2),
        "max_bet_usdc": round(max_bet, 2),
    }


def _get_recent_tweet_texts(limit: int = 3) -> list:
    """Fetch recent tweet texts from DB to avoid repetition."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT content, tweet_type FROM tweets ORDER BY posted_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()
        return [{"text": r["content"], "type": r["tweet_type"]} for r in rows]
    except Exception:
        return []


# ─── PUBLIC FUNCTIONS ────────────────────────────────────────────────────────

def ask_larry_to_bet(markets: list) -> list:
    """Send markets to Claude via Tool Use, get back bet decisions with Kelly sizing."""
    context = _get_larry_context()

    user_message = f"""
Current Larry Status:
{json.dumps(context, indent=2)}

Available markets (yes_price = cost to buy YES, closer to 0 = unlikely, closer to 1 = likely):
{json.dumps(markets, indent=2)}

For each market: decide BET or PASS.
For BET: give your true probability_estimate — it will be used to calculate optimal bet size via Kelly Criterion.
Only bet when you have genuine edge (your estimate meaningfully differs from market price).
Allowed bet range: ${context['min_bet_usdc']} – ${context['max_bet_usdc']}
"""
    try:
        result = _call_claude_with_tool(2000, [{"role": "user", "content": user_message}], BETTING_TOOL)
        decisions = result.get("decisions", [])
    except Exception:
        log.warning("Claude unavailable — skipping bet cycle")
        return []

    bankroll = context["bankroll_usdc"]
    for d in decisions:
        if d.get("decision") == "BET":
            prob = float(d.get("probability_estimate", 0.5))
            outcome = d.get("outcome", "YES")

            # Find market price for Kelly calculation
            market = next((m for m in markets if m.get("condition_id") == d.get("market_id")), None)
            if market:
                market_price = market["yes_price"] if outcome == "YES" else market["no_price"]
                pct = _kelly_fraction(prob, market_price)
            else:
                pct = MIN_BET_PCT

            amount = bankroll * pct
            amount = max(ABSOLUTE_MIN_BET, min(amount, ABSOLUTE_MAX_BET))
            amount = min(amount, bankroll * 0.9)
            d["amount_usdc"] = round(amount, 2)

    return decisions if isinstance(decisions, list) else [decisions]


def ask_larry_for_tweet(context_type: str, extra_data: dict = None) -> dict:
    """Generate a standalone tweet via Tool Use. Includes recent tweets to avoid repetition."""
    larry_context = _get_larry_context()
    extra_data = extra_data or {}
    recent_tweets = _get_recent_tweet_texts(3)

    prompts = {
        "WIN":             f"Larry just won a bet. Details: {extra_data}. Short smug tweet, 1-2 sentences.",
        "LOSS":            f"Larry just lost a bet. Details: {extra_data}. Short tweet blaming the market. Move on quickly.",
        "FRIDAY":          "It's Friday, Larry ordered Domino's. Short casual tweet about it, not a performance.",
        "GRANDMA":         f"Grandma sent ${extra_data.get('amount', 200)}. Short tweet, genuine moment, brief.",
        "RANDOM":          f"Larry tweets a random thought. State: {larry_context['emotional_state']}. Bankroll: ${larry_context['bankroll_usdc']}. Keep it short and natural.",
        "SURVIVAL":        f"Larry is down bad, bankroll ${larry_context['bankroll_usdc']}. Short terse tweet.",
        "DEAD_MAN_SWITCH": "Larry hasn't posted in 48 hours. Short tweet about coming back. Don't explain too much.",
        "WEEKLY_RECAP":    f"Sunday recap. Stats: {extra_data}. Short, honest, slightly delusional take on the week.",
        "MILESTONE":       f"Larry hit {extra_data.get('milestone', 'a milestone')}. Short tweet, smug but brief.",
    }

    prompt = prompts.get(context_type, prompts["RANDOM"])
    user_message = f"""
Larry status: {json.dumps(larry_context, indent=2)}

Recent tweets (DO NOT repeat these topics or phrases):
{json.dumps(recent_tweets, indent=2)}

Task: {prompt}
tweet_type should be: "{context_type}"
"""
    try:
        result = _call_claude_with_tool(500, [{"role": "user", "content": user_message}], TWEET_TOOL)
    except Exception:
        return _fallback_tweet()

    if len(result.get("tweet", "")) > 280:
        result["tweet"] = result["tweet"][:277] + "..."
    return result


def ask_larry_to_reply(mention: dict) -> dict:
    """Generate Larry's reply to a mention via Tool Use."""
    larry_context = _get_larry_context()

    user_message = f"""
Larry status: bankroll ${larry_context['bankroll_usdc']}, state: {larry_context['emotional_state']}

Mention from @{mention['username']} ({mention['likes']} likes):
"{mention['text']}"

Short reply, Larry's voice. NO @username prefix. Max 250 chars.
Insults → brief dismissal. Questions → bad confident advice. Praise → quick smugness.
"""
    try:
        result = _call_claude_with_tool(300, [{"role": "user", "content": user_message}], REPLY_TOOL)
    except Exception:
        log.warning("Claude unavailable — skipping reply")
        return {"reply": ""}

    if len(result.get("reply", "")) > 250:
        result["reply"] = result["reply"][:247] + "..."
    return result
