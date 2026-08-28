"""Alert triggers. Volume bursts are the primary signal, as in the
pump.fun-style volume channels: a short window of concentrated buying."""
from __future__ import annotations
import time

from . import cards, config as cfg, fmt, intel, signals, stats, wallets
from . import chain as C
from .db import Db
from .indexer import Indexer
from .rpc import Rpc

FOREVER = 10 ** 9   # whale alerts key off tx hash, so they never repeat


def _eth_usd(idx: Indexer, db: Db) -> float:
    try:
        return idx.eth_usd()
    except Exception:
        row = db.one("SELECT eth_usd FROM price_history ORDER BY ts DESC LIMIT 1")
        return row["eth_usd"] if row else 0.0


def launch_messages(rpc, db, idx, launches) -> list[str]:
    return [cards.launch_card(rpc, db, idx, l) for l in launches] if launches else []


def burst_messages(rpc: Rpc, db: Db, idx: Indexer) -> list[str]:
    """Per-token buy-volume bursts over a short window."""
    now = int(time.time())
    since = now - cfg.VOL_WINDOW
    rows = db.q(
        "SELECT s.pool, t.address, "
        "  COALESCE(SUM(ABS(s.weth_amt)),0) vol, "
        "  COALESCE(SUM(CASE WHEN s.is_buy=1 THEN ABS(s.weth_amt) END),0) buy_vol, "
        "  COALESCE(SUM(s.is_buy),0) buys, COUNT(*) n "
        "FROM swaps s JOIN tokens t ON t.pool = s.pool "
        "WHERE s.ts >= ? AND t.address != ? "   # PONS itself excluded
        "GROUP BY s.pool HAVING buy_vol >= ? "
        "ORDER BY buy_vol DESC LIMIT ?",
        (since, C.PONS, cfg.VOL_MIN_ETH, cfg.MAX_ALERTS_PER_CYCLE))

    out = []
    for r in rows:
        token = r["address"]
        if not db.alert_ready("burst", token, now, cfg.ALERT_COOLDOWN):
            continue
        db.mark_alert("burst", token, now)
        burst = {"weth": r["vol"], "buy_weth": r["buy_vol"],
                 "buys": r["buys"], "swaps": r["n"]}
        card = cards.volume_card(rpc, db, idx, token, cfg.VOL_WINDOW,
                                 burst, cfg.VOL_MIN_ETH)
        if card:
            out.append(card)
            _record(db, idx, token, "burst")
    return out


def _record(db: Db, idx: Indexer, token: str, kind: str) -> None:
    """Log what the token was worth when we called it, so /calls can score."""
    try:
        row = db.one("SELECT pool,total_supply FROM tokens WHERE address=?",
                     (token,))
        last = db.one("SELECT price_weth FROM swaps WHERE pool=? AND price_weth>0 "
                      "ORDER BY ts DESC LIMIT 1", (row["pool"],))
        eth = _eth_usd(idx, db)
        price = (last["price_weth"] if last else 0) * eth
        wallets.record_call(db, token, kind, price,
                            price * (row["total_supply"] or 0))
    except Exception:
        pass


GRAD_STEPS = (50, 75, 90, 100)


def graduation_messages(rpc: Rpc, db: Db, idx: Indexer) -> list[str]:
    """Fire the FULL alert card as a token crosses graduation milestones.

    Graduation is the metric that decides whether a launch completes, so these
    go out to every subscriber with the same volume/holders/safety detail as a
    volume-burst alert, headlined by the milestone reached.
    """
    now = int(time.time())
    since = now - 3600
    rows = db.q(
        "SELECT t.address, t.symbol, t.name, t.pool, "
        "  COALESCE(SUM(ABS(s.weth_amt)),0) vol, "
        "  COALESCE(SUM(CASE WHEN s.is_buy=1 THEN ABS(s.weth_amt) END),0) buy_vol, "
        "  COALESCE(SUM(s.is_buy),0) buys, COUNT(s.tx) n "
        "FROM tokens t LEFT JOIN swaps s "
        "  ON s.pool = t.pool AND s.ts >= ? "
        "WHERE t.address != ? GROUP BY t.address "
        "HAVING n > 0", (since, C.PONS))
    out = []
    for r in rows:
        g = intel.graduation(rpc, r["address"])
        if not g:
            continue
        pct = 100.0 if g["graduated"] else g["pct"]
        step = max((x for x in GRAD_STEPS if pct >= x), default=None)
        if step is None:
            continue
        key = f"{r['address']}:{step}"
        if not db.alert_ready("grad", key, now, FOREVER):
            continue
        db.mark_alert("grad", key, now)
        head = ("🎓 GRADUATED" if (g["graduated"] or step == 100)
                else f"📊 {step}% TO GRADUATION")
        burst = {"weth": r["vol"], "buy_weth": r["buy_vol"],
                 "buys": r["buys"], "swaps": r["n"]}
        card = cards.volume_card(rpc, db, idx, r["address"], 3600, burst,
                                 cfg.VOL_MIN_ETH, headline=head)
        if card:
            out.append(card)
            _record(db, idx, r["address"], "graduation")
        if len(out) >= cfg.MAX_ALERTS_PER_CYCLE:
            break
    return out


