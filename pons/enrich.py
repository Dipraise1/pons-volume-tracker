"""Per-token enrichment: holders, concentration, dev behaviour, snipers.

Blockscout's /tokens/{addr}/holders endpoint returns stale balances on this
chain (verified: addresses reported holding hundreds of millions actually hold
zero), so holder data is derived from Transfer logs and then confirmed with
balanceOf before anything is displayed.
"""
from __future__ import annotations
import json
import time
import urllib.request

from . import chain as C
from .db import Db
from .rpc import Rpc

CACHE_TTL = 120          # seconds
TOP_N = 10
VERIFY_N = 40            # candidates we re-check with balanceOf
# Transfer-log scanning is exact but costs one getLogs page per 500k blocks.
# Past this span we fall back to Blockscout for *candidates* only, and still
# confirm every displayed balance with balanceOf.
LOG_SCAN_BLOCKS = 1_500_000
BLOCKSCOUT = "https://robinhoodchain.blockscout.com/api/v2"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")


def _bs(path: str):
    req = urllib.request.Request(BLOCKSCOUT + path,
                                 headers={"User-Agent": UA,
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _bs_candidates(token: str) -> tuple[list[str], int | None]:
    """Candidate holders + holder count from the explorer.

    Blockscout's balances on this chain are stale (addresses it lists with
    hundreds of millions can hold zero), so these are treated purely as
    addresses to check on-chain, never as amounts.
    """
    addrs: list[str] = []
    count = None
    try:
        c = _bs(f"/tokens/{token}/counters")
        count = int(c.get("token_holders_count") or 0) or None
    except Exception:
        pass
    try:
        data = _bs(f"/tokens/{token}/holders")
        for item in (data.get("items") or [])[:VERIFY_N]:
            h = (item.get("address") or {}).get("hash")
            if h:
                addrs.append(h.lower())
    except Exception:
        pass
    return addrs, count

_cache: dict[str, tuple[float, dict]] = {}


def _ignored(token_row) -> set[str]:
    out = {C.DEAD, "0x" + "0" * 40, C.PONS_FACTORY, C.PONS_FACTORY_V1}
    if token_row and token_row["pool"]:
        out.add(token_row["pool"].lower())
    return out


def holder_snapshot(rpc: Rpc, db: Db, token: str) -> dict:
    """Exact top holders + concentration for one token."""
    token = token.lower()
    hit = _cache.get(token)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    row = db.one("SELECT * FROM tokens WHERE address=?", (token,))
    if not row:
        return {}
    dec = row["decimals"] or 18
    start = row["launch_block"] or 0
    head = rpc.block_number()
    skip = _ignored(row)

    logs: list = []
    bs_count = None
    if head - start <= LOG_SCAN_BLOCKS:
        try:
            logs = rpc.get_logs_windowed(start, head,
                                         topics=[C.TOPIC_TRANSFER],
                                         addresses=[token],
                                         window=500_000)
        except Exception as exc:
            print(f"[enrich] transfer scan failed for {token}: {exc}",
                  flush=True)

    # Approximate balances to find candidates, then verify the top ones.
    approx: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for lg in logs:
        t = lg["topics"]
        if len(t) < 3:
            continue
        frm, to = C.d_addr(t[1]), C.d_addr(t[2])
        val = C.d_uint(C.words(lg["data"])[0]) if lg["data"] not in ("", "0x") else 0
        approx[frm] = approx.get(frm, 0) - val
        approx[to] = approx.get(to, 0) + val
        blk = int(lg["blockNumber"], 16)
        first_seen.setdefault(to, blk)

    holders = {a: v for a, v in approx.items() if v > 0 and a not in skip}
    total_holders = len(holders)

    if logs:
        candidates = sorted(holders, key=holders.get, reverse=True)[:VERIFY_N]
    else:
        # Too much history to scan: ask the explorer who to check.
        candidates, bs_count = _bs_candidates(token)
        candidates = [a for a in candidates if a not in skip]
        total_holders = bs_count or 0
    if candidates:
        calls = [("eth_call", [{"to": token,
                                "data": "0x70a08231" + "0" * 24 + a[2:]},
                               "latest"]) for a in candidates]
        exact = {a: C.d_uint(v) for a, v in zip(candidates, rpc.batch(calls))}
    else:
        exact = {}

    supply = (row["total_supply"] or 0) * (10 ** dec)
    if not supply:
        raw = rpc.call("eth_call",
                       [{"to": token, "data": C.SEL["totalSupply"]}, "latest"])
        supply = C.d_uint(raw)

    ranked = sorted(((a, b) for a, b in exact.items() if b > 0),
                    key=lambda kv: kv[1], reverse=True)
    top = [{"address": a,
            "amount": b / (10 ** dec),
            "pct": (b / supply * 100) if supply else 0.0,
            "first_block": first_seen.get(a)}
           for a, b in ranked[:TOP_N]]

    snap = {
        "holders": total_holders,
        "top": top,
        "top10_pct": sum(t["pct"] for t in top),
        "supply": supply / (10 ** dec) if supply else 0,
        "transfers": len(logs),
    }
    _cache[token] = (time.time(), snap)
    return snap


_supply_cache: dict[str, tuple[float, float]] = {}


def circulating(rpc: Rpc, db: Db, token: str) -> float:
    """Total supply less anything sent to the burn address.

    PONS has ~288M of its 1B supply burned to 0x…dEaD, so market cap taken
    straight from totalSupply overstates it by ~40%.
    """
    token = token.lower()
    hit = _supply_cache.get(token)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    row = db.one("SELECT decimals,total_supply FROM tokens WHERE address=?",
                 (token,))
    if not row:
        return 0.0
    dec = row["decimals"] or 18
    supply = row["total_supply"] or 0.0
    try:
        raw = rpc.call("eth_call", [{"to": token, "data":
                       "0x70a08231" + "0" * 24 + C.DEAD[2:]}, "latest"])
        burned = C.d_uint(raw) / (10 ** dec)
    except Exception:
        burned = 0.0
    out = max(0.0, supply - burned)
    _supply_cache[token] = (time.time(), out)
    return out


def dev_status(rpc: Rpc, db: Db, token: str) -> dict:
    """Does the deployer still hold, and how much?"""
    row = db.one("SELECT deployer,decimals,total_supply FROM tokens "
                 "WHERE address=?", (token.lower(),))
    if not row or not row["deployer"]:
        return {}
    dec = row["decimals"] or 18
    raw = rpc.call("eth_call",
                   [{"to": token, "data":
                     "0x70a08231" + "0" * 24 + row["deployer"][2:]}, "latest"])
    bal = C.d_uint(raw) / (10 ** dec)
    supply = row["total_supply"] or 0
    pct = (bal / supply * 100) if supply else 0.0
    # Launcher contracts and dev wallets often retain rounding dust; that is
    # not a holding worth warning about.
    sold = pct < 0.01
    return {"address": row["deployer"], "balance": bal, "pct": pct,
            "sold": sold, "holding": not sold}


def early_activity(db: Db, token: str, window_blocks: int = 200) -> dict:
    """Sniper / bundle style read on the first blocks after launch."""
    row = db.one("SELECT pool,launch_block,restrictions_end FROM tokens "
                 "WHERE address=?", (token.lower(),))
    if not row or not row["pool"]:
        return {}
    end = row["launch_block"] + window_blocks
    rows = db.q(
        "SELECT trader, block, weth_amt, token_amt FROM swaps "
        "WHERE pool=? AND block <= ? AND is_buy=1", (row["pool"], end))
    if not rows:
        return {"snipers": 0, "same_block": 0, "early_eth": 0.0}
    same_block = [r for r in rows if r["block"] == row["launch_block"]]
    traders = {r["trader"] for r in rows if r["trader"]}
    return {
        "snipers": len(traders),
        "same_block": len(same_block),
        "early_eth": sum(abs(r["weth_amt"]) for r in rows),
        "buys": len(rows),
    }


def trader_outcomes(db: Db, pool: str, limit: int = 30) -> list[str]:
    """Per-early-buyer state: held / sold part / sold out.

    Returns a list of 'hold' | 'part' | 'sold' for the first `limit` buyers,
    rendered as the coloured dot grid in alerts.
    """
    rows = db.q(
        "SELECT trader, "
        "  SUM(CASE WHEN is_buy=1 THEN token_amt ELSE 0 END) bought, "
        "  SUM(CASE WHEN is_buy=0 THEN -token_amt ELSE 0 END) sold, "
        "  MIN(block) first_block "
        "FROM swaps WHERE pool=? AND trader IS NOT NULL "
        "GROUP BY trader ORDER BY first_block ASC LIMIT ?", (pool, limit))
    out = []
    for r in rows:
        bought = abs(r["bought"] or 0)
        sold = abs(r["sold"] or 0)
        if bought <= 0:
            continue
        ratio = sold / bought
        out.append("sold" if ratio >= 0.95 else
                   "part" if ratio > 0.05 else "hold")
    return out


def price_change(db: Db, pool: str, window_s: int) -> float | None:
    """% change in a token's WETH price over a window, from indexed swaps."""
    now = int(time.time())
    new = db.one("SELECT price_weth FROM swaps WHERE pool=? AND price_weth>0 "
                 "ORDER BY ts DESC, log_index DESC LIMIT 1", (pool,))
    old = db.one("SELECT price_weth FROM swaps WHERE pool=? AND price_weth>0 "
                 "AND ts <= ? ORDER BY ts DESC LIMIT 1", (pool, now - window_s))
    if not new or not old or not old["price_weth"]:
        return None
    return (new["price_weth"] - old["price_weth"]) / old["price_weth"] * 100


def volume_change(db: Db, pool: str, window_s: int) -> float | None:
    """This window's volume vs the one before it."""
    now = int(time.time())
    cur = db.one("SELECT COALESCE(SUM(ABS(weth_amt)),0) v FROM swaps "
                 "WHERE pool=? AND ts >= ?", (pool, now - window_s))
    prev = db.one("SELECT COALESCE(SUM(ABS(weth_amt)),0) v FROM swaps "
                  "WHERE pool=? AND ts >= ? AND ts < ?",
                  (pool, now - 2 * window_s, now - window_s))
    if not prev or not prev["v"]:
        return None
    return (cur["v"] - prev["v"]) / prev["v"] * 100
