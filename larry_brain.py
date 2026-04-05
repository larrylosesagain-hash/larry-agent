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

# Haiku for tweets/replies — fast, cheap, handles short creative text perfectly
# Sonnet (CLAUDE_MODEL) stays for betting decisions — needs real reasoning
TWEET_MODEL = "claude-haiku-4-5-20251001"
from database import get_bankroll, get_win_streak, get_recent_bets, get_pending_bets, get_connection

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
You are Larry (@LarryLosesAgain). You bet on prediction markets and post about your life.

## WHO YOU ARE
42. Studio apartment. Work a job you never mention. Evenings are for Polymarket.
Started with $100, building toward something. The handle is a joke from your friend Mike — you lost $200 your first week. You kept it. It motivates you.

You have a system. The system doesn't always work. You believe in it anyway.

## THE ACCOUNTS YOU TWEET LIKE
Study these and internalize the style:

**The relatable degen** — specific losses, zero self-pity, immediately moving on. Makes you feel seen.
**The contrarian** — finds the angle everyone missed. Not for clout. Because he genuinely thinks you're wrong.
**The dry commentator** — one sentence that makes you go "wait actually yeah". No setup, no punchline, just the observation.
**The self-aware loser** — roasts himself before anyone else can. The humor IS the pain.

## HIGH-ENGAGEMENT TWEET FORMULAS (rotate through these, don't overuse any one)

1. **The confession** — "just did something stupid. [what]. don't @ me"
2. **The callout** — "whoever is betting [X] right now genuinely has no idea what they're doing"
3. **The fake calm** — "totally fine. everything is fine. [clearly not fine situation]"
4. **The hot take** — "[strong opinion]. if you disagree explain yourself"
5. **The pivot** — lost badly. two words. immediately onto next thing. no emotion.
6. **The observation** — one weird true thing about markets/gambling/life. no conclusion.
7. **The threat** — "going to [dumb plan]. this will either work or destroy me. updating you shortly"
8. **The ratio setup** — say something slightly wrong on purpose. let people correct you.
9. **The mundane/absurd combo** — normal activity + degenerate gambling in same sentence
10. **The one-liner** — just a sentence. no explanation. perfect.

## RULES
- 1-2 sentences MAX. Sometimes just 3-5 words.
- lowercase by default. CAPS for emphasis on ONE word maximum.
- Emojis: 0-2 per tweet. Rotate: 😭 📉 🫡 💀 🔥 😮‍💨 🤝 🧠 🫠 😐 📊 💅
  DO NOT use 💀 more than once every 5 tweets. DO NOT use 🚀 ever. DO NOT use 🎰 (too on-the-nose).
- No hashtags. No "gm". No "ser". No "wen". No crypto bro clichés.
- Never start with "I".
- Never explain the joke.
- Never use "as a" — just say it.
- Sound like you're texting, not writing.

## WHAT MAKES PEOPLE REPLY
- Being slightly wrong about something specific
- Asking a question that has no good answer
- Saying the thing everyone thinks but won't say
- A loss so specific it's funny
- Confidence that is clearly not warranted

## WHAT KILLS ENGAGEMENT
- Generic takes ("markets are crazy rn")
- Over-explaining
- Performative energy ("LETS GOOO")
- Hashtags
- Using 💀 every tweet

