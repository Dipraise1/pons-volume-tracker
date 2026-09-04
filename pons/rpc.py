"""Batched JSON-RPC client for Robinhood Chain (Arbitrum Nitro, ~101ms blocks).

Four things make this chain awkward to index and all four are handled here:
  * the public RPC rejects requests with a default Python user-agent (403)
  * eth_getLogs caps results at 10,000 per query, and at 101ms blocks a
    one-hour window is ~35,600 blocks, so that cap is hit constantly
  * the endpoint is latency-bound rather than rate-limited: a single
    connection sees ~0.7s round trips and so tops out near 1 req/s, while
    four concurrent connections sustain ~460 calls/s in batches of 100.
    Requests therefore run in parallel up to `concurrency` rather than being
    paced one at a time.
  * oversized requests (batches of ~500+) are rejected with 429, so a 429 is
    treated as a signal to back the whole client off briefly rather than to
    retry immediately on one thread.
"""
from __future__ import annotations
import itertools
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")

# Substrings the node uses when a getLogs range is too wide.
_TOO_MANY = ("exceeds limit", "too many", "response size", "more than",
             "limit exceeded", "timed out", "timeout", "too large",
             "range is too", "exceed maximum")

RETRYABLE_HTTP = {403, 408, 429, 500, 502, 503, 504}

# Set by config: endpoint used ONLY for eth_getLogs (e.g. a public RPC that
# allows wide ranges, when the main url is a plan that caps getLogs ranges).
DEFAULT_LOGS_URL = None

# Measured ceilings on the public endpoint: batches of 100 are served in
# ~0.7s, 500 is refused outright; throughput stops improving past ~4
# connections and gets slightly worse at 8.
DEFAULT_CONCURRENCY = 4
DEFAULT_CHUNK = 100
COOLDOWN_AFTER_429 = 2.0


class RpcError(Exception):
    pass


