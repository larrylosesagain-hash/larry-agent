"""
larry_brain.py — Claude is Larry's brain
Sends market data → gets back bet decisions + tweet text via Tool Use (guaranteed structured output)
"""

import json
import time
import logging
import requests
import anthropic
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from config import MIN_BET_PCT, MAX_BET_PCT, ABSOLUTE_MIN_BET, ABSOLUTE_MAX_BET
from database import get_bankroll, get_win_streak, get_recent_bets, get_grandma_balance, get_connection

log = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Edge thresholds by market type:
# Efficient markets (crypto, politics) — crowd is usually right, need real edge
MIN_EDGE_EFFICIENT = 0.05
# Cultural/entertainment/sports — less efficient, mispricing is common
MIN_EDGE = 0.03


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
                        "probability_estimate": {"type": "number", "description": "Your true probability estimate (0.0-1.0). Required for BET."},
                        "reasoning":            {"type": "string"},
                        "larry_tweet":          {"type": "string", "description": "Short natural tweet announcing the bet, 1-2 sentences max. BET decisions only — skip for PASS."},
                    },
                    "required": ["decision", "market_id", "outcome", "probability_estimate", "reasoning"]
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
    # Cache both system prompt and tool definition to save tokens
    cached_tool = {**tool, "cache_control": {"type": "ephemeral"}}
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
                tools=[cached_tool],
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


# ─── CONTEXT BUILDERS ─────────────────────────────────────────────────────────

def _get_emotional_state(bankroll: float, win_streak: int) -> str:
    if bankroll < 50:   return "GRANDMA_MODE"
    if bankroll < 80:   return "SURVIVAL"
    if bankroll >= 5000: return "PEAK_LARRY"
    if bankroll >= 500 and win_streak >= 3: return "WINNING_STREAK"
    return "GRINDING"


def _get_larry_context() -> dict:
    """Full context for betting decisions — includes recent bet history."""
    bankroll = get_bankroll()
    win_streak = get_win_streak()
    recent = get_recent_bets(3)  # 3 is enough to avoid repeats; 5 was wasting tokens

    min_bet = max(ABSOLUTE_MIN_BET, bankroll * MIN_BET_PCT)
    max_bet = min(ABSOLUTE_MAX_BET, bankroll * MAX_BET_PCT, bankroll * 0.9)

    # Slim recent bets: only fields Larry actually needs to avoid duplicate bets
    slim_recent = [
        {
            "q": r.get("question", "")[:60],   # truncated question
            "outcome": r.get("outcome"),
            "status": r.get("status"),
            "amount": r.get("amount_usdc"),
        }
        for r in recent
    ]

    return {
        "bankroll_usdc": round(bankroll, 2),
        "win_streak": win_streak,
        "emotional_state": _get_emotional_state(bankroll, win_streak),
        "recent_bets": slim_recent,
        "min_bet_usdc": round(min_bet, 2),
        "max_bet_usdc": round(max_bet, 2),
    }


def _get_tweet_context() -> dict:
    """Lightweight context for tweet/reply generation — no full bet history needed."""
    bankroll = get_bankroll()
    win_streak = get_win_streak()
    return {
        "bankroll_usdc": round(bankroll, 2),
        "win_streak": win_streak,
        "emotional_state": _get_emotional_state(bankroll, win_streak),
    }


