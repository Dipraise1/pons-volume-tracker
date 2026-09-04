"""SQLite storage. One connection, shared across the indexer and bot threads."""
from __future__ import annotations
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    address        TEXT PRIMARY KEY,
    symbol         TEXT,
    name           TEXT,
    decimals       INTEGER DEFAULT 18,
    pool           TEXT,
    pair_token     TEXT,
    pair_is_token0 INTEGER,
    deployer       TEXT,
    factory        TEXT,
    launch_block   INTEGER,
    launch_ts      INTEGER,
    initial_buy    REAL,
    restrictions_end INTEGER,
    position_id    INTEGER,
    fee            INTEGER,
    tx             TEXT,
    total_supply   REAL,
    is_pons        INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_tokens_pool  ON tokens(pool);
CREATE INDEX IF NOT EXISTS idx_tokens_block ON tokens(launch_block);

CREATE TABLE IF NOT EXISTS swaps (
    tx         TEXT,
    log_index  INTEGER,
    pool       TEXT,
    block      INTEGER,
    ts         INTEGER,
    weth_amt   REAL,      -- signed, from the pool's perspective
    token_amt  REAL,
    is_buy     INTEGER,
    price_weth REAL,      -- token price in WETH, from sqrtPriceX96
    trader     TEXT,
    PRIMARY KEY (tx, log_index)
);
CREATE INDEX IF NOT EXISTS idx_swaps_pool_ts ON swaps(pool, ts);
CREATE INDEX IF NOT EXISTS idx_swaps_ts      ON swaps(ts);
CREATE INDEX IF NOT EXISTS idx_swaps_block   ON swaps(block);

