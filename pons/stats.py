"""Aggregation queries over indexed swaps."""
from __future__ import annotations
import time

from . import chain as C
from .db import Db


def _since(window_s: int) -> int:
    return int(time.time()) - window_s


def volume(db: Db, window_s: int, pool: str | None = None) -> dict:
    where = "ts >= ?"
    args: list = [_since(window_s)]
    if pool:
        where += " AND pool = ?"
        args.append(pool.lower())
    row = db.one(
        f"SELECT COUNT(*) n, "
        f"       COALESCE(SUM(ABS(weth_amt)),0) vol, "
        f"       COALESCE(SUM(is_buy),0) buys "
        f"FROM swaps WHERE {where}", tuple(args))
    n = row["n"] or 0
    return {"swaps": n, "weth": row["vol"] or 0.0,
            "buys": row["buys"] or 0, "sells": n - (row["buys"] or 0)}


def pons_volume(db: Db, window_s: int) -> dict:
    marks = ",".join("?" * len(C.PONS_POOLS))
    row = db.one(
        f"SELECT COUNT(*) n, COALESCE(SUM(ABS(weth_amt)),0) vol, "
        f"COALESCE(SUM(is_buy),0) buys FROM swaps "
        f"WHERE ts >= ? AND pool IN ({marks})",
        (_since(window_s), *C.PONS_POOLS))
    n = row["n"] or 0
    return {"swaps": n, "weth": row["vol"] or 0.0,
            "buys": row["buys"] or 0, "sells": n - (row["buys"] or 0)}


def top_tokens(db: Db, window_s: int, limit: int = 10) -> list[dict]:
    rows = db.q(
        "SELECT t.symbol, t.address, t.pool, "
        "       COUNT(s.tx) n, SUM(ABS(s.weth_amt)) vol, SUM(s.is_buy) buys "
        "FROM swaps s JOIN tokens t ON t.pool = s.pool "
        "WHERE s.ts >= ? GROUP BY t.pool "
        "HAVING vol > 0 ORDER BY vol DESC LIMIT ?", (_since(window_s), limit))
    return [dict(r) for r in rows]


def recent_launches(db: Db, limit: int = 10) -> list[dict]:
    rows = db.q(
        "SELECT address,symbol,name,pool,deployer,factory,launch_block,"
        "launch_ts,initial_buy,tx FROM tokens ORDER BY launch_block DESC LIMIT ?",
        (limit,))
    return [dict(r) for r in rows]


def launch_counts(db: Db) -> dict:
    now = int(time.time())
    out = {}
    for label, secs in (("1h", 3600), ("24h", 86400), ("7d", 604800)):
        row = db.one("SELECT COUNT(*) n FROM tokens WHERE launch_ts >= ?",
                     (now - secs,))
        out[label] = row["n"] or 0
    row = db.one("SELECT COUNT(*) n FROM tokens")
    out["total"] = row["n"] or 0
    return out


def token_by_query(db: Db, needle: str):
    needle = needle.strip().lower()
    row = db.one("SELECT * FROM tokens WHERE LOWER(address)=?", (needle,))
    if row:
        return dict(row)
    row = db.one("SELECT * FROM tokens WHERE LOWER(symbol)=? "
                 "ORDER BY launch_block DESC", (needle,))
    return dict(row) if row else None


def price_change(db: Db, window_s: int) -> tuple[float, float] | None:
    """(old_price, new_price) around the given window, if we have both."""
    now = int(time.time())
    new = db.one("SELECT pons_usd FROM price_history "
                 "ORDER BY ts DESC LIMIT 1")
    old = db.one("SELECT pons_usd FROM price_history "
                 "WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (now - window_s,))
    if not new or not old or not old["pons_usd"] or not new["pons_usd"]:
        return None
    return old["pons_usd"], new["pons_usd"]


def launchpad_breakdown(db: Db, window_s: int) -> list[dict]:
    """Launches + tracked volume grouped by launchpad, over a window."""
    since = _since(window_s)
    rows = db.q(
        "SELECT t.factory, "
        "  COUNT(DISTINCT t.address) launches, "
        "  COALESCE(SUM(ABS(s.weth_amt)),0) vol, "
        "  COUNT(s.tx) swaps "
        "FROM tokens t LEFT JOIN swaps s "
        "  ON s.pool = t.pool AND s.ts >= ? "
        "WHERE t.launch_ts >= ? "
        "GROUP BY t.factory", (since, since))
    return [dict(r) for r in rows if r["factory"]]


def launchpad_totals(db: Db) -> list[dict]:
    """All-time launch counts per launchpad."""
    rows = db.q("SELECT factory, COUNT(*) n FROM tokens "
                "WHERE factory IS NOT NULL GROUP BY factory ORDER BY n DESC")
    return [dict(r) for r in rows]


def tokens_with_volume(db: Db, window_s: int, min_eth: float) -> list[dict]:
    """Every token that traded at least `min_eth` in the window."""
    since = _since(window_s)
    rows = db.q(
        "SELECT t.address, t.symbol, t.name, t.pool, t.factory, t.total_supply, "
        "  COUNT(s.tx) n, COALESCE(SUM(ABS(s.weth_amt)),0) v, "
        "  COALESCE(SUM(s.is_buy),0) b "
        "FROM swaps s JOIN tokens t ON t.pool = s.pool "
        "WHERE s.ts >= ? AND t.address != ? "
        "GROUP BY s.pool HAVING v >= ? ORDER BY v DESC",
        (since, C.PONS, min_eth))
    return [dict(r) for r in rows]


def token_surges(db: Db, window_s: int, min_swaps: int = 3) -> list[dict]:
    """Per-pool price change over a window, computed from first vs last swap
    price. One pass over the window's swaps — cheap enough to run each cycle.
    """
    since = _since(window_s)
    rows = db.q(
        "SELECT s.pool, t.address, "
        "  COUNT(*) n, "
        "  COALESCE(SUM(ABS(s.weth_amt)),0) vol, "
        "  COALESCE(SUM(s.is_buy),0) buys "
        "FROM swaps s JOIN tokens t ON t.pool = s.pool "
        "WHERE s.ts >= ? AND s.price_weth > 0 "
        "GROUP BY s.pool HAVING n >= ?", (since, min_swaps))
    out = []
    for r in rows:
        first = db.one("SELECT price_weth FROM swaps WHERE pool=? AND ts>=? "
                       "AND price_weth>0 ORDER BY ts ASC, log_index ASC LIMIT 1",
                       (r["pool"], since))
        last = db.one("SELECT price_weth FROM swaps WHERE pool=? AND price_weth>0 "
                      "ORDER BY ts DESC, log_index DESC LIMIT 1", (r["pool"],))
        if not first or not last or not first["price_weth"]:
            continue
        pct = (last["price_weth"] - first["price_weth"]) / first["price_weth"] * 100
        out.append({"pool": r["pool"], "address": r["address"], "pct": pct,
                    "vol": r["vol"], "swaps": r["n"], "buys": r["buys"]})
    return out


def indexed_range(db: Db) -> dict:
    row = db.one("SELECT MIN(ts) lo, MAX(ts) hi, COUNT(*) n FROM swaps")
    return {"lo": row["lo"], "hi": row["hi"], "n": row["n"] or 0}
