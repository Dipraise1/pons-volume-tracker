"""Rich Telegram cards for token alerts."""
from __future__ import annotations
import time

from . import chain as C
from . import enrich, fmt, intel, signals, wallets
from .db import Db
from .indexer import Indexer
from .rpc import Rpc

DOT = {"hold": "🟢", "part": "🟡", "sold": "🔴"}


def _intensity(value: float, threshold: float, cap: int = 9) -> str:
    """More heat for bigger prints, like the reference channel's siren row."""
    if threshold <= 0:
        return "🚨"
    n = min(cap, max(1, int(value / threshold)))
    return "🚨" * n


def _pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:+,.0f}%" if abs(v) >= 10 else f"{v:+.1f}%"


def _age(launch_ts: int | None) -> str:
    if not launch_ts:
        return "?"
    d = max(0, int(time.time()) - int(launch_ts))
    if d < 3600:
        return f"{d//60}m"
    if d < 86400:
        return f"{d//3600}h{(d%3600)//60:02d}m"
    return f"{d//86400}d{(d%86400)//3600:02d}h"


def volume_card(rpc: Rpc, db: Db, idx: Indexer, token: str,
                window_s: int, burst: dict, threshold_eth: float,
                headline: str = "NEW ALERT") -> str:
    """The main alert card: a volume burst on one Pons token."""
    row = db.one("SELECT * FROM tokens WHERE address=?", (token.lower(),))
    if not row:
        return ""
    pool = row["pool"]
    dec = row["decimals"] or 18
    eth_usd = _safe(lambda: idx.eth_usd(), db, 0.0, "eth_usd")

    # --- price / marketcap ------------------------------------------
    last = db.one("SELECT price_weth FROM swaps WHERE pool=? AND price_weth>0 "
                  "ORDER BY ts DESC, log_index DESC LIMIT 1", (pool,))
    price_weth = last["price_weth"] if last else 0.0
    price_usd = price_weth * eth_usd
    supply = _safe(lambda: enrich.circulating(rpc, db, token), db, 0.0,
                   "supply") or (row["total_supply"] or 0)
    mcap = price_usd * supply

    win_lbl = _win_label(window_s)
    p_chg = enrich.price_change(db, pool, window_s)
    if p_chg is None:
        p_chg = _since_launch_change(db, pool)   # brand-new token
    v_1h = enrich.volume_change(db, pool, 3600)

    hour = db.one("SELECT COUNT(*) n, COALESCE(SUM(ABS(weth_amt)),0) v, "
                  "COALESCE(SUM(is_buy),0) b FROM swaps "
                  "WHERE pool=? AND ts >= ?", (pool, int(time.time()) - 3600))
    h_n = hour["n"] or 0
    h_b = hour["b"] or 0

    lines = [
        f"<b>{fmt.esc(row['name'] or row['symbol'])} "
        f"({fmt.esc(row['symbol'])})</b> {headline}!!!",
        _intensity(burst["weth"], threshold_eth),
        f"<b>Last {win_lbl} buy: {fmt.eth(burst['buy_weth'])} "
        f"in {burst['buys']} buys</b>",
        "",
        f"<code>{row['address']}</code>",
        f"USD:  {fmt.usd(price_usd)} ({_pct(p_chg)})",
        f"Dex:  Uniswap V3 · {C.launchpad_name(row['factory'])}",
        f"MC:   {fmt.usd(mcap)} | ⏳ {_age(row['launch_ts'])}",
        f"Vol:  {fmt.usd(hour['v'] * eth_usd)} | 1H: "
        f"{_pct(v_1h) if v_1h is not None else 'new'} "
        f"🅑 {h_b} 🅢 {h_n - h_b}",
    ]

    # --- bundlers -----------------------------------------------------
    bnd = _safe(lambda: enrich.bundlers(rpc, db, token), db, {}, "bundlers")
    if bnd and bnd.get("count"):
        lines.append(
            f"Bundle: {bnd['count']} wallets · {bnd['supply_pct']:.1f}% of supply")
        lines.append(
            f" └ now hold {bnd['held_pct']:.1f}% · "
            f"{bnd['holding']} holding / {bnd['sold_out']} sold")

    # --- graduation ---------------------------------------------------
    grad = _safe(lambda: intel.graduation(rpc, token), db, {}, "graduation")
    if grad:
        if grad["graduated"]:
            lines.append("Grad: ✅ GRADUATED")
        else:
            lines.append(
                f"Grad: {intel.progress_bar(grad['pct'])} {grad['pct']:.1f}%")
            lines.append(f" └ {grad['accumulated']:.3f}/{grad['threshold']:.1f} Ξ"
                         f" · {grad['remaining']:.3f} Ξ to go")

    # --- holders ------------------------------------------------------
    snap = _safe(lambda: enrich.holder_snapshot(rpc, db, token), db, {}, "holders")
    if snap and snap.get("top"):
        lines.append(f"TH:   {snap['holders']:,} (total) | "
                     f"Top 10: {snap['top10_pct']:.1f}%")
        tops = "|".join(f"{t['pct']:.1f}" for t in snap["top"])
        lines.append(f" └ {tops}")

    # --- safety ---------------------------------------------------------
    sf = _safe(lambda: intel.safety(rpc, db, token), db, {}, "safety")
    if sf:
        icon = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}[sf["grade"]]
        lines.append(f"Risk: {icon} {sf['grade']} ({sf['score']}/100)")
        for reason in sf["reasons"][:4]:
            lines.append(f" ├ {reason}")

    # --- smart money ------------------------------------------------------
    smart = _safe(lambda: wallets.smart_buyers(db, pool, window_s), db, [],
                  "smart")
    if smart:
        total = sum(w["realized"] for w in smart)
        lines.append(f"🧠 Smart money: {len(smart)} wallet(s) bought")
        lines.append(f" └ combined realised PnL {fmt.eth(total)}")

    # --- deployer track record --------------------------------------------
    hist = _safe(lambda: intel.deployer_history(rpc, db, row["deployer"]),
                 db, {}, "deployer")
    if hist and hist.get("launches", 0) > 1:
        flag = "🔴" if hist["dead_pct"] >= 60 else "⚠️" if hist["dead_pct"] >= 30 else "✅"
        lines.append(f"Dev history: {flag} {hist['launches']} launches, "
                     f"{hist['dead']} dead ({hist['dead_pct']:.0f}%)")

    # --- early activity -------------------------------------------------
    early = _safe(lambda: enrich.early_activity(db, token), db, {}, "early")
    if early and early.get("buys"):
        lines.append("Early:")
        lines.append(f" ├ Snipers: {early['snipers']}")
        lines.append(f" ├ Same-block buys: {early['same_block']}")
        lines.append(f" └ Early inflow: {fmt.eth(early['early_eth'])}")

    # --- holder outcome grid ---------------------------------------------
    outcomes = _safe(lambda: enrich.trader_outcomes(db, pool), db, [], "outcomes")
    if outcomes:
        lines.append(_grid(outcomes))
        hold = outcomes.count("hold")
        part = outcomes.count("part")
        sold = outcomes.count("sold")
        lines.append(f" └ Hold {hold} | Sold part {part} | Sold {sold}")

    lines.append("")
    lines.append(" · ".join([
        fmt.link("token", C.explorer_token(row["address"])),
        fmt.link("pool", C.explorer_addr(pool)),
        fmt.link("deployer", C.explorer_addr(row["deployer"] or "")),
    ]))
    return "\n".join(lines)