CREATE TABLE IF NOT EXISTS anchors (
    block INTEGER PRIMARY KEY,
    ts    INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS alert_log (
    kind    TEXT,
    subject TEXT,
    ts      INTEGER,
    PRIMARY KEY (kind, subject)
);

CREATE TABLE IF NOT EXISTS subscribers (
    chat_id    TEXT PRIMARY KEY,
    title      TEXT,
    kind       TEXT,
    joined_at  INTEGER,
    muted      INTEGER DEFAULT 0,
    min_eth    REAL
);

-- Every alert we fire, so the bot's own call performance is measurable.
CREATE TABLE IF NOT EXISTS calls (
    token      TEXT,
    ts         INTEGER,
    kind       TEXT,
    price_usd  REAL,
    mcap_usd   REAL,
    peak_usd   REAL,
    PRIMARY KEY (token, ts)
);
CREATE INDEX IF NOT EXISTS idx_calls_token ON calls(token);

-- Cached per-wallet trading performance, rebuilt from indexed swaps.
CREATE TABLE IF NOT EXISTS wallets (
    address     TEXT PRIMARY KEY,
    tokens      INTEGER,
    wins        INTEGER,
    realized    REAL,
    unrealized  REAL,
    volume      REAL,
    updated_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_wallets_realized ON wallets(realized DESC);

CREATE TABLE IF NOT EXISTS followed_wallets (
    address   TEXT PRIMARY KEY,
    label     TEXT,
    source    TEXT,          -- 'manual' | 'smart'
    added_ts  INTEGER
);

-- Official Robinhood stock tokens, confirmed against the registry.
-- Cached because the registry answer is immutable per token; only the
-- multiplier moves, and it moves on corporate actions.
CREATE TABLE IF NOT EXISTS stock_tokens (
    address    TEXT PRIMARY KEY,
    symbol     TEXT,
    name       TEXT,
    multiplier REAL DEFAULT 1.0,
    updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_stock_symbol ON stock_tokens(symbol);

CREATE TABLE IF NOT EXISTS kols (
    address   TEXT PRIMARY KEY,
    handle    TEXT,
    added_ts  INTEGER
);

CREATE TABLE IF NOT EXISTS price_history (
    ts       INTEGER PRIMARY KEY,
    pons_usd REAL,
    eth_usd  REAL
);
"""


class Db:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        self.lock = threading.Lock()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(swaps)")}
        for col, decl in (("price_weth", "REAL"), ("trader", "TEXT")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE swaps ADD COLUMN {col} {decl}")
        tcols = {r[1] for r in self.conn.execute("PRAGMA table_info(tokens)")}
        for col, decl in (("restrictions_end", "INTEGER"),
                          ("position_id", "INTEGER"),
                          ("fee", "INTEGER")):
            if col not in tcols:
                self.conn.execute(f"ALTER TABLE tokens ADD COLUMN {col} {decl}")

    # --- generic -------------------------------------------------------
    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def run(self, sql: str, args: tuple = ()) -> None:
        with self.lock:
            self.conn.execute(sql, args)
            self.conn.commit()

    def many(self, sql: str, rows: list[tuple]) -> None:
        if not rows:
            return
        with self.lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    # --- meta ----------------------------------------------------------
    def get_meta(self, key: str, default=None):
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value) -> None:
        self.run("INSERT INTO meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))

    # --- KOLs (curated influencer wallets) -----------------------------
    def add_kol(self, address: str, handle: str, now: int) -> None:
        self.run("INSERT INTO kols(address,handle,added_ts) VALUES(?,?,?) "
                 "ON CONFLICT(address) DO UPDATE SET handle=excluded.handle",
                 (address.lower(), handle, now))

    def del_kol(self, address: str) -> None:
        self.run("DELETE FROM kols WHERE address=?", (address.lower(),))

    def kols(self) -> dict:
        return {r["address"]: r["handle"] for r in self.q("SELECT * FROM kols")}

    # --- followed wallets ---------------------------------------------
    def follow(self, address: str, label: str, source: str, now: int) -> bool:
        addr = address.lower()
        existing = self.one("SELECT 1 FROM followed_wallets WHERE address=?", (addr,))
        self.run("INSERT INTO followed_wallets(address,label,source,added_ts) "
                 "VALUES(?,?,?,?) ON CONFLICT(address) DO UPDATE SET "
                 "label=excluded.label", (addr, label, source, now))
        return existing is None

    def unfollow(self, address: str) -> None:
        self.run("DELETE FROM followed_wallets WHERE address=?", (address.lower(),))

    def followed(self) -> dict[str, dict]:
        return {r["address"]: dict(r)
                for r in self.q("SELECT * FROM followed_wallets")}

    # --- subscribers ---------------------------------------------------
    def add_subscriber(self, chat_id, title: str, kind: str, now: int) -> bool:
        """Returns True if this chat is newly subscribed."""
        existing = self.one("SELECT chat_id FROM subscribers WHERE chat_id=?",
                            (str(chat_id),))
        self.run("INSERT INTO subscribers(chat_id,title,kind,joined_at,muted) "
                 "VALUES(?,?,?,?,0) ON CONFLICT(chat_id) DO UPDATE SET "
                 "title=excluded.title, muted=0", (str(chat_id), title, kind, now))
        return existing is None

    def remove_subscriber(self, chat_id) -> None:
        self.run("DELETE FROM subscribers WHERE chat_id=?", (str(chat_id),))

    def set_subscriber_muted(self, chat_id, muted: bool) -> None:
        self.run("UPDATE subscribers SET muted=? WHERE chat_id=?",
                 (1 if muted else 0, str(chat_id)))

    def active_subscribers(self) -> list[str]:
        return [r["chat_id"] for r in
                self.q("SELECT chat_id FROM subscribers WHERE muted=0")]

    # --- alert dedupe --------------------------------------------------
    def alert_ready(self, kind: str, subject: str, now: int, cooldown: int) -> bool:
        row = self.one("SELECT ts FROM alert_log WHERE kind=? AND subject=?",
                       (kind, subject))
        return not row or (now - row["ts"]) >= cooldown

    def mark_alert(self, kind: str, subject: str, now: int) -> None:
        self.run("INSERT INTO alert_log(kind,subject,ts) VALUES(?,?,?) "
                 "ON CONFLICT(kind,subject) DO UPDATE SET ts=excluded.ts",
                 (kind, subject, now))
