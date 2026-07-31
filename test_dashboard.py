"""Tests for paper.py ledger/PnL and dashboard.py HTTP endpoints. No network."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PRIVATE_KEY", "0x" + "1" * 64)
os.environ.setdefault("PROXY_WALLET_ADDRESS", "0xProxy")
os.environ.setdefault("RPC_URL", "https://polygon-rpc.com")
os.environ.setdefault("TELEGRAM_TOKEN", "123456:ABCDEF")
os.environ.setdefault("TELEGRAM_ADMIN_ID", "999")
os.environ.setdefault("TARGET_WALLET", "0x" + "a" * 40)
os.environ.setdefault("DRY_RUN", "true")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from dashboard import Dashboard  # noqa: E402
from paper import PaperLedger  # noqa: E402


@dataclass
class Trade:
    token_id: str = "TOK1"
    price: float = 0.40
    size: float = 12.0
    slug: str = "market-x"
    condition_id: str = "0xc"
    outcome: str = "Yes"


def _tmp_path() -> str:
    p = os.path.join(tempfile.gettempdir(), "paper_test.jsonl")
    if os.path.exists(p):
        os.remove(p)
    return p


def test_record_and_load():
    led = PaperLedger(_tmp_path())
    pos = led.record_buy(Trade())
    assert pos["size"] == 2.5             # round(1/0.40, 2)
    assert abs(pos["cost_usdc"] - 1.0) < 1e-6   # 2.5 * 0.40 = 1.0
    rows = led.load()
    assert len(rows) == 1 and rows[0]["token_id"] == "TOK1"
    print("OK  record_buy computes size/cost and persists")


async def test_compute_pnl():
    led = PaperLedger(_tmp_path())
    led.record_buy(Trade(token_id="A", price=0.50, slug="m-a"))  # size 2.0, cost 1.0
    led.record_buy(Trade(token_id="B", price=0.20, slug="m-b"))  # size 5.0, cost 1.0

    async def fake_mid(token_ids):
        return {"A": 0.60, "B": 0.10}  # A up, B down

    with patch.object(led, "_fetch_midpoints", side_effect=fake_mid):
        data = await led.compute_pnl()

    by_tok = {r["token_id"]: r for r in data["rows"]}
    assert abs(by_tok["A"]["pnl"] - 0.20) < 1e-6   # 2.0*0.60 - 1.0 = +0.20
    assert abs(by_tok["B"]["pnl"] + 0.50) < 1e-6   # 5.0*0.10 - 1.0 = -0.50
    t = data["totals"]
    assert t["count"] == 2
    assert abs(t["cost"] - 2.0) < 1e-6
    assert abs(t["pnl"] + 0.30) < 1e-6             # +0.20 - 0.50 = -0.30
    print("OK  compute_pnl marks to market (A +0.20, B -0.50, total -0.30)")


async def test_live_and_mode_split():
    led = PaperLedger(_tmp_path())
    led.record_buy(Trade(token_id="A", price=0.50), mode="paper")        # size 2.0, cost 1.0
    led.record_buy(Trade(token_id="B", price=0.50), mode="live",
                   size=3.0, tx="0xLIVE")                                 # size 3.0, cost 1.5

    async def fake_mid(token_ids):
        return {"A": 0.50, "B": 0.60}

    with patch.object(led, "_fetch_midpoints", side_effect=fake_mid):
        data = await led.compute_pnl()

    rows = {r["token_id"]: r for r in data["rows"]}
    assert rows["B"]["mode"] == "live" and rows["B"]["tx"] == "0xLIVE"
    assert rows["B"]["size"] == 3.0, "live size override respected"
    modes = data["modes"]
    assert modes["paper"]["count"] == 1 and modes["live"]["count"] == 1
    # live: 3.0*0.60 - 1.5 = +0.30
    assert abs(modes["live"]["pnl"] - 0.30) < 1e-6
    print("OK  live fills recorded + paper/live mode split")


async def test_pnl_missing_price():
    led = PaperLedger(_tmp_path())
    led.record_buy(Trade(token_id="X", price=0.50))

    async def fake_mid(token_ids):
        return {"X": None}  # price fetch failed

    with patch.object(led, "_fetch_midpoints", side_effect=fake_mid):
        data = await led.compute_pnl()
    assert data["rows"][0]["pnl"] is None
    assert data["totals"]["value"] == 0.0
    print("OK  compute_pnl handles missing prices gracefully")


async def test_dashboard_routes():
    led = PaperLedger(_tmp_path())
    led.record_buy(Trade(token_id="A", price=0.50))
    dash = Dashboard(led, token="secret")

    async def fake_mid(token_ids):
        return {"A": 0.55}

    app_client = TestClient(TestServer(_build_app(dash)))
    await app_client.start_server()
    try:
        # index requires token
        r = await app_client.get("/")
        assert r.status == 401
        r = await app_client.get("/?key=secret")
        assert r.status == 200 and "Paper PnL" in await r.text()

        # api requires token; returns PnL json
        r = await app_client.get("/api/pnl")
        assert r.status == 401
        with patch.object(led, "_fetch_midpoints", side_effect=fake_mid):
            r = await app_client.get("/api/pnl?key=secret")
            assert r.status == 200
            j = await r.json()
            assert j["totals"]["count"] == 1
            assert abs(j["rows"][0]["pnl"] - 0.10) < 1e-6  # 2.0*0.55-1.0
    finally:
        await app_client.close()
    print("OK  dashboard: token gate + / and /api/pnl work")


def _build_app(dash: Dashboard):
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/", dash._index)
    app.router.add_get("/api/pnl", dash._api)
    return app


async def _run_async():
    await test_compute_pnl()
    await test_live_and_mode_split()
    await test_pnl_missing_price()
    await test_dashboard_routes()


def main():
    test_record_and_load()
    asyncio.run(_run_async())
    print("\nAll paper/dashboard tests passed.")


if __name__ == "__main__":
    main()