class Rpc:
    def __init__(self, url: str, timeout: int = 30, max_retries: int = 6,
                 min_interval: float = 0.0,
                 concurrency: int = DEFAULT_CONCURRENCY,
                 chunk: int = DEFAULT_CHUNK,
                 logs_url: str | None = None):
        self.url = url
        # eth_getLogs is routed here (a public RPC that allows wide ranges when
        # the main endpoint caps them, e.g. QuickNode's free plan → 5 blocks).
        self.logs_url = logs_url or DEFAULT_LOGS_URL or url
        self.timeout = timeout
        self.max_retries = max_retries
        # Kept for callers that deliberately want paced, polite traffic. It is
        # off by default: pacing a latency-bound endpoint only wastes headroom.
        self.min_interval = min_interval
        self.concurrency = max(1, concurrency)
        self.chunk = max(1, chunk)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._gate = threading.Lock()
        self._last = 0.0
        # Caps total in-flight HTTP requests across every thread using this
        # client, so the indexer and a burst of bot commands cannot together
        # open far more connections than the endpoint rewards.
        self._sem = threading.BoundedSemaphore(self.concurrency)
        self._pool: ThreadPoolExecutor | None = None
        self._pool_lock = threading.Lock()
        # Set when any thread sees a 429; every thread waits it out, so one
        # oversized request does not turn into a retry storm.
        self._cooldown_until = 0.0

    # --- pacing --------------------------------------------------------
    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        with self._gate:
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def _await_cooldown(self) -> None:
        while True:
            wait = self._cooldown_until - time.time()
            if wait <= 0:
                return
            time.sleep(min(wait, 0.25))

    def _trip_cooldown(self, seconds: float = COOLDOWN_AFTER_429) -> None:
        self._cooldown_until = max(self._cooldown_until, time.time() + seconds)

    def _executor(self) -> ThreadPoolExecutor:
        with self._pool_lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=self.concurrency, thread_name_prefix="rpc")
            return self._pool

    def _next_id(self) -> int:
        with self._lock:
            return next(self._ids)

    # --- transport -----------------------------------------------------
    def _post(self, payload, url: str | None = None):
        target = url or self.url
        body = json.dumps(payload).encode()
        delay, last = 0.4, None
        for attempt in range(self.max_retries):
            self._await_cooldown()
            self._throttle()
            try:
                req = urllib.request.Request(
                    target, data=body,
                    headers={"Content-Type": "application/json",
                             "Accept": "application/json",
                             "User-Agent": UA},
                )
                with self._sem:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}"
                if exc.code == 429:
                    # Usually an oversized request rather than a rate cap;
                    # either way every thread should ease off, not just this one.
                    self._trip_cooldown()
                    delay = max(delay, 1.5)
                if exc.code not in RETRYABLE_HTTP or attempt == self.max_retries - 1:
                    raise RpcError(f"{last}: {exc.read()[:200]!r}") from exc
            except Exception as exc:  # timeouts, connection resets, bad JSON
                last = f"{type(exc).__name__}: {exc}"
                if attempt == self.max_retries - 1:
                    raise RpcError(last) from exc
            time.sleep(delay)
            delay *= 2
        raise RpcError(f"exhausted retries: {last}")

    def call(self, method: str, params: list):
        url = self.logs_url if method == "eth_getLogs" else self.url
        res = self._post({"jsonrpc": "2.0", "id": self._next_id(),
                          "method": method, "params": params}, url=url)
        if isinstance(res, dict) and res.get("error"):
            raise RpcError(res["error"].get("message", str(res["error"])))
        return res.get("result")

    # --- batching ------------------------------------------------------
    def _run_chunk(self, part: list[tuple[str, list]]) -> list:
        """One JSON-RPC batch request. Results follow the order given."""
        payload, order = [], []
        for method, params in part:
            rid = self._next_id()
            order.append(rid)
            payload.append({"jsonrpc": "2.0", "id": rid,
                            "method": method, "params": params})
        # A transport failure is deliberately allowed to propagate. Returning
        # Nones here would be indistinguishable from "the chain has no value
        # for this call", which is how missing metadata and phantom zero
        # balances get into the database.
        res = self._post(payload)
        if isinstance(res, dict):  # node returned a single error object
            return [None] * len(part)
        by_id = {r.get("id"): r for r in res}
        out = []
        for rid in order:
            r = by_id.get(rid) or {}
            out.append(None if r.get("error") else r.get("result"))
        return out

    def batch(self, calls: list[tuple[str, list]], chunk: int | None = None) -> list:
        """calls -> results, in the order given. None for any that errored.

        Chunks are issued concurrently because the endpoint is latency-bound;
        results are reassembled by chunk index, so ordering is unaffected by
        the order in which responses arrive.
        """
        if not calls:
            return []
        size = chunk or self.chunk
        parts = [calls[i:i + size] for i in range(0, len(calls), size)]
        if len(parts) == 1:
            return self._run_chunk(parts[0])
        results = list(self._executor().map(self._run_chunk, parts))
        return [r for part in results for r in part]

    # --- convenience ---------------------------------------------------
    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def block_timestamp(self, block: int) -> int:
        b = self.call("eth_getBlockByNumber", [hex(block), False])
        return int(b["timestamp"], 16)

    def get_logs(self, from_block: int, to_block: int,
                 topics: list | None = None,
                 addresses: list[str] | None = None) -> list[dict]:
        """eth_getLogs that splits its own range when the node refuses."""
        if from_block > to_block:
            return []
        params: dict = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
        if topics:
            params["topics"] = topics
        if addresses:
            params["address"] = addresses
        try:
            return self.call("eth_getLogs", [params]) or []
        except RpcError as exc:
            msg = str(exc).lower()
            if from_block < to_block and any(h in msg for h in _TOO_MANY):
                mid = (from_block + to_block) // 2
                return (self.get_logs(from_block, mid, topics, addresses)
                        + self.get_logs(mid + 1, to_block, topics, addresses))
            raise

    def get_logs_windowed(self, from_block: int, to_block: int,
                          topics=None, addresses=None,
                          window: int = 20_000) -> list[dict]:
        """Walk a wide range in fixed windows, each self-splitting as needed."""
        logs: list[dict] = []
        start = from_block
        while start <= to_block:
            end = min(start + window - 1, to_block)
            logs.extend(self.get_logs(start, end, topics, addresses))
            start = end + 1
        return logs
