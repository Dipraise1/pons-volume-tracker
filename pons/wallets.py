"""Wallet PnL, smart-money detection, and the bot's own call record.

Both answer questions a trader cares about more than raw volume:
  who is buying this, and is this alert feed actually any good.
"""
from __future__ import annotations
import time

from .db import Db

MIN_TOKENS_FOR_RANK = 2      # wallets must trade >1 token to be "smart"
DUST_ETH = 0.005


def rebuild(db: Db, window_s: int = 7 * 86400) -> int:
    """Recompute per-wallet PnL from indexed swaps.

    Average-cost basis: realised = proceeds - (tokens sold x average cost).
    Unrealised is deliberately excluded from the ranking so a wallet cannot
    look profitable purely by holding a token whose last print was a wick.
    """
    since = int(time.time()) - window_s
    rows = db.q(
        "SELECT trader, pool, "
        "  SUM(CASE WHEN is_buy=1 THEN ABS(weth_amt) ELSE 0 END) in_eth, "
        "  SUM(CASE WHEN is_buy=1 THEN ABS(token_amt) ELSE 0 END) in_tok, "
        "  SUM(CASE WHEN is_buy=0 THEN ABS(weth_amt) ELSE 0 END) out_eth, "
        "  SUM(CASE WHEN is_buy=0 THEN ABS(token_amt) ELSE 0 END) out_tok "
        "FROM swaps WHERE ts >= ? AND trader IS NOT NULL AND trader != '' "
        "GROUP BY trader, pool", (since,))

    agg: dict[str, dict] = {}
    for r in rows:
        if (r["in_eth"] or 0) < DUST_ETH:
            continue
        in_tok = r["in_tok"] or 0
        if in_tok <= 0:
            continue
        avg_cost = (r["in_eth"] or 0) / in_tok
        sold_tok = min(r["out_tok"] or 0, in_tok)
        realized = (r["out_eth"] or 0) - sold_tok * avg_cost
        a = agg.setdefault(r["trader"], {"tokens": 0, "wins": 0,
                                         "realized": 0.0, "volume": 0.0})
        a["tokens"] += 1
        a["wins"] += 1 if realized > 0 else 0
        a["realized"] += realized
        a["volume"] += (r["in_eth"] or 0) + (r["out_eth"] or 0)

    now = int(time.time())
    db.run("DELETE FROM wallets")
    db.many("INSERT INTO wallets(address,tokens,wins,realized,unrealized,"
            "volume,updated_at) VALUES(?,?,?,?,?,?,?)",
            [(a, v["tokens"], v["wins"], v["realized"], 0.0, v["volume"], now)
             for a, v in agg.items()])
    return len(agg)


def smart_money(db: Db, limit: int = 15) -> list[dict]:
    rows = db.q("SELECT * FROM wallets WHERE tokens >= ? AND realized > 0 "
                "ORDER BY realized DESC LIMIT ?", (MIN_TOKENS_FOR_RANK, limit))
    return [dict(r) for r in rows]


def smart_set(db: Db, limit: int = 40) -> set[str]:
    return {r["address"] for r in
            db.q("SELECT address FROM wallets WHERE tokens >= ? AND realized > 0 "
                 "ORDER BY realized DESC LIMIT ?", (MIN_TOKENS_FOR_RANK, limit))}


def wallet(db: Db, address: str) -> dict | None:
    r = db.one("SELECT * FROM wallets WHERE address=?", (address.lower(),))
    return dict(r) if r else None


def smart_buyers(db: Db, pool: str, window_s: int, limit: int = 40) -> list[dict]:
    """Ranked wallets that bought this pool inside the window."""
    smart = smart_set(db, limit)
    if not smart:
        return []
    since = int(time.time()) - window_s
    rows = db.q("SELECT DISTINCT trader FROM swaps "
                "WHERE pool=? AND is_buy=1 AND ts >= ?", (pool, since))
    hits = [r["trader"] for r in rows if r["trader"] in smart]
    out = []
    for h in hits:
        w = wallet(db, h)
        if w:
            out.append(w)
    return sorted(out, key=lambda w: w["realized"], reverse=True)


# --- call performance -----------------------------------------------------
def record_call(db: Db, token: str, kind: str, price_usd: float,
                mcap_usd: float) -> None:
    db.run("INSERT OR IGNORE INTO calls(token,ts,kind,price_usd,mcap_usd,"
           "peak_usd) VALUES(?,?,?,?,?,?)",
           (token.lower(), int(time.time()), kind, price_usd, mcap_usd,
            price_usd))


def update_peaks(db: Db) -> None:
    """Mark the best price each called token has printed since the call."""
    for r in db.q("SELECT token, ts, price_usd, peak_usd FROM calls"):
        row = db.one(
            "SELECT MAX(s.price_weth) p FROM swaps s "
            "JOIN tokens t ON t.pool = s.pool "
            "WHERE t.address = ? AND s.ts >= ?", (r["token"], r["ts"]))
        if not row or not row["p"]:
            continue
        eth = db.one("SELECT eth_usd FROM price_history ORDER BY ts DESC LIMIT 1")
        peak = row["p"] * (eth["eth_usd"] if eth else 0)
        if peak > (r["peak_usd"] or 0):
            db.run("UPDATE calls SET peak_usd=? WHERE token=? AND ts=?",
                   (peak, r["token"], r["ts"]))


def scoreboard(db: Db, limit: int = 10) -> dict:
    update_peaks(db)
    rows = db.q("SELECT c.token, c.ts, c.kind, c.price_usd, c.mcap_usd, "
                "c.peak_usd, t.symbol FROM calls c "
                "LEFT JOIN tokens t ON t.address = c.token "
                "ORDER BY c.ts DESC LIMIT ?", (limit,))
    calls = []
    for r in rows:
        base = r["price_usd"] or 0
        mult = (r["peak_usd"] / base) if base else 0
        calls.append({**dict(r), "multiple": mult})
    wins = [c for c in calls if c["multiple"] >= 2]
    return {"calls": calls, "total": len(calls), "doubles": len(wins),
            "best": max((c["multiple"] for c in calls), default=0)}


def sync_smart_follows(db, top: int = 20) -> int:
    """Keep the top smart-money wallets in the followed set (source='smart')."""
    import time
    now = int(time.time())
    ranked = smart_money(db, top)
    for w in ranked:
        db.follow(w["address"], f"smart (+{w['realized']:.2f}Ξ)", "smart", now)
    return len(ranked)


def copy_buys(db, new_swaps: list) -> list[dict]:
    """From this cycle's swaps, the buys made by followed wallets."""
    followed = db.followed()
    if not followed:
        return []
    out = []
    for sw in new_swaps:
        if not sw.get("is_buy"):
            continue
        tr = (sw.get("trader") or "").lower()
        if tr in followed:
            out.append({**sw, "label": followed[tr].get("label") or "",
                        "source": followed[tr].get("source") or "manual"})
    return out