def launch_card(rpc: Rpc, db: Db, idx: Indexer, launch) -> str:
    row = db.one("SELECT * FROM tokens WHERE address=?", (launch.token,))
    if not row:
        return ""
    eth_usd = _safe(lambda: idx.eth_usd(), db, 0.0, "eth_usd")
    buy = launch.initial_buy / 1e18
    grade = _safe(lambda: signals.launch_grade(rpc, db, launch.token), db, {}, "grade")
    tag = grade.get("tag", "") if grade else ""
    return "\n".join([
        f"🚀 <b>{fmt.esc(row['name'] or row['symbol'])} "
        f"({fmt.esc(row['symbol'])})</b> LAUNCHED on "
        f"{C.launchpad_name(launch.factory)}"
        + (f"  {tag}" if tag else ""),
        "",
        f"<code>{row['address']}</code>",
        f"Supply:  {fmt.num(row['total_supply'] or 0)}",
        f"Dev buy: {fmt.eth(buy)} ({fmt.usd(buy * eth_usd)})",
        f"Dex:     Uniswap V3 · {C.launchpad_name(row['factory'])}",
        f"Block:   {launch.block:,}",
        f"Dev:     {fmt.link(fmt.short(launch.deployer), C.explorer_addr(launch.deployer))}",
        "",
        " · ".join([
            fmt.link("token", C.explorer_token(row["address"])),
            fmt.link("pool", C.explorer_addr(launch.pool)),
            fmt.link("tx", C.explorer_tx(launch.tx)),
        ]),
    ])