def _get_recent_tweet_texts(limit: int = 3) -> list:
    """Fetch recent tweet texts from DB to avoid repetition. Truncated to save tokens."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT content, tweet_type FROM tweets ORDER BY posted_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()
        # Truncate to 80 chars — enough to detect repetition, not enough to waste tokens
        return [{"text": r["content"][:80], "type": r["tweet_type"]} for r in rows]
    except Exception:
        return []


# ─── WEB SEARCH FOR MARKET CONTEXT ───────────────────────────────────────────

def _search_news(question: str) -> str:
    """
    Quick DuckDuckGo search for current context about a market.
    No API key needed. Returns brief summary or empty string on failure.
    """
    try:
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={
                "q": question[:120],
                "format": "json",
                "no_html": "1",
                "skip_disambig": "1",
            },
            timeout=4,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        data = resp.json()
        # Try abstract first (Wikipedia summary), then answer, then related topics
        text = data.get("AbstractText", "") or data.get("Answer", "")
        if not text:
            topics = data.get("RelatedTopics", [])
            snippets = [t.get("Text", "") for t in topics[:2] if isinstance(t, dict)]
            text = " | ".join(s for s in snippets if s)
        return text[:400] if text else ""
    except Exception:
        return ""


def _enrich_markets_with_news(markets: list) -> list:
    """
    Add real-world news context to entertainment/sports/culture markets in parallel.
    Crypto and politics Claude already knows well — skip those to save time.
    Falls back silently if search fails for any market.
    """
    cultural = {"entertainment", "sports", "weird"}
    to_search = [(i, m) for i, m in enumerate(markets) if m.get("category") in cultural]
    if not to_search:
        return markets

    enriched = [dict(m) for m in markets]  # shallow copy
    try:
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(_search_news, m["question"]): i
                for i, m in to_search
            }
            for future in as_completed(futures, timeout=8):
                idx = futures[future]
                try:
                    news = future.result()
                    if news:
                        enriched[idx]["news"] = news
                except Exception:
                    pass
    except Exception:
        pass  # if parallel search fails entirely, return markets as-is

    found = sum(1 for m in enriched if "news" in m)
    if found:
        log.info(f"🔍 Enriched {found} markets with web search context")
    return enriched


# ─── PUBLIC FUNCTIONS ────────────────────────────────────────────────────────

def ask_larry_to_bet(markets: list) -> list:
    """Send markets to Claude via Tool Use, get back bet decisions with Kelly sizing."""
    context = _get_larry_context()

    # Enrich cultural/entertainment markets with current web search context
    # so Larry can reason about real-world narrative, not just price
    markets = _enrich_markets_with_news(markets)

    # Compact JSON (no indent) — saves ~25% tokens with no quality loss
    user_message = (
        f"Larry Status: {json.dumps(context, separators=(',',':'))}\n\n"
        f"Markets (yes_price=cost to buy YES, 'news' = current web context if available):\n"
        f"{json.dumps(markets, separators=(',',':'))}\n\n"
        f"Decide BET or PASS. Rules:\n"
        f"- Edge = |your_prob - market_price|. Min: crypto/politics={MIN_EDGE_EFFICIENT:.0%}, else={MIN_EDGE:.0%}\n"
        f"- Use 'news' field when available — reason about current narrative and sentiment\n"
        f"- CONTRARIAN: 97% on anyone = skip that, look for who's underpriced. "
        f"Is the Academy in a 'comeback' mood? Is there a split vote risk? Is the frontrunner's film out of fashion?\n"
        f"- YES and NO both valid — sometimes bet NO on an overpriced favorite\n"
        f"- Entertainment/culture: lean toward betting if you have any read at all\n"
        f"- Small gut-feel bets fine on interesting markets (min ${context['min_bet_usdc']})\n"
        f"Bet range: ${context['min_bet_usdc']}–${context['max_bet_usdc']}"
    )
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
            market = next((m for m in markets if m.get("condition_id") == d.get("market_id")), None)

            if market:
                market_price = market["yes_price"] if outcome == "YES" else round(1 - market["yes_price"], 4)

                # Category-aware edge threshold:
                # crypto/politics = efficient, need more edge; everything else = less efficient
                category = market.get("category", "weird")
                threshold = MIN_EDGE_EFFICIENT if category in ("crypto", "politics") else MIN_EDGE

                # Hard edge check — override Claude if edge is too small
                edge = abs(prob - market_price)
                if edge < threshold:
                    log.info(f"PASS (edge {edge:.1%} < {threshold:.0%} for {category}): {d.get('market_id','')[:16]}...")
                    d["decision"] = "PASS"
                    d["reasoning"] = f"Edge {edge:.1%} below {threshold:.0%} threshold for {category} market"
                    continue

                pct = _kelly_fraction(prob, market_price)
            else:
                pct = MIN_BET_PCT

            amount = bankroll * pct
            amount = max(ABSOLUTE_MIN_BET, min(amount, ABSOLUTE_MAX_BET, bankroll * 0.9))
            d["amount_usdc"] = round(amount, 2)

    return decisions if isinstance(decisions, list) else [decisions]


def ask_larry_for_tweet(context_type: str, extra_data: dict = None) -> dict:
    """Generate a standalone tweet via Tool Use. Uses lightweight context + tweet memory."""
    ctx = _get_tweet_context()
    extra_data = extra_data or {}
    recent_tweets = _get_recent_tweet_texts(3)

    prompts = {
        "WIN":             f"Larry just won a bet. Details: {extra_data}. Short smug tweet, 1-2 sentences.",
        "LOSS":            f"Larry just lost a bet. Details: {extra_data}. Short tweet blaming the market. Move on quickly.",
        "FRIDAY":          "It's Friday, Larry ordered Domino's. Short casual tweet about it, not a performance.",
        "GRANDMA":         f"Grandma sent ${extra_data.get('amount', 200)}. Short tweet, genuine moment, brief.",
        "RANDOM":          f"Larry tweets a random thought. State: {ctx['emotional_state']}. Bankroll: ${ctx['bankroll_usdc']}. Keep it short and natural.",
        "SURVIVAL":        f"Larry is down bad, bankroll ${ctx['bankroll_usdc']}. Short terse tweet.",
        "DEAD_MAN_SWITCH": "Larry hasn't posted in 48 hours. Short tweet about coming back. Don't explain too much.",
        "WEEKLY_RECAP":    f"Sunday recap. Stats: {extra_data}. Short, honest, slightly delusional take on the week.",
        "MILESTONE":       f"Larry hit {extra_data.get('milestone', 'a milestone')}. Short tweet, smug but brief.",
    }

    prompt = prompts.get(context_type, prompts["RANDOM"])
    user_message = (
        f"Larry: bankroll ${ctx['bankroll_usdc']}, state={ctx['emotional_state']}, streak={ctx['win_streak']}\n"
        f"Recent tweets (don't repeat): {json.dumps(recent_tweets, separators=(',',':'))}\n"
        f"Task: {prompt}\n"
        f"tweet_type: \"{context_type}\""
    )
    try:
        result = _call_claude_with_tool(500, [{"role": "user", "content": user_message}], TWEET_TOOL)
    except Exception:
        return _fallback_tweet()

    if len(result.get("tweet", "")) > 280:
        result["tweet"] = result["tweet"][:277] + "..."
    return result


def ask_larry_to_reply(mention: dict) -> dict:
    """Generate Larry's reply to a mention via Tool Use."""
    bankroll = get_bankroll()
    win_streak = get_win_streak()
    state = _get_emotional_state(bankroll, win_streak)

    user_message = (
        f"Larry: bankroll ${round(bankroll,2)}, state={state}\n"
        f"Mention from @{mention['username']} ({mention['likes']} likes): \"{mention['text']}\"\n"
        f"Short reply, Larry's voice. NO @username prefix. Max 250 chars.\n"
        f"Insults → brief dismissal. Questions → bad confident advice. Praise → quick smugness."
    )
    try:
        result = _call_claude_with_tool(300, [{"role": "user", "content": user_message}], REPLY_TOOL)
    except Exception:
        log.warning("Claude unavailable — skipping reply")
        return {"reply": ""}

    if len(result.get("reply", "")) > 250:
        result["reply"] = result["reply"][:247] + "..."
    return result