def whale_messages(rpc, db: Db, idx: Indexer, swaps: list) -> list[str]:
    if not swaps:
        return []
    now = int(time.time())
    out = []
    for s in sorted(swaps, key=lambda x: abs(x["weth"]), reverse=True):
        if s.get("token", "").lower() == C.PONS:   # skip PONS's own swaps
            continue
        if abs(s["weth"]) < cfg.WHALE_ETH:
            break
        if not db.alert_ready("whale", s["tx"], now, FOREVER):
            continue
        db.mark_alert("whale", s["tx"], now)
        out.append(cards.whale_card(db, idx, s))
        if len(out) >= cfg.MAX_ALERTS_PER_CYCLE:
            break
    return out


def spike_message(db: Db, idx: Indexer) -> str | None:
    """Chain-level PONS volume spike vs its own trailing baseline."""
    now = int(time.time())
    if not db.alert_ready("spike", "pons", now, cfg.ALERT_COOLDOWN):
        return None
    recent = stats.pons_volume(db, 900)
    if recent["weth"] < cfg.SPIKE_MIN_ETH:
        return None
    trailing = stats.pons_volume(db, 4 * 3600)
    baseline = (trailing["weth"] - recent["weth"]) / 15
    if baseline <= 0:
        return None
    mult = recent["weth"] / baseline
    if mult < cfg.SPIKE_MULT:
        return None
    db.mark_alert("spike", "pons", now)
    eth = _eth_usd(idx, db)
    ratio = recent["buys"] / recent["swaps"] * 100 if recent["swaps"] else 0
    return "\n".join([
        "📈 <b>$PONS volume spike</b>",
        f"15m volume {fmt.eth(recent['weth'])} ({fmt.usd(recent['weth']*eth)})",
        f"<b>{mult:.1f}×</b> the trailing 4h baseline",
        f"{recent['swaps']:,} swaps · {ratio:.0f}% buys",
        fmt.bar(recent["buys"], recent["sells"]),
    ])


def price_message(db: Db, idx: Indexer) -> str | None:
    now = int(time.time())
    if not db.alert_ready("price", "pons", now, cfg.ALERT_COOLDOWN):
        return None
    ch = stats.price_change(db, 900)
    if not ch:
        return None
    old, new = ch
    pct = (new - old) / old * 100
    if abs(pct) < cfg.PRICE_MOVE_PCT:
        return None
    db.mark_alert("price", "pons", now)
    try:
        circ = idx.circulating()
    except Exception:
        circ = 0
    arrow = "🟢" if pct > 0 else "🔴"
    lines = [f"{arrow} <b>$PONS {pct:+.1f}% / 15m</b>",
             f"{fmt.usd(old)} → <b>{fmt.usd(new)}</b>"]
    if circ:
        lines.append(f"Market cap {fmt.usd(new*circ)}")
    return "\n".join(lines)


def quote_messages(rpc: Rpc, db: Db, idx: Indexer) -> list[str]:
    """Follow-up quotes on coins we already called that still have volume.

    'We bought / called it' == it is in the calls table. When such a coin sees
    fresh volume in the window, re-quote it with its current volume and the
    multiple since our call. Cooldown keeps it to at most one quote per period.
    """
    now = int(time.time())
    win = cfg.QUOTE_WINDOW
    rows = db.q(
        "SELECT c.token, c.ts, c.kind, c.price_usd, c.mcap_usd, "
        "  COALESCE(SUM(ABS(s.weth_amt)),0) vol, COUNT(s.tx) n "
        "FROM calls c JOIN tokens t ON t.address = c.token "
        "LEFT JOIN swaps s ON s.pool = t.pool AND s.ts >= ? "
        "GROUP BY c.token HAVING vol >= ? "
        "ORDER BY vol DESC LIMIT ?",
        (now - win, cfg.QUOTE_MIN_ETH, cfg.MAX_ALERTS_PER_CYCLE))
    out = []
    for r in rows:
        if not db.alert_ready("quote", r["token"], now, cfg.QUOTE_COOLDOWN):
            continue
        db.mark_alert("quote", r["token"], now)
        call_row = {"ts": r["ts"], "price_usd": r["price_usd"],
                    "mcap_usd": r["mcap_usd"]}
        card = cards.quote_card(rpc, db, idx, r["token"], call_row, win)
        if card:
            out.append(card)
    return out