def quote_card(rpc: Rpc, db: Db, idx: Indexer, token: str, call_row: dict,
               window_s: int) -> str:
    """Follow-up 'quote' on a coin we've already called: current volume and how
    it's performed since we flagged it."""
    row = db.one("SELECT * FROM tokens WHERE address=?", (token.lower(),))
    if not row:
        return ""
    pool = row["pool"]
    eth_usd = _safe(lambda: idx.eth_usd(), db, 0.0, "eth_usd")
    now = int(time.time())

    win = db.one("SELECT COUNT(*) n, COALESCE(SUM(ABS(weth_amt)),0) v, "
                 "COALESCE(SUM(is_buy),0) b FROM swaps WHERE pool=? AND ts>=?",
                 (pool, now - window_s))
    last = db.one("SELECT price_weth FROM swaps WHERE pool=? AND price_weth>0 "
                  "ORDER BY ts DESC, log_index DESC LIMIT 1", (pool,))
    price_usd = (last["price_weth"] if last else 0) * eth_usd
    supply = row["total_supply"] or 0
    mcap = price_usd * supply

    # performance since the call
    entry_mcap = call_row.get("mcap_usd") or 0
    entry_price = call_row.get("price_usd") or 0
    mult = (price_usd / entry_price) if entry_price else 0
    since = _age_secs(now - (call_row.get("ts") or now))
    arrow = "🟢" if mult >= 1 else "🔴"

    n = win["n"] or 0
    b = win["b"] or 0
    lines = [
        f"📈 <b>{fmt.esc(row['name'] or row['symbol'])} "
        f"({fmt.esc(row['symbol'])})</b> — WE CALLED THIS",
        f"<code>{row['address']}</code>",
        "",
        f"Vol ({_win_label(window_s)}):  <b>{fmt.eth(win['v'])}</b> "
        f"({fmt.usd(win['v']*eth_usd)})",
        f"Swaps:  {n}  ·  {b}B / {n-b}S",
        f"Price:  {fmt.usd(price_usd)}  ·  MC {fmt.usd(mcap)}",
    ]
    if entry_price and mult:
        lines.append(f"Since call ({since}): {arrow} <b>{mult:.2f}x</b> "
                     f"(from {fmt.usd(entry_mcap)} mcap)")
    grad = _safe(lambda: intel.graduation(rpc, token), db, {}, "graduation")
    if grad and not grad.get("graduated"):
        lines.append(f"Grad:  {intel.progress_bar(grad['pct'])} {grad['pct']:.0f}%")
    elif grad and grad.get("graduated"):
        lines.append("Grad:  ✅ GRADUATED")
    lines.append("")
    lines.append(" · ".join([
        fmt.link("token", C.explorer_token(row["address"])),
        fmt.link("pool", C.explorer_addr(pool)),
    ]))
    return "\n".join(lines)


def _age_secs(d: int) -> str:
    if d < 3600:
        return f"{d//60}m"
    if d < 86400:
        return f"{d//3600}h"
    return f"{d//86400}d"


def whale_card(db: Db, idx: Indexer, s: dict) -> str:
    eth_usd = _safe(lambda: idx.eth_usd(), db, 0.0, "eth_usd")
    size = abs(s["weth"])
    side = "BUY" if s["is_buy"] else "SELL"
    icon = "🟢" if s["is_buy"] else "🔴"
    row = db.one("SELECT name,address,total_supply FROM tokens WHERE pool=?",
                 (s["pool"],))
    name = row["name"] if row else s["symbol"]
    lines = [
        f"🐋 <b>{fmt.esc(name)} ({fmt.esc(s['symbol'])})</b> — {icon} WHALE {side}",
        _intensity(size, max(0.1, size / 3)),
        f"<b>{fmt.eth(size)}</b> ({fmt.usd(size * eth_usd)})",
        f"{fmt.num(abs(s['token_amt']))} {fmt.esc(s['symbol'])}",
    ]
    if s.get("price"):
        lines.append(f"Price: {fmt.usd(s['price'] * eth_usd)}")
    if s.get("trader"):
        lines.append(f"Trader: {fmt.link(fmt.short(s['trader']), C.explorer_addr(s['trader']))}")
    lines.append("")
    lines.append(fmt.link("tx", C.explorer_tx(s["tx"])))
    return "\n".join(lines)


