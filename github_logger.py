"""
github_logger.py — Posts Larry's important events as comments to GitHub issue #2.
Requires GITHUB_TOKEN env var (PAT with issues:write scope).
Requires GITHUB_REPO env var, e.g. "larrylosesagain-hash/larry-agent".
Silently no-ops if token is missing so Larry still runs without it.
"""

import os
import logging
import urllib.request
import urllib.error
import json
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
_GITHUB_REPO  = os.environ.get("GITHUB_REPO", "larrylosesagain-hash/larry-agent")
_LOG_ISSUE    = 2   # issue number for live logs


def _post_comment(body: str) -> bool:
    """Post a comment to the log issue. Returns True on success."""
    if not _GITHUB_TOKEN:
        return False
    url = f"https://api.github.com/repos/{_GITHUB_REPO}/issues/{_LOG_ISSUE}/comments"
    payload = json.dumps({"body": body}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {_GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "larry-agent",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except urllib.error.URLError as e:
        logger.warning("github_logger: failed to post — %s", e)
        return False


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ─── Public helpers ──────────────────────────────────────────────────────────

def log_bet_placed(question: str, outcome: str, amount: float, odds: float, bankroll: float):
    body = (
        f"### 🎰 Bet Placed — {_ts()}\n"
        f"**Market:** {question}\n"
        f"**Side:** {outcome} @ {odds:.0%}\n"
        f"**Amount:** ${amount:.2f} &nbsp;|&nbsp; **Bankroll after:** ${bankroll:.2f}"
    )
    _post_comment(body)


def log_bet_won(question: str, outcome: str, payout: float, bankroll: float):
    body = (
        f"### ✅ Bet WON — {_ts()}\n"
        f"**Market:** {question}\n"
        f"**Side:** {outcome}\n"
        f"**Payout:** +${payout:.2f} &nbsp;|&nbsp; **Bankroll:** ${bankroll:.2f}"
    )
    _post_comment(body)


def log_bet_lost(question: str, outcome: str, amount: float, bankroll: float):
    body = (
        f"### 💸 Bet LOST — {_ts()}\n"
        f"**Market:** {question}\n"
        f"**Side:** {outcome}\n"
        f"**Lost:** -${amount:.2f} &nbsp;|&nbsp; **Bankroll:** ${bankroll:.2f}"
    )
    _post_comment(body)


def log_error(context: str, error: Exception):
    body = (
        f"### ⚠️ Error — {_ts()}\n"
        f"**Context:** {context}\n"
        f"```\n{type(error).__name__}: {error}\n```"
    )
    _post_comment(body)


def log_grandma(event: str, amount: float, balance: float):
    emoji = "👵💸" if event == "INJECT" else "👵💰"
    body = (
        f"### {emoji} Grandma Wallet — {_ts()}\n"
        f"**Event:** {event} ${amount:.2f}\n"
        f"**Balance after:** ${balance:.2f}"
    )
    _post_comment(body)


def log_bankroll(reason: str, change: float, balance: float):
    emoji = "📈" if change >= 0 else "📉"
    body = (
        f"### {emoji} Bankroll Update — {_ts()}\n"
        f"**Reason:** {reason}\n"
        f"**Change:** {'+'if change>=0 else ''}{change:.2f} → **${balance:.2f}**"
    )
    _post_comment(body)


def log_survival_mode(bankroll: float):
    body = (
        f"### 🚨 SURVIVAL MODE — {_ts()}\n"
        f"Bankroll dropped to **${bankroll:.2f}**. Larry is in trouble."
    )
    _post_comment(body)
