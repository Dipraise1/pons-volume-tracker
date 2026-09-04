"""Command handlers for the Telegram bot."""
from __future__ import annotations
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import chain as C
from . import cards, fmt, intel, signals, stats, stock, wallets
from .db import Db
from .indexer import Indexer
from .telegram import Telegram

WINDOWS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400,
           "6h": 21600, "12h": 43200, "24h": 86400, "7d": 604800}

HELP = """<b>Pons Volume Tracker</b> — Robinhood Chain

<b>Market</b>
/price — PONS price, market cap, supply
/volume [1h] — PONS volume + buy/sell split
/top [24h] — highest-volume Pons tokens
/chain — chain-wide Pons activity

<b>Tokens</b>
/launches — newest tokens off the factory
/token &lt;symbol|addr&gt; — full card for one token
/grad [24h] — tokens closest to graduating
/launchpads [24h] — Pons vs Lemon vs others
/safety &lt;symbol|addr&gt; — rug-risk breakdown

<b>Stock tokens</b>
/stocks — every verified Robinhood equity + drift
/stock &lt;SYMBOL&gt; [wallet] — verify + true balance
/blocked &lt;wallet&gt; — registry blocklist check

<b>Signals</b>
/trending [15m] — hottest by momentum
/smart — most profitable wallets
/calls — this bot's own call record

/wallet &lt;addr&gt; — a wallet's record

<b>Admin</b> (group owners)
/admin — admin command list
/kols — tracked KOL wallets (/addkol 0x @handle)
/quote &lt;coin&gt; — current volume of a called coin

<b>Admin</b>
/status · /settings · /mute · /unmute · /stop

Windows: 5m, 15m, 1h, 4h, 6h, 12h, 24h, 7d"""


