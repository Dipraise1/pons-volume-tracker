"""Uniswap V4 price reads on Robinhood Chain.

The chain's main DEX is a Uniswap V4 singleton PoolManager. Pool state lives in
the singleton's storage, read via extsload at keccak256(abi.encode(poolId, 6)).
Many quote tokens (WEALD, QVR, EARN) only have V4 pools that pair against NATIVE
ETH (0x0), so they were unpriceable through V3 getPool. This prices them.
"""
from __future__ import annotations
import time

from . import chain as C
from .keccak import keccak256

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
# Initialize(bytes32 indexed id, address indexed c0, address indexed c1, ...)
TOPIC_INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
NATIVE_ETH = "0x0000000000000000000000000000000000000000"
POOLS_SLOT = 6
EXTSLOAD = "0x1e2eaeaf"   # extsload(bytes32) -> bytes32

_pools_cache: dict[str, list] = {}      # token -> [pool dicts]  (permanent/session)
_price_cache: dict[str, tuple] = {}     # token -> (ts, usd)


def _pad(addr: str) -> str:
    return "0" * 24 + addr[2:].lower()


def _slot0_slot(pool_id_hex: str) -> str:
    pid = bytes.fromhex(pool_id_hex[2:])
    enc = pid + (POOLS_SLOT).to_bytes(32, "big")
    return "0x" + keccak256(enc).hex()


def _add_slot(slot_hex: str, n: int) -> str:
    return "0x" + format(int(slot_hex, 16) + n, "064x")


def discover_pools(rpc, token: str, from_block: int, to_block: int) -> list:
    """All V4 pools for `token` (as currency0 or currency1) with the counter
    currency. Cached per session — pools don't disappear."""
    token = token.lower()
    if token in _pools_cache:
        return _pools_cache[token]
    pools = []
    for as_c0 in (True, False):
        topics = [TOPIC_INIT, None, None, None]
        topics[2 if as_c0 else 3] = "0x" + _pad(token)
        try:
            logs = rpc.get_logs_windowed(from_block, to_block,
                                         topics=topics, addresses=[POOL_MANAGER],
                                         window=500_000)
        except Exception:
            logs = []
        for lg in logs:
            t = lg["topics"]
            pid = t[1]
            c0 = C.d_addr(t[2]); c1 = C.d_addr(t[3])
            counter = c1 if as_c0 else c0
            pools.append({"id": pid, "counter": counter,
                          "token_is_c0": as_c0, "c0": c0, "c1": c1})
    _pools_cache[token] = pools
    return pools


def pool_sqrt_liq(rpc, pool_id_hex: str):
    slot = _slot0_slot(pool_id_hex)
    try:
        raw = rpc.call("eth_call", [{"to": POOL_MANAGER,
                       "data": EXTSLOAD + slot[2:]}, "latest"])
        slot0 = int(raw, 16)
        sqrt = slot0 & ((1 << 160) - 1)
        lraw = rpc.call("eth_call", [{"to": POOL_MANAGER,
                        "data": EXTSLOAD + _add_slot(slot, 3)[2:]}, "latest"])
        liq = int(lraw, 16) & ((1 << 128) - 1)
    except Exception:
        return 0, 0
    return sqrt, liq


def price_usd(rpc, token: str, eth_usd: float, dec_of, from_block: int,
              to_block: int, depth: int = 0):
    """USD price of `token` via its deepest V4 pool against a base (ETH/USDG),
    with one transitive hop if no direct base pool exists."""
    token = token.lower()
    hit = _price_cache.get(token)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    if depth > 1:
        return None
    base_usd = {NATIVE_ETH: eth_usd, C.USDG: 1.0}
    pools = discover_pools(rpc, token, from_block, to_block)
    best = None   # (liq, usd)
    fallback = None  # (liq, poolinfo) for transitive
    for p in pools:
        sqrt, liq = pool_sqrt_liq(rpc, p["id"])
        if not sqrt:
            continue
        counter = p["counter"]
        tdec = dec_of(token)
        cdec = dec_of(counter)
        # sqrtPrice encodes token1/token0 (decimal-adjusted)
        if p["token_is_c0"]:
            # price = counter per token
            per = C.sqrt_to_price(sqrt, tdec, cdec)  # token1(counter)/token0(token)
            counter_per_token = per
        else:
            per = C.sqrt_to_price(sqrt, cdec, tdec)  # token1(token)/token0(counter)
            counter_per_token = (1.0 / per) if per else 0.0
        if counter_per_token <= 0:
            continue
        if counter in base_usd and base_usd[counter]:
            usd = counter_per_token * base_usd[counter]
            if 1e-15 < usd < 1e9 and (best is None or liq > best[0]):
                best = (liq, usd)
        elif fallback is None or liq > fallback[0]:
            fallback = (liq, counter, counter_per_token)
    if best:
        _price_cache[token] = (time.time(), best[1])
        return best[1]
    # transitive: price via the deepest counter token
    if fallback and depth < 1:
        _, counter, counter_per_token = fallback
        cusd = price_usd(rpc, counter, eth_usd, dec_of, from_block, to_block, depth + 1)
        if cusd:
            usd = counter_per_token * cusd
            if 1e-15 < usd < 1e9:
                _price_cache[token] = (time.time(), usd)
                return usd
    return None
