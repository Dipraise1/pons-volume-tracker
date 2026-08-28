"""Signal-quality engine: momentum, acceleration, trending rank, launch grade.

The goal is to surface pumps *early* (before absolute volume is obvious) and to
rank what's hot by acceleration, not just raw size.
"""
from __future__ import annotations
import time

from . import chain as C
from .db import Db


def _win(db: Db, pool: str, start: int, end: int) -> dict:
    r = db.one(
        "SELECT COUNT(*) n, COALESCE(SUM(ABS(weth_amt)),0) vol, "
        "COALESCE(SUM(is_buy),0) buys, COUNT(DISTINCT trader) traders "
        "FROM swaps WHERE pool=? AND ts>=? AND ts<?", (pool, start, end))
    return {"n": r["n"] or 0, "vol": r["vol"] or 0.0,
            "buys": r["buys"] or 0, "traders": r["traders"] or 0}


def momentum(db: Db, pool: str, window_s: int = 300) -> dict:
    """Compare the last window to the one before it. Acceleration is the ratio.

    A coin doing 2 ETH now vs 0.3 ETH in the prior window (6.7x) is accelerating
    even if 2 ETH alone wouldn't clear a size threshold — that's the early signal.
    """
    now = int(time.time())
    cur = _win(db, pool, now - window_s, now + 1)
    prev = _win(db, pool, now - 2 * window_s, now - window_s)
    accel = (cur["vol"] / prev["vol"]) if prev["vol"] > 0 else (
        float("inf") if cur["vol"] > 0 else 0.0)
    buy_ratio = (cur["buys"] / cur["n"]) if cur["n"] else 0.0
    # score rewards acceleration, buy dominance, and fresh unique buyers
    score = 0.0
    if cur["vol"] > 0:
        a = min(accel, 10) if accel != float("inf") else 10
        score = a * (0.5 + buy_ratio) * (1 + cur["traders"] / 10)
    return {"cur": cur, "prev": prev, "accel": accel,
            "buy_ratio": buy_ratio, "score": score, "window": window_s}


def trending(db: Db, window_s: int = 900, limit: int = 12) -> list[dict]:
    """Rank recently-active tokens by momentum score."""
    since = int(time.time()) - window_s
    pools = db.q(
        "SELECT DISTINCT s.pool, t.address, t.symbol FROM swaps s "
        "JOIN tokens t ON t.pool = s.pool "
        "WHERE s.ts >= ? AND t.address != ?", (since, C.PONS))
    rows = []
    for p in pools:
        m = momentum(db, p["pool"], 300)
        if m["cur"]["vol"] <= 0:
            continue
        rows.append({"symbol": p["symbol"], "address": p["address"],
                     "pool": p["pool"], **m})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:limit]


def acceleration_alerts(db: Db, min_accel: float, min_eth: float,
                        window_s: int = 300) -> list[dict]:
    """Tokens whose volume is accelerating hard right now — early-pump signal."""
    since = int(time.time()) - window_s
    pools = db.q(
        "SELECT DISTINCT s.pool, t.address, t.symbol FROM swaps s "
        "JOIN tokens t ON t.pool = s.pool "
        "WHERE s.ts >= ? AND t.address != ?", (since, C.PONS))
    out = []
    for p in pools:
        m = momentum(db, p["pool"], window_s)
        if m["cur"]["vol"] < min_eth:
            continue
        if m["accel"] >= min_accel and m["buy_ratio"] >= 0.55:
            out.append({"symbol": p["symbol"], "address": p["address"],
                        "pool": p["pool"], **m})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


# --- launch quality grade -------------------------------------------------
def launch_grade(rpc, db: Db, token: str) -> dict:
    """Instant SAFE / RISKY / AVOID tag for a fresh launch.

    Reuses the same on-chain safety read used elsewhere, distilled to a single
    tag a trader can act on at a glance.
    """
    from . import intel
    sf = intel.safety(rpc, db, token) or {}
    score = sf.get("score", 50)
    early = intel.early_activity(db, token) or {}
    snipers = early.get("same_block", 0)

    # sniper-heavy opens are a red flag even when the rest looks clean
    if snipers >= 5:
        score -= 15
    tag = "🟢 SAFE" if score >= 75 else "🟡 RISKY" if score >= 50 else "🔴 AVOID"
    return {"tag": tag, "score": max(0, score),
            "lp_locked": sf.get("lp_locked"), "dev_pct": sf.get("dev_pct"),
            "top10": sf.get("top10"), "snipers": snipers,
            "reasons": sf.get("reasons", [])}
