"""Robinhood stock tokens: authenticity, true balances, corporate actions.

Tokenised equities on this chain do not behave like the launchpad tokens the
rest of this bot indexes, and the difference is invisible to ordinary ERC-20
tooling:

  * Value moves through a multiplier, not the balance. A dividend or split
    calls updateMultiplier(); `balanceOf` never changes. Every wallet and
    explorer that reads `balanceOf` alone therefore under-reports holdings,
    and the error compounds with each corporate action.
  * Authenticity cannot be judged by name. Counterfeits copy the symbol, the
    company name, and the "• Robinhood Token" suffix exactly. The only thing
    they cannot forge is ACCESS_CONTROLLED_REGISTRY(), which every official
    token answers with the same registry address.
  * The tokens are administrable. A single registry can blocklist a wallet or
    pause every stock token at once, and the beacon behind them can be
    upgraded under all of them simultaneously.

Corporate actions are announced on-chain only minutes before they take effect
(the first observed AAPL update led its own effectiveAt by 9m40s), which is
why this lives in the alert path rather than in a daily digest.
"""
from __future__ import annotations
import json
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import chain as C
from .db import Db
from .rpc import Rpc

BLOCKSCOUT = "https://robinhoodchain.blockscout.com/api/v2"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")

VERIFY_TTL = 300          # a token's registry answer never changes; state does
_cache: dict[str, tuple[float, "Stock | None"]] = {}
_lock = threading.Lock()


@dataclass
class Stock:
    """Live state of one official stock token."""
    address: str
    symbol: str | None = None
    name: str | None = None
    multiplier: float = 1.0
    new_multiplier: float | None = None
    effective_at: int = 0
    token_paused: bool = False
    oracle_paused: bool = False
    supply_raw: float = 0.0
    supply_true: float = 0.0

    @property
    def drift_pct(self) -> float:
        """How far raw balances have fallen behind true holdings, in percent."""
        return (self.multiplier - 1.0) * 100

    @property
    def pending(self) -> dict | None:
        """A corporate action announced but not yet effective."""
        if not self.new_multiplier or not self.effective_at:
            return None
        if abs(self.new_multiplier - self.multiplier) < 1e-18:
            return None
        return {
            "new_multiplier": self.new_multiplier,
            "change_pct": (self.new_multiplier / self.multiplier - 1) * 100
                          if self.multiplier else 0.0,
            "effective_at": self.effective_at,
            "seconds_out": self.effective_at - int(time.time()),
        }

    @property
    def frozen(self) -> bool:
        return self.token_paused or self.oracle_paused


def _call(token: str, selector: str, arg: str | None = None) -> tuple[str, list]:
    data = selector + ("0" * 24 + arg[2:].lower() if arg else "")
    return ("eth_call", [{"to": token, "data": data}, "latest"])


def _u(raw) -> int:
    return C.d_uint(raw) if raw and raw != "0x" else 0


def verify(rpc: Rpc, token: str, use_cache: bool = True) -> Stock | None:
    """Return live state if `token` is an official stock token, else None.

    One batched round trip. The registry answer is the authenticity check;
    everything after it is state that ordinary ERC-20 reads cannot see.
    """
    token = token.lower()
    if use_cache:
        with _lock:
            hit = _cache.get(token)
        if hit and time.time() - hit[0] < VERIFY_TTL:
            return hit[1]

    fields = ["stockRegistry", "symbol", "name", "uiMultiplier",
              "newUIMultiplier", "effectiveAt", "tokenPaused", "oraclePaused",
              "totalSupply", "totalSupplyUI"]
    try:
        res = rpc.batch([_call(token, C.SEL[f]) for f in fields])
    except Exception:
        return None
    got = dict(zip(fields, res))

    reg = got["stockRegistry"]
    registry = ("0x" + reg[-40:].lower()) if reg and reg != "0x" else None
    if registry != C.STOCK_REGISTRY:
        # Not registry-backed: make sure it is never treated as a quote asset
        # on the strength of its name alone.
        C.deny_base(token)
        with _lock:
            _cache[token] = (time.time(), None)
        return None

    mult = _u(got["uiMultiplier"]) or C.STOCK_ONE
    new = _u(got["newUIMultiplier"])
    info = Stock(
        address=token,
        symbol=C.d_string(got["symbol"]),
        name=C.d_string(got["name"]),
        multiplier=mult / C.STOCK_ONE,
        new_multiplier=(new / C.STOCK_ONE) if new else None,
        effective_at=_u(got["effectiveAt"]),
        token_paused=bool(_u(got["tokenPaused"])),
        oracle_paused=bool(_u(got["oraclePaused"])),
        supply_raw=_u(got["totalSupply"]) / 1e18,
        supply_true=_u(got["totalSupplyUI"]) / 1e18,
    )
    C.mark_base(token)
    with _lock:
        _cache[token] = (time.time(), info)
    return info


def is_official(rpc: Rpc, token: str) -> bool:
    return verify(rpc, token) is not None


def holdings(rpc: Rpc, token: str, wallet: str) -> dict | None:
    """Raw vs true balance for one wallet.

    `understated` is what every balanceOf-based tracker is currently missing.
    """
    try:
        raw, ui = rpc.batch([_call(token, C.SEL["balanceOf"], wallet),
                             _call(token, C.SEL["balanceOfUI"], wallet)])
    except Exception:
        return None
    if raw is None or ui is None:
        return None
    r, u = _u(raw) / 1e18, _u(ui) / 1e18
    return {"raw": r, "true": u, "understated": u - r}


