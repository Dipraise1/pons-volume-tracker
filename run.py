#!/usr/bin/env python3
"""Pons volume tracker — indexes Robinhood Chain and reports to Telegram.

  python3 run.py            # index + alerts + command bot
  python3 run.py --once     # a single index cycle, print a summary, exit
  python3 run.py --no-bot   # indexer and alerts only
"""
from __future__ import annotations
import argparse
import signal
import sys
import threading
import time

from pons import alerts, config as cfg, fmt, stats, wallets
from pons.bot import Bot
from pons.db import Db
from pons.indexer import Indexer
from pons.rpc import Rpc
from pons.telegram import Telegram

stop = threading.Event()
_last_wallet_rebuild = 0.0


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def subscribers(db: Db) -> list[str]:
    """Everyone who has sent /start and not muted or stopped.

    The configured CHAT_ID is seeded once so the owner is subscribed even
    before they message the bot.
    """
    if cfg.CHAT_ID and not db.one(
            "SELECT chat_id FROM subscribers WHERE chat_id=?", (cfg.CHAT_ID,)):
        db.add_subscriber(cfg.CHAT_ID, "owner", "private", int(time.time()))
    return db.active_subscribers()


def broadcast(tg: Telegram, db: Db, msgs: list[str]) -> int:
    """Fan alerts out to every subscriber, pruning chats that blocked us."""
    chats = subscribers(db)
    sent = 0
    for chat in chats:
        for m in msgs:
            res = tg.send(chat, m)
            if res.get("ok"):
                sent += 1
                continue
            desc = str(res.get("description", "")).lower()
            if "blocked" in desc or "chat not found" in desc or "kicked" in desc:
                db.remove_subscriber(chat)
                log(f"dropped subscriber {chat}: {desc}")
                break
    return sent


def index_loop(idx: Indexer, db: Db, tg: Telegram) -> None:
    while not stop.is_set():
        started = time.time()
        try:
            tick = idx.tick(cfg.CONFIRMATIONS, cfg.BACKFILL_HOURS,
                            cfg.LAUNCH_BACKFILL_DAYS)
            span = tick.to_block - tick.from_block + 1
            if tick.new_launches or tick.new_swaps:
                log(f"blocks {tick.from_block:,}→{tick.to_block:,} "
                    f"({span:,}) · {len(tick.new_swaps)} swaps · "
                    f"{len(tick.new_launches)} launches")
            msgs = alerts.collect(idx.rpc, db, idx, tick)
            if msgs:
                chats = subscribers(db)
                if chats:
                    n = broadcast(tg, db, msgs)
                    log(f"sent {len(msgs)} alert(s) to {len(chats)} chat(s) "
                        f"({n} deliveries)")
                else:
                    log(f"{len(msgs)} alert(s) held — no subscribers yet")
            # Wallet rankings power the smart-money signal; refresh sparingly.
            global _last_wallet_rebuild
            if time.time() - _last_wallet_rebuild > 900:
                _last_wallet_rebuild = time.time()
                try:
                    n = wallets.rebuild(db)
                    followed = 0
                    if cfg.AUTOFOLLOW_SMART:
                        followed = wallets.sync_smart_follows(db, cfg.AUTOFOLLOW_SMART)
                    log(f"wallet index rebuilt: {n} wallets, "
                        f"{followed} smart auto-followed")
                except Exception as exc:
                    log(f"wallet rebuild failed: {exc}")
        except Exception as exc:
            log(f"index error: {type(exc).__name__}: {exc}")
        stop.wait(max(1.0, cfg.POLL_INTERVAL - (time.time() - started)))


def summary(db: Db, idx: Indexer) -> str:
    try:
        eth = idx.eth_usd()
        ppw = idx.pons_per_weth()
        price = eth / ppw if ppw else 0
        circ = idx.circulating()
    except Exception as exc:
        return f"price unavailable: {exc}"
    v1 = stats.pons_volume(db, 3600)
    v24 = stats.pons_volume(db, 86400)
    lc = stats.launch_counts(db)
    rng = stats.indexed_range(db)
    return "\n".join([
        f"  PONS        {fmt.usd(price)}   mcap {fmt.usd(price*circ)}",
        f"  ETH         {fmt.usd(eth)}",
        f"  1h volume   {fmt.eth(v1['weth'])} ({fmt.usd(v1['weth']*eth)})"
        f"  {v1['swaps']} swaps  {v1['buys']}B/{v1['sells']}S",
        f"  24h volume  {fmt.eth(v24['weth'])} ({fmt.usd(v24['weth']*eth)})"
        f"  {v24['swaps']} swaps",
        f"  launches    1h {lc['1h']} · 24h {lc['24h']} · 7d {lc['7d']}"
        f" · indexed {lc['total']}",
        f"  swaps db    {rng['n']:,}",
    ])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="one index cycle, print summary, exit")
    ap.add_argument("--no-bot", action="store_true",
                    help="skip the command-polling thread")
    ap.add_argument("--test-alert", action="store_true",
                    help="send a test message to the registered chat")
    args = ap.parse_args()

    if not cfg.BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN missing from .env", file=sys.stderr)
        return 1

    db = Db(cfg.DB_PATH)
    rpc = Rpc(cfg.RPC_URL)
    idx = Indexer(rpc, db)
    tg = Telegram(cfg.BOT_TOKEN)
    # The bot gets its OWN rpc/indexer so interactive command lookups run on a
    # separate throttle from the indexer and from each other.
    bot_rpc = Rpc(cfg.RPC_URL, min_interval=0.0)
    bot_idx = Indexer(bot_rpc, db)
    bot = Bot(tg, db, bot_idx)
    db.set_meta("started_at", int(time.time()))

    me = tg.me()
    log(f"bot @{me.get('username','?')} · chain {rpc.call('eth_chainId', [])}")
    chats = subscribers(db)
    log(f"subscribers: {len(chats)}"
        + (f" -> {', '.join(chats[:5])}" if chats else " (send /start)"))

    if args.test_alert:
        if not chats:
            log("no subscribers; send /start to the bot first")
            return 1
        n = broadcast(tg, db, ["✅ <b>Pons tracker</b> connected.\n\n"
                               + summary(db, idx)])
        log(f"test broadcast: {n} delivery/deliveries")
        return 0

    if args.once:
        t0 = time.time()
        tick = idx.tick(cfg.CONFIRMATIONS, cfg.BACKFILL_HOURS,
                            cfg.LAUNCH_BACKFILL_DAYS)
        log(f"indexed blocks {tick.from_block:,}→{tick.to_block:,} "
            f"in {time.time()-t0:.1f}s · {len(tick.new_swaps)} swaps · "
            f"{len(tick.new_launches)} launches")
        print(summary(db, idx))
        return 0

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    threads = [threading.Thread(target=index_loop, args=(idx, db, tg),
                                daemon=True, name="indexer")]
    if not args.no_bot:
        threads.append(threading.Thread(target=bot.poll_forever, args=(stop,),
                                        daemon=True, name="bot"))
    for t in threads:
        t.start()
    log(f"running ({', '.join(t.name for t in threads)}) — ctrl-c to stop")
    try:
        while not stop.is_set():
            stop.wait(1)
    except KeyboardInterrupt:
        stop.set()
    log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
