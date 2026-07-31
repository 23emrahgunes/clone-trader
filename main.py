"""Orchestration entry point for the Polymarket clone trading bot.

Wires the four modules together:

    config  -> credentials + tunables + safety switches
    bot     -> Telegram control surface + shared BotState + notifications
    tracker -> hybrid whale detection (WS firehose + data-api backfill)
    trader  -> Polymarket CLOB execution (fixed 1 USDC FOK buys)

Flow: tracker detects a target-wallet trade -> ``_copy_decision`` filters it
(BUY only, ARM/DRY_RUN double-lock) -> live order via trader OR a paper
notification -> result pushed back to Telegram.

Includes graceful shutdown on SIGINT / SIGTERM (Ctrl+C): the tracker is stopped,
the socket + poller drain, and the Telegram bot shuts down cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from bot import TelegramBot
from config import settings
from dashboard import Dashboard
from paper import PaperLedger
from tracker import WhaleTracker, WhaleTrade
from trader import PolymarketTrader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("main")


class Orchestrator:
    """Owns and connects the bot, tracker, and trader; runs the copy loop."""

    def __init__(self) -> None:
        # Bot owns the shared BotState; provide it the balance + target hooks.
        self.bot = TelegramBot(
            balance_provider=self._get_balance,
            on_targets_changed=self._on_targets_changed,
            on_reset=self._reset_ledger,
        )
        self.state = self.bot.state
        self.trader = PolymarketTrader()
        # tracker calls _copy_decision for every unique detected trade.
        self.tracker = WhaleTracker(self._copy_decision)
        # Keep tracker and state aligned at startup.
        self.tracker.set_targets(self.state.target_wallets)
        # Paper ledger + web dashboard for simulated PnL.
        self.paper = PaperLedger()
        self.dashboard = Dashboard(
            self.paper,
            port=settings.DASHBOARD_PORT,
            token=settings.DASHBOARD_TOKEN,
        )

    # -- hooks injected into the bot -----------------------------------------

    async def _on_targets_changed(self, wallets: list) -> None:
        """Propagate a Telegram target-list change to the live tracker."""
        self.tracker.set_targets(wallets)
        logger.info("Runtime targets updated: %d wallet(s)", len(wallets))

    async def _get_balance(self) -> "float | None":
        """Balance provider for /status (best-effort)."""
        return await self.trader.get_usdc_balance()

    async def _reset_ledger(self, scope: str) -> int:
        """Clear the PnL ledger (Telegram /reset). Returns rows removed."""
        return self.paper.reset(scope)

    # -- copy decision (the filter between detection and execution) ----------

    async def _copy_decision(self, trade: WhaleTrade) -> None:
        """Decide what to do with a detected whale trade.

        1. Always notify (a whale moved).
        2. Copy BUYs only.
        3. Double-lock:
             - DRY_RUN True  -> paper notification, no order.
             - ARMED False   -> skip (paused), no order.
             - ARMED & live  -> send a real 1 USDC FOK buy.
        """
        # 1. Detection notification for every target-wallet trade.
        await self.bot.notify_whale(trade)

        # 2. Only mirror BUY-side entries.
        if trade.side != "BUY":
            logger.info("Skip %s (only BUY is copied)", trade.side)
            return

        # 3a. Paper / simulation mode: record the position for PnL tracking.
        if self.state.dry_run:
            pos = self.paper.record_buy(trade)
            logger.info("[PAPER] recorded 1 USDC buy of %s @ %.4f (size=%.2f)",
                        trade.token_id, trade.price, pos["size"])
            await self.bot._send(
                "🧪 [SIMULATION / PAPER] Kopya emri simüle edildi (borsaya gönderilmedi).\n"
                f"Pazar: {trade.slug or trade.condition_id}\n"
                f"Fiyat: {trade.price}\n"
                f"Size: {pos['size']} (~{pos['cost_usdc']} USDC)\n"
                f"📊 PnL: dashboard'da izle"
            )
            return

        # 3b. Live but paused -> do not send.
        if not self.state.armed:
            logger.info("PAUSED: live order suppressed for %s", trade.token_id)
            await self.bot._send(
                "⏸️ PAUSED — balina alımı yakalandı ama emir gönderilmedi (/arm ile aktifleştir)."
            )
            return

        # 3c. ARMED and not DRY_RUN => execute a real order.
        assert self.state.can_execute_live
        logger.info("LIVE copy: 1 USDC buy of %s @ %.4f", trade.token_id, trade.price)
        result = await self.trader.execute_1usd_buy(trade.token_id, trade.price)
        # Record accepted live fills so they show on the dashboard too.
        if str(result.get("status")) == "submitted":
            self.paper.record_buy(
                trade,
                mode="live",
                size=result.get("size"),
                tx=TelegramBot._extract_tx(result),
            )
        await self.bot.notify_trade(result)

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        """Start everything and block until a shutdown signal arrives."""
        logger.info(
            "Starting clone trader | %d target(s)=%s | DRY_RUN=%s | boot=PAUSED",
            len(self.state.target_wallets), self.state.target_wallets, self.state.dry_run,
        )

        await self.bot.start()
        await self.dashboard.start()
        tracker_task = asyncio.create_task(self.tracker.run(), name="tracker")

        stop = asyncio.Event()
        self._install_signal_handlers(stop)

        try:
            # Exit if a signal fires OR the tracker task dies unexpectedly.
            done, _ = await asyncio.wait(
                {asyncio.create_task(stop.wait()), tracker_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if tracker_task in done:
                exc = tracker_task.exception() if not tracker_task.cancelled() else None
                if exc:
                    logger.error("Tracker crashed: %s", exc)
        finally:
            await self._shutdown(tracker_task)

    def _install_signal_handlers(self, stop: asyncio.Event) -> None:
        """Register SIGINT/SIGTERM handlers cross-platform."""
        loop = asyncio.get_running_loop()

        def _trip() -> None:
            logger.info("Shutdown signal received")
            loop.call_soon_threadsafe(stop.set)

        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, stop.set)  # POSIX
            except (NotImplementedError, RuntimeError):
                # Windows: add_signal_handler is unsupported; fall back.
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, lambda *_: _trip())

    async def _shutdown(self, tracker_task: asyncio.Task) -> None:
        """Stop the tracker and bot cleanly (idempotent-ish)."""
        logger.info("Graceful shutdown starting...")

        self.tracker.stop()
        tracker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tracker_task

        with contextlib.suppress(Exception):
            await self.dashboard.stop()

        with contextlib.suppress(Exception):
            await self.bot.stop()

        logger.info("Shutdown complete")


def main() -> None:
    orchestrator = Orchestrator()
    try:
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        # Last-resort catch if the signal handler path did not intercept.
        logger.info("Interrupted")


if __name__ == "__main__":
    main()
