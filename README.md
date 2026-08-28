# Pons Volume Tracker

A real-time intelligence bot for **Robinhood Chain** launchpad tokens
(Pons, Lemon, LaunchFactory and any future fork), reporting to Telegram.

Everything is read **directly from the chain's JSON-RPC** — there is no
third-party price feed or DEX screener in the data path. Third-party sources
are used only as a *candidate list* for holder lookups, and every balance shown
is re-verified on-chain with `balanceOf`.

## Features

**Alerts** (broadcast to every subscriber who sends `/start`)
- 💸 **Volume log** — any token that trades above a small floor is logged
- 🚀 **Volume bursts** & 📈 **price surges** (per token, all launchpads)
- ⚡ **Momentum** — early-pump detection via volume acceleration
- 🎓 **Graduation** — progress toward the 4.2 Ξ threshold, full stats on each milestone
- 🐋 **Whale** trades
- 🚀 **New launches** with an instant 🟢 SAFE / 🟡 RISKY / 🔴 AVOID grade
- 📈 **Called-coin quotes** — re-quote a coin we flagged when it moves, with PnL since the call

**Per-token intelligence**
- Graduation %, rug-safety score (LP lock, dev holdings, holder concentration, pool depth)
- Deployer reputation (prior launches, how many went dead)
- Holder count & top-10 concentration, all balance-verified on-chain

**Commands**

| Command | What |
|---|---|
| `/trending [15m]` | hottest tokens by momentum |
| `/top [24h]` | top tokens by volume |
| `/quote <coin>` | current volume of a coin |
| `/grad [24h]` | closest to graduation |
| `/launches` | newest launches |
| `/launchpads [24h]` | launchpad market share |
| `/safety <coin>` | rug-risk breakdown |
| `/smart` · `/wallet <addr>` | profitable-wallet ranking |
| `/calls` | the bot's own call performance |
| `/price` · `/token <coin>` · `/chain` | market data |
| `/admin` | admin controls (group owners) |

**Admin / group controls** — `/setvol`, `/setsurge`, `/setlog`,
`/broadcast`, `/subs`, `/pauseall`, `/resumeall`, `/thresholds`.

## How launchpads are detected

Every launchpad factory on the chain emits the same
`TokenLaunched(token, deployer, dexFactory, pairToken, pool, …)` event, so the
indexer discovers them **dynamically by topic** — Pons, Lemon, LaunchFactory,
and any new fork automatically. Each token is tagged with its launchpad.

## Why not just use a screener

Blockscout's holder endpoint returns stale balances on this chain (addresses it
lists holding hundreds of millions can hold zero on-chain), so holder data is
derived from `Transfer` logs and confirmed with `balanceOf` before display.

## Running

Pure Python 3 standard library — no dependencies, no `pip install`.

```bash
cp .env.example .env      # fill in TELEGRAM_BOT_TOKEN (and TELEGRAM_CHAT_ID)
python3 run.py            # indexer + alerts + command bot
python3 run.py --once     # one index cycle + summary, then exit
python3 run.py --no-bot   # indexer and alerts only
```

Get a bot token from [@BotFather](https://t.me/BotFather). The bot learns your
chat automatically the first time you send it `/start`; anyone who sends
`/start` is subscribed and receives alerts.

### Production

Run it under any process manager. Example with **pm2**:

```bash
pm2 start run.py --name pons-tracker --interpreter python3
pm2 save
```

Or **systemd** — point a simple unit at `python3 /path/to/run.py` with
`Restart=always`.

## Configuration

All thresholds live in `.env` (see `.env.example`) and can be tuned live by an
admin via `/setvol`, `/setsurge`, `/setlog`. Key knobs:

| Var | Meaning |
|---|---|
| `POLL_INTERVAL` | seconds between index cycles |
| `LOG_MIN_ETH` | min volume to log any token |
| `VOL_MIN_ETH` | min buy volume for a burst alert |
| `SURGE_PCT` | price-surge threshold (%) |
| `ACCEL_MULT` | momentum acceleration multiple |
| `WHALE_ETH` | min single-swap size for a whale alert |
| `MAX_ALERTS_PER_CYCLE` | cap per cycle |

## Chain notes

Robinhood Chain is an Arbitrum Nitro L2 (chain id 4663, ~101 ms blocks). The
public RPC rejects a default user-agent, caps `eth_getLogs` at 10k results, and
rate-limits — the client sets a browser UA, paces requests, backs off on 429,
and recursively splits any log range the node refuses.

## License

MIT
