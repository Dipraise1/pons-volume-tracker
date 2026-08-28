"""Discovers Pons launches and ingests swap volume from raw chain logs."""
from __future__ import annotations
import time
from dataclasses import dataclass

from . import chain as C
from .db import Db
from .rpc import Rpc

# Pools we filter logs against per eth_getLogs call.
ADDR_CHUNK = 100
# Only follow tokens launched within this many days, capped by MAX_POOLS.
# Pons has launched ~250k tokens lifetime; almost all are dead, and filtering
# logs against 250k addresses is not viable. Recency is the useful signal.
ACTIVE_DAYS = 14
MAX_POOLS = 600
# Hydrating a token costs 4 eth_calls. Pons has ~250k lifetime launches, so an
# unbounded historical backfill would mean ~1M calls. Cap per sync and say so.
MAX_NEW_TOKENS = 400

FALLBACK_BLOCK_TIME = 0.101   # measured: Blockscout reports ~101ms


class BlockClock:
    """Maps block numbers to timestamps by interpolating sampled anchors.

    Fetching a timestamp per swap would mean one RPC round-trip per log;
    at ~500 swaps/hour on PONS alone that is wasteful, and block times on
    Nitro are regular enough that interpolation is accurate to a second or two.
    """

    def __init__(self, rpc: Rpc, db: Db):
        self.rpc, self.db = rpc, db
        self._cache: dict[int, int] = {}
        for row in db.q("SELECT block, ts FROM anchors"):
            self._cache[row["block"]] = row["ts"]

    def anchor(self, block: int) -> int:
        if block in self._cache:
            return self._cache[block]
        ts = self.rpc.block_timestamp(block)
        self._cache[block] = ts
        self.db.run("INSERT OR REPLACE INTO anchors(block,ts) VALUES(?,?)",
                    (block, ts))
        return ts

    def block_time(self) -> float:
        if len(self._cache) < 2:
            return FALLBACK_BLOCK_TIME
        lo, hi = min(self._cache), max(self._cache)
        if hi == lo:
            return FALLBACK_BLOCK_TIME
        dt = (self._cache[hi] - self._cache[lo]) / (hi - lo)
        return dt if 0.01 < dt < 10 else FALLBACK_BLOCK_TIME

    def ts_of(self, block: int) -> int:
        if block in self._cache:
            return self._cache[block]
        below = [b for b in self._cache if b <= block]
        above = [b for b in self._cache if b >= block]
        if below and above:
            lo, hi = max(below), min(above)
            if hi == lo:
                return self._cache[lo]
            frac = (block - lo) / (hi - lo)
            return int(self._cache[lo] + frac * (self._cache[hi] - self._cache[lo]))
        ref = max(below) if below else min(above)
        return int(self._cache[ref] + (block - ref) * self.block_time())

    def blocks_for(self, seconds: float) -> int:
        return max(1, int(seconds / self.block_time()))


@dataclass
class TickResult:
    new_launches: list
    new_swaps: list
    from_block: int
    to_block: int


