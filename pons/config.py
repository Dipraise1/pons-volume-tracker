"""Configuration, loaded from .env next to the project root."""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        # strip trailing inline comments, but not '#' inside a quoted value
        val = val.split("#", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)


_load_env()


def _f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _i(key: str, default: int) -> int:
    try:
        return int(float(os.environ.get(key, "") or default))
    except ValueError:
        return default


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
RPC_URL = os.environ.get("RPC_URL", "https://rpc.mainnet.chain.robinhood.com").strip()
# eth_getLogs endpoint: the public RPC allows wide ranges (QuickNode free caps
# them at 5 blocks). Everything else uses RPC_URL (fast, no rate limit).
LOGS_URL = os.environ.get("LOGS_URL", "https://rpc.mainnet.chain.robinhood.com").strip()
try:
    from . import rpc as _rpc
    _rpc.DEFAULT_LOGS_URL = LOGS_URL
except Exception:
    pass
# The endpoint is latency-bound, not rate-limited: one connection tops out
# near 1 req/s while four sustain ~460 calls/s in batches of 100. Throughput
# stops improving past ~4 connections, and batches of 500+ are refused (429).
RPC_CONCURRENCY = _i("RPC_CONCURRENCY", 4)
RPC_CHUNK = _i("RPC_CHUNK", 100)

BACKFILL_HOURS = _f("BACKFILL_HOURS", 6)
LAUNCH_BACKFILL_DAYS = _f("LAUNCH_BACKFILL_DAYS", 2)
CONFIRMATIONS = _i("CONFIRMATIONS", 8)
POLL_INTERVAL = _i("POLL_INTERVAL", 6)

WHALE_ETH = _f("WHALE_ETH", 0.25)
SPIKE_MULT = _f("SPIKE_MULT", 3.0)
SPIKE_MIN_ETH = _f("SPIKE_MIN_ETH", 5.0)
PRICE_MOVE_PCT = _f("PRICE_MOVE_PCT", 10.0)
ALERT_COOLDOWN = _i("ALERT_COOLDOWN", 300)

# Buy-velocity alerts: high-signal buy pressure over a short window
VELOCITY_MIN_ETH = _f("VELOCITY_MIN_ETH", 2.0)   # min buy volume to fire
VELOCITY_MAX_ETH = _f("VELOCITY_MAX_ETH", 20.0)  # top of the intensity scale
VELOCITY_WINDOW = _i("VELOCITY_WINDOW", 300)     # 5 min (the "1-5 min" window)
VELOCITY_COOLDOWN = _i("VELOCITY_COOLDOWN", 600)

# Fresh-token sensitivity: the alpha is in early low-mcap movers, so fresh
# tokens trigger on much less volume than established ones.
FRESH_WINDOW = _i("FRESH_WINDOW", 3600)            # "fresh" for this long after launch
FRESH_VOL_MIN_ETH = _f("FRESH_VOL_MIN_ETH", 0.02)  # burst floor for fresh tokens
FRESH_VELOCITY_MIN_ETH = _f("FRESH_VELOCITY_MIN_ETH", 0.5)  # velocity floor for fresh

# Usefulness gate — suppress alerts that aren't actionable for a meme trader.
MAX_MCAP_USD = _f("MAX_MCAP_USD", 1500000)   # skip already-big tokens (no upside)
MIN_BUY_RATIO = _f("MIN_BUY_RATIO", 0.55)    # skip sell-dominated "pumps"
DEDUP_WINDOW = _i("DEDUP_WINDOW", 21600)     # one alert per token per 6h (any type)
MIN_POOL_ETH = _f("MIN_POOL_ETH", 0.3)       # skip tokens with a near-empty pool
DISABLE_VOLUME_LOG = _i("DISABLE_VOLUME_LOG", 1)  # kill the broad any-token feed

# Re-analysis of poor/critical tokens (re-alert if they turn good)
REANALYZE_INTERVAL = _i("REANALYZE_INTERVAL", 600)   # re-check cadence (s)
WATCH_GOOD_SCORE = _i("WATCH_GOOD_SCORE", 75)        # score that counts as "now good"
WATCH_MAX_PER_PASS = _i("WATCH_MAX_PER_PASS", 8)     # cap RPC-heavy re-grades/pass
WATCH_MIN_VOL_ETH = _f("WATCH_MIN_VOL_ETH", 0.2)     # min 1h volume to bother re-checking
WATCH_EXPIRY_DAYS = _f("WATCH_EXPIRY_DAYS", 3)       # drop dead tokens after this

# Volume-burst alerts (the primary signal)
VOL_WINDOW = _i("VOL_WINDOW", 180)              # seconds
VOL_MIN_ETH = _f("VOL_MIN_ETH", 0.05)           # buy volume in that window
MAX_ALERTS_PER_CYCLE = _i("MAX_ALERTS_PER_CYCLE", 25)

# Follow-up quotes on coins we already called
QUOTE_WINDOW = _i("QUOTE_WINDOW", 900)      # volume window for the quote
QUOTE_MIN_ETH = _f("QUOTE_MIN_ETH", 0.05)   # min volume to re-quote
QUOTE_COOLDOWN = _i("QUOTE_COOLDOWN", 900) # min seconds between quotes/coin

# Price-surge alerts (any token)
SURGE_PCT = _f("SURGE_PCT", 10.0)           # min % price jump in the window
SURGE_WINDOW = _i("SURGE_WINDOW", 300)      # window (seconds) to measure over
SURGE_MIN_ETH = _f("SURGE_MIN_ETH", 0.03)    # min volume, to suppress thin wicks
SURGE_COOLDOWN = _i("SURGE_COOLDOWN", 600) # per token per 25% bucket

# Momentum (early-pump acceleration) alerts
ACCEL_MULT = _f("ACCEL_MULT", 3.0)          # window vol >= this x the prior window
ACCEL_MIN_ETH = _f("ACCEL_MIN_ETH", 0.1)    # min current-window volume

# Copy / followed-wallet alerts
COPY_MIN_ETH = _f("COPY_MIN_ETH", 0.03)     # min buy size to alert
AUTOFOLLOW_SMART = _i("AUTOFOLLOW_SMART", 0)   # wallet-copy alerts disabled

# Broad "any token with volume" log feed
LOG_MIN_ETH = _f("LOG_MIN_ETH", 0.02)          # min volume in window to log
LOG_WINDOW = _i("LOG_WINDOW", 300)             # window seconds
LOG_COOLDOWN = _i("LOG_COOLDOWN", 300)         # re-log a token at most this often
LOG_MAX_PER_CYCLE = _i("LOG_MAX_PER_CYCLE", 25)

# Admin ids (comma-separated Telegram user ids). Owner is always admin.
ADMIN_IDS = [x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
if CHAT_ID and CHAT_ID not in ADMIN_IDS:
    ADMIN_IDS.append(CHAT_ID)

DB_PATH = ROOT / "pons.db"