# --- helpers -------------------------------------------------------------
def _grid(outcomes: list[str], per_row: int = 10) -> str:
    dots = [DOT[o] for o in outcomes]
    rows = [" ├ " + "".join(dots[i:i + per_row])
            for i in range(0, len(dots), per_row)]
    return "\n".join(rows)


def _since_launch_change(db: Db, pool: str):
    first = db.one("SELECT price_weth FROM swaps WHERE pool=? AND price_weth>0 "
                   "ORDER BY ts ASC, log_index ASC LIMIT 1", (pool,))
    last = db.one("SELECT price_weth FROM swaps WHERE pool=? AND price_weth>0 "
                  "ORDER BY ts DESC, log_index DESC LIMIT 1", (pool,))
    if not first or not last or not first["price_weth"]:
        return None
    return (last["price_weth"] - first["price_weth"]) / first["price_weth"] * 100


def _win_label(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds//60} mins"
    if seconds < 86400:
        return f"{seconds//3600}h"
    return f"{seconds//86400}d"


def _safe(fn, db: Db, default=0.0, label: str = ""):
    """Enrichment is best-effort: a card must still render if one lookup
    fails. Failures are logged rather than silently swallowed."""
    try:
        out = fn()
        return out if out is not None else default
    except Exception as exc:
        print(f"[cards] {label or 'lookup'} failed: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return default


def copy_card(db: Db, idx: Indexer, buy: dict) -> str:
    eth_usd = _safe(lambda: idx.eth_usd(), db, 0.0, "eth_usd")
    size = abs(buy["weth"])
    label = buy.get("label") or fmt.short(buy["trader"])
    return "\n".join([
        f"🎯 <b>FOLLOWED WALLET BUY</b> · {fmt.esc(buy['symbol'])}",
        f"{fmt.link(fmt.short(buy['trader']), C.explorer_addr(buy['trader']))}"
        f"  <i>{fmt.esc(label)}</i>",
        f"Bought <b>{fmt.eth(size)}</b> ({fmt.usd(size*eth_usd)})",
        f"{fmt.num(abs(buy['token_amt']))} {fmt.esc(buy['symbol'])}",
        "",
        f"{fmt.link('token', C.explorer_token(buy['token']))} · "
        f"{fmt.link('tx', C.explorer_tx(buy['tx']))}",
    ])


def momentum_card(rpc: Rpc, db: Db, idx: Indexer, m: dict) -> str:
    eth_usd = _safe(lambda: idx.eth_usd(), db, 0.0, "eth_usd")
    accel = m["accel"]
    accel_s = "∞" if accel == float("inf") else f"{accel:.1f}x"
    burst = {"weth": m["cur"]["vol"], "buy_weth": m["cur"]["vol"],
             "buys": m["cur"]["buys"], "swaps": m["cur"]["n"]}
    return volume_card(rpc, db, idx, m["address"], m["window"], burst,
                       0.0, headline=f"⚡ MOMENTUM {accel_s} accel")


def log_card(db: Db, idx: Indexer, row: dict, win: dict, window_s: int) -> str:
    """Compact log line for ANY token that traded in the window. Cheap: uses
    only the last indexed price + cached eth_usd, no holder/safety RPC."""
    eth_usd = _safe(lambda: idx.eth_usd(), db, 0.0, "eth_usd")
    pool = row["pool"]
    last = db.one("SELECT price_weth FROM swaps WHERE pool=? AND price_weth>0 "
                  "ORDER BY ts DESC, log_index DESC LIMIT 1", (pool,))
    price = (last["price_weth"] if last else 0) * eth_usd
    mcap = price * (row["total_supply"] or 0)
    n = win["n"]; b = win["b"]
    pad = C.launchpad_name(row.get("factory"))
    return "\n".join([
        f"💸 <b>{fmt.esc(row['symbol'] or '???')}</b> "
        f"<i>{fmt.esc(row['name'] or '')}</i> · {fmt.esc(pad)}",
        f"Vol ({_win_label(window_s)}): <b>{fmt.eth(win['v'])}</b> "
        f"({fmt.usd(win['v']*eth_usd)}) · {b}B/{n-b}S",
        f"Price {fmt.usd(price)} · MC {fmt.usd(mcap)}",
        f"<code>{row['address']}</code>",
        f"{fmt.link('chart', C.explorer_token(row['address']))} · "
        f"{fmt.link('pool', C.explorer_addr(pool))}",
    ])
