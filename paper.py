"""Paper-trade ledger + PnL calculation for the clone trading bot.

In DRY_RUN mode the bot doesn't place real orders, but we still want to know
"what if?" — so every simulated 1 USDC BUY is recorded here as an open paper
position. Unrealized PnL is marked to market against Polymarket's public
midpoint price endpoint (no auth / private key required).

This is intentionally simple: each paper BUY is one open position of ~1 USDC.
Realized PnL (whale exits) is not modelled yet — see [[clone-trader-plan]] F5.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

import httpx

# Public CLOB endpoint — returns {"mid": "0.52"} for a token, no auth needed.
CLOB_MIDPOINT_URL = "https://clob.polymarket.com/midpoint"


@dataclass
class PaperPosition:
    ts: int              # ms epoch when recorded
    token_id: str
    market: str          # slug or condition id, for display
    outcome: str
    side: str            # "BUY"
    entry_price: float   # whale's fill price at detection
    size: float          # shares = round(1 / entry_price, 2)
    cost_usdc: float     # size * entry_price (~1.00)


class PaperLedger:
    """Append-only JSONL ledger of simulated positions + mark-to-market PnL."""

    def __init__(self, path: str = "paper_trades.jsonl") -> None:
        self._path = path

    # -- writes --------------------------------------------------------------

    def record_buy(self, trade: Any) -> dict:
        """Record a simulated 1 USDC BUY from a detected whale trade."""
        price = float(getattr(trade, "price", 0.0) or 0.0)
        size = round(1.0 / price, 2) if price > 0 else 0.0
        pos = PaperPosition(
            ts=int(time.time() * 1000),
            token_id=str(getattr(trade, "token_id", "")),
            market=str(getattr(trade, "slug", "") or getattr(trade, "condition_id", "")),
            outcome=str(getattr(trade, "outcome", "")),
            side="BUY",
            entry_price=price,
            size=size,
            cost_usdc=round(size * price, 4),
        )
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(pos), separators=(",", ":")) + "\n")
        except OSError:
            pass
        return asdict(pos)

    # -- reads ---------------------------------------------------------------

    def load(self) -> list[dict]:
        """Read all recorded paper positions (best-effort)."""
        if not os.path.exists(self._path):
            return []
        rows: list[dict] = []
        try:
            with open(self._path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return rows

    # -- PnL -----------------------------------------------------------------

    async def compute_pnl(self) -> dict:
        """Mark every open position to market and return a PnL summary."""
        positions = self.load()
        token_ids = sorted({p.get("token_id", "") for p in positions if p.get("token_id")})
        prices = await self._fetch_midpoints(token_ids)

        rows: list[dict] = []
        total_cost = 0.0
        total_value = 0.0
        for p in positions:
            cur = prices.get(p.get("token_id", ""))
            size = float(p.get("size", 0.0))
            cost = float(p.get("cost_usdc", 0.0))
            if cur is not None:
                value = round(size * cur, 4)
                pnl = round(value - cost, 4)
                total_value += value
            else:
                value = None
                pnl = None
            total_cost += cost
            rows.append({**p, "current_price": cur, "current_value": value, "pnl": pnl})

        total_pnl = round(total_value - total_cost, 4)
        return {
            "rows": rows,
            "totals": {
                "count": len(positions),
                "cost": round(total_cost, 4),
                "value": round(total_value, 4),
                "pnl": total_pnl,
                "pnl_pct": round((total_pnl / total_cost * 100.0) if total_cost else 0.0, 2),
            },
            "generated_at": int(time.time() * 1000),
        }

    async def _fetch_midpoints(self, token_ids: list[str]) -> dict[str, Optional[float]]:
        """Fetch current midpoint price for each token (None on failure)."""
        out: dict[str, Optional[float]] = {}
        if not token_ids:
            return out
        async with httpx.AsyncClient(timeout=10.0) as client:
            for tid in token_ids:
                try:
                    resp = await client.get(CLOB_MIDPOINT_URL, params={"token_id": tid})
                    resp.raise_for_status()
                    data = resp.json()
                    out[tid] = float(data.get("mid"))
                except Exception:  # noqa: BLE001 - price fetch is best-effort
                    out[tid] = None
        return out
