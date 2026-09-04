"""Dex Paid / CTO flags and on-chain CTO heuristic.

Dex Paid and paid-CTO come from DEX Screener's orders API. That API rate-limits
datacenter IPs (HTTP 1015), so results are best-effort and cached; when it's
unreachable the flags read as unknown rather than wrong. CTO also has an
on-chain heuristic (dev abandoned + renounced + still trading) that needs no
external service and always works.
"""
from __future__ import annotations
import json
import time
import urllib.request

from . import chain as C
from .db import Db
from .rpc import Rpc

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")
DS_ORDERS = "https://api.dexscreener.com/orders/v1/robinhood/"
_cache: dict[str, tuple[float, dict]] = {}
TTL = 1800


def dex_flags(token: str) -> dict:
    """{'dex_paid': bool|None, 'cto_paid': bool|None} from DEX Screener.
    None means 'couldn't check' (rate-limited); never guessed."""
    token = token.lower()
    hit = _cache.get(token)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    out = {"dex_paid": None, "cto_paid": None}
    try:
        req = urllib.request.Request(DS_ORDERS + token,
                                     headers={"User-Agent": UA,
                                              "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            orders = json.load(r)
        if isinstance(orders, list):
            out["dex_paid"] = any(
                o.get("type") == "tokenProfile" and o.get("status") == "approved"
                for o in orders)
            out["cto_paid"] = any(
                o.get("type") == "communityTakeover"
                and o.get("status") == "approved" for o in orders)
    except Exception:
        pass   # rate-limited / unreachable -> leave as None
    _cache[token] = (time.time(), out)
    return out


def cto_onchain(rpc: Rpc, db: Db, token: str) -> bool:
    """Heuristic 'possible CTO': the deployer has fully exited (balance ~0) yet
    the token is still actively bought. Necessary-not-sufficient — a real CTO
    also needs a community, which isn't observable on-chain."""
    row = db.one("SELECT deployer,pool,decimals,launch_ts FROM tokens "
                 "WHERE address=?", (token.lower(),))
    if not row or not row["deployer"]:
        return False
    dec = row["decimals"] or 18
    try:
        raw = rpc.call("eth_call", [{"to": token.lower(), "data":
                       "0x70a08231" + "0" * 24 + row["deployer"][2:]}, "latest"])
        dev_bal = C.d_uint(raw) / (10 ** dec)
    except Exception:
        return False
    if dev_bal > 0:
        return False
    # still trading recently, and not brand-new (needs time to be "taken over")
    now = int(time.time())
    if (row["launch_ts"] or now) > now - 3600:
        return False
    r = db.one("SELECT COUNT(*) n FROM swaps WHERE pool=? AND is_buy=1 AND ts>=?",
               (row["pool"], now - 1800))
    return (r["n"] or 0) >= 5


def cto_flag(rpc: Rpc, db: Db, token: str) -> str:
    """Combined CTO indicator for the card."""
    f = dex_flags(token)
    if f.get("cto_paid"):
        return "✅"
    return "🟡 maybe" if cto_onchain(rpc, db, token) else "❌"