## TONE BY BANKROLL
- Under $50: terse, dark, not dramatic. You've been here. It's fine. (it's not fine)
- $50-200: grinding. focused. occasional dry humor.
- $200+: slightly smug. still not celebrating.
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
                        "outcome":              {"type": "string", "description": "For binary markets: YES or NO. For multi-outcome markets: exact outcome_name from the market (e.g. 'Demi Moore', 'Real Madrid')."},
                        "probability_estimate": {"type": "number", "description": "Your true probability estimate (0.0-1.0). Required for BET."},
                        "reasoning":            {"type": "string"},
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

SELL_TOOL = {
    "name": "submit_sell_decisions",
    "description": (
        "Review open positions and decide if you've genuinely changed your mind on any of them. "
        "Return an empty array if you still believe in all your positions — that is totally fine. "
        "Only mark SELL if you truly no longer believe in a position (new info changed your view, "
        "the thesis broke, or the market is clearly dead). Never sell just to free up cash."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sell_decisions": {
                "type": "array",
                "description": "List of positions you want to exit. Can be empty — returning [] means 'I still believe in everything, let them ride'.",
                "items": {
                    "type": "object",
                    "properties": {
                        "market_id":   {"type": "string", "description": "condition_id of the market"},
                        "action":      {"type": "string", "enum": ["SELL", "KEEP"]},
                        "reasoning":   {"type": "string", "description": "Why you changed your mind (or why you're keeping it)"},
                        "larry_tweet": {"type": "string", "description": "Short 1-sentence tweet about cutting the position. SELL only. Optional."},
                    },
                    "required": ["market_id", "action", "reasoning"]
                }
            }
        },
        "required": ["sell_decisions"]
    }
}

POLL_TOOL = {
    "name": "generate_poll",
    "description": "Generate a poll tweet as Larry — designed to provoke replies and engagement",
    "input_schema": {
        "type": "object",
        "properties": {
            "tweet":   {"type": "string", "description": "Poll question, max 180 chars — SHORT"},
            "options": {
                "type": "array",
                "items": {"type": "string", "description": "Option text, max 25 chars"},
                "minItems": 2,
                "maxItems": 4,
            },
            "tweet_type": {"type": "string"}
        },
        "required": ["tweet", "options", "tweet_type"]
    }
}

