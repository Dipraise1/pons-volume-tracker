"""Robinhood Chain constants, event decoding, and pricing.

Every address below was resolved from chain state / verified Blockscout
source, not from a third-party index.
"""
from __future__ import annotations
from dataclasses import dataclass

CHAIN_ID = 4663
EXPLORER = "https://robinhoodchain.blockscout.com"

# --- core tokens ---------------------------------------------------------
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"   # 6 decimals, ~$1 (Global Dollar)
WEALD = "0xf0d4453a74581b4fa074a062b684cf120f875722"  # a recurring quote asset
QVR = "0xf130c9630efa5fd5f660c90e71aaada344ff8d2b"    # Quiver, recurring quote
PONS = "0x39dbed3a2bd333467115de45665cc57f813c4571"
DEAD = "0x000000000000000000000000000000000000dead"

# --- Pons launchpad ------------------------------------------------------
# Live factory (ERC1967 proxy, launchEnabled == true).
PONS_FACTORY = "0xf4fc0cd27fc8ecf17e55ee4c3f7201897df3eb75"
# Original factory, now disabled (launchEnabled == false) but still holds
# the history for tokens launched in July, PONS-era included.
PONS_FACTORY_V1 = "0xa5aab3f0c6eeadf30ef1d3eb997108e976351feb"
PONS_FACTORIES = [PONS_FACTORY, PONS_FACTORY_V1]

# Every launchpad factory emits the SAME TokenLaunched event, so the indexer
# discovers them dynamically by topic. This maps the known ones to display
# names; anything new is labelled by its short address until named here.
LAUNCHPADS = {
    PONS_FACTORY: "Pons",
    PONS_FACTORY_V1: "Pons",
    "0x2ba793fd69bf251fd1af90b576be8b9fa6be46db": "Lemon",
    "0xa24d48d50fd7985c6de816eaf77c1a17d3593bbe": "LaunchFactory",
    "0xd9ec2db5f3d1b236843925949fe5bd8a3836fccb": "LaunchFactory",
}
# Factories queried for graduationStatus() when a token's own factory is unknown.
GRAD_FACTORIES = [PONS_FACTORY, "0x2ba793fd69bf251fd1af90b576be8b9fa6be46db",
                  "0xa24d48d50fd7985c6de816eaf77c1a17d3593bbe", PONS_FACTORY_V1]


def launchpad_name(factory: str) -> str:
    if not factory:
        return "?"
    f = factory.lower()
    if f in LAUNCHPADS:
        return LAUNCHPADS[f]
    if f in V3_FACTORIES:            # pool-discovered token: show the DEX venue
        return V3_FACTORIES[f]
    return f"{f[:6]}…{f[-4:]}"

# Earliest block worth scanning: V1 factory deployment.
GENESIS_BLOCK = 8_991_118

# --- venues --------------------------------------------------------------
UNIV3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
# Every DEX factory on the chain that emits Uniswap-V3-style PoolCreated.
# Indexing filters by the PoolCreated topic only (no address filter), so this
# list is informational / for naming.
V3_FACTORIES = {
    "0x1f7d7550b1b028f7571e69a784071f0205fd2efa": "Uniswap V3",
    "0xe51960f1b45f1c9fb6d166e6a884f866fc70433b": "Uniswap V3",
    "0xe0c4ceb92d08ca985bb70fe0a22feb121a9854a8": "Ramses",
    "0xece6ecd61177336ea6fb9b17937ac439d85ee20b": "CL",
    "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865": "Pancake V3",
    "0xd3504c3a32467e5e3c988aaf500dd689285c587e": "LaunchFactory",
}
# Quote/base assets: the non-tradeable side of a pool. A pool between two of
# these is a pure quote pair and is skipped.
BASE_TOKENS = {WETH, USDG, WEALD, QVR}
# USD value of one unit of each base (for cross-pair volume normalisation).
# WETH resolves live via the oracle; USDG is a dollar; others price transitively.
BASE_USD_STATIC = {USDG: 1.0}
UNIV4_POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
# From the live factory's DexConfig(0) / LaunchConfig(0).
POSITION_MANAGER = "0x73991a25c818bf1f1128deaab1492d45638de0d3"
SWAP_ROUTER = "0xcaf681a66d020601342297493863e78c959e5cb2"
LOCKER = "0x10f2756e373bab14999fdc9177587d51d30a1cf5"

# Launch parameters every Pons token is created with.
GRADUATION_THRESHOLD = 4.2        # WETH accumulated before graduation
POOL_FEE = 10000                  # 1%
MAX_WALLET_BPS = 500              # 5% of supply
MAX_TX_BPS = 550                  # 5.5% of supply
RESTRICTION_BLOCKS = 366          # ~37s of anti-snipe limits at 101ms blocks

# WETH/USDG pool used as the ETH price oracle (token0=WETH, token1=USDG).
ETH_USD_POOL = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"

# The two live PONS/WETH pools.
PONS_POOLS = [
    "0xed50bdeea8adc232f159486192a4157281d722ff",   # 0.30%
    "0x10cc6bd38112cac182db90b6a71d8bb5939526ba",   # 1.00%
]

# --- event topics --------------------------------------------------------
# TokenLaunched(address,address,address,address,address,
#               uint256,uint256,uint256,uint256,uint256)
TOPIC_TOKEN_LAUNCHED = "0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a"
# Uniswap V3 Swap(address,address,int256,int256,uint160,uint128,int24)
TOPIC_V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
# Transfer(address,address,uint256)
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# Uniswap V3 PoolCreated(address indexed t0, address indexed t1, uint24 indexed fee, int24 tickSpacing, address pool)
TOPIC_POOL_CREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

