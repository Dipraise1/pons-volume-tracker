"""Trader-facing intelligence: graduation progress, rug safety, deployer history.

These are the questions a meme trader actually asks before buying:
  how close is it to graduating, can the LP be pulled, has the dev done this
  before, and is the float concentrated.
"""
from __future__ import annotations
import time

from . import chain as C
from . import enrich
from .db import Db
from .rpc import Rpc

_grad_cache: dict[str, tuple[float, dict]] = {}
GRAD_TTL = 30


# --- 1. graduation --------------------------------------------------------
def graduation(rpc: Rpc, token: str) -> dict:
    """Progress toward the 4.2 WETH graduation threshold.

    This is the Pons equivalent of a bonding-curve percentage: the single
    number that tells you how much room is left before the launch completes.
    """
    token = token.lower()
    hit = _grad_cache.get(token)
    if hit and time.time() - hit[0] < GRAD_TTL:
        return hit[1]
    out: dict = {}
    for factory in C.GRAD_FACTORIES:
        try:
            raw = rpc.call("eth_call", [{
                "to": factory,
                "data": C.SEL["graduationStatus"] + "0" * 24 + token[2:],
            }, "latest"])
        except Exception:
            continue
        if not raw or raw == "0x":
            continue
        w = C.words(raw)
        if len(w) < 3:
            continue
        acc = C.d_uint(w[0]) / 1e18
        threshold = C.d_uint(w[1]) / 1e18 or C.GRADUATION_THRESHOLD
        graduated = bool(C.d_uint(w[2]))
        if acc or graduated:
            out = {"accumulated": acc, "threshold": threshold,
                   "graduated": graduated,
                   "pct": (acc / threshold * 100) if threshold else 0.0,
                   "remaining": max(0.0, threshold - acc)}
            break
    _grad_cache[token] = (time.time(), out)
    return out


def progress_bar(pct: float, width: int = 14) -> str:
    filled = max(0, min(width, round(width * pct / 100)))
    return "█" * filled + "░" * (width - filled)


# --- 2. safety ------------------------------------------------------------
def safety(rpc: Rpc, db: Db, token: str) -> dict:
    """Composite rug-risk read. Every point is justified in `reasons`."""
    row = db.one("SELECT * FROM tokens WHERE address=?", (token.lower(),))
    if not row:
        return {}
    dec = row["decimals"] or 18
    supply = row["total_supply"] or 0
    pool = row["pool"]
    reasons: list[str] = []
    score = 100

    # LP lock: the factory mints the V3 position, the locker should hold it.
    lp_locked = None
    if row["position_id"]:
        try:
            raw = rpc.call("eth_call", [{
                "to": C.POSITION_MANAGER,
                "data": C.SEL["ownerOf"] + f"{int(row['position_id']):064x}",
            }, "latest"])
            owner = C.d_addr(C.words(raw)[0]) if raw and raw != "0x" else ""
            lp_locked = owner == C.LOCKER
        except Exception:
            lp_locked = None
    if lp_locked is True:
        reasons.append("✅ LP locked in the Pons locker")
    elif lp_locked is False:
        score -= 40
        reasons.append("🔴 LP position is NOT held by the locker")

    # Dev holdings.
    dev = enrich.dev_status(rpc, db, token) or {}
    if dev:
        if dev.get("sold"):
            reasons.append("✅ Dev holds nothing")
        else:
            pct = dev["pct"]
            score -= 30 if pct > 5 else 15 if pct > 1 else 5
            reasons.append(f"⚠️ Dev holds {pct:.2f}%")

    # Float concentration.
    snap = enrich.holder_snapshot(rpc, db, token) or {}
    if snap.get("top"):
        top10 = snap["top10_pct"]
        if top10 > 50:
            score -= 30
            reasons.append(f"🔴 Top 10 hold {top10:.1f}%")
        elif top10 > 30:
            score -= 15
            reasons.append(f"⚠️ Top 10 hold {top10:.1f}%")
        else:
            reasons.append(f"✅ Top 10 hold {top10:.1f}%")
        if (snap.get("holders") or 0) < 25:
            score -= 10
            reasons.append(f"⚠️ Only {snap['holders']} holders")

    # Pool depth — a drained pool means there is nothing to sell into.
    depth = None
    try:
        raw = rpc.call("eth_call", [{
            "to": C.WETH,
            "data": C.SEL["balanceOf"] + "0" * 24 + pool[2:]}, "latest"])
        depth = C.d_uint(raw) / 1e18
    except Exception:
        pass
    if depth is not None:
        if depth < 0.05:
            score -= 35
            reasons.append(f"🔴 Pool holds only {depth:.4f} Ξ")
        else:
            reasons.append(f"✅ Pool depth {depth:.3f} Ξ")

    # Anti-snipe restrictions.
    restricted = False
    try:
        head = rpc.block_number()
        restricted = bool(row["restrictions_end"]) and head < row["restrictions_end"]
    except Exception:
        pass
    if restricted:
        reasons.append(f"⏳ Anti-snipe limits still active "
                       f"(max wallet {C.MAX_WALLET_BPS/100:.0f}%)")

    score = max(0, min(100, score))
    grade = ("LOW" if score >= 75 else "MEDIUM" if score >= 50 else "HIGH")
    return {"score": score, "grade": grade, "reasons": reasons,
            "lp_locked": lp_locked, "depth": depth,
            "dev_pct": dev.get("pct"), "restricted": restricted,
            "top10": snap.get("top10_pct"), "holders": snap.get("holders")}


# --- 3. deployer reputation ----------------------------------------------
def deployer_history(rpc: Rpc, db: Db, deployer: str) -> dict:
    """Prior launches by the same wallet, and how many went to zero.

    A drained pool is the clearest on-chain signature of a dead launch, and
    it is one cheap balanceOf per prior token.
    """
    if not deployer:
        return {}
    rows = db.q("SELECT address,symbol,pool,launch_ts FROM tokens "
                "WHERE LOWER(deployer)=? ORDER BY launch_block DESC LIMIT 25",
                (deployer.lower(),))
    if len(rows) <= 1:
        return {"launches": len(rows), "dead": 0, "prior": []}
    calls = [("eth_call", [{"to": C.WETH,
                            "data": C.SEL["balanceOf"] + "0" * 24 + r["pool"][2:]},
                           "latest"]) for r in rows if r["pool"]]
    depths = [C.d_uint(x) / 1e18 if x else 0.0 for x in rpc.batch(calls)]
    prior, dead = [], 0
    for r, d in zip([r for r in rows if r["pool"]], depths):
        is_dead = d < 0.05
        dead += is_dead
        prior.append({"symbol": r["symbol"], "address": r["address"],
                      "depth": d, "dead": is_dead, "ts": r["launch_ts"]})
    return {"launches": len(prior), "dead": dead, "prior": prior,
            "dead_pct": (dead / len(prior) * 100) if prior else 0.0}