class Bot:
    def __init__(self, tg: Telegram, db: Db, idx: Indexer):
        self.tg, self.db, self.idx = tg, db, idx
        # Command handlers run in worker threads so a slow command (e.g. /grad,
        # which does many RPC calls) never blocks the poll loop or other users.
        self._pool = ThreadPoolExecutor(max_workers=6,
                                        thread_name_prefix="cmd")

    # --- admin ----------------------------------------------------------
    def _is_admin(self, user_id, chat_id) -> bool:
        from . import config as cfg
        uid = str(user_id)
        # global admins, plus admins of a group where the bot is added
        if uid in cfg.ADMIN_IDS:
            return True
        return False

    def cmd_admin(self, chat_id, args, user_id=None):
        if not self._is_admin(user_id, chat_id):
            return "🔒 Admins only."
        return "\n".join([
            "<b>Admin commands</b>",
            "/setvol &lt;eth&gt; — min volume to log a token",
            "/setsurge &lt;pct&gt; — price-surge threshold",
            "/setlog &lt;eth&gt; — broad volume-log floor",
            "/broadcast &lt;msg&gt; — message all subscribers",
            "/subs — subscriber count &amp; list",
            "/pauseall · /resumeall — global alert switch",
            "/addadmin &lt;id&gt; — grant admin (session only)",
            "/thresholds — show all live thresholds",
        ])

    def _set_env(self, key, val):
        """Update a live threshold on the config module (and .env for persist)."""
        from . import config as cfg
        import pathlib
        setattr(cfg, key, val)
        try:
            envp = cfg.ROOT / ".env"
            lines = envp.read_text().splitlines() if envp.exists() else []
            done = False
            for i, ln in enumerate(lines):
                if ln.startswith(f"{key}="):
                    lines[i] = f"{key}={val}"; done = True
            if not done:
                lines.append(f"{key}={val}")
            envp.write_text("\n".join(lines) + "\n")
        except Exception:
            pass

    def cmd_setvol(self, chat_id, args, user_id=None):
        if not self._is_admin(user_id, chat_id):
            return "🔒 Admins only."
        try:
            v = float(args[0]); self._set_env("VOL_MIN_ETH", v)
            return f"✅ Volume-burst floor set to {v} Ξ."
        except Exception:
            return "Usage: /setvol 0.05"

    def cmd_setsurge(self, chat_id, args, user_id=None):
        if not self._is_admin(user_id, chat_id):
            return "🔒 Admins only."
        try:
            v = float(args[0]); self._set_env("SURGE_PCT", v)
            return f"✅ Surge threshold set to {v}%."
        except Exception:
            return "Usage: /setsurge 10"

    def cmd_setlog(self, chat_id, args, user_id=None):
        if not self._is_admin(user_id, chat_id):
            return "🔒 Admins only."
        try:
            v = float(args[0]); self._set_env("LOG_MIN_ETH", v)
            return f"✅ Volume-log floor set to {v} Ξ."
        except Exception:
            return "Usage: /setlog 0.02"

    def cmd_broadcast(self, chat_id, args, user_id=None):
        if not self._is_admin(user_id, chat_id):
            return "🔒 Admins only."
        if not args:
            return "Usage: /broadcast your message"
        text = "📢 " + " ".join(args)
        sent = 0
        for c in self.db.active_subscribers():
            if self.tg.send(c, text).get("ok"):
                sent += 1
        return f"Broadcast sent to {sent} chat(s)."

    def cmd_subs(self, chat_id, args, user_id=None):
        if not self._is_admin(user_id, chat_id):
            return "🔒 Admins only."
        rows = self.db.q("SELECT chat_id,title,kind,muted FROM subscribers")
        active = sum(1 for r in rows if not r["muted"])
        out = [f"<b>{len(rows)} subscriber(s)</b> ({active} active)"]
        for r in rows[:30]:
            m = " 🔇" if r["muted"] else ""
            out.append(f"• {fmt.esc(r['title'] or r['chat_id'])} "
                       f"<i>{r['kind']}</i>{m}")
        return "\n".join(out)

    def cmd_pauseall(self, chat_id, args, user_id=None):
        if not self._is_admin(user_id, chat_id):
            return "🔒 Admins only."
        self.db.set_meta("global_paused", "1")
        return "⏸️ All alerts paused globally. /resumeall to resume."

    def cmd_resumeall(self, chat_id, args, user_id=None):
        if not self._is_admin(user_id, chat_id):
            return "🔒 Admins only."
        self.db.set_meta("global_paused", "0")
        return "▶️ Alerts resumed globally."

    def cmd_addadmin(self, chat_id, args, user_id=None):
        if not self._is_admin(user_id, chat_id):
            return "🔒 Admins only."
        from . import config as cfg
        if not args:
            return "Usage: /addadmin <telegram_user_id>"
        cfg.ADMIN_IDS.append(str(args[0]))
        return f"✅ {fmt.esc(args[0])} is now an admin (until restart)."

    def cmd_addkol(self, chat_id, args, user_id=None):
        if not self._is_admin(user_id, chat_id):
            return "🔒 Admins only."
        if len(args) < 2 or not (args[0].startswith("0x") and len(args[0]) == 42):
            return "Usage: <code>/addkol 0x&lt;wallet&gt; @handle</code>"
        self.db.add_kol(args[0], " ".join(args[1:]), int(time.time()))
        return (f"✅ KOL added: {fmt.link(fmt.short(args[0]), C.explorer_addr(args[0]))}"
                f" = {fmt.esc(' '.join(args[1:]))}\n<i>{len(self.db.kols())} KOL(s) tracked.</i>")

    def cmd_delkol(self, chat_id, args, user_id=None):
        if not self._is_admin(user_id, chat_id):
            return "🔒 Admins only."
        if not args:
            return "Usage: <code>/delkol 0x…</code>"
        self.db.del_kol(args[0])
        return f"Removed KOL {fmt.short(args[0])}."

    def cmd_kols(self, chat_id, args, user_id=None):
        kols = self.db.kols()
        if not kols:
            return ("No KOL wallets tracked yet. Admins add them with "
                    "<code>/addkol 0x… @handle</code> — then alerts show which "
                    "KOLs aped each coin.")
        out = [f"<b>🌟 {len(kols)} KOL wallet(s) tracked</b>"]
        for a, h in list(kols.items())[:40]:
            out.append(f"• {fmt.link(fmt.short(a), C.explorer_addr(a))} — {fmt.esc(h)}")
        return "\n".join(out)

    def cmd_thresholds(self, chat_id, args, user_id=None):
        from . import config as cfg
        return "\n".join([
            "<b>Live thresholds</b>",
            f"Poll every {cfg.POLL_INTERVAL}s",
            f"Volume log ≥ {cfg.LOG_MIN_ETH} Ξ (any token)",
            f"Volume burst ≥ {cfg.VOL_MIN_ETH} Ξ",
            f"Surge ≥ {cfg.SURGE_PCT}%",
            f"Momentum accel ≥ {cfg.ACCEL_MULT}x",
            f"Whale ≥ {cfg.WHALE_ETH} Ξ",
            f"Max alerts/cycle: {cfg.MAX_ALERTS_PER_CYCLE}",
        ])

    # --- helpers --------------------------------------------------------
    def _window(self, args: list[str], default: str = "1h") -> tuple[str, int]:
        if args and args[0].lower() in WINDOWS:
            key = args[0].lower()
        else:
            key = default
        return key, WINDOWS[key]

    def _eth_usd(self) -> float:
        try:
            return self.idx.eth_usd()
        except Exception:
            row = self.db.one("SELECT eth_usd FROM price_history "
                              "ORDER BY ts DESC LIMIT 1")
            return row["eth_usd"] if row else 0.0

    # --- commands -------------------------------------------------------
    def cmd_start(self, chat_id, args, chat: dict | None = None):
        chat = chat or {}
        title = (chat.get("title") or chat.get("username")
                 or chat.get("first_name") or "")
        is_new = self.db.add_subscriber(chat_id, title,
                                        chat.get("type") or "private",
                                        int(time.time()))
        # Keep the legacy single-chat key pointing somewhere sane.
        if not self.db.get_meta("chat_id"):
            self.db.set_meta("chat_id", chat_id)
        n = len(self.db.active_subscribers())
        head = ("✅ <b>Subscribed.</b> You'll get launch, volume-burst, "
                "whale and graduation alerts.") if is_new else \
               ("🔔 <b>Alerts resumed</b> for this chat.")
        return f"{head}\n<i>{n} chat(s) subscribed.</i>\n\n{HELP}"

    def cmd_stop(self, chat_id, args):
        self.db.remove_subscriber(chat_id)
        return "👋 Unsubscribed. /start to come back."

    def cmd_grad(self, chat_id, args):
        """Tokens closest to the 4.2 Ξ graduation threshold."""
        key, secs = self._window(args, "24h")
        rows = stats.top_tokens(self.db, secs, 12)   # cap RPC fan-out
        if not rows:
            return f"No token volume indexed in the last {key}."
        out = []
        for r in rows:
            g = intel.graduation(self.idx.rpc, r["address"])
            if not g or g.get("graduated"):
                continue
            out.append((g["pct"], r, g))
        if not out:
            return "No tokens with graduation progress right now."
        out.sort(key=lambda x: -x[0])
        lines = [f"<b>Closest to graduation · {key}</b>"]
        for pct, r, g in out[:10]:
            lines.append(
                f"{fmt.link(r['symbol'] or '???', C.explorer_token(r['address']))}"
                f"  <b>{pct:.1f}%</b>"
                f"\n   {intel.progress_bar(pct)} "
                f"{g['accumulated']:.2f}/{g['threshold']:.1f} Ξ")
        return "\n".join(lines)

    def cmd_safety(self, chat_id, args):
        if not args:
            return "Usage: <code>/safety SYMBOL</code>"
        t = stats.token_by_query(self.db, args[0])
        if not t:
            return f"No indexed token matching <code>{fmt.esc(args[0])}</code>."
        sf = intel.safety(self.idx.rpc, self.db, t["address"])
        if not sf:
            return "Could not build a safety read for that token."
        icon = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}[sf["grade"]]
        lines = [f"<b>{fmt.esc(t['symbol'])}</b> — risk {icon} "
                 f"<b>{sf['grade']}</b> ({sf['score']}/100)", ""]
        lines += [f"• {r}" for r in sf["reasons"]]
        hist = intel.deployer_history(self.idx.rpc, self.db, t["deployer"])
        if hist and hist.get("launches", 0) > 1:
            lines += ["", f"Deployer: {hist['launches']} launches, "
                          f"{hist['dead']} dead ({hist['dead_pct']:.0f}%)"]
        return "\n".join(lines)

    # --- Robinhood stock tokens ---------------------------------------
    def cmd_stock(self, chat_id, args):
        """Verify a tokenised equity and show what balanceOf hides."""
        if not args:
            return self.cmd_stocks(chat_id, args)
        query = args[0]
        addr = stock.by_query(self.db, query)
        if not addr:
            return (f"No stock token matching <code>{fmt.esc(query)}</code>. "
                    "Run <code>/stockscan</code> to (re)build the list, or pass "
                    "a contract address.")
        info = stock.verify(self.idx.rpc, addr)
        if not info:
            meta = None
            try:
                meta = self.idx.token_meta([addr]).get(addr.lower())
            except Exception:
                pass
            return cards.counterfeit_card(addr, meta)
        stock.remember(self.db, info)

        holder = None
        held = blocked = None
        if len(args) > 1 and args[1].startswith("0x") and len(args[1]) == 42:
            holder = args[1]
            held = stock.holdings(self.idx.rpc, addr, holder)
            blocked = stock.wallet_blocked(self.idx.rpc, holder)
        return cards.stock_card(self.idx.rpc, info, holder, held, blocked)

    def cmd_stocks(self, chat_id, args):
        """Every official stock token, worst multiplier drift first."""
        rows = stock.known(self.db)
        if not rows:
            return ("No stock tokens cached yet — run <code>/stockscan</code> "
                    "to discover and verify them.")
        drifted = [r for r in rows if abs((r["multiplier"] or 1.0) - 1.0) > 1e-9]
        drifted.sort(key=lambda r: -abs(r["multiplier"] - 1.0))
        lines = [f"<b>Robinhood stock tokens</b> — {len(rows)} verified", ""]
        if drifted:
            lines.append("<b>Raw balances already wrong</b>")
            for r in drifted[:15]:
                lines.append(f"  {fmt.esc(r['symbol'] or '?'):<6} "
                             f"{(r['multiplier']-1)*100:+.4f}%")
            lines.append("")
        lines.append(f"{len(rows)-len(drifted)} still at 1.000000000")
        paused = stock.registry_paused(self.idx.rpc)
        if paused:
            lines += ["", "🔴 <b>Registry is PAUSED — all stock tokens frozen.</b>"]
        lines += ["", "<code>/stock SYMBOL [wallet]</code> for one token"]
        return "\n".join(lines)

    def cmd_stockscan(self, chat_id, args, user_id=None):
        """Rebuild the verified stock-token list from the explorer index."""
        if not self._is_admin(user_id, chat_id):
            return "Admins only."
        res = stock.discover(self.idx.rpc, self.db)
        return ("<b>Stock token scan</b>\n"
                f"candidates   {res['candidates']}\n"
                f"official     {res['official']}\n"
                f"counterfeit  {res['counterfeit']}")

    def cmd_blocked(self, chat_id, args):
        """Is a wallet blocklisted from holding stock tokens?"""
        if not args or not args[0].startswith("0x"):
            return "Usage: <code>/blocked 0xwallet</code>"
        w = args[0]
        res = stock.wallet_blocked(self.idx.rpc, w)
        if res is None:
            return "Could not read the registry."
        if res:
            return (f"🔴 <b>{fmt.short(w)}</b> is <b>blocklisted</b> — it cannot "
                    "send or receive Robinhood stock tokens.")
        return f"🟢 <b>{fmt.short(w)}</b> is not blocklisted."

    def cmd_smart(self, chat_id, args):
        top = wallets.smart_money(self.db, 10)   # background loop keeps this fresh
        if not top:
            return ("No profitable multi-token wallets ranked yet — the wallet "
                    "index rebuilds every ~15 min.")
        lines = ["<b>Most profitable Pons wallets</b> <i>(realised, 7d)</i>"]
        for i, w in enumerate(top, 1):
            wr = w["wins"] / w["tokens"] * 100 if w["tokens"] else 0
            lines.append(
                f"{i}. {fmt.link(fmt.short(w['address']), C.explorer_addr(w['address']))}"
                f" — <b>{fmt.eth(w['realized'])}</b>"
                f"\n    {w['tokens']} tokens · {wr:.0f}% win rate"
                f" · {fmt.eth(w['volume'])} vol")
        return "\n".join(lines)

    def cmd_wallet(self, chat_id, args):
        if not args:
            return "Usage: <code>/wallet 0x…</code>"
        w = wallets.wallet(self.db, args[0])
        if not w:
            return "No indexed activity for that wallet."
        wr = w["wins"] / w["tokens"] * 100 if w["tokens"] else 0
        return "\n".join([
            f"<b>{fmt.short(w['address'])}</b>",
            f"<code>{w['address']}</code>",
            f"Realised  <b>{fmt.eth(w['realized'])}</b>",
            f"Tokens    {w['tokens']} · {wr:.0f}% win rate",
            f"Volume    {fmt.eth(w['volume'])}",
        ])

    def cmd_quote(self, chat_id, args):
        """Quote a coin's current volume on demand."""
        if not args:
            return "Usage: <code>/quote SYMBOL</code> or <code>/quote 0x…</code>"
        t = stats.token_by_query(self.db, args[0])
        if not t:
            return f"No indexed token matching <code>{fmt.esc(args[0])}</code>."
        call = self.db.one("SELECT ts,price_usd,mcap_usd FROM calls "
                           "WHERE token=? ORDER BY ts ASC LIMIT 1",
                           (t["address"],))
        from . import cards
        card = cards.quote_card(self.idx.rpc, self.db, self.idx, t["address"],
                                dict(call) if call else {}, 900)
        return card or "Could not build a quote for that token."

    def cmd_calls(self, chat_id, args):
        sb = wallets.scoreboard(self.db, 12)
        if not sb["calls"]:
            return "No alerts fired yet — nothing to score."
        lines = [f"<b>Call record</b> — {sb['total']} calls, "
                 f"{sb['doubles']} did 2x+, best {sb['best']:.1f}x", ""]
        for c in sb["calls"]:
            lines.append(
                f"{fmt.esc(c['symbol'] or c['token'][:10])} "
                f"· {fmt.ago(c['ts'])} · entry {fmt.usd(c['mcap_usd'])} mcap "
                f"· <b>{c['multiple']:.2f}x</b>")
        return "\n".join(lines)

    def cmd_help(self, chat_id, args):
        return HELP

    def cmd_price(self, chat_id, args):
        try:
            eth = self.idx.eth_usd()
            ppw = self.idx.pons_per_weth()
            price = eth / ppw if ppw else 0
            circ = self.idx.circulating()
        except Exception as exc:
            return f"⚠️ RPC error: {fmt.esc(exc)}"
        lines = [
            "<b>$PONS</b>",
            f"Price      <b>{fmt.usd(price)}</b>",
            f"Market cap {fmt.usd(price * circ)}",
            f"Circulating {fmt.num(circ)} / 1.00B  "
            f"({(1 - circ/1e9)*100:.1f}% burned)",
            f"Rate       {fmt.num(ppw)} PONS per Ξ",
            f"ETH        {fmt.usd(eth)}",
        ]
        for label in ("15m", "1h", "24h"):
            ch = stats.price_change(self.db, WINDOWS[label])
            if ch and ch[0]:
                pct = (ch[1] - ch[0]) / ch[0] * 100
                arrow = "🟢" if pct >= 0 else "🔴"
                lines.append(f"{label:<4} {arrow} {pct:+.2f}%")
        return "\n".join(lines)

    def cmd_volume(self, chat_id, args):
        key, secs = self._window(args)
        v = stats.pons_volume(self.db, secs)
        eth = self._eth_usd()
        if not v["swaps"]:
            return (f"No PONS swaps indexed in the last {key}.\n"
                    f"If you just started the tracker, give it a minute.")
        ratio = v["buys"] / v["swaps"] * 100
        return "\n".join([
            f"<b>$PONS volume · {key}</b>",
            f"Volume  <b>{fmt.eth(v['weth'])}</b>  ({fmt.usd(v['weth']*eth)})",
            f"Swaps   {v['swaps']:,}",
            f"Buys    {v['buys']:,}  ·  Sells {v['sells']:,}",
            f"{fmt.bar(v['buys'], v['sells'])}  {ratio:.0f}% buys",
        ])

    def cmd_chain(self, chat_id, args):
        key, secs = self._window(args, "24h")
        v = stats.volume(self.db, secs)
        lc = stats.launch_counts(self.db)
        eth = self._eth_usd()
        return "\n".join([
            f"<b>Pons activity · {key}</b>",
            f"Tracked volume {fmt.eth(v['weth'])} ({fmt.usd(v['weth']*eth)})",
            f"Swaps          {v['swaps']:,}",
            f"Buy/sell       {v['buys']:,} / {v['sells']:,}",
            "",
            f"Launches  1h {lc['1h']} · 24h {lc['24h']} · 7d {lc['7d']}",
            f"Indexed tokens: {lc['total']:,}",
        ])

    def cmd_top(self, chat_id, args):
        key, secs = self._window(args, "24h")
        rows = stats.top_tokens(self.db, secs, 10)
        if not rows:
            return f"No token volume indexed in the last {key}."
        eth = self._eth_usd()
        out = [f"<b>Top Pons tokens · {key}</b>"]
        for i, r in enumerate(rows, 1):
            buys = r["buys"] or 0
            pct = buys / r["n"] * 100 if r["n"] else 0
            out.append(
                f"{i}. {fmt.link(r['symbol'] or '???', C.explorer_token(r['address']))}"
                f" — {fmt.eth(r['vol'])} ({fmt.usd(r['vol']*eth)})"
                f"\n    {r['n']:,} swaps · {pct:.0f}% buys")
        return "\n".join(out)

    def cmd_launches(self, chat_id, args):
        rows = stats.recent_launches(self.db, 10)
        if not rows:
            return "No launches indexed yet."
        out = ["<b>Recent launches</b>"]
        for r in rows:
            pad = C.launchpad_name(r.get("factory"))
            out.append(
                f"{fmt.link(r['symbol'] or '???', C.explorer_token(r['address']))}"
                f" · {fmt.esc(r['name'])} · <i>{fmt.esc(pad)}</i>"
                f"\n   {fmt.ago(r['launch_ts'])} · initial buy "
                f"{fmt.eth(r['initial_buy'] or 0)} · block {r['launch_block']:,}")
        return "\n".join(out)

    def cmd_token(self, chat_id, args):
        if not args:
            return "Usage: <code>/token SYMBOL</code> or <code>/token 0x…</code>"
        t = stats.token_by_query(self.db, args[0])
        if not t:
            return f"No indexed Pons token matching <code>{fmt.esc(args[0])}</code>."
        eth = self._eth_usd()
        out = [f"<b>{fmt.esc(t['symbol'])}</b> — {fmt.esc(t['name'])}",
               f"<code>{t['address']}</code>",
               f"Launched {fmt.ago(t['launch_ts'])} · block {t['launch_block']:,}",
               f"Deployer {fmt.link(fmt.short(t['deployer']), C.explorer_addr(t['deployer']))}",
               f"Initial buy {fmt.eth(t['initial_buy'] or 0)}", ""]
        for label in ("1h", "24h"):
            v = stats.volume(self.db, WINDOWS[label], t["pool"])
            out.append(f"{label:<4} {fmt.eth(v['weth'])} ({fmt.usd(v['weth']*eth)})"
                       f" · {v['swaps']:,} swaps · {v['buys']}B/{v['sells']}S")
        out.append("")
        out.append(fmt.link("pool", C.explorer_addr(t["pool"])) + " · "
                   + fmt.link("token", C.explorer_token(t["address"])))
        return "\n".join(out)

    def cmd_launchpads(self, chat_id, args):
        """Launch + volume share across every launchpad on the chain."""
        key, secs = self._window(args, "24h")
        rows = stats.launchpad_breakdown(self.db, secs)
        if not rows:
            return f"No launches indexed in the last {key}."
        eth = self._eth_usd()
        rows.sort(key=lambda r: (r["launches"], r["vol"]), reverse=True)
        tot_l = sum(r["launches"] for r in rows) or 1
        tot_v = sum(r["vol"] for r in rows) or 1
        out = [f"<b>Launchpads · {key}</b>", ""]
        for r in rows:
            name = C.launchpad_name(r["factory"])
            lshare = r["launches"] / tot_l * 100
            vshare = r["vol"] / tot_v * 100
            out.append(
                f"<b>{fmt.esc(name)}</b>  {r['launches']} launches ({lshare:.0f}%)"
                f"\n   vol {fmt.eth(r['vol'])} ({fmt.usd(r['vol']*eth)}) · "
                f"{vshare:.0f}% share · {r['swaps']} swaps")
        out.append("")
        out.append(f"<i>{tot_l} launches across {len(rows)} launchpad(s)</i>")
        return "\n".join(out)

    def cmd_trending(self, chat_id, args):
        """Hottest tokens by momentum (acceleration), not just raw volume."""
        key, secs = self._window(args, "15m")
        rows = signals.trending(self.db, secs, 10)
        if not rows:
            return f"Nothing trending in the last {key}."
        eth = self._eth_usd()
        out = [f"<b>🔥 Trending · {key}</b> <i>(by momentum)</i>"]
        for i, r in enumerate(rows, 1):
            accel = r["accel"]
            a = "∞" if accel == float("inf") else f"{accel:.1f}x"
            out.append(
                f"{i}. {fmt.link(r['symbol'] or '???', C.explorer_token(r['address']))}"
                f" — {fmt.eth(r['cur']['vol'])} ({fmt.usd(r['cur']['vol']*eth)})"
                f"\n    accel {a} · {r['cur']['buys']}/{r['cur']['n']} buys"
                f" · {r['cur']['traders']} traders")
        return "\n".join(out)

    def cmd_follow(self, chat_id, args):
        if not args:
            return "Usage: <code>/follow 0x… [label]</code>"
        addr = args[0]
        if not (addr.startswith("0x") and len(addr) == 42):
            return "That doesn't look like a wallet address."
        label = " ".join(args[1:]) if len(args) > 1 else "manual"
        self.db.follow(addr, label, "manual", int(time.time()))
        n = len(self.db.followed())
        return (f"✅ Following {fmt.link(fmt.short(addr), C.explorer_addr(addr))}"
                f" <i>{fmt.esc(label)}</i>\nYou'll get an alert when it buys."
                f"\n<i>{n} wallet(s) followed.</i>")

    def cmd_unfollow(self, chat_id, args):
        if not args:
            return "Usage: <code>/unfollow 0x…</code>"
        self.db.unfollow(args[0])
        return f"Removed {fmt.short(args[0])}."

    def cmd_following(self, chat_id, args):
        f = self.db.followed()
        if not f:
            return ("Not following any wallets yet.\n"
                    "Top smart-money wallets are auto-followed once ranked; "
                    "add your own with /follow 0x…")
        manual = [w for w in f.values() if w["source"] == "manual"]
        smart = [w for w in f.values() if w["source"] == "smart"]
        out = [f"<b>Following {len(f)} wallet(s)</b>"]
        if manual:
            out.append("\n<b>Manual:</b>")
            for w in manual[:15]:
                out.append(f"• {fmt.link(fmt.short(w['address']), C.explorer_addr(w['address']))}"
                           f" <i>{fmt.esc(w['label'] or '')}</i>")
        if smart:
            out.append(f"\n<b>Smart money (auto):</b> {len(smart)}")
        return "\n".join(out)

    def cmd_copy(self, chat_id, args):
        """Recent buys by followed / smart wallets."""
        key, secs = self._window(args, "1h")
        followed = self.db.followed()
        if not followed:
            return "No wallets followed yet. Smart money auto-follows once ranked."
        since = int(time.time()) - secs
        marks = ",".join("?" * len(followed))
        rows = self.db.q(
            f"SELECT s.trader, t.symbol, t.address, s.weth_amt, s.ts, s.tx "
            f"FROM swaps s JOIN tokens t ON t.pool=s.pool "
            f"WHERE s.is_buy=1 AND s.ts>=? AND LOWER(s.trader) IN ({marks}) "
            f"ORDER BY s.ts DESC LIMIT 15", (since, *followed.keys()))
        if not rows:
            return f"No followed-wallet buys in the last {key}."
        eth = self._eth_usd()
        out = [f"<b>🎯 Followed-wallet buys · {key}</b>"]
        for r in rows:
            lbl = followed.get(r["trader"].lower(), {}).get("label", "")
            out.append(
                f"{fmt.link(r['symbol'] or '???', C.explorer_token(r['address']))}"
                f" — {fmt.eth(abs(r['weth_amt']))} ({fmt.usd(abs(r['weth_amt'])*eth)})"
                f"\n   {fmt.link(fmt.short(r['trader']), C.explorer_addr(r['trader']))}"
                f" <i>{fmt.esc(lbl)}</i> · {fmt.ago(r['ts'])}")
        return "\n".join(out)

    def cmd_status(self, chat_id, args):
        rng = stats.indexed_range(self.db)
        last = self.db.get_meta("last_block")
        started = self.db.get_meta("started_at")
        muted = self.db.get_meta("muted", "0") == "1"
        try:
            head = self.idx.rpc.block_number()
            lag = head - int(last) if last else None
        except Exception:
            head, lag = None, None
        bt = self.idx.clock.block_time()
        lines = [
            "<b>Indexer status</b>",
            f"Head block     {head:,}" if head else "Head block     unavailable",
            f"Indexed to     {int(last):,}" if last else "Indexed to     —",
        ]
        if lag is not None:
            lines.append(f"Lag            {lag:,} blocks (~{lag*bt:.0f}s)")
        lines += [
            f"Block time     {bt*1000:.0f} ms",
            f"Swaps stored   {rng['n']:,}",
            f"Oldest swap    {fmt.ago(rng['lo'])}",
            f"Alerts         {'muted 🔇' if muted else 'active 🔔'}",
        ]
        if started:
            lines.append(f"Running since  {fmt.ago(int(started))}")
        return "\n".join(lines)

    def cmd_settings(self, chat_id, args):
        from . import config as cfg
        return "\n".join([
            "<b>Alert thresholds</b>",
            f"Whale swap    ≥ {cfg.WHALE_ETH} Ξ",
            f"Volume spike  ≥ {cfg.SPIKE_MULT}× baseline, min {cfg.SPIKE_MIN_ETH} Ξ",
            f"Price move    ≥ {cfg.PRICE_MOVE_PCT}% / 15m",
            f"Cooldown      {cfg.ALERT_COOLDOWN}s",
            "",
            "Edit <code>.env</code> and restart to change these.",
        ])

    def cmd_mute(self, chat_id, args):
        self.db.set_subscriber_muted(chat_id, True)
        return "🔇 Alerts muted for this chat. /unmute to resume."

    def cmd_unmute(self, chat_id, args):
        self.db.set_subscriber_muted(chat_id, False)
        return "🔔 Alerts resumed for this chat."

    # --- dispatch -------------------------------------------------------
    HANDLERS = {
        "start": cmd_start, "help": cmd_help, "price": cmd_price,
        "volume": cmd_volume, "vol": cmd_volume, "top": cmd_top,
        "launches": cmd_launches, "new": cmd_launches, "token": cmd_token,
        "chain": cmd_chain, "status": cmd_status, "settings": cmd_settings,
        "mute": cmd_mute, "unmute": cmd_unmute, "stop": cmd_stop,
        "grad": cmd_grad, "graduation": cmd_grad, "safety": cmd_safety,
        "launchpads": cmd_launchpads, "pads": cmd_launchpads,
        "stock": cmd_stock, "stocks": cmd_stocks, "equities": cmd_stocks,
        "stockscan": cmd_stockscan, "blocked": cmd_blocked,
        "smart": cmd_smart, "wallet": cmd_wallet, "calls": cmd_calls,
        "quote": cmd_quote, "q": cmd_quote,
        "trending": cmd_trending, "hot": cmd_trending,
        "admin": cmd_admin, "setvol": cmd_setvol, "setsurge": cmd_setsurge,
        "setlog": cmd_setlog, "broadcast": cmd_broadcast, "subs": cmd_subs,
        "pauseall": cmd_pauseall, "resumeall": cmd_resumeall,
        "addadmin": cmd_addadmin, "thresholds": cmd_thresholds,
        "addkol": cmd_addkol, "delkol": cmd_delkol, "kols": cmd_kols,
    }

    def _safe_handle(self, msg: dict) -> None:
        try:
            self.handle(msg)
        except Exception as exc:
            print(f"[bot] handler crashed: {type(exc).__name__}: {exc}",
                  flush=True)

    def handle(self, msg: dict) -> None:
        text = (msg.get("text") or "").strip()
        chat_id = (msg.get("chat") or {}).get("id")
        if not text.startswith("/") or not chat_id:
            return
        parts = text.split()
        cmd = parts[0][1:].split("@")[0].lower()
        fn = self.HANDLERS.get(cmd)
        if not fn:
            return
        print(f"[bot] /{cmd} from {chat_id}", flush=True)
        user_id = (msg.get("from") or {}).get("id")
        import inspect
        try:
            if cmd == "start":
                reply = fn(self, chat_id, parts[1:], msg.get("chat"))
            elif "user_id" in inspect.signature(fn).parameters:
                reply = fn(self, chat_id, parts[1:], user_id=user_id)
            else:
                reply = fn(self, chat_id, parts[1:])
        except Exception as exc:
            import traceback; traceback.print_exc()
            reply = f"⚠️ {type(exc).__name__}: {fmt.esc(exc)}"
        if reply:
            res = self.tg.send(chat_id, reply)
            if not res.get("ok"):
                print(f"[bot] reply FAILED: {res.get('description')}", flush=True)
            else:
                print(f"[bot] replied to /{cmd} ({len(reply)} chars)", flush=True)

    def poll_forever(self, stop) -> None:
        offset = None
        saved = self.db.get_meta("update_offset")
        if saved:
            offset = int(saved)
        while not stop.is_set():
            try:
                for up in self.tg.get_updates(offset, timeout=25):
                    offset = up["update_id"] + 1
                    self.db.set_meta("update_offset", offset)
                    if "message" in up:
                        # dispatch async — poll loop returns to Telegram at once
                        self._pool.submit(self._safe_handle, up["message"])
            except Exception as exc:
                print(f"[bot] poll error: {type(exc).__name__}: {exc}",
                      flush=True)
                time.sleep(3)