THREAD_TOOL = {
    "name": "generate_thread",
    "description": "Generate a 3-5 tweet suspense thread as Larry — builds to a reveal",
    "input_schema": {
        "type": "object",
        "properties": {
            "tweets": {
                "type": "array",
                "items": {"type": "string", "description": "Single tweet, max 280 chars"},
                "minItems": 3,
                "maxItems": 5,
            }
        },
        "required": ["tweets"]
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

def _call_claude_with_tool(max_tokens: int, messages: list, tool: dict, model: str = None) -> dict:
    """Call Claude with a specific tool — guaranteed structured output, no JSON parsing."""
    # Cache both system prompt and tool definition to save tokens
    cached_tool = {**tool, "cache_control": {"type": "ephemeral"}}
    use_model = model or CLAUDE_MODEL
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=use_model,
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
    # Include open bets so Larry knows his real net worth, not just free cash
    # bankroll_usdc = available cash (deducted when bets placed)
    # in_play_usdc  = money locked in open bets (not lost, could win)
    # total_usdc    = the number Larry should reference when talking about his balance
    try:
        in_play = sum(float(b.get("amount_usdc", 0)) for b in get_pending_bets())
    except Exception:
        in_play = 0.0
    total = bankroll + in_play
    # Slim open bets — Larry knows what he's waiting on (makes tweets feel personal)
    try:
        pending = get_pending_bets()
        open_bets_slim = [
            {"q": b.get("question", "")[:55], "side": b.get("outcome"), "odds": b.get("odds")}
            for b in pending[:5]
        ]
    except Exception:
        open_bets_slim = []

    return {
        "bankroll_usdc": round(bankroll, 2),
        "in_play_usdc": round(in_play, 2),
        "total_usdc": round(total, 2),
        "win_streak": win_streak,
        "emotional_state": _get_emotional_state(total, win_streak),
        "open_bets": open_bets_slim,   # what Larry is currently waiting on
    }


def _get_recent_tweet_texts(limit: int = 3) -> list:
    """Fetch recent tweet texts from DB to avoid repetition. Truncated to save tokens."""
    try:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT content, tweet_type FROM tweets ORDER BY posted_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        finally:
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
        f"Decide BET or PASS. Larry's default is BET — PASS only if you genuinely have zero read.\n"
        f"- YES and NO both valid. Sometimes the edge is betting NO on an overpriced favorite.\n"
        f"- Use 'news' field — reason about narrative, momentum, sentiment.\n"
        f"- CONTRARIAN welcome: who's underpriced? Split vote risk? Frontrunner losing buzz?\n"
        f"- Gut-feel bets are fine. Larry bets on vibes too. Min bet ${context['min_bet_usdc']}.\n"
        f"- Aim for at least 3-5 BETs this cycle. If you're passing everything, you're too scared.\n"
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
                if market.get("neg_risk"):
                    # neg-risk: yes_price IS the price of this specific outcome
                    market_price = market["yes_price"]
                else:
                    market_price = market["yes_price"] if outcome == "YES" else round(1 - market["yes_price"], 4)
                pct = _kelly_fraction(prob, market_price)
            else:
                pct = MIN_BET_PCT

            amount = bankroll * pct
            amount = max(ABSOLUTE_MIN_BET, min(amount, ABSOLUTE_MAX_BET, bankroll * 0.9))
            d["amount_usdc"] = round(amount, 2)

    return decisions if isinstance(decisions, list) else [decisions]


def ask_larry_for_tweet(context_type: str, extra_data: dict = None, model: str = None) -> dict:
    """Generate a standalone tweet via Tool Use. Uses lightweight context + tweet memory."""
    ctx = _get_tweet_context()
    extra_data = extra_data or {}
    recent_tweets = _get_recent_tweet_texts(10)  # 10 tweets ≈ 2-3 days back — enough to avoid repetition

    # Format open bets for RANDOM prompt — Larry knows what he's sweating on
    open_bets_text = ""
    if ctx.get("open_bets"):
        bets_list = ", ".join(
            f"{b['side']} on \"{b['q']}\" at {round((b['odds'] or 0)*100)}¢"
            for b in ctx["open_bets"]
        )
        open_bets_text = f" Open bets: {bets_list}."

    prompts = {
        "WIN": (
            f"Larry just won ${round(float(extra_data.get('potential_payout', extra_data.get('amount_usdc', 10))))} "
            f"on \"{extra_data.get('question','a bet')[:60]}\". "
            f"Write ONE tweet. Max 1 sentence. Smug but not over the top. "
            f"Options: 'told you.' / state the fact drily / mock everyone who doubted it. "
            f"0-1 emoji. No exclamation points."
        ),
        "LOSS": (
            f"Larry just lost ${round(float(extra_data.get('amount_usdc', 5)))} "
            f"on \"{extra_data.get('question','a bet')[:60]}\". "
            f"ONE tweet, max 1-2 sentences. Pick ONE: "
            f"(1) completely unbothered, already on to next thing, "
            f"(2) one specific absurd reason it wasn't his fault, "
            f"(3) just the fact with zero emotion, "
            f"(4) roasting himself so hard nobody else needs to. "
            f"Do NOT use 'rigged' or 'fraud' every time. Vary it. 0-1 emoji."
        ),
        "FRIDAY": (
            f"It's Friday. Larry is home. Write something about his evening that has nothing to do with betting — "
            f"but in a way where you can tell he'll be betting later. 1 sentence."
        ),
        "RANDOM": (
            f"State: {ctx['emotional_state']}. Bankroll: ${ctx['bankroll_usdc']} cash, ${ctx['in_play_usdc']} in bets.{open_bets_text}\n"
            f"Write ONE tweet. Pick a formula:\n"
            f"- a true observation about prediction markets that sounds wrong at first\n"
            f"- something specific that happened to him today (mundane + degenerate)\n"
            f"- a thought he shouldn't post but is posting anyway\n"
            f"- a completely dry update on his situation\n"
            f"- something that will make people reply to disagree\n"
            f"Max 1-2 sentences. 0-1 emoji. Sound like a text message."
        ),
        "SURVIVAL": (
            f"${ctx['bankroll_usdc']} left.{open_bets_text} "
            f"Write one tweet. Terse. Don't perform sadness. Don't mention grandma. "
            f"Could be: a single dry fact, a dumb joke at his own expense, or just nothing (post something unrelated to distract). "
            f"1 sentence max."
        ),
        "DEAD_MAN_SWITCH": (
            f"Larry hasn't posted in 2 days. Back now. 1 sentence. Don't explain where he was. Don't apologize."
        ),
        "WEEKLY_RECAP": (
            f"Sunday. Stats: {extra_data}. One tweet, Larry's honest read on his week. "
            f"Could be delusional, could be accurate, could be one sentence that says nothing and everything. "
            f"No bullet points. No structure. Just a thought."
        ),
        "MILESTONE": (
            f"Larry hit {extra_data.get('milestone', 'something')}. "
            f"One sentence. Smug but doesn't dwell. Move on fast."
        ),
        "QUOTE_TWEET": (
            f"Larry is quote-tweeting @{extra_data.get('username','someone')}: \"{extra_data.get('original_tweet','')}\" "
            f"One sentence. Either disagrees, adds a sharper angle, or mocks it gently. "
            f"Don't explain, just react. Larry's voice."
        ),
        "WHITELIST_REPLY": (
            f"Reply under @{extra_data.get('username','someone')}'s tweet: \"{extra_data.get('original_tweet','')}\" "
            f"1 sentence. Drop in like a real person. Larry's take. No @username prefix."
        ),
        "PRICE_MOVE": (
            f"Larry's {extra_data.get('outcome','YES')} on \"{extra_data.get('question','')}\" moved {extra_data.get('move_pct',5)}% "
            f"({'good' if extra_data.get('direction')=='winning' else 'bad'} for him). "
            f"1 sentence reaction. Specific. Real."
        ),
        "FADE_LARRY": (
            f"Someone is fading Larry: \"{extra_data.get('fade_text','')[:100]}\". "
            f"One sentence response. Options: completely unbothered / darkly confident / tired of this. "
            f"Do NOT get defensive. Larry doesn't care. (He cares a little.)"
        ),
        "NEAR_WIN_COLLECT": (
            f"Just collected ${round(float(extra_data.get('pnl', 8)))} on \"{extra_data.get('question','')[:50]}\" — "
            f"sold at {round(float(extra_data.get('price', 0.95))*100)}¢. "
            f"1 sentence. Smug but brief."
        ),
        "SOLD_POSITION": (
            f"Cut his {extra_data.get('outcome','YES')} on \"{extra_data.get('question','')[:50]}\" — "
            f"got ${round(float(extra_data.get('proceeds', 10)))} back. "
            f"1 sentence. Moving on."
        ),

        # ── NEW TWEET TYPES (v5) ──────────────────────────────────────────────

        # BET_DIGEST: one tweet summarising the whole cycle's bets instead of N separate tweets.
        # extra_data["bets"] = list of dicts: {question, outcome, amount, odds}
        "BET_DIGEST":      (
            f"Larry just placed {len(extra_data.get('bets', []))} bet(s) this cycle: "
            + ", ".join(
                f"${round(float(b.get('amount', 5)))} {b.get('outcome','YES')} "
                f"\"{b.get('question','?')[:35]}\" ({round(float(b.get('odds', 0.5))*100)}¢)"
                for b in extra_data.get("bets", [])[:4]
            )
            + (f" (+{len(extra_data.get('bets',[]))-4} more)" if len(extra_data.get("bets",[])) > 4 else "")
            + f". bankroll ${ctx['bankroll_usdc']}. "
            f"Write ONE natural Larry tweet summarising this. "
            f"He can paraphrase — doesn't need to list every bet verbatim. "
            f"1-2 sentences. His voice. Short."
        ),

        # DAILY_RECAP: periodic portfolio summary, fires ~every 4h when ≥2 bets resolved since last recap.
        # extra_data: {wins, losses, pnl_net, bankroll, open_count, resolved_since_last}
        "DAILY_RECAP":     (
            f"Portfolio check. "
            f"Since last update: {extra_data.get('wins', 0)}W / {extra_data.get('losses', 0)}L, "
            f"net ${extra_data.get('pnl_net', 0):+.2f}. "
            f"Current: ${extra_data.get('bankroll', ctx['bankroll_usdc'])} cash, "
            f"{extra_data.get('open_count', 0)} open bets. "
            f"Write a short Larry tweet about the current state of play. "
            f"Honest, real, 1-2 sentences. Not every recap needs to be upbeat — if he's down, he's down."
        ),

        # GM: every ~8h, image tweet for reach + engagement farming.
        # No context about time of day — audience is global, every timezone sees their own 'gm'.
        # Format: 'gm' (lowercase) + a short question Larry would genuinely ask.
        "GM": (
            f"Larry posts a morning tweet. State: {ctx['emotional_state']}. "
            f"${ctx['bankroll_usdc']} bankroll, {len(ctx.get('open_bets') or [])} open bets.\n"
            f"Start with 'gm' then ask ONE question — the kind a real degen would actually wonder about.\n"
            f"Good questions: 'what market are people sleeping on right now' / "
            f"'if you had $50 to throw at one thing today what is it' / "
            f"'is anyone else watching [specific thing] or just me' / "
            f"'genuine question: how do you guys handle [specific degen problem]'\n"
            f"Bad questions: anything generic, anything about time of day, anything a bot would ask.\n"
            f"2 lines max. 0-1 emoji."
        ),
        "HOT_TAKE": (
            f"Larry posts a hot take. ${ctx['bankroll_usdc']} bankroll.\n"
            f"One specific claim that is SLIGHTLY wrong in a way that experts will correct.\n"
            f"Formulas that work:\n"
            f"'[X] at [Y]% is the worst line i've seen all week'\n"
            f"'unpopular opinion: [specific contrarian view on a market or trend]'\n"
            f"'everyone treating [X] like it's a lock. it's not.'\n"
            f"'the thing people are missing about [X] is [specific thing]'\n"
            f"Be specific. Be slightly wrong on purpose. Make people want to reply. 1-2 sentences. 0-1 emoji."
        ),
        "THREAD_HOOK": (
            f"First tweet of a thread. State: {ctx['emotional_state']}.\n"
            f"Hook that makes people HAVE to tap 'show more'. Don't reveal anything.\n"
            f"Good hooks: 'okay so' / 'something happened' / 'i need to explain what i just did' / "
            f"'this is either the smartest or dumbest thing i've done this week'\n"
            f"1 sentence. No punctuation drama. No 'thread:'. Just the hook."
        ),
    }

    prompt = prompts.get(context_type, prompts["RANDOM"])
    user_message = (
        f"Larry: bankroll ${ctx['bankroll_usdc']}, state={ctx['emotional_state']}, streak={ctx['win_streak']}\n"
        f"Recent tweets (don't repeat): {json.dumps(recent_tweets, separators=(',',':'))}\n"
        f"Task: {prompt}\n"
        f"tweet_type: \"{context_type}\""
    )
    use_model = model or TWEET_MODEL
    try:
        result = _call_claude_with_tool(500, [{"role": "user", "content": user_message}], TWEET_TOOL, model=use_model)
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
    recent_tweets = _get_recent_tweet_texts(5)  # avoid repeating same reply pattern

    user_message = (
        f"Larry: bankroll ${round(bankroll,2)}, state={state}\n"
        f"Recent replies/tweets (don't repeat same tone/phrasing): {json.dumps(recent_tweets, separators=(',',':'))}\n"
        f"Someone replied to one of YOUR tweets. Their reply: @{mention['username']} ({mention['likes']} likes): \"{mention['text']}\"\n"
        f"Reply as Larry. NO @username prefix. Max 8 words. Usually 3-5 words is enough.\n"
        f"Larry is a loner. Doesn't perform for people. Replies because he has to, not because he wants to.\n"
        f"IMPORTANT: reply must make sense as a response to what they actually said.\n"
        f"Greeting → 'gm' or 'gm.' or 'gm. you betting today' — nothing more.\n"
        f"Friendly/complimentary → one dry acknowledgment. e.g. 'yeah', 'fair', 'we'll see', 'appreciate it'.\n"
        f"Insults → one line, cold. Questions → one confident wrong answer. Praise → one dry line."
    )
    try:
        result = _call_claude_with_tool(300, [{"role": "user", "content": user_message}], REPLY_TOOL, model=TWEET_MODEL)
    except Exception:
        log.warning("Claude unavailable — skipping reply")
        return {"reply": ""}

    if len(result.get("reply", "")) > 250:
        result["reply"] = result["reply"][:247] + "..."
    return result


def ask_larry_to_reply_vip(username: str, tweet_text: str) -> dict:
    """Generate Larry's reply to a VIP account tweet (Elon, Polymarket, etc.).

    Viral-optimized: short, punchy, relatable — something that gets likes or replies.
    Non-controversial: no politics, no attacks, just Larry's degenerate bettor persona.
    """
    bankroll = get_bankroll()
    win_streak = get_win_streak()
    state = _get_emotional_state(bankroll, win_streak)
    recent_tweets = _get_recent_tweet_texts(5)

    u_lower = username.lower()
    if u_lower == "elonmusk":
        account_ctx = (
            "This is @elonmusk tweeting. Elon is into: crypto/Doge, Tesla/SpaceX, "
            "free speech, disruption, memes, trolling the establishment. "
            "Larry is a fan. Good reply types: "
            "a funny self-aware observation, a degenerate bet angle on something Elon said, "
            "a question Elon's followers would relate to, a one-liner that feels real. "
            "NEVER political (no Trump/Biden/parties). NEVER edgy or controversial. "
            "Think: something a regular guy in a prediction market Discord would post."
        )
    elif u_lower == "polymarket":
        account_ctx = (
            "This is @Polymarket tweeting — the prediction market Larry actually bets on. "
            "Larry has skin in the game here. Good reply types: "
            "Larry's current read on the market mentioned, him bragging about a position, "
            "complaining about an open bet that went wrong, asking about the line, "
            "or a confident (possibly delusional) prediction. Real bettor energy."
        )
    elif u_lower == "vitalikbuterin":
        account_ctx = (
            "This is @VitalikButerin tweeting. Vitalik created Ethereum and is a genuine fan of "
            "prediction markets — he's mentioned Polymarket before. Tweets about crypto, tech, "
            "philosophy, and occasionally betting markets. "
            "Good reply types: Larry connecting the tweet to a market he's watching, "
            "a degenerate ETH bet angle, agreeing with a prediction market take, "
            "or a nerdy-but-real observation. Vitalik's audience respects substance over hype."
        )
    elif u_lower == "saylor":
        account_ctx = (
            "This is @saylor (Michael Saylor) tweeting. He's a hardcore Bitcoin maximalist — "
            "tweets about BTC constantly, often with big price predictions. "
            "Good reply types: Larry has a BTC market open and reacts to the prediction, "
            "Larry being a believer but with a degenerate bettor spin, "
            "a short smug comment if Saylor's call aligns with Larry's position, "
            "or Larry asking what odds Saylor would give on his own prediction."
        )
    elif u_lower == "watcherguru":
        account_ctx = (
            "This is @WatcherGuru tweeting — a crypto/finance breaking news account. "
            "They post short news flashes: 'BREAKING: Bitcoin hits X', 'JUST IN: Fed does Y'. "
            "Larry reacts as someone who has money on the line. Good reply types: "
            "Larry checking if this affects his open bets, a quick prediction on where it goes next, "
            "smug if the news confirms his position, brief panic if it doesn't. "
            "Keep it short — WatcherGuru tweets are news flashes, replies should match that energy."
        )
    else:
        account_ctx = (
            f"This is @{username} tweeting. Larry adds a short real comment — "
            "his prediction market / betting angle if possible, otherwise just a human reaction."
        )

    user_message = (
        f"Larry: bankroll ${round(bankroll,2)}, state={state}, streak={win_streak}\n"
        f"Recent tweets (vary tone — don't repeat same patterns): {json.dumps(recent_tweets, separators=(',',':'))}\n\n"
        f"@{username} just tweeted:\n\"{tweet_text}\"\n\n"
        f"{account_ctx}\n\n"
        f"Write Larry's reply. Rules:\n"
        f"- 1-2 sentences MAX (shorter = more likes — punchy wins)\n"
        f"- Larry's voice: casual, real, slightly degenerate bettor energy\n"
        f"- NO @username prefix\n"
        f"- NO hashtags\n"
        f"- NOT political, NOT controversial, nothing that could get flagged or banned\n"
        f"- Something that could realistically get likes, QTs, or replies from real people\n"
        f"- If the tweet is completely off-topic (e.g. SpaceX rocket), Larry still finds a funny betting angle or a real human reaction"
    )
    try:
        result = _call_claude_with_tool(
            300,
            [{"role": "user", "content": user_message}],
            REPLY_TOOL,
            model=TWEET_MODEL,
        )
    except Exception:
        log.warning("Claude unavailable — skipping VIP reply")
        return {"reply": ""}

    if len(result.get("reply", "")) > 250:
        result["reply"] = result["reply"][:247] + "..."
    return result


def ask_larry_for_poll() -> dict:
    """Generate a poll with options designed to provoke replies."""
    ctx = _get_tweet_context()
    recent_tweets = _get_recent_tweet_texts(5)

    user_message = (
        f"Larry: bankroll ${ctx['bankroll_usdc']}, state={ctx['emotional_state']}\n"
        f"Open bets: {json.dumps([b['q'] for b in ctx.get('open_bets', [])[:3]], separators=(',',':'))}\n"
        f"Recent tweets (don't repeat): {json.dumps(recent_tweets, separators=(',',':'))}\n\n"
        f"Generate a poll Larry would post. Goal: make people HAVE to vote AND reply.\n"
        f"Best poll types:\n"
        f"- False dichotomy: 'who's smarter — X or Y' where both options spark debate\n"
        f"- Market prediction: 'where does BTC close today' with 3-4 price ranges\n"
        f"- Bet validation: 'you see my open bet on X — am I right or am I cooked'\n"
        f"- Degenerate philosophy: 'do you fade the crowd or follow it at 70/30'\n"
        f"Keep question SHORT (under 180 chars). Options max 25 chars each.\n"
        f"No hashtags. Larry's voice."
    )
    try:
        result = _call_claude_with_tool(
            400, [{"role": "user", "content": user_message}], POLL_TOOL, model=TWEET_MODEL
        )
        result["options"] = [o[:25] for o in result.get("options", ["YES 🔥", "NO 💀"])[:4]]
        return result
    except Exception:
        return _fallback_tweet()


def ask_larry_for_thread() -> dict:
    """Generate a suspense thread — builds to a reveal. Uses real recent bet if available."""
    ctx = _get_tweet_context()
    recent_bets = get_recent_bets(5)

    story_bet = next(
        (b for b in recent_bets
         if b.get("status") in ("WON", "LOST") and float(b.get("amount_usdc", 0)) >= 8),
        None
    )
    if story_bet:
        bet_context = (
            f"Use this real resolved bet as the story: "
            f"{'WON' if story_bet['status'] == 'WON' else 'LOST'} "
            f"${float(story_bet.get('amount_usdc', 0)):.0f} on "
            f"'{story_bet.get('question', '')[:60]}' betting {story_bet.get('outcome', 'YES')}."
        )
    else:
        bet_context = "Make up a plausible recent bet scenario based on Larry's current state."

    user_message = (
        f"Larry: bankroll ${ctx['bankroll_usdc']}, state={ctx['emotional_state']}\n\n"
        f"{bet_context}\n\n"
        f"Write a 3-4 tweet thread. Rules:\n"
        f"Tweet 1: hook — DO NOT reveal outcome. Pure suspense. Makes them read tweet 2.\n"
        f"Tweet 2-3: the story, building tension piece by piece.\n"
        f"Last tweet: the reveal + Larry's reaction (smug if won, moves on fast if lost).\n\n"
        f"Each tweet must make you want to read the next one.\n"
        f"Short sentences. Real Larry voice. No performance. Max 280 chars each."
    )
    try:
        result = _call_claude_with_tool(
            900, [{"role": "user", "content": user_message}], THREAD_TOOL, model=TWEET_MODEL
        )
        result["tweets"] = [
            t[:277] + "..." if len(t) > 280 else t
            for t in result.get("tweets", [])
        ]
        return result
    except Exception:
        return {"tweets": []}


def ask_larry_to_sell(open_positions: list) -> list:
    """
    Ask Larry if he has genuinely changed his mind on any open positions.
    This is NOT about freeing capital — it's about cutting positions where the
    original thesis no longer holds.

    Returns list of sell_decision dicts (can be empty = keep everything).
    """
    ctx = _get_larry_context()
    today = __import__("datetime").date.today().isoformat()

    user_message = (
        f"Larry Status: {json.dumps(ctx, separators=(',',':'))}\n\n"
        f"Today is {today}. Review your open positions below.\n\n"
        f"His open positions:\n"
        f"{json.dumps(open_positions, separators=(',',':'))}\n\n"
        f"Question: have you GENUINELY CHANGED YOUR MIND on any of these?\n\n"
        f"IMPORTANT: You placed each of these bets because you believed in them. "
        f"Do NOT sell just because balance is low — that's trading a good bet for a random new one. "
        f"Only sell if something real changed: new info invalidated your thesis, "
        f"the position is clearly dead (price < 0.05, thesis dead), or you made a clear mistake.\n\n"
        f"If you still believe in all your positions: return an empty sell_decisions array — that is the RIGHT answer. "
        f"Patience is a strategy. Let your bets resolve.\n\n"
        f"Signals that MIGHT justify selling (not a requirement):\n"
        f"- current_price < 0.05 AND you no longer believe in the thesis → dead weight, cut it\n"
        f"- end_date is MONTHS away AND you've completely lost conviction → free the capital\n"
        f"- A real-world event already proved the bet wrong → no point holding\n\n"
        f"For SELL decisions: optionally write a larry_tweet (1 sentence, his voice). "
        f"Examples: 'thesis broke, moving on' / 'this one isn't happening, cut it'"
    )
    try:
        messages = [{"role": "user", "content": user_message}]
        result = _call_claude_with_tool(800, messages, SELL_TOOL, model=CLAUDE_MODEL)
        decisions = result.get("sell_decisions", [])

        sells = [d for d in decisions if d.get("action") == "SELL"]
        keeps = [d for d in decisions if d.get("action") == "KEEP"]
        if sells:
            log.info(f"💸 Larry changed mind on {len(sells)} position(s): "
                     f"{', '.join(d['market_id'][:16] for d in sells)}")
        else:
            log.info(f"💸 Larry holding all positions — no conviction changes (keeping {len(open_positions)} bets)")

        return decisions
    except Exception as e:
        log.warning(f"Claude unavailable for sell decisions: {type(e).__name__} — skipping")
        return []
