"""Polymarket CLOB execution module for the clone trading bot.

Owns a single, long-lived (hot) ``ClobClient`` and exposes a copy-execution
primitive that mirrors a whale's BUY with a fixed 1.00 USDC allocation while
enforcing the safety rules from .skills:

  1. FIXED ALLOCATION   -> every buy spends exactly 1.00 USDC.
  2. SLIPPAGE PROTECTION -> abort if the live ask is > 3% above target price.
  3. MINIMUM ORDER SIZE -> guarantee size * price >= 1.00 USDC.
  4. ORDER TYPE         -> Fill-or-Kill (FOK); no hanging limit orders.

py-clob-client is synchronous, so every blocking network call is offloaded to a
worker thread via ``asyncio.to_thread`` to keep the asyncio event loop responsive
(rule 5: asynchronous, non-blocking pattern).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY

from config import settings

logger = logging.getLogger(__name__)

# Tick sizes returned by get_tick_size are strings like "0.01"; used to round the
# protection price so the exchange accepts it.
_DEFAULT_TICK = 0.01


class ExecutionError(RuntimeError):
    """Raised when an order cannot be built, priced, or accepted."""


class PolymarketTrader:
    """Thin async wrapper around a single keep-alive ``ClobClient``."""

    def __init__(self) -> None:
        # Build the client once with the signing key + proxy (funder) from config.
        # If explicit L2 API credentials are supplied in the env, pass them straight
        # to the constructor; otherwise the client starts credential-less and
        # derive_api_key() is called lazily in connect().
        preset_creds: Optional[ApiCreds] = None
        if settings.has_clob_creds:
            preset_creds = ApiCreds(
                api_key=settings.CLOB_API_KEY,
                api_secret=settings.CLOB_API_SECRET,
                api_passphrase=settings.CLOB_API_PASSPHRASE,
            )

        self._client: ClobClient = ClobClient(
            host=settings.CLOB_HOST,
            chain_id=settings.CHAIN_ID,
            key=settings.PRIVATE_KEY,
            creds=preset_creds,                      # None -> derived in connect()
            signature_type=settings.SIGNATURE_TYPE,  # from .env (default 3)
            funder=settings.PROXY_WALLET_ADDRESS,
        )
        # Already authenticated at construction time when creds came from the env.
        self._connected: bool = preset_creds is not None

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        """Ensure L2 API credentials are attached (idempotent). Must run before trading.

        No-op when credentials were injected from the env at construction time;
        otherwise derives them from the private key via derive_api_key().
        """
        if self._connected:
            return
        try:
            creds: ApiCreds = await asyncio.to_thread(self._client.derive_api_key)
            self._client.set_api_creds(creds)
            self._connected = True
            logger.info("ClobClient connected via derived creds (funder=%s, sig_type=%s)",
                        settings.PROXY_WALLET_ADDRESS, settings.SIGNATURE_TYPE)
        except Exception as exc:  # noqa: BLE001 - surface any auth/network failure
            logger.exception("Failed to derive/set API credentials")
            raise ExecutionError(f"CLOB authentication failed: {exc}") from exc

    # -- account -------------------------------------------------------------

    async def get_usdc_balance(self) -> Optional[float]:
        """Return the proxy wallet's USDC.e collateral balance, or None on failure.

        Best-effort: used for the /status readout, so it never raises.
        """
        try:
            if not self._connected:
                await self.connect()
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=settings.SIGNATURE_TYPE,
            )
            resp = await asyncio.to_thread(self._client.get_balance_allowance, params)
        except Exception:  # noqa: BLE001 - status readout must not crash
            logger.exception("Failed to fetch USDC balance")
            return None

        raw = resp.get("balance") if isinstance(resp, dict) else resp
        try:
            # USDC.e has 6 decimals; the API returns base units as a string.
            return float(raw) / 1_000_000.0
        except (TypeError, ValueError):
            return None

    # -- pricing helpers -----------------------------------------------------

    async def _get_best_ask(self, token_id: str) -> float:
        """Return the current best ask (the price a BUY would pay) for ``token_id``."""
        try:
            resp: Any = await asyncio.to_thread(self._client.get_price, token_id, BUY)
        except Exception as exc:  # noqa: BLE001
            logger.exception("get_price failed for token %s", token_id)
            raise ExecutionError(f"Could not fetch market price for {token_id}: {exc}") from exc

        # get_price returns {"price": "0.52"} (or a bare value depending on version).
        raw = resp.get("price") if isinstance(resp, dict) else resp
        try:
            price = float(raw)
        except (TypeError, ValueError) as exc:
            raise ExecutionError(f"Malformed price response for {token_id}: {resp!r}") from exc
        if price <= 0.0:
            raise ExecutionError(f"Non-positive market price for {token_id}: {price}")
        return price

    async def _get_tick_size(self, token_id: str) -> float:
        """Return the market tick size, defaulting to 0.01 on any failure."""
        try:
            tick = await asyncio.to_thread(self._client.get_tick_size, token_id)
            return float(tick)
        except Exception:  # noqa: BLE001 - non-fatal; fall back to default tick
            logger.warning("get_tick_size failed for %s; defaulting to %s", token_id, _DEFAULT_TICK)
            return _DEFAULT_TICK

    # -- public API ----------------------------------------------------------

    async def execute_1usd_buy(self, token_id: str, target_price: float) -> dict:
        """Copy a whale BUY on ``token_id`` with a fixed 1.00 USDC allocation.

        Enforces the .skills business rules and submits a Fill-or-Kill market
        order with an explicit price cap for slippage protection.

        Returns a status dict. ``{"status": "cancelled", ...}`` when a safety
        rule blocks the trade; ``{"status": "submitted", ...}`` on acceptance.
        """
        if not self._connected:
            await self.connect()

        # --- validate inputs ---
        if target_price <= 0.0 or target_price >= 1.0:
            return {
                "status": "cancelled",
                "reason": "invalid_target_price",
                "detail": f"target_price must be in (0, 1); got {target_price}",
                "token_id": token_id,
            }

        # --- Rule 1: FIXED ALLOCATION -> shares that spend ~1.00 USDC at target ---
        allocation = settings.FIXED_ALLOCATION_USDC  # 1.00 USDC
        size = round(allocation / target_price, 2)

        # --- Rule 2: SLIPPAGE PROTECTION (up-front check on the live ask) ---
        best_ask = await self._get_best_ask(token_id)
        max_price = target_price * (1.0 + settings.MAX_SLIPPAGE_PCT / 100.0)
        if best_ask > max_price:
            slippage_pct = (best_ask - target_price) / target_price * 100.0
            logger.warning(
                "Slippage guard tripped for %s: ask=%.4f target=%.4f (%.2f%% > %.2f%%)",
                token_id, best_ask, target_price, slippage_pct, settings.MAX_SLIPPAGE_PCT,
            )
            return {
                "status": "cancelled",
                "reason": "slippage_exceeded",
                "token_id": token_id,
                "target_price": target_price,
                "market_price": best_ask,
                "slippage_pct": round(slippage_pct, 2),
                "max_slippage_pct": settings.MAX_SLIPPAGE_PCT,
            }

        # --- Rule 3: MINIMUM ORDER SAFETY -> size * price >= 1.00 USDC ---
        # Value if filled at the worst acceptable price (the cap). If the rounded
        # size would fall below the minimum, bump it to satisfy the exchange rule.
        if size * max_price < settings.MIN_ORDER_USDC:
            size = round(settings.MIN_ORDER_USDC / max_price + 0.005, 2)
        if size * max_price < settings.MIN_ORDER_USDC:
            return {
                "status": "cancelled",
                "reason": "below_min_order",
                "token_id": token_id,
                "size": size,
                "price_cap": max_price,
                "min_order_usdc": settings.MIN_ORDER_USDC,
            }

        # --- Rule 4: FOK market order with price cap for slippage protection ---
        # Round the cap down to the tick grid so we never exceed the 3% ceiling.
        tick = await self._get_tick_size(token_id)
        price_cap = max(tick, (int(max_price / tick)) * tick)
        price_cap = round(price_cap, 4)

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=allocation,      # BUY market order: amount is USDC to spend (fixed 1.00)
            side=BUY,
            price=price_cap,        # protection cap; fill must be at or below this
            order_type=OrderType.FOK,
        )

        try:
            signed = await asyncio.to_thread(self._client.create_market_order, order_args)
            resp = await asyncio.to_thread(self._client.post_order, signed, OrderType.FOK)
        except Exception as exc:  # noqa: BLE001 - report, never crash the tracker loop
            logger.exception("Order submission failed for %s", token_id)
            return {
                "status": "error",
                "reason": "submission_failed",
                "token_id": token_id,
                "detail": str(exc),
            }

        accepted = bool(resp.get("success", False)) if isinstance(resp, dict) else True
        logger.info(
            "FOK buy %s: size=%.2f cap=%.4f accepted=%s resp=%s",
            token_id, size, price_cap, accepted, resp,
        )
        return {
            "status": "submitted" if accepted else "rejected",
            "token_id": token_id,
            "side": BUY,
            "allocation_usdc": allocation,
            "size": size,
            "target_price": target_price,
            "price_cap": price_cap,
            "order_type": "FOK",
            "response": resp,
        }
