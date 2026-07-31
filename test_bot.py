"""Unit tests for bot.py — admin guard, ARM/KILL double-lock, set_target,
notification dispatch, and message templates. No network required.

Run:  python -m pytest test_bot.py -v   (or)   python test_bot.py
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

# --- Provide required env BEFORE importing config/bot ---
os.environ.setdefault("PRIVATE_KEY", "0x" + "1" * 64)
os.environ.setdefault("PROXY_WALLET_ADDRESS", "0xProxy")
os.environ.setdefault("RPC_URL", "https://polygon-rpc.com")
os.environ.setdefault("TELEGRAM_TOKEN", "123456:ABCDEF")
os.environ.setdefault("TELEGRAM_ADMIN_ID", "999")
os.environ.setdefault("TARGET_WALLET", "0x" + "a" * 40)
os.environ.setdefault("DRY_RUN", "true")

from telegram.ext import ApplicationBuilder  # noqa: E402

import bot as botmod  # noqa: E402
from bot import BotState, TelegramBot, is_valid_address  # noqa: E402

ADMIN_ID = 999
STRANGER_ID = 111


# --- test helpers -------------------------------------------------------------

def make_bot(**kwargs) -> TelegramBot:
    """Build a TelegramBot on a real (offline) Application with a dummy token."""
    app = ApplicationBuilder().token("123456:ABCDEF").build()
    return TelegramBot(application=app, **kwargs)


def make_update(user_id: int) -> MagicMock:
    """Fake telegram Update whose message.reply_text is an AsyncMock."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def make_ctx(args=None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


@dataclass
class FakeTrade:
    slug: str = "trump-2024"
    side: str = "BUY"
    price: float = 0.42
    size: float = 10.0
    condition_id: str = "0xcond"
    token_id: str = "TOK1"


# --- tests --------------------------------------------------------------------

async def test_boots_paused():
    b = make_bot()
    assert b.state.armed is False, "bot must boot PAUSED"
    assert b.state.can_execute_live is False
    print("OK  boots PAUSED")


async def test_unauthorized_ignored():
    b = make_bot()
    upd = make_update(STRANGER_ID)
    await b._cmd_arm(upd, make_ctx())
    assert b.state.armed is False, "stranger must not be able to ARM"
    upd.message.reply_text.assert_not_called()
    print("OK  unauthorized user ignored (no state change, no reply)")


async def test_arm_and_kill():
    b = make_bot()
    upd = make_update(ADMIN_ID)

    await b._cmd_arm(upd, make_ctx())
    assert b.state.armed is True
    assert b.state.mode == "ARMED"

    await b._cmd_kill(upd, make_ctx())
    assert b.state.armed is False
    assert b.state.mode == "PAUSED"
    print("OK  /arm -> ARMED, /kill -> PAUSED")


async def test_double_lock_dry_run():
    # armed but dry_run -> still not live
    b = make_bot()
    b.state.dry_run = True
    b.state.armed = True
    assert b.state.can_execute_live is False, "dry_run must block live orders"
    b.state.dry_run = False
    assert b.state.can_execute_live is True, "armed + not dry_run -> live"
    print("OK  double-lock: armed AND not dry_run required for live")


async def test_set_target_valid():
    captured = {}

    async def hook(wallets):
        captured["wallets"] = wallets

    b = make_bot(on_targets_changed=hook)
    upd = make_update(ADMIN_ID)
    new = "0x" + "b" * 40
    await b._cmd_set_target(upd, make_ctx([new]))
    assert b.state.target_wallets == [new.lower()]
    assert captured["wallets"] == [new.lower()], "on_targets_changed hook must fire"
    print("OK  /set_target valid -> state updated + hook fired")


async def test_set_target_multi():
    captured = {}

    async def hook(wallets):
        captured["wallets"] = list(wallets)

    b = make_bot(on_targets_changed=hook)
    upd = make_update(ADMIN_ID)
    a, c = "0x" + "a" * 40, "0x" + "c" * 40
    # replace with two, then add a third, then remove one
    await b._cmd_set_target(upd, make_ctx([a, c]))
    assert b.state.target_wallets == [a, c]
    await b._cmd_add_target(upd, make_ctx(["0x" + "d" * 40]))
    assert len(b.state.target_wallets) == 3
    await b._cmd_remove_target(upd, make_ctx([a]))
    assert a not in b.state.target_wallets and len(b.state.target_wallets) == 2
    assert captured["wallets"] == b.state.target_wallets, "hook gets full list"
    print("OK  multi-target: set / add / remove")


