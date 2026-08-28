"""Batched JSON-RPC client for Robinhood Chain (Arbitrum Nitro, ~101ms blocks).

Two things make this chain awkward to index and both are handled here:
  * the public RPC rejects requests with a default Python user-agent (403)
  * eth_getLogs caps results at 10,000 per query, and at 101ms blocks a
    one-hour window is ~35,600 blocks, so that cap is hit constantly
"""
from __future__ import annotations
import itertools
import json
import threading
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")

# Substrings the node uses when a getLogs range is too wide.
_TOO_MANY = ("exceeds limit", "too many", "response size", "more than",
             "limit exceeded", "timed out", "timeout", "too large",
             "range is too", "exceed maximum")

RETRYABLE_HTTP = {403, 408, 429, 500, 502, 503, 504}


class RpcError(Exception):
    pass


class Rpc:
    def __init__(self, url: str, timeout: int = 30, max_retries: int = 6,
                 min_interval: float = 0.06):
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        # The public RPC is shared and rate limited; pace requests rather
        # than discovering the limit through 429s.
        self.min_interval = min_interval
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._gate = threading.Lock()
        self._last = 0.0

    def _next_id(self) -> int:
        with self._lock:
            return next(self._ids)

    def _throttle(self) -> None:
        with self._gate:
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def _post(self, payload):
        body = json.dumps(payload).encode()
        delay, last = 0.4, None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                req = urllib.request.Request(
                    self.url, data=body,
                    headers={"Content-Type": "application/json",
                             "Accept": "application/json",
                             "User-Agent": UA},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}"
                if exc.code not in RETRYABLE_HTTP or attempt == self.max_retries - 1:
                    raise RpcError(f"{last}: {exc.read()[:200]!r}") from exc
                if exc.code == 429:
                    delay = max(delay, 1.5)   # back off harder when throttled
            except Exception as exc:  # timeouts, connection resets, bad JSON
                last = f"{type(exc).__name__}: {exc}"
                if attempt == self.max_retries - 1:
                    raise RpcError(last) from exc
            time.sleep(delay)
            delay *= 2
        raise RpcError(f"exhausted retries: {last}")

    def call(self, method: str, params: list):
        res = self._post({"jsonrpc": "2.0", "id": self._next_id(),
                          "method": method, "params": params})
        if isinstance(res, dict) and res.get("error"):
            raise RpcError(res["error"].get("message", str(res["error"])))
        return res.get("result")

    def batch(self, calls: list[tuple[str, list]], chunk: int = 40) -> list:
        """calls -> results, in the order given. None for any that errored."""
        out: list = []
        for i in range(0, len(calls), chunk):
            part = calls[i:i + chunk]
            payload, order = [], []
            for method, params in part:
                rid = self._next_id()
                order.append(rid)
                payload.append({"jsonrpc": "2.0", "id": rid,
                                "method": method, "params": params})
            res = self._post(payload)
            if isinstance(res, dict):  # node returned a single error object
                out.extend([None] * len(part))
                continue
            by_id = {r.get("id"): r for r in res}
            for rid in order:
                r = by_id.get(rid) or {}
                out.append(None if r.get("error") else r.get("result"))
        return out

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