def wallet_blocked(rpc: Rpc, wallet: str) -> bool | None:
    """Whether the registry has blocklisted this wallet from stock tokens."""
    try:
        res = rpc.call("eth_call", [{
            "to": C.STOCK_REGISTRY,
            "data": C.SEL["isBlocked"] + "0" * 24 + wallet.lower()[2:],
        }, "latest"])
    except Exception:
        return None
    return bool(_u(res)) if res and res != "0x" else None


def registry_paused(rpc: Rpc) -> bool | None:
    """Chain-wide freeze covering every stock token at once."""
    try:
        res = rpc.call("eth_call", [{"to": C.STOCK_REGISTRY,
                                     "data": C.SEL["paused"]}, "latest"])
    except Exception:
        return None
    return bool(_u(res)) if res and res != "0x" else None


# --- known-token registry -------------------------------------------------
def known(db: Db) -> list[dict]:
    return [dict(r) for r in db.q(
        "SELECT * FROM stock_tokens ORDER BY symbol")]


def remember(db: Db, info: Stock, now: int | None = None) -> None:
    db.run(
        "INSERT INTO stock_tokens(address,symbol,name,multiplier,updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(address) DO UPDATE SET "
        "symbol=excluded.symbol, name=excluded.name, "
        "multiplier=excluded.multiplier, updated_at=excluded.updated_at",
        (info.address, info.symbol, info.name, info.multiplier,
         now or int(time.time())))


def by_query(db: Db, query: str) -> str | None:
    """Resolve a ticker or address to an official stock token address."""
    q = query.strip().lower()
    if q.startswith("0x") and len(q) == 42:
        return q
    row = db.one("SELECT address FROM stock_tokens WHERE lower(symbol)=?", (q,))
    if row:
        return row["address"]
    row = db.one("SELECT address FROM stock_tokens WHERE lower(symbol) LIKE ? "
                 "OR lower(name) LIKE ? ORDER BY length(symbol) LIMIT 1",
                 (f"{q}%", f"%{q}%"))
    return row["address"] if row else None


def discover(rpc: Rpc, db: Db, limit_pages: int = 6) -> dict:
    """Find official stock tokens and cache them.

    Blockscout only supplies *candidates* — its index happily lists
    counterfeits under the same name. Every candidate is confirmed against the
    registry before it is stored, so the cached table contains official tokens
    only.
    """
    seen: dict[str, dict] = {}
    for term in ("Robinhood Token", "Robinhood"):
        url = (f"{BLOCKSCOUT}/tokens?type=ERC-20&q="
               + urllib.parse.quote(term))
        data = None
        # The explorer throttles under load and answers with an error rather
        # than an empty list; a single failed attempt would silently yield an
        # empty stock-token table, so back off and retry before giving up.
        for attempt in range(4):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": UA,
                                  "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    data = json.load(r)
                break
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        if not data:
            continue
        for t in data.get("items", []):
            addr = (t.get("address_hash") or t.get("address") or "").lower()
            if addr:
                seen[addr] = t

    now = int(time.time())
    official, rejected = 0, 0
    for addr in seen:
        info = verify(rpc, addr, use_cache=False)
        if info:
            remember(db, info, now)
            official += 1
        else:
            rejected += 1
    if official:
        db.set_meta("stock_discovered_at", now)
    return {"candidates": len(seen), "official": official,
            "counterfeit": rejected}


# --- event watching -------------------------------------------------------
@dataclass
class StockEvent:
    kind: str                      # multiplier | blocked | unblocked
                                   # | paused | unpaused | upgraded
    block: int
    tx: str
    token: str | None = None
    old: float = 0.0
    new: float = 0.0
    effective_at: int = 0
    subject: str | None = None     # blocklisted wallet / new implementation
    extra: dict = field(default_factory=dict)


# A multiplier scan is topic-only (no address filter) so that a stock token
# this bot has never seen is still caught the first time it acts. That is
# cheap over a live tick and expensive over a cold-start backfill, so wide
# ranges are trimmed to the recent end: corporate actions are rare, and
# current state is always available from verify() regardless.
MAX_SCAN_BLOCKS = 50_000


def scan(rpc: Rpc, from_block: int, to_block: int) -> list[StockEvent]:
    """Corporate actions and registry controls in a block range."""
    if from_block > to_block:
        return []
    out: list[StockEvent] = []
    start = max(from_block, to_block - MAX_SCAN_BLOCKS + 1)

    try:
        logs = rpc.get_logs(start, to_block, [[C.TOPIC_UI_MULTIPLIER]])
    except Exception:
        logs = []
    for lg in logs:
        w = C.words(lg.get("data") or "")
        if len(w) < 3:
            continue
        out.append(StockEvent(
            kind="multiplier",
            block=int(lg["blockNumber"], 16),
            tx=lg["transactionHash"],
            token=lg["address"].lower(),
            old=C.d_uint(w[0]) / C.STOCK_ONE,
            new=C.d_uint(w[1]) / C.STOCK_ONE,
            effective_at=C.d_uint(w[2]),
        ))

    kinds = {C.TOPIC_BLOCKED: "blocked", C.TOPIC_UNBLOCKED: "unblocked",
             C.TOPIC_PAUSED: "paused", C.TOPIC_UNPAUSED: "unpaused",
             C.TOPIC_UPGRADED: "upgraded"}
    try:
        logs = rpc.get_logs(from_block, to_block, [list(kinds)],
                            [C.STOCK_REGISTRY])   # address-filtered: cheap
    except Exception:
        logs = []
    for lg in logs:
        topics = lg.get("topics") or []
        kind = kinds.get(topics[0]) if topics else None
        if not kind:
            continue
        out.append(StockEvent(
            kind=kind,
            block=int(lg["blockNumber"], 16),
            tx=lg["transactionHash"],
            subject=C.d_addr(topics[1]) if len(topics) > 1 else None,
        ))
    out.sort(key=lambda e: e.block)
    return out
