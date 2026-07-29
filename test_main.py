"""Integration tests for main.py orchestration + tracker runtime target.

Covers:
  * tracker.target_wallet property/setter (runtime switch + poll re-prime)
  * _copy_decision routing under the ARM/DRY_RUN double-lock
  * /set_target hook propagates to the tracker
  * graceful shutdown stops tracker + bot

No network: the trader and Telegram send are mocked.

Run:  PYTHONIOENCODING=utf-8 python test_main.py
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from unittest.mock import AsyncMock

# --- required env before importing config ---
os.environ.setdefault("PRIVATE_KEY", "0x" + "1" * 64)
os.environ.setdefault("PROXY_WALLET_ADDRESS", "0xProxy")
os.environ.setdefault("RPC_URL", "https://polygon-rpc.com")
os.environ.setdefault("TELEGRAM_TOKEN", "123456:ABCDEF")
os.environ.setdefault("TELEGRAM_ADMIN_ID", "999")
os.environ.setdefault("TARGET_WALLET", "0x" + "a" * 40)
os.environ.setdefault("DRY_RUN", "true")

import tracker as trackermod  # noqa: E402
from tracker import WhaleTracker, WhaleTrade  # noqa: E402


@dataclass
class Trade:
    side: str = "BUY"
    token_id: str = "TOK1"
    price: float = 0.42
    size: float = 10.0
    slug: str = "market-x"
    condition_id: str = "0xcond"


def build_orchestrator():
    """Import main and build an Orchestrator with mocked trader + bot._send."""
    import main as mainmod
    orch = mainmod.Orchestrator()

    # Neutralize real network / Telegram.
    orch.trader.execute_1usd_buy = AsyncMock(
        return_value={"status": "submitted", "price_cap": 0.43, "size": 2.3,
                      "response": {"transactionHash": "0xtx"}}
    )
    orch.trader.get_usdc_balance = AsyncMock(return_value=100.0)
    orch.bot._send = AsyncMock()
    orch.bot.notify_whale = AsyncMock()
    orch.bot.notify_trade = AsyncMock()
    return orch


# --- tracker runtime target ---------------------------------------------------

def test_tracker_runtime_target():
    tr = WhaleTracker(AsyncMock())
    start = tr.target_wallet
    assert start == ("0x" + "a" * 40)

    tr._poll_primed = True
    tr.target_wallet = "0x" + "B" * 40  # mixed case -> normalized lower
    assert tr.target_wallet == "0x" + "b" * 40
    assert tr._poll_primed is False, "changing target must re-prime the poller"

    # no-op when same
    tr._poll_primed = True
    tr.target_wallet = "0x" + "b" * 40
    assert tr._poll_primed is True, "same target must not reset priming"
    print("OK  tracker.target_wallet setter switches + re-primes")


async def test_tracker_ws_uses_new_target():
    """After switching target, WS parsing filters for the NEW wallet."""
    emitted = []

    async def cb(t):
        emitted.append(t)

    tr = WhaleTracker(cb)
    new_wallet = "0x" + "e" * 40
    tr.target_wallet = new_wallet

    import json
    msg = {"payload": {"proxyWallet": "0x" + "E" * 40, "side": "BUY", "price": "0.3",
                       "size": "5", "asset": "TOKZ", "transactionHash": "0xh"}}
    await tr._handle_ws_message(json.dumps(msg))
    assert len(emitted) == 1 and emitted[0].token_id == "TOKZ"
    print("OK  tracker WS matches the new runtime target")


# --- copy_decision routing ----------------------------------------------------

async def test_decision_skips_sell():
    orch = build_orchestrator()
    orch.state.dry_run = False
    orch.state.armed = True
    await orch._copy_decision(Trade(side="SELL"))
    orch.bot.notify_whale.assert_awaited_once()      # still notified
    orch.trader.execute_1usd_buy.assert_not_awaited()  # but not executed
    print("OK  decision: SELL notified but not executed")


async def test_decision_dry_run_paper():
    orch = build_orchestrator()
    orch.state.dry_run = True
    orch.state.armed = True  # armed but dry_run -> paper
    await orch._copy_decision(Trade())
    orch.trader.execute_1usd_buy.assert_not_awaited()
    sent = orch.bot._send.call_args[0][0]
    assert "SIMULATION / PAPER" in sent
    print("OK  decision: DRY_RUN -> [SIMULATION/PAPER], no order")


async def test_decision_paused_no_order():
    orch = build_orchestrator()
    orch.state.dry_run = False
    orch.state.armed = False  # live mode but PAUSED
    await orch._copy_decision(Trade())
    orch.trader.execute_1usd_buy.assert_not_awaited()
    assert "PAUSED" in orch.bot._send.call_args[0][0]
    print("OK  decision: live+PAUSED -> suppressed")


async def test_decision_live_executes():
    orch = build_orchestrator()
    orch.state.dry_run = False
    orch.state.armed = True  # can_execute_live == True
    await orch._copy_decision(Trade())
    orch.trader.execute_1usd_buy.assert_awaited_once_with("TOK1", 0.42)
    orch.bot.notify_trade.assert_awaited_once()
    print("OK  decision: ARMED + live -> executes 1 USDC buy + notifies")


async def test_set_target_hook_propagates():
    orch = build_orchestrator()
    new = "0x" + "d" * 40
    await orch._on_set_target(new)
    assert orch.tracker.target_wallet == new
    print("OK  /set_target hook propagates to tracker")


async def test_graceful_shutdown():
    orch = build_orchestrator()

    async def fake_run():
        await asyncio.Event().wait()  # blocks until cancelled

    task = asyncio.create_task(fake_run())
    orch.bot.stop = AsyncMock()
    await orch._shutdown(task)
    assert task.cancelled() or task.done()
    orch.bot.stop.assert_awaited_once()
    print("OK  graceful shutdown cancels tracker + stops bot")


async def _run_async():
    await test_tracker_ws_uses_new_target()
    await test_decision_skips_sell()
    await test_decision_dry_run_paper()
    await test_decision_paused_no_order()
    await test_decision_live_executes()
    await test_set_target_hook_propagates()
    await test_graceful_shutdown()


def main():
    test_tracker_runtime_target()
    asyncio.run(_run_async())
    print("\nAll main.py / tracker integration tests passed.")


if __name__ == "__main__":
    main()
