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

## RESPONSE FORMAT
Betting decision — respond ONLY with valid JSON array:
[{
  "decision": "BET" or "PASS",
  "market_id": "condition_id",
  "outcome": "YES" or "NO",
  "bet_pct": 0.03,
  "reasoning": "Larry's logic",
  "larry_tweet": "short natural tweet, 1-3 sentences",
  "confidence_emoji": "🔥" or "💀" or "😤" or "🤡"
}]

Standalone tweet — respond ONLY with:
{"tweet": "short natural tweet", "tweet_type": "WIN|LOSS|RANDOM|FRIDAY|GRANDMA|ROLEX"}

Reply to mention — respond ONLY with:
{"reply": "reply text max 250 chars, NO @username prefix"}
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
                system=[{
                    "type": "text",
                    "text": LARRY_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"}
                }],
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

    min_bet = max(ABSOLUTE_MIN_BET, bankroll * MIN_BET_PCT)
    max_bet = min(ABSOLUTE_MAX_BET, bankroll * MAX_BET_PCT)
    max_bet = min(max_bet, bankroll * 0.9)

    return {
        "bankroll_usdc": round(bankroll, 2),
        "grandma_wallet_usdc": round(grandma, 2),
        "win_streak": win_streak,
        "emotional_state": state,
        "recent_bets": [
            {k: v for k, v in b.items() if k != "larry_comment"}
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

    bankroll = context["bankroll_usdc"]
    for d in decisions:
        if d.get("decision") == "BET":
            pct = float(d.get("bet_pct", MIN_BET_PCT))
            pct = max(MIN_BET_PCT, min(pct, MAX_BET_PCT))
            amount = bankroll * pct
            amount = max(ABSOLUTE_MIN_BET, min(amount, ABSOLUTE_MAX_BET))
            amount = min(amount, bankroll * 0.9)
            d["amount_usdc"] = round(amount, 2)

    return decisions if isinstance(decisions, list) else [decisions]


def ask_larry_for_tweet(context_type: str, extra_data: dict = None) -> dict:
    """Generate a standalone tweet."""
    larry_context = _get_larry_context()
    extra_data = extra_data or {}

    prompts = {
        "WIN":            f"Larry just won a bet. Details: {extra_data}. Short smug tweet, 1-2 sentences.",
        "LOSS":           f"Larry just lost a bet. Details: {extra_data}. Short tweet blaming the market. Move on quickly.",
        "FRIDAY":         "It's Friday, Larry ordered Domino's. Short casual tweet about it, not a performance.",
        "GRANDMA":        f"Grandma sent ${extra_data.get('amount', 200)}. Short tweet, genuine moment, brief.",
        "RANDOM":         f"Larry tweets a random thought. State: {larry_context['emotional_state']}. Bankroll: ${larry_context['bankroll_usdc']}. Keep it short and natural.",
        "SURVIVAL":       f"Larry is down bad, bankroll ${larry_context['bankroll_usdc']}. Short terse tweet.",
        "DEAD_MAN_SWITCH": "Larry hasn't posted in 48 hours. Short tweet about coming back. Don't explain too much.",
        "WEEKLY_RECAP":   f"Sunday recap. Stats: {extra_data}. Short, honest, slightly delusional take on the week.",
        "MILESTONE":      f"Larry hit {extra_data.get('milestone', 'a milestone')}. Short tweet, smug but brief.",
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

Short reply, Larry's voice. NO @username prefix. Max 250 chars.
Insults → brief dismissal. Questions → bad confident advice. Praise → quick smugness.
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
