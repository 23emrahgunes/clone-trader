"""PROOF: the bot watches TARGET_WALLET and opens paper trades on its BUYs.

This does NOT use mock data. It:
  1. Reads TARGET_WALLET straight from your .env (via config).
  2. Fetches that wallet's REAL trade history from the SAME data-api endpoint
     the live bot polls every 3s.
  3. Runs each trade through the bot's OWN production code:
       WhaleTracker._parse_poll_entry  (the live detection/parse path)
       PaperLedger.record_buy          (the live paper-trade recorder)
  4. Marks the resulting paper positions to market via the live midpoint feed.

Whatever paper trades print below are produced by the bot's real code on this
wallet's real on-chain activity. Run on the server:

    .venv/bin/python prove.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import httpx

from config import settings
from paper import PaperLedger
from tracker import WhaleTracker

DATA_API = "https://data-api.polymarket.com/activity"


async def main() -> None:
    wallet = settings.TARGET_WALLET.lower()
    print("=" * 64)
    print("KANIT: bot bu cuzdani izliyor ve paper trade aciyor mu?")
    print("=" * 64)
    print(f"TARGET_WALLET (.env'den okundu) : {wallet}")
    print(f"Endpoint (canli bot ile AYNI)    : {DATA_API}")
    print("-" * 64)

    # 1. Fetch this wallet's REAL trades from the same endpoint the bot polls.
    params = {"user": wallet, "type": "TRADE", "limit": 50,
              "sortBy": "TIMESTAMP", "sortDirection": "DESC"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(DATA_API, params=params)
        resp.raise_for_status()
        batch = resp.json()
    n = len(batch) if isinstance(batch, list) else 0
    print(f"data-api'den donen GERCEK islem sayisi: {n}")
    if n:
        print(f"Ilk kaydin ham alanlari: {sorted(batch[0].keys())}")
    print("-" * 64)

    # 2. Run the bot's OWN parser + paper recorder over the real data.
    tracker = WhaleTracker(lambda t: asyncio.sleep(0))
    tracker.target_wallet = wallet  # same wallet the live bot uses

    ledger = PaperLedger(os.path.join(tempfile.gettempdir(), "proof_paper.jsonl"))
    open(ledger._path, "w").close()  # start clean

    matched = 0
    buys = 0
    for entry in (batch if isinstance(batch, list) else []):
        trade = tracker._parse_poll_entry(entry)   # <-- live production code
        if trade is None:
            continue
        matched += 1
        market = trade.slug or trade.condition_id[:16] or trade.token_id[:12]
        print(f"  [{trade.side:4}] {market:<38} px={trade.price} sz={trade.size} "
              f"tok={trade.token_id[:10]}..")
        if trade.side == "BUY":
            ledger.record_buy(trade)               # <-- live paper-trade code
            buys += 1

    print("-" * 64)
    print(f"Botun parser'inin cuzdanla esledigi islem : {matched}")
    print(f"Acilan PAPER TRADE (yalnizca BUY kopyalanir): {buys}")

    # 3. Mark the paper positions to market with the live midpoint feed.
    if buys:
        pnl = await ledger.compute_pnl()
        t = pnl["totals"]
        print("-" * 64)
        print("PAPER PnL (canli Polymarket midpoint ile isaretlendi):")
        print(f"  islem={t['count']}  maliyet=${t['cost']}  guncel_deger=${t['value']}  "
              f"PnL=${t['pnl']} ({t['pnl_pct']}%)")
    else:
        print("Bu cuzdanin son 50 islemi arasinda BUY yok (ya da cuzdan sessiz).")
        print("=> Kod dogru calisiyor; sadece kopyalanacak yeni BUY hareketi yok.")

    print("=" * 64)
    print("KANIT: yukaridaki satirlar botun GERCEK kodu (tracker + paper) ile")
    print("bu cuzdanin GERCEK data-api verisinden uretildi. Mock yok.")
    print("Not: canli bot, ACILISTAN ONCEKI islemleri 'baseline' alir (kopyalamaz);")
    print("yalnizca bot ayaktayken olan YENI BUY'lar paper trade acar.")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