class Indexer:
    def __init__(self, rpc: Rpc, db: Db):
        self.rpc, self.db = rpc, db
        self.clock = BlockClock(rpc, db)
        self._meta_cache: dict[str, dict] = {}
        self._price_cache: dict[str, tuple[float, float]] = {}
        self._price_ttl = 12.0

    # --- token metadata -------------------------------------------------
    def token_meta(self, addrs: list[str]) -> dict[str, dict]:
        want = [a for a in addrs if a not in self._meta_cache]
        if want:
            calls = []
            for a in want:
                for sel in ("symbol", "name", "decimals", "totalSupply"):
                    calls.append(("eth_call",
                                  [{"to": a, "data": C.SEL[sel]}, "latest"]))
            res = self.rpc.batch(calls)
            for i, a in enumerate(want):
                sym, nm, dec, sup = res[i * 4:i * 4 + 4]
                self._meta_cache[a] = {
                    "symbol": C.d_string(sym) or "???",
                    "name": C.d_string(nm) or "",
                    "decimals": C.d_uint(dec) or 18,
                    "total_supply": C.d_uint(sup),
                }
        return {a: self._meta_cache[a] for a in addrs if a in self._meta_cache}

    # --- launches -------------------------------------------------------
    def sync_launches(self, from_block: int, to_block: int) -> list[C.Launch]:
        # No address filter: every launchpad factory emits the same event, so
        # we discover them all (Pons, Lemon, LaunchFactory, and any new fork).
        logs = self.rpc.get_logs_windowed(
            from_block, to_block,
            topics=[C.TOPIC_TOKEN_LAUNCHED],
            window=250_000,          # factory logs are sparse
        )
        if not logs:
            return []
        launches = [C.decode_launch(l) for l in logs]
        known = {r["address"] for r in self.db.q("SELECT address FROM tokens")}
        fresh = [l for l in launches if l.token not in known]
        if not fresh:
            return []
        if len(fresh) > MAX_NEW_TOKENS:
            fresh.sort(key=lambda l: l.block, reverse=True)
            dropped = len(fresh) - MAX_NEW_TOKENS
            fresh = fresh[:MAX_NEW_TOKENS]
            print(f"[indexer] {dropped} older launches skipped this pass "
                  f"(cap {MAX_NEW_TOKENS}); keeping the most recent")

        seen_pads = {C.launchpad_name(l.factory) for l in fresh}
        if len(seen_pads) > 1 or (seen_pads and "Pons" not in seen_pads):
            print(f"[indexer] launches from: {', '.join(sorted(seen_pads))}",
                  flush=True)
        meta = self.token_meta([l.token for l in fresh])
        rows = []
        for l in fresh:
            m = meta.get(l.token, {})
            dec = m.get("decimals", 18)
            rows.append((
                l.token, m.get("symbol", "???"), m.get("name", ""), dec,
                l.pool, l.pair_token, int(C.pool_order(l.token, l.pair_token)),
                l.deployer, l.factory, l.block, self.clock.ts_of(l.block),
                l.initial_buy / 1e18, l.tx,
                m.get("total_supply", 0) / (10 ** dec), 1,
                l.restrictions_end, l.position_id,
            ))
        self.db.many(
            "INSERT OR IGNORE INTO tokens(address,symbol,name,decimals,pool,"
            "pair_token,pair_is_token0,deployer,factory,launch_block,launch_ts,"
            "initial_buy,tx,total_supply,is_pons,restrictions_end,position_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return fresh

    def seed_pons(self) -> None:
        """PONS launched in July, before any sane backfill window, so it is
        registered explicitly. Its deeper fee tier is used as the canonical
        pool; chain-wide PONS stats still sum both tiers."""
        if self.db.one("SELECT 1 FROM tokens WHERE address=?", (C.PONS,)):
            return
        meta = self.token_meta([C.PONS]).get(C.PONS, {})
        calls = [("eth_call", [{"to": p, "data": C.SEL["liquidity"]}, "latest"])
                 for p in C.PONS_POOLS]
        liq = [C.d_uint(x) for x in self.rpc.batch(calls)]
        pool = C.PONS_POOLS[liq.index(max(liq))] if any(liq) else C.PONS_POOLS[0]
        launch_block = 8_963_150      # PONS token deployment
        self.db.run(
            "INSERT OR IGNORE INTO tokens(address,symbol,name,decimals,pool,"
            "pair_token,pair_is_token0,deployer,factory,launch_block,launch_ts,"
            "initial_buy,tx,total_supply,is_pons) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (C.PONS, meta.get("symbol", "PONS"), meta.get("name", "Pons"),
             meta.get("decimals", 18), pool, C.WETH, 1,
             "0x0c37a24f5d23a486fa692d1500881d698b1f77a4", C.PONS_FACTORY_V1,
             launch_block, self.clock.ts_of(launch_block), 0.0, "",
             meta.get("total_supply", 0) / 1e18, 1))

    # --- pools we care about --------------------------------------------
    def active_pools(self) -> dict[str, dict]:
        cutoff = int(time.time()) - ACTIVE_DAYS * 86400
        rows = self.db.q(
            "SELECT address,symbol,decimals,pool,pair_is_token0 FROM tokens "
            "WHERE pool IS NOT NULL AND launch_ts >= ? "
            "ORDER BY launch_block DESC LIMIT ?", (cutoff, MAX_POOLS))
        pools = {
            r["pool"].lower(): {
                "token": r["address"], "symbol": r["symbol"],
                "decimals": r["decimals"], "pair_is_token0": bool(r["pair_is_token0"]),
            } for r in rows
        }
        # PONS itself: always tracked, both fee tiers, WETH is token0 in both.
        for p in C.PONS_POOLS:
            pools.setdefault(p, {"token": C.PONS, "symbol": "PONS",
                                 "decimals": 18, "pair_is_token0": True})
        return pools

    # --- swaps ----------------------------------------------------------
    def sync_swaps(self, from_block: int, to_block: int) -> list[dict]:
        pools = self.active_pools()
        if not pools:
            return []
        addrs = list(pools)
        logs: list[dict] = []
        for i in range(0, len(addrs), ADDR_CHUNK):
            logs.extend(self.rpc.get_logs_windowed(
                from_block, to_block,
                topics=[C.TOPIC_V3_SWAP],
                addresses=addrs[i:i + ADDR_CHUNK],
                window=20_000,
            ))
        out, rows = [], []
        for log in logs:
            s = C.decode_swap(log)
            info = pools.get(s.pool)
            if not info:
                continue
            if info["pair_is_token0"]:
                weth_raw, token_raw = s.amount0, s.amount1
            else:
                weth_raw, token_raw = s.amount1, s.amount0
            weth = weth_raw / 1e18
            tok = token_raw / (10 ** info["decimals"])
            is_buy = weth > 0          # pool receives WETH => trader bought
            ts = self.clock.ts_of(s.block)
            # sqrtPriceX96 is post-swap price of token1 in token0 terms;
            # invert when the pair token sorts first so we always store
            # "WETH per 1 token".
            raw = C.sqrt_to_price(s.sqrt_price, 18, info["decimals"])
            if info["pair_is_token0"]:
                price = 1 / raw if raw else 0.0
            else:
                price = C.sqrt_to_price(s.sqrt_price, info["decimals"], 18)
            trader = s.recipient or s.sender
            rows.append((s.tx, s.log_index, s.pool, s.block, ts,
                         weth, tok, int(is_buy), price, trader))
            out.append({"pool": s.pool, "symbol": info["symbol"],
                        "token": info["token"], "block": s.block, "ts": ts,
                        "weth": weth, "token_amt": tok, "is_buy": is_buy,
                        "tx": s.tx, "price": price, "trader": trader,
                        "decimals": info["decimals"]})
        self.db.many(
            "INSERT OR IGNORE INTO swaps(tx,log_index,pool,block,ts,"
            "weth_amt,token_amt,is_buy,price_weth,trader) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        return out

    # --- prices ---------------------------------------------------------
    def _cached(self, key: str, fn):
        hit = self._price_cache.get(key)
        if hit and (time.time() - hit[0]) < self._price_ttl:
            return hit[1]
        val = fn()
        self._price_cache[key] = (time.time(), val)
        return val

    def _eth_usd_live(self) -> float:
        raw = self.rpc.call("eth_call",
                            [{"to": C.ETH_USD_POOL, "data": C.SEL["slot0"]},
                             "latest"])
        sqrt = C.d_uint(C.words(raw)[0]) if raw else 0
        return C.sqrt_to_price(sqrt, 18, 6)     # WETH(18) -> USDG(6)

    def eth_usd(self) -> float:
        return self._cached("eth_usd", self._eth_usd_live)

    def pons_per_weth(self) -> float:
        return self._cached("ppw", self._pons_per_weth_live)

    def _pons_per_weth_live(self) -> float:
        """Liquidity-weighted PONS/WETH across both fee tiers."""
        calls = []
        for p in C.PONS_POOLS:
            calls.append(("eth_call", [{"to": p, "data": C.SEL["slot0"]}, "latest"]))
            calls.append(("eth_call", [{"to": p, "data": C.SEL["liquidity"]}, "latest"]))
        res = self.rpc.batch(calls)
        num = den = 0.0
        for i in range(len(C.PONS_POOLS)):
            raw, liq_raw = res[i * 2], res[i * 2 + 1]
            if not raw:
                continue
            price = C.sqrt_to_price(C.d_uint(C.words(raw)[0]), 18, 18)
            liq = C.d_uint(liq_raw)
            if price > 0 and liq > 0:
                num += price * liq
                den += liq
        return num / den if den else 0.0

    def pons_usd(self) -> float:
        ppw = self.pons_per_weth()
        return (self.eth_usd() / ppw) if ppw else 0.0

    def circulating(self) -> float:
        calls = [
            ("eth_call", [{"to": C.PONS, "data": C.SEL["totalSupply"]}, "latest"]),
            ("eth_call", [{"to": C.PONS,
                           "data": "0x70a08231" + "0" * 24 + C.DEAD[2:]}, "latest"]),
        ]
        supply, burned = (C.d_uint(x) for x in self.rpc.batch(calls))
        return (supply - burned) / 1e18

    # --- main loop step --------------------------------------------------
    def tick(self, confirmations: int, backfill_hours: float,
             launch_backfill_days: float = 2.0) -> TickResult:
        head = self.rpc.block_number() - confirmations
        self.clock.anchor(head)
        self.seed_pons()

        last = self.db.get_meta("last_block")
        if last is None:
            start = head - self.clock.blocks_for(backfill_hours * 3600)
            self.clock.anchor(start)
            # Only reach back as far as we actually track pools for; scanning
            # to the V1 factory's genesis would pull in ~250k dead tokens.
            lb = max(C.GENESIS_BLOCK,
                     head - self.clock.blocks_for(launch_backfill_days * 86400))
            if lb < start:
                self.sync_launches(lb, start - 1)
        else:
            start = int(last) + 1
        if start > head:
            return TickResult([], [], start, head)

        launches = self.sync_launches(start, head)
        swaps = self.sync_swaps(start, head)

        self.db.set_meta("last_block", head)
        now = int(time.time())
        try:
            self.db.run(
                "INSERT OR REPLACE INTO price_history(ts,pons_usd,eth_usd) "
                "VALUES(?,?,?)", (now, self.pons_usd(), self.eth_usd()))
        except Exception:
            pass
        return TickResult(launches, swaps, start, head)
