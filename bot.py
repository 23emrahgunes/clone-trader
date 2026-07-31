"""Telegram control + notification interface for the clone trading bot.

Provides the human-in-the-loop safety layer:

  * ADMIN-ONLY: every command is ignored unless it comes from
    ``settings.TELEGRAM_ADMIN_ID`` (silent drop for anyone else).
  * ARM / KILL double-lock: the bot boots PAUSED (``armed = False``) so trades
    are tracked but NOT sent. ``/arm`` enables live sending; ``/kill`` and
    ``/pause`` disable it instantly. Combined with ``DRY_RUN`` this is the
    dry->live double-lock: real orders require ``armed and not dry_run``.
  * Runtime target switch via ``/set_target <0x...>``.
  * Push notifications for whale detections, executed trades, and cancellations.

The shared ``BotState`` is the single source of truth that the orchestrator
(main.py) reads before routing a detected trade to the executor.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from config import settings

logger = logging.getLogger(__name__)

# Ethereum/Polygon address shape: 0x + 40 hex chars.
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Optional async providers injected by main.py.
BalanceProvider = Callable[[], Awaitable[Optional[float]]]
# Called with the full, updated list of target wallets after any change.
TargetChangeHook = Callable[[list], Awaitable[None]]


@dataclass
class BotState:
    """Shared, mutable runtime state — the authority for the execution gate."""

    armed: bool = False                          # ARM/KILL lock; boots False (PAUSED)
    dry_run: bool = True                         # paper switch from settings.DRY_RUN
    target_wallets: list = field(default_factory=list)  # copy targets; runtime-mutable

    @property
    def mode(self) -> str:
        """Human-readable arm state."""
        return "ARMED" if self.armed else "PAUSED"

    @property
    def can_execute_live(self) -> bool:
        """True only when BOTH locks permit a real order (armed and live)."""
        return self.armed and not self.dry_run


def is_valid_address(addr: str) -> bool:
    """True if ``addr`` is a well-formed 0x-prefixed 40-hex address."""
    return bool(_ADDRESS_RE.match(addr.strip())) if addr else False


class TelegramBot:
    """python-telegram-bot (v20+) async control surface."""

    def __init__(
        self,
        *,
        state: Optional[BotState] = None,
        balance_provider: Optional[BalanceProvider] = None,
        on_targets_changed: Optional[TargetChangeHook] = None,
        application: Optional[Application] = None,
    ) -> None:
        self.state = state or BotState(
            armed=False,                       # always boot PAUSED (rule 3)
            dry_run=settings.DRY_RUN,
            target_wallets=list(settings.target_wallet_list),
        )
        self._admin_id = settings.TELEGRAM_ADMIN_ID
        # Where notifications go; falls back to the admin's own chat.
        self._chat_id = settings.TELEGRAM_CHAT_ID or str(self._admin_id)
        self._balance_provider = balance_provider
        self._on_targets_changed = on_targets_changed

        # Allow injection of a prebuilt Application (used by unit tests).
        self.app: Application = application or ApplicationBuilder().token(settings.TELEGRAM_TOKEN).build()
        self._register_handlers()

    # -- setup ---------------------------------------------------------------

    def _register_handlers(self) -> None:
        self.app.add_handler(CommandHandler(["start", "status"], self._cmd_status))
        self.app.add_handler(CommandHandler("arm", self._cmd_arm))
        self.app.add_handler(CommandHandler(["kill", "pause"], self._cmd_kill))
        self.app.add_handler(CommandHandler("set_target", self._cmd_set_target))
        self.app.add_handler(CommandHandler(["add_target", "add"], self._cmd_add_target))
        self.app.add_handler(CommandHandler(["remove_target", "remove", "rm"], self._cmd_remove_target))
        self.app.add_handler(CommandHandler(["targets", "list"], self._cmd_targets))

    def _is_admin(self, update: Update) -> bool:
        """True only for the configured admin user; everyone else is ignored."""
        user = update.effective_user
        if user is None or user.id != self._admin_id:
            logger.warning("Ignoring command from unauthorized user: %s",
                           getattr(user, "id", "unknown"))
            return False
        return True

    # -- command handlers ----------------------------------------------------

    async def _cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        balance = await self._safe_balance()
        await update.message.reply_text(self._build_status_text(balance))

    async def _cmd_arm(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        self.state.armed = True
        logger.info("Bot ARMED by admin")
        live = "CANLI" if not self.state.dry_run else "DRY_RUN (kağıt)"
        await update.message.reply_text(
            f"🟢 ARMED — emir gönderimi AKTİF ({live}).\n"
            f"Takip edilen cüzdan: {len(self.state.target_wallets)}"
        )

    async def _cmd_kill(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        self.state.armed = False
        logger.info("Bot PAUSED (kill/pause) by admin")
        await update.message.reply_text(
            "🔴 PAUSED — emir gönderimi DURDU. Sadece izleme/loglama aktif."
        )

    async def _sync_targets(self) -> None:
        """Push the current target list to the tracker via the injected hook."""
        if self._on_targets_changed is not None:
            try:
                await self._on_targets_changed(list(self.state.target_wallets))
            except Exception:  # noqa: BLE001 - hook failure must not break the command
                logger.exception("on_targets_changed hook failed")

    def _valid_args(self, ctx: ContextTypes.DEFAULT_TYPE) -> tuple:
        """Split command args into (valid_lower_addresses, invalid_tokens)."""
        valid, invalid = [], []
        for a in (ctx.args or []):
            a = a.strip()
            (valid if is_valid_address(a) else invalid).append(a.lower() if is_valid_address(a) else a)
        return valid, invalid

    async def _cmd_set_target(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Replace the WHOLE target list with the given address(es)."""
        if not self._is_admin(update):
            return
        valid, invalid = self._valid_args(ctx)
        if not valid:
            await update.message.reply_text("Kullanım: /set_target <0x...> [<0x...> ...]")
            return
        self.state.target_wallets = list(dict.fromkeys(valid))  # dedupe, keep order
        await self._sync_targets()
        msg = "🎯 Hedef listesi güncellendi (%d cüzdan):\n%s" % (
            len(self.state.target_wallets), "\n".join(self.state.target_wallets))
        if invalid:
            msg += "\n⚠️ Geçersiz atlandı: " + ", ".join(invalid)
        await update.message.reply_text(msg)

    async def _cmd_add_target(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Add one or more wallets to the target list."""
        if not self._is_admin(update):
            return
        valid, invalid = self._valid_args(ctx)
        if not valid:
            await update.message.reply_text("Kullanım: /add_target <0x...> [<0x...> ...]")
            return
        added = []
        for w in valid:
            if w not in self.state.target_wallets:
                self.state.target_wallets.append(w)
                added.append(w)
        if added:
            await self._sync_targets()
        msg = ("➕ Eklendi (%d):\n%s" % (len(added), "\n".join(added))) if added else "Zaten ekli."
        if invalid:
            msg += "\n⚠️ Geçersiz atlandı: " + ", ".join(invalid)
        msg += f"\n\nToplam takip: {len(self.state.target_wallets)}"
        await update.message.reply_text(msg)

    async def _cmd_remove_target(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Remove one or more wallets from the target list."""
        if not self._is_admin(update):
            return
        valid, _ = self._valid_args(ctx)
        if not valid:
            await update.message.reply_text("Kullanım: /remove_target <0x...>")
            return
        removed = []
        for w in valid:
            if w in self.state.target_wallets:
                self.state.target_wallets.remove(w)
                removed.append(w)
        if removed:
            await self._sync_targets()
        msg = ("➖ Çıkarıldı (%d):\n%s" % (len(removed), "\n".join(removed))) if removed \
            else "Listede yoktu."
        msg += f"\n\nKalan takip: {len(self.state.target_wallets)}"
        await update.message.reply_text(msg)

    async def _cmd_targets(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """List the wallets currently being mirrored."""
        if not self._is_admin(update):
            return
        wallets = self.state.target_wallets
        if not wallets:
            await update.message.reply_text("Takip edilen cüzdan yok. /add_target <0x...> ile ekle.")
            return
        lines = "\n".join(f"{i+1}. {w}" for i, w in enumerate(wallets))
        await update.message.reply_text(f"🎯 Takip edilen {len(wallets)} cüzdan:\n{lines}")

    # -- notifications (called by tracker/trader callbacks) ------------------

    async def notify_whale(self, trade: Any) -> None:
        """Push a '🎯 whale detected' message."""
        await self._send(self.format_whale(trade))

    async def notify_trade(self, result: dict) -> None:
        """Push either a '✅ trade sent' or '⚠️ cancelled' message per result status."""
        status = str(result.get("status", "")).lower()
        if status == "submitted":
            await self._send(self.format_trade_result(result))
        else:
            await self._send(self.format_cancelled(result))

    async def _send(self, text: str) -> None:
        """Send a message to the admin chat (best-effort; never raises)."""
        try:
            await self.app.bot.send_message(chat_id=self._chat_id, text=text)
        except Exception:  # noqa: BLE001 - notification failure must not break flow
            logger.exception("Failed to send Telegram notification")

    # -- message templates (pure, unit-testable) -----------------------------

    @staticmethod
    def format_whale(trade: Any) -> str:
        market = getattr(trade, "slug", "") or getattr(trade, "condition_id", "") \
            or getattr(trade, "token_id", "?")
        side = getattr(trade, "side", "?")
        price = getattr(trade, "price", 0.0)
        size = getattr(trade, "size", 0.0)
        return (
            "🎯 Balina Hareketi Yakalandı!\n"
            f"Pazar: {market}\n"
            f"Yön: {side}\n"
            f"Fiyat: {price}\n"
            f"Tutar: {size}"
        )

    @staticmethod
    def format_trade_result(result: dict) -> str:
        price = result.get("price_cap", result.get("target_price", "?"))
        size = result.get("size", "?")
        tx = TelegramBot._extract_tx(result)
        return (
            "✅ 1 USDC'lik Clone Trade Atıldı! "
            f"| Fiyat: {price} | Size: {size} | Tx: {tx}"
        )

    @staticmethod
    def format_cancelled(result: Any) -> str:
        if isinstance(result, str):
            return f"⚠️ İşlem İptal Edildi: {result}"

        reason = str(result.get("reason", "bilinmeyen"))
        if reason == "slippage_exceeded":
            detail = (f"Slippage %{result.get('slippage_pct')} > "
                      f"%{result.get('max_slippage_pct')} "
                      f"(hedef {result.get('target_price')}, piyasa {result.get('market_price')})")
        elif reason == "below_min_order":
            detail = f"Minimum emir altında (size {result.get('size')})"
        elif reason == "invalid_target_price":
            detail = result.get("detail", "geçersiz hedef fiyat")
        elif reason in ("submission_failed", "error"):
            detail = f"Emir hatası: {result.get('detail', '?')}"
        else:
            detail = result.get("detail", reason)
        return f"⚠️ İşlem İptal Edildi: {detail}"

    @staticmethod
    def _extract_tx(result: dict) -> str:
        resp = result.get("response")
        if isinstance(resp, dict):
            tx = resp.get("transactionHash") or resp.get("transactionsHashes") \
                or resp.get("orderID") or resp.get("orderId")
            if tx:
                return str(tx)
        return "-"

    def _build_status_text(self, balance: Optional[float]) -> str:
        bal = f"{balance:.2f} USDC" if isinstance(balance, (int, float)) else "N/A"
        dry = "AÇIK (kağıt)" if self.state.dry_run else "KAPALI (canlı)"
        wallets = self.state.target_wallets
        if len(wallets) <= 1:
            targets = wallets[0] if wallets else "(yok)"
        else:
            targets = f"{len(wallets)} cüzdan (/targets ile listele)"
        return (
            "📊 Clone Trader Durumu\n"
            f"Hedefler     : {targets}\n"
            f"Durum        : {self.state.mode}\n"
            f"USDC Bakiye  : {bal}\n"
            f"DRY_RUN      : {dry}\n"
            f"Canlı emir   : {'EVET' if self.state.can_execute_live else 'HAYIR'}"
        )

    async def _safe_balance(self) -> Optional[float]:
        """Fetch balance via the injected provider; None on absence/failure."""
        if self._balance_provider is None:
            return None
        try:
            return await self._balance_provider()
        except Exception:  # noqa: BLE001
            logger.exception("Balance provider failed")
            return None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Initialize and start long-polling (non-blocking)."""
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Telegram bot started (admin=%s, boot=PAUSED)", self._admin_id)

    async def stop(self) -> None:
        """Gracefully stop polling and shut down."""
        try:
            if self.app.updater:
                await self.app.updater.stop()
            await self.app.stop()
        finally:
            await self.app.shutdown()
        logger.info("Telegram bot stopped")