def surge_messages(rpc: Rpc, db: Db, idx: Indexer) -> list[str]:
    """Price-surge alerts for ANY tracked token whose price jumped over the
    window. Fires the full card so the surge comes with volume/holders/safety."""
    now = int(time.time())
    surges = stats.token_surges(db, cfg.SURGE_WINDOW)
    surges = [x for x in surges
              if x["pct"] >= cfg.SURGE_PCT
              and x["vol"] >= cfg.SURGE_MIN_ETH
              and x["address"].lower() != C.PONS]     # PONS excluded per request
    surges.sort(key=lambda x: x["pct"], reverse=True)
    out = []
    for x in surges:
        # dedupe per token per rounded surge bucket, so a coin re-alerts only
        # when it makes a NEW leg up, not every cycle it stays elevated
        bucket = int(x["pct"] // 15) * 15   # re-alert every +15% leg
        key = f"{x['address']}:{bucket}"
        if not db.alert_ready("surge", key, now, cfg.SURGE_COOLDOWN):
            continue
        db.mark_alert("surge", key, now)
        burst = {"weth": x["vol"], "buy_weth": x["vol"],
                 "buys": x["buys"], "swaps": x["swaps"]}
        card = cards.volume_card(rpc, db, idx, x["address"], cfg.SURGE_WINDOW,
                                 burst, cfg.SURGE_MIN_ETH,
                                 headline=f"🚀 +{x['pct']:.0f}% SURGE")
        if card:
            out.append(card)
            _record(db, idx, x["address"], "surge")
        if len(out) >= cfg.MAX_ALERTS_PER_CYCLE:
            break
    return out


def momentum_messages(rpc: Rpc, db: Db, idx: Indexer) -> list[str]:
    """Early-pump alerts: volume accelerating hard even before it's huge."""
    now = int(time.time())
    hot = signals.acceleration_alerts(db, cfg.ACCEL_MULT, cfg.ACCEL_MIN_ETH,
                                      cfg.SURGE_WINDOW)
    out = []
    for m in hot:
        if not db.alert_ready("momentum", m["address"], now, cfg.ALERT_COOLDOWN):
            continue
        db.mark_alert("momentum", m["address"], now)
        card = cards.momentum_card(rpc, db, idx, m)
        if card:
            out.append(card)
            _record(db, idx, m["address"], "momentum")
        if len(out) >= cfg.MAX_ALERTS_PER_CYCLE:
            break
    return out


def copy_messages(db: Db, idx: Indexer, new_swaps: list) -> list[str]:
    """Alerts when a followed / smart-money wallet buys."""
    now = int(time.time())
    buys = wallets.copy_buys(db, new_swaps)
    out = []
    for b in sorted(buys, key=lambda x: abs(x["weth"]), reverse=True):
        if abs(b["weth"]) < cfg.COPY_MIN_ETH:
            continue
        # dedupe on the tx so a buy alerts once
        if not db.alert_ready("copy", b["tx"], now, FOREVER):
            continue
        db.mark_alert("copy", b["tx"], now)
        out.append(cards.copy_card(db, idx, b))
        if len(out) >= cfg.MAX_ALERTS_PER_CYCLE:
            break
    return out


def volume_log_messages(db: Db, idx: Indexer) -> list[str]:
    """Log ANY token on the chain that traded in the window. Broad + frequent;
    uses the compact card so it stays cheap even with many tokens."""
    now = int(time.time())
    rows = stats.tokens_with_volume(db, cfg.LOG_WINDOW, cfg.LOG_MIN_ETH)
    out = []
    for r in rows:
        # one log per token per window bucket so it refreshes, not spams
        bucket = int(now // cfg.LOG_COOLDOWN)
        key = f"{r['address']}:{bucket}"
        if not db.alert_ready("vlog", r["address"], now, cfg.LOG_COOLDOWN):
            continue
        db.mark_alert("vlog", r["address"], now)
        win = {"n": r["n"], "v": r["v"], "b": r["b"]}
        card = cards.log_card(db, idx, r, win, cfg.LOG_WINDOW)
        if card:
            out.append(card)
        if len(out) >= cfg.LOG_MAX_PER_CYCLE:
            break
    return out


def collect(rpc: Rpc, db: Db, idx: Indexer, tick) -> list[str]:
    if db.get_meta("global_paused", "0") == "1":
        return []
    msgs = launch_messages(rpc, db, idx, tick.new_launches)
    msgs += burst_messages(rpc, db, idx)
    msgs += graduation_messages(rpc, db, idx)
    msgs += whale_messages(rpc, db, idx, tick.new_swaps)
    msgs += surge_messages(rpc, db, idx)
    msgs += momentum_messages(rpc, db, idx)
    msgs += volume_log_messages(db, idx)
    msgs += quote_messages(rpc, db, idx)
    # PONS-token spike/price alerts intentionally omitted — the feed is about
    # other launchpad tokens (launches, volume bursts, graduations), not PONS.
    return [m for m in msgs if m]