async def test_set_target_invalid():
    b = make_bot()
    before = list(b.state.target_wallets)
    upd = make_update(ADMIN_ID)
    await b._cmd_set_target(upd, make_ctx(["not-an-address"]))
    assert b.state.target_wallets == before, "invalid address must be rejected"
    reply = upd.message.reply_text.call_args[0][0]
    assert "Kullanım" in reply
    print("OK  /set_target invalid -> rejected")


async def test_set_target_unauthorized():
    b = make_bot()
    before = list(b.state.target_wallets)
    upd = make_update(STRANGER_ID)
    await b._cmd_set_target(upd, make_ctx(["0x" + "c" * 40]))
    assert b.state.target_wallets == before
    print("OK  /set_target from stranger -> ignored")


async def test_reset_command():
    calls = {}

    async def on_reset(scope):
        calls["scope"] = scope
        return 7

    b = make_bot(on_reset=on_reset)
    # unauthorized ignored
    await b._cmd_reset(make_update(STRANGER_ID), make_ctx(["all"]))
    assert "scope" not in calls
    # admin, default paper
    upd = make_update(ADMIN_ID)
    await b._cmd_reset(upd, make_ctx())
    assert calls["scope"] == "paper"
    assert "7 kayıt" in upd.message.reply_text.call_args[0][0]
    # admin, all
    await b._cmd_reset(make_update(ADMIN_ID), make_ctx(["all"]))
    assert calls["scope"] == "all"
    print("OK  /reset: admin-only, paper default, all supported")


async def test_status_text_and_balance_provider():
    async def bal():
        return 42.5

    b = make_bot(balance_provider=bal)
    upd = make_update(ADMIN_ID)
    await b._cmd_status(upd, make_ctx())
    text = upd.message.reply_text.call_args[0][0]
    assert "PAUSED" in text
    assert "42.50 USDC" in text
    assert "DRY_RUN" in text
    print("OK  /status shows target, PAUSED, balance, DRY_RUN")


def test_templates():
    # whale
    w = TelegramBot.format_whale(FakeTrade())
    assert w.startswith("🎯 Balina Hareketi Yakalandı!")
    assert "Pazar: trump-2024" in w and "Fiyat: 0.42" in w and "Tutar: 10.0" in w

    # trade sent
    res = {"status": "submitted", "price_cap": 0.43, "size": 2.32,
           "response": {"transactionHash": "0xabc123"}}
    t = TelegramBot.format_trade_result(res)
    assert t.startswith("✅ 1 USDC'lik Clone Trade Atıldı!")
    assert "Fiyat: 0.43" in t and "Size: 2.32" in t and "Tx: 0xabc123" in t

    # cancelled (slippage)
    c = TelegramBot.format_cancelled(
        {"status": "cancelled", "reason": "slippage_exceeded",
         "slippage_pct": 20.0, "max_slippage_pct": 3.0,
         "target_price": 0.5, "market_price": 0.6})
    assert c.startswith("⚠️ İşlem İptal Edildi:")
    assert "Slippage %20.0 > %3.0" in c
    print("OK  templates: whale / trade-sent / cancelled")


async def test_notify_dispatch():
    b = make_bot()
    # ExtBot forbids attribute assignment, so mock our own _send wrapper.
    b._send = AsyncMock()

    # whale notification
    await b.notify_whale(FakeTrade())
    assert "🎯 Balina" in b._send.call_args[0][0]

    # submitted -> success template
    await b.notify_trade({"status": "submitted", "price_cap": 0.4, "size": 2.5,
                          "response": {"orderID": "0xoid"}})
    assert "✅" in b._send.call_args[0][0]

    # cancelled -> warning template
    await b.notify_trade({"status": "cancelled", "reason": "below_min_order", "size": 1.0})
    assert "⚠️" in b._send.call_args[0][0]
    print("OK  notify_trade routes submitted->✅ and cancelled->⚠️")


def test_address_validator():
    assert is_valid_address("0x" + "A" * 40)
    assert not is_valid_address("0x123")
    assert not is_valid_address("")
    assert not is_valid_address("zz" + "a" * 40)
    print("OK  address validator")


async def _run_async_tests():
    await test_boots_paused()
    await test_unauthorized_ignored()
    await test_arm_and_kill()
    await test_double_lock_dry_run()
    await test_set_target_valid()
    await test_set_target_multi()
    await test_set_target_invalid()
    await test_set_target_unauthorized()
    await test_status_text_and_balance_provider()
    await test_reset_command()
    await test_notify_dispatch()


def main():
    test_address_validator()
    test_templates()
    asyncio.run(_run_async_tests())
    print("\nAll bot.py tests passed.")


if __name__ == "__main__":
    main()
