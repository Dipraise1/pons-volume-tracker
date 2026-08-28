"""Telegram message formatting (HTML parse mode)."""
from __future__ import annotations
import html
import math
import time

from . import chain as C


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def usd(v: float) -> str:
    if v is None:
        return "?"
    a = abs(v)
    if a >= 1_000_000_000:
        return f"${v/1e9:,.2f}B"
    if a >= 1_000_000:
        return f"${v/1e6:,.2f}M"
    if a >= 1_000:
        return f"${v/1e3:,.1f}K"
    if a >= 1:
        return f"${v:,.2f}"
    if a == 0:
        return "$0"
    # Sub-dollar prices carry the signal for new tokens, so show four
    # significant figures rather than falling back to scientific notation.
    prec = min(18, max(2, 3 - math.floor(math.log10(a))))
    return f"${v:,.{prec}f}"


def eth(v: float) -> str:
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f} Ξ"
    if a >= 1:
        return f"{v:,.3f} Ξ"
    return f"{v:.5f} Ξ"


def num(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000_000:
        return f"{v/1e9:,.2f}B"
    if a >= 1_000_000:
        return f"{v/1e6:,.2f}M"
    if a >= 1_000:
        return f"{v/1e3:,.1f}K"
    return f"{v:,.2f}"


def ago(ts: int | None) -> str:
    if not ts:
        return "?"
    d = max(0, int(time.time()) - int(ts))
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d//60}m ago"
    if d < 86400:
        return f"{d//3600}h ago"
    return f"{d//86400}d ago"


def link(text: str, url: str) -> str:
    return f'<a href="{esc(url)}">{esc(text)}</a>'


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else str(addr)


def bar(buys: int, sells: int, width: int = 12) -> str:
    total = buys + sells
    if not total:
        return "─" * width
    filled = round(width * buys / total)
    return "█" * filled + "░" * (width - filled)