# --- function selectors --------------------------------------------------
SEL = {
    "symbol": "0x95d89b41",
    "name": "0x06fdde03",
    "decimals": "0x313ce567",
    "totalSupply": "0x18160ddd",
    "token0": "0x0dfe1681",
    "token1": "0xd21220a7",
    "slot0": "0x3850c7bd",
    "liquidity": "0x1a686502",
    "launchEnabled": "0x8e7ea5b2",
    "graduationStatus": "0x98d652f1",
    "ownerOf": "0x6352211e",
    "balanceOf": "0x70a08231",
}


# --- ABI decoding --------------------------------------------------------
def words(data: str) -> list[str]:
    d = data[2:] if data.startswith("0x") else data
    return [d[i * 64:(i + 1) * 64] for i in range(len(d) // 64)]


def d_addr(word: str) -> str:
    return "0x" + word[-40:].lower()


def d_uint(word: str | None) -> int:
    if not word or word == "0x":
        return 0
    return int(word, 16)


def d_int(word: str | None) -> int:
    v = d_uint(word)
    return v - (1 << 256) if v >= (1 << 255) else v


def d_string(hexdata: str | None) -> str | None:
    """Decode an ABI-encoded string; tolerate bytes32-style returns."""
    if not hexdata or hexdata == "0x":
        return None
    raw = bytes.fromhex(hexdata[2:])
    if len(raw) >= 64:
        try:
            length = int.from_bytes(raw[32:64], "big")
            if 0 < length <= 256 and len(raw) >= 64 + length:
                return raw[64:64 + length].decode("utf-8", "replace").strip()
        except Exception:
            pass
    # bytes32 fallback
    text = raw.rstrip(b"\x00").decode("utf-8", "replace").strip()
    return text or None


@dataclass
class Launch:
    token: str
    deployer: str
    dex_factory: str
    pair_token: str
    pool: str
    dex_id: int
    config_id: int
    position_id: int
    restrictions_end: int
    initial_buy: int
    block: int
    tx: str
    factory: str


def decode_launch(log: dict) -> Launch:
    t = log["topics"]
    w = words(log["data"])
    return Launch(
        token=d_addr(t[1]),
        deployer=d_addr(t[2]),
        dex_factory=d_addr(t[3]),
        pair_token=d_addr(w[0]),
        pool=d_addr(w[1]),
        dex_id=d_uint(w[2]),
        config_id=d_uint(w[3]),
        position_id=d_uint(w[4]),
        restrictions_end=d_uint(w[5]),
        initial_buy=d_uint(w[6]),
        block=int(log["blockNumber"], 16),
        tx=log["transactionHash"],
        factory=log["address"].lower(),
    )


@dataclass
class Swap:
    pool: str
    block: int
    log_index: int
    tx: str
    amount0: int
    amount1: int
    sqrt_price: int
    tick: int
    sender: str
    recipient: str


def decode_swap(log: dict) -> Swap:
    w = words(log["data"])
    t = log["topics"]
    return Swap(
        sender=d_addr(t[1]) if len(t) > 1 else "",
        recipient=d_addr(t[2]) if len(t) > 2 else "",
        pool=log["address"].lower(),
        block=int(log["blockNumber"], 16),
        log_index=int(log["logIndex"], 16),
        tx=log["transactionHash"],
        amount0=d_int(w[0]),
        amount1=d_int(w[1]),
        sqrt_price=d_uint(w[2]),
        tick=d_int(w[6]) if len(w) > 6 else 0,
    )


# --- pricing -------------------------------------------------------------
Q96 = 2 ** 96


def sqrt_to_price(sqrt_price: int, dec0: int, dec1: int) -> float:
    """token1 per token0, decimal-adjusted."""
    if not sqrt_price:
        return 0.0
    return (sqrt_price / Q96) ** 2 * (10 ** (dec0 - dec1))


def pool_order(token: str, pair: str) -> bool:
    """Uniswap sorts by address. Returns True if `pair` is token0."""
    return pair.lower() < token.lower()


STOCK_SUFFIX = "• Robinhood Token"
_extra_base: set[str] = set()   # runtime-grown: tokenized stocks used as quotes


def mark_base(addr: str) -> None:
    _extra_base.add(addr.lower())


def is_base(addr: str, name: str | None = None) -> bool:
    a = addr.lower()
    if a in BASE_TOKENS or a in _extra_base:
        return True
    if name and name.rstrip().endswith(STOCK_SUFFIX):
        _extra_base.add(a)
        return True
    return False


@dataclass
class PoolCreated:
    token0: str
    token1: str
    fee: int
    pool: str
    block: int
    tx: str
    dex_factory: str


def decode_pool_created(log: dict) -> PoolCreated:
    t = log["topics"]
    w = words(log["data"])
    return PoolCreated(
        token0=d_addr(t[1]),
        token1=d_addr(t[2]),
        fee=int(t[3], 16),
        pool=d_addr(w[1]) if len(w) > 1 else "",
        block=int(log["blockNumber"], 16),
        tx=log["transactionHash"],
        dex_factory=log["address"].lower(),
    )


def explorer_token(addr: str) -> str:
    return f"{EXPLORER}/token/{addr}"


def explorer_addr(addr: str) -> str:
    return f"{EXPLORER}/address/{addr}"


def explorer_tx(tx: str) -> str:
    return f"{EXPLORER}/tx/{tx}"
