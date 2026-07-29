"""Hybrid whale activity tracker for the Polymarket clone trading bot.

Detects trades made by ``settings.TARGET_WALLET`` with the lowest practical
latency by running TWO sources concurrently and de-duplicating across them:

  * RTDS WebSocket firehose (primary, low-latency) -- subscribes to the public
    real-time ``activity`` topic (which has no server-side wallet filter) and
    filters client-side for the target wallet.
  * data-api polling (backfill / safety net) -- periodically queries
    ``/activity?user=<wallet>&type=TRADE`` to catch anything the socket missed
    (drops, reconnect gaps).

Every unique detected trade is journaled to JSONL and handed to an async
callback, which the orchestrator (main.py) wires to
``PolymarketTrader.execute_1usd_buy`` for BUY-side copies.

All I/O is non-blocking asyncio (rule 5). Network calls are wrapped in
try/except with meaningful logging (rule: strict error handling); a failure in
one source or in the callback never tears down the tracker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Optional

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from config import settings

logger = logging.getLogger(__name__)

# Public Polymarket real-time + data endpoints.
RTDS_WS_URL = "wss://ws-live-data.polymarket.com"
DATA_API_ACTIVITY_URL = "https://data-api.polymarket.com/activity"

# App-level keepalive cadence for the RTDS socket.
_PING_INTERVAL_S = 5.0
# WebSocket reconnect backoff bounds.
_WS_BACKOFF_MIN_S = 1.0
_WS_BACKOFF_MAX_S = 30.0

TradeCallback = Callable[["WhaleTrade"], Awaitable[None]]


@dataclass
class WhaleTrade:
    """A normalized trade event, unified across the WS and polling sources."""

    proxy_wallet: str
    side: str            # "BUY" / "SELL"
    token_id: str        # ERC-1155 asset / token id
    price: float
    size: float
    condition_id: str = ""
    outcome: str = ""
    slug: str = ""
    tx_hash: str = ""
    timestamp: int = 0   # ms epoch
    source: str = ""     # "ws" | "poll"

    @property
    def dedup_key(self) -> str:
        """Stable identity for cross-source de-duplication.

        Prefers the on-chain tx hash; falls back to a content signature for
        WS match events that are published before settlement (no hash yet).
        """
        if self.tx_hash:
            return f"tx:{self.tx_hash.lower()}:{self.token_id}:{self.side}"
        return (
            f"sig:{self.proxy_wallet}:{self.token_id}:{self.side}"
            f":{self.price}:{self.size}:{self.timestamp}"
        )


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class WhaleTracker:
    """Runs the WS firehose + data-api poller and emits unique target trades."""

    def __init__(
        self,
        callback: TradeCallback,
        *,
        poll_interval_s: float = 3.0,
        journal_path: str = "whale_activity.jsonl",
        dedup_maxlen: int = 5000,
    ) -> None:
        self._callback = callback
        self._poll_interval_s = poll_interval_s
        self._journal_path = journal_path
        self._dedup_maxlen = dedup_maxlen

        # Copy target is a proxy wallet address; compare case-insensitively.
        self._target = settings.TARGET_WALLET.lower()

        # Bounded, insertion-ordered set of seen dedup keys.
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._poll_primed = False  # first poll seeds baseline without emitting
        self._stopping = asyncio.Event()

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        """Run both sources until stop() is called or cancelled."""
        logger.info("WhaleTracker starting for target=%s (poll=%.1fs)",
                    self._target, self._poll_interval_s)
        try:
            await asyncio.gather(self._ws_loop(), self._poll_loop())
        except asyncio.CancelledError:
            logger.info("WhaleTracker cancelled")
            raise

    def stop(self) -> None:
        """Signal both loops to exit at the next opportunity."""
        self._stopping.set()

    # -- runtime target -----------------------------------------------------

    @property
    def target_wallet(self) -> str:
        """The wallet currently being mirrored (lower-cased)."""
        return self._target

    @target_wallet.setter
    def target_wallet(self, value: str) -> None:
        """Switch the tracked wallet at runtime (from Telegram /set_target).

        Takes effect immediately: the WS firehose has no server-side wallet
        filter, so the next message is matched against the new target. The
        poller re-primes its baseline so the new wallet's existing history is
        NOT replayed as fresh copy signals.
        """
        new = (value or "").strip().lower()
        if not new or new == self._target:
            return
        logger.info("Tracker target changed: %s -> %s", self._target, new)
        self._target = new
        self._poll_primed = False  # re-seed baseline for the new wallet

    # -- de-dup + emit -------------------------------------------------------

    def _is_new(self, trade: WhaleTrade) -> bool:
        """Return True and record the trade if unseen; False if a duplicate."""
        key = trade.dedup_key
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > self._dedup_maxlen:
            self._seen.popitem(last=False)  # evict oldest
        return True

    def _journal(self, trade: WhaleTrade) -> None:
        """Append the trade to the JSONL journal (best-effort)."""
        try:
            with open(self._journal_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(trade), separators=(",", ":")) + "\n")
        except OSError as exc:
            logger.warning("Failed to journal trade: %s", exc)

    async def _emit(self, trade: WhaleTrade) -> None:
        """De-dup, journal, and hand a fresh trade to the callback."""
        if not self._is_new(trade):
            return
        self._journal(trade)
        logger.info("Whale %s %s size=%.2f @ %.4f (src=%s tok=%s)",
                    trade.side, trade.slug or trade.condition_id, trade.size,
                    trade.price, trade.source, trade.token_id)
        try:
            await self._callback(trade)
        except Exception:  # noqa: BLE001 - a bad callback must not kill the tracker
            logger.exception("Trade callback failed for %s", trade.dedup_key)

    # -- source 1: RTDS WebSocket firehose -----------------------------------

    async def _ws_loop(self) -> None:
        """Maintain the RTDS socket with reconnect + backoff, forever."""
        backoff = _WS_BACKOFF_MIN_S
        subscribe_msg = json.dumps({
            "action": "subscribe",
            "subscriptions": [{"topic": "activity", "type": "*"}],
        })

        while not self._stopping.is_set():
            try:
                async with websockets.connect(RTDS_WS_URL, ping_interval=None) as ws:
                    await ws.send(subscribe_msg)
                    logger.info("RTDS socket connected + subscribed to activity")
                    backoff = _WS_BACKOFF_MIN_S  # reset after a healthy connect

                    ping_task = asyncio.create_task(self._ws_keepalive(ws))
                    try:
                        async for raw in ws:
                            await self._handle_ws_message(raw)
                            if self._stopping.is_set():
                                break
                    finally:
                        ping_task.cancel()
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, OSError, websockets.WebSocketException) as exc:
                logger.warning("RTDS socket dropped (%s); reconnecting in %.1fs", exc, backoff)
            except Exception:  # noqa: BLE001 - never let the loop die unexpectedly
                logger.exception("Unexpected RTDS socket error; reconnecting in %.1fs", backoff)

            if self._stopping.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _WS_BACKOFF_MAX_S)

    async def _ws_keepalive(self, ws: Any) -> None:
        """Send an app-level PING every few seconds until cancelled."""
        try:
            while True:
                await asyncio.sleep(_PING_INTERVAL_S)
                await ws.send("PING")
        except (asyncio.CancelledError, ConnectionClosed):
            return

    async def _handle_ws_message(self, raw: Any) -> None:
        """Parse a raw WS frame, extract target-wallet trades, and emit them."""
        if not raw or raw in ("PONG", "PING"):
            return
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return  # non-JSON keepalive frame

        for entry in self._iter_ws_entries(msg):
            trade = self._parse_ws_entry(entry)
            if trade is not None:
                await self._emit(trade)

    @staticmethod
    def _iter_ws_entries(msg: Any) -> list[dict]:
        """Flatten the various RTDS envelope shapes into a list of entry dicts."""
        entries: list[dict] = []
        candidates = msg if isinstance(msg, list) else [msg]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            payload = item.get("payload", item)
            if isinstance(payload, list):
                entries.extend(p for p in payload if isinstance(p, dict))
            elif isinstance(payload, dict):
                # Skip the initial snapshot envelope ({"data":[...]}) — history, not live.
                if isinstance(payload.get("data"), list):
                    continue
                entries.append(payload)
        return entries

    def _parse_ws_entry(self, d: dict) -> Optional[WhaleTrade]:
        """Build a WhaleTrade from an RTDS activity entry, filtered by target wallet."""
        wallet = str(d.get("proxyWallet") or d.get("user") or d.get("userAddress") or "").lower()
        if not wallet or wallet != self._target:
            return None

        side = str(d.get("side") or "").upper()
        token_id = str(d.get("asset") or d.get("tokenId") or "")
        if not token_id or side not in ("BUY", "SELL"):
            return None

        return WhaleTrade(
            proxy_wallet=wallet,
            side=side,
            token_id=token_id,
            price=_to_float(d.get("price")),
            size=_to_float(d.get("size")),
            condition_id=str(d.get("conditionId") or ""),
            outcome=str(d.get("outcome") or ""),
            slug=str(d.get("slug") or d.get("eventSlug") or ""),
            tx_hash=str(d.get("transactionHash") or ""),
            timestamp=_to_int(d.get("timestamp")),
            source="ws",
        )

    # -- source 2: data-api polling backfill ---------------------------------

    async def _poll_loop(self) -> None:
        """Poll /activity for the target wallet as a safety net for missed WS events."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            while not self._stopping.is_set():
                # Rebuilt each iteration so a runtime /set_target is picked up.
                params = {
                    "user": self._target,
                    "type": "TRADE",
                    "limit": 50,
                    "sortBy": "TIMESTAMP",
                    "sortDirection": "DESC",
                }
                try:
                    resp = await client.get(DATA_API_ACTIVITY_URL, params=params)
                    resp.raise_for_status()
                    batch = resp.json()
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("data-api poll failed: %s", exc)
                    batch = None

                if isinstance(batch, list):
                    await self._ingest_poll_batch(batch)

                await self._sleep_or_stop(self._poll_interval_s)

    async def _ingest_poll_batch(self, batch: list) -> None:
        """Emit new trades from a poll batch; the first batch only seeds baseline."""
        trades = [t for t in (self._parse_poll_entry(e) for e in batch) if t is not None]

        if not self._poll_primed:
            # Startup: record current history as seen so we don't copy old trades.
            for t in trades:
                self._is_new(t)
            self._poll_primed = True
            logger.info("data-api poller primed with %d historical trades", len(trades))
            return

        # API returns newest-first; emit oldest-first so copies keep chronological order.
        for t in reversed(trades):
            await self._emit(t)

    def _parse_poll_entry(self, d: Any) -> Optional[WhaleTrade]:
        """Build a WhaleTrade from a data-api /activity TRADE entry."""
        if not isinstance(d, dict):
            return None
        if str(d.get("type") or "").upper() not in ("TRADE", ""):
            return None

        side = str(d.get("side") or "").upper()
        token_id = str(d.get("asset") or d.get("tokenId") or "")
        if not token_id or side not in ("BUY", "SELL"):
            return None

        return WhaleTrade(
            proxy_wallet=str(d.get("proxyWallet") or self._target).lower(),
            side=side,
            token_id=token_id,
            price=_to_float(d.get("price")),
            size=_to_float(d.get("size")),
            condition_id=str(d.get("conditionId") or ""),
            outcome=str(d.get("outcome") or ""),
            slug=str(d.get("slug") or d.get("eventSlug") or ""),
            tx_hash=str(d.get("transactionHash") or ""),
            timestamp=_to_int(d.get("timestamp")),
            source="poll",
        )

    # -- helpers -------------------------------------------------------------

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep, but wake early if stop() was signalled."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
