"""Signal-Trader: Polymarket 5dk BTC up/down icin bilgi/latency yonlu bot.

FIKIR (uydurma degil, ilkeli):
  Pencere kapaninca "Up" kazanir eger BTC kapanis > acilis (price-to-beat). Herhangi bir
  t aninda:  d = anlik_fiyat - price_to_beat,  tau = kapanisa kalan sn,  sigma = anlik
  oynaklik.  Gelecek getiri ~ N(0, sigma^2 * tau) varsayimiyla (Bachelier / aritmetik
  Brown hareketi, kisa vade):
        P(Up kazanir) = Phi( d / (sigma * sqrt(tau)) )
  Market'in ima ettigi fiyatla (YES/NO ask) karsilastir. FEE dusulmus beklenen deger
  (EV) esigi asarsa ucuz tarafi al.

KANIT:  Bot her sinyali ve SONUCUNU (tahmin tuttu mu) kaydeder. Paper (kagit) modda gercek
  isabet orani + PnL birikir. ONCE bunu gor; pozitifse dashboard'daki tek tikla CANLIYA gec.

Mod:
  - Varsayilan PAPER (kagit) -- gercek emir YOK, sadece simule + kayit.
  - Dashboard'daki "CANLIYA GEC" (ARM) butonu ile canli emir acilir. "DURDUR" ile kapanir.
  - Paper kaydi canlida da devam eder (sicil hep buyur).

READ/WRITE: canli ARM edilmedikce hicbir gercek emir gitmez.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import requests
import websockets
from dotenv import load_dotenv

load_dotenv()
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

logger = logging.getLogger("signal-trader")

# =====================================================================================
# Konfigurasyon
# =====================================================================================

def _req(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name)
    if v and v.strip():
        return v.strip()
    if default is not None:
        return default
    raise RuntimeError(f"Zorunlu ortam degiskeni eksik: {name}")


def _f(name: str, d: float) -> float:
    v = os.getenv(name)
    try:
        return float(v) if v and v.strip() else d
    except ValueError:
        return d


def _i(name: str, d: int) -> int:
    v = os.getenv(name)
    try:
        return int(v) if v and v.strip() else d
    except ValueError:
        return d


def _b(name: str, d: bool) -> bool:
    v = os.getenv(name)
    if not v or not v.strip():
        return d
    return v.strip().lower() in ("1", "true", "yes", "on", "y", "evet")


@dataclass(frozen=True)
class Settings:
    PRIVATE_KEY: str = field(repr=False)
    PROXY_WALLET_ADDRESS: str = ""
    SIGNATURE_TYPE: int = 3
    CHAIN_ID: int = 137
    CLOB_HOST: str = "https://clob.polymarket.com"
    GAMMA_HOST: str = "https://gamma-api.polymarket.com"
    RTDS_WS_URL: str = "wss://ws-live-data.polymarket.com"
    CLOB_API_KEY: Optional[str] = None
    CLOB_API_SECRET: Optional[str] = field(default=None, repr=False)
    CLOB_API_PASSPHRASE: Optional[str] = field(default=None, repr=False)

    ASSET: str = "btc"
    TIMEFRAME_MIN: int = 5

    # sinyal parametreleri
    EDGE_MARGIN: float = 0.05        # fee dusulmus min EV (USDC/share) -> sinyal esigi
    VOL_WINDOW_SEC: float = 90.0     # oynaklik tahmin penceresi
    MIN_TICKS: int = 20              # oynaklik icin min tick
    MIN_SEC_TO_CLOSE: float = 20.0   # bu kadar kaladan az ise girme
    MIN_ELAPSED_SEC: float = 15.0    # pencere basindan bu kadar gecmeden girme
    MAX_ABS_D: float = 0.0           # |d| bunu asarsa girme (0=limit yok). Reverse icin ~25 onerilir
    ORDER_SHARES: float = 5.0        # emir/pozisyon boyutu (share)
    FEE_BPS_DEFAULT: float = 1000.0  # fee alinamAzsa varsayilan (bps)
    FEE_EXP_DEFAULT: float = 1.0

    DASHBOARD_PORT: int = 8091       # limit-trader 8090, clone-trader 8080 -> 8091
    DASHBOARD_TOKEN: Optional[str] = None
    LEDGER_FILE: str = "signal_trades.jsonl"
    REVERSE: bool = False            # True: sinyalin TERSI tarafi al (mean-reversion tezi testi)

    @property
    def has_creds(self) -> bool:
        return bool(self.CLOB_API_KEY and self.CLOB_API_SECRET and self.CLOB_API_PASSPHRASE)

    @property
    def window_sec(self) -> int:
        return self.TIMEFRAME_MIN * 60

    @classmethod
    def load(cls) -> "Settings":
        s = cls(
            PRIVATE_KEY=_req("PRIVATE_KEY", ""),
            PROXY_WALLET_ADDRESS=_req("PROXY_WALLET_ADDRESS", ""),
            SIGNATURE_TYPE=_i("SIGNATURE_TYPE", 3),
            CLOB_HOST=_req("CLOB_HOST", "https://clob.polymarket.com"),
            GAMMA_HOST=_req("GAMMA_HOST", "https://gamma-api.polymarket.com"),
            RTDS_WS_URL=_req("RTDS_WS_URL", "wss://ws-live-data.polymarket.com"),
            CLOB_API_KEY=os.getenv("CLOB_API_KEY"),
            CLOB_API_SECRET=os.getenv("CLOB_API_SECRET"),
            CLOB_API_PASSPHRASE=os.getenv("CLOB_API_PASSPHRASE"),
            ASSET=_req("ASSET", "btc").lower(),
            TIMEFRAME_MIN=_i("TIMEFRAME_MIN", 5),
            EDGE_MARGIN=_f("EDGE_MARGIN", 0.05),
            VOL_WINDOW_SEC=_f("VOL_WINDOW_SEC", 90.0),
            MIN_TICKS=_i("MIN_TICKS", 20),
            MIN_SEC_TO_CLOSE=_f("MIN_SEC_TO_CLOSE", 20.0),
            MIN_ELAPSED_SEC=_f("MIN_ELAPSED_SEC", 15.0),
            MAX_ABS_D=_f("MAX_ABS_D", 0.0),
            ORDER_SHARES=_f("ORDER_SHARES", 5.0),
            DASHBOARD_PORT=_i("DASHBOARD_PORT", 8091),
            DASHBOARD_TOKEN=os.getenv("DASHBOARD_TOKEN"),
            LEDGER_FILE=_req("LEDGER_FILE", "signal_trades.jsonl"),
            REVERSE=_b("REVERSE", False),
        )
        if s.SIGNATURE_TYPE not in (0, 1, 2, 3):
            raise RuntimeError("SIGNATURE_TYPE 0/1/2/3 olmali (v2 fork; POLY_1271=3).")
        return s


# =====================================================================================
# CLOB istemci (kompakt) -- order book + fee + canli emir
# =====================================================================================

_SDK = False
try:
    from py_clob_client_v2 import (
        ApiCreds, ClobClient, OrderArgs, OrderType, PartialCreateOrderOptions, Side,
    )
    _SDK = True
except Exception:
    ClobClient = None  # type: ignore
    Side = None  # type: ignore


class Clob:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self._c: Any = None

    async def connect(self) -> None:
        if not _SDK:
            raise RuntimeError("py-clob-client-v2 kurulu degil (pip install py-clob-client-v2).")
        def build():
            creds = None
            if self.s.has_creds:
                creds = ApiCreds(api_key=self.s.CLOB_API_KEY, api_secret=self.s.CLOB_API_SECRET,
                                 api_passphrase=self.s.CLOB_API_PASSPHRASE)
            c = ClobClient(host=self.s.CLOB_HOST, chain_id=self.s.CHAIN_ID, key=self.s.PRIVATE_KEY,
                           creds=creds, signature_type=self.s.SIGNATURE_TYPE,
                           funder=self.s.PROXY_WALLET_ADDRESS or None)
            if creds is None:
                c.set_api_creds(c.derive_api_key())
            return c
        self._c = await asyncio.to_thread(build)

    async def best_ask(self, token: str) -> tuple[Optional[float], float]:
        def go():
            book = self._c.get_order_book(token)
            asks = getattr(book, "asks", None) or (book.get("asks") if isinstance(book, dict) else None) or []
            rows = []
            for a in asks:
                pr = getattr(a, "price", None) if not isinstance(a, dict) else a.get("price")
                sz = getattr(a, "size", None) if not isinstance(a, dict) else a.get("size")
                try:
                    rows.append((float(pr), float(sz)))
                except (TypeError, ValueError):
                    continue
            if not rows:
                return None, 0.0
            rows.sort(key=lambda x: x[0])
            return rows[0]
        try:
            return await asyncio.to_thread(go)
        except Exception:
            return None, 0.0

    async def fee(self, token: str) -> tuple[float, float]:
        def go():
            bps = self._c.get_fee_rate_bps(token)
            exp = self._c.get_fee_exponent(token)
            return bps / 10000.0, float(exp)
        try:
            return await asyncio.to_thread(go)
        except Exception:
            return self.s.FEE_BPS_DEFAULT / 10000.0, self.s.FEE_EXP_DEFAULT

    async def market_meta(self, token: str) -> tuple[str, bool]:
        def go():
            tick = str(self._c.get_tick_size(token))
            neg = bool(self._c.get_neg_risk(token))
            return tick, neg
        try:
            return await asyncio.to_thread(go)
        except Exception:
            return "0.01", False

    async def buy_fok(self, token: str, price: float, shares: float) -> Optional[str]:
        """Ucuz tarafi aninda al (marketable FOK). Kabul edilen order_id doner."""
        tick, neg = await self.market_meta(token)
        tf = float(tick)
        limit = min(0.999, max(tf, round((price + 0.02) / tf) * tf))  # doldurmayi garanti icin tampon
        def go():
            args = OrderArgs(token_id=token, price=round(limit, 6), size=shares, side=Side.BUY)
            opt = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg)
            return self._c.create_and_post_order(args, opt, OrderType.FOK)
        try:
            resp = await asyncio.to_thread(go)
        except Exception as exc:
            logger.error("Canli emir hatasi token=%s: %s", token[:10], exc)
            return None
        for k in ("orderID", "orderId", "id", "order_id"):
            v = resp.get(k) if isinstance(resp, dict) else getattr(resp, k, None)
            if v:
                return str(v)
        return "posted"


# =====================================================================================
# RTDS Chainlink feed (reversal-trading portu) + oynaklik
# =====================================================================================

RTDS_SUB = json.dumps({"action": "subscribe", "subscriptions": [
    {"topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"btc/usd"}'}]})


def parse_rtds(raw: str) -> Optional[tuple[float, int]]:
    if not raw or raw[0] not in "{[":
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    p = obj.get("payload")
    if not isinstance(p, dict) or p.get("symbol") != "btc/usd":
        return None
    try:
        price = float(p["value"]); ts = int(p["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    return (price, ts) if price > 0 else None


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# =====================================================================================
# Paylasilan durum + strateji
# =====================================================================================

@dataclass
class Position:
    window: int
    side: str            # "UP" / "DOWN"
    token: str
    entry_price: float
    shares: float
    fee_share: float     # fee per share (USDC)
    p_up: float
    d: float
    p2b: float           # bu pencerenin acilis fiyati (price-to-beat) -- cozumleme icin
    live: bool
    order_id: str = ""
    ts: float = 0.0


class Strategy:
    def __init__(self, s: Settings, clob: Clob) -> None:
        self.s = s
        self.clob = clob
        # feed durumu
        self.price: Optional[float] = None
        self.price_ts: int = 0
        self.window: int = 0
        self.p2b: Optional[float] = None       # bu pencere price-to-beat
        self._ticks: deque = deque(maxlen=600)  # (ts_ms, price)
        self._prev_price: Optional[float] = None
        self._prev_ts: int = 0
        self._close_price: dict[int, float] = {}  # window -> kapanis fiyati (bir sonrakinin acilisi)
        # market
        self.market: dict | None = None
        self.market_window: int = -1
        self.yes_ask: Optional[float] = None
        self.no_ask: Optional[float] = None
        self.yes_fee: tuple[float, float] = (0.10, 1.0)
        self.no_fee: tuple[float, float] = (0.10, 1.0)
        # sinyal/pozisyon
        self.sigma: Optional[float] = None
        self.p_up: Optional[float] = None
        self.ev_up: Optional[float] = None
        self.ev_down: Optional[float] = None
        self.signal: str = "-"
        self.open_pos: dict[int, Position] = {}   # window -> acik pozisyon (giris market basi 1)
        # arm / sicil
        self.live_armed = False
        self.events: deque = deque(maxlen=60)
        self.stats = {"paper_trades": 0, "paper_wins": 0, "paper_pnl": 0.0,
                      "live_trades": 0, "live_wins": 0, "live_pnl": 0.0}
        self.equity: deque = deque(maxlen=200)  # kumulatif paper PnL egrisi
        self.started = time.time()

    # ---- feed ----
    def on_price(self, price: float, ts_ms: int) -> None:
        self.price = price
        self.price_ts = ts_ms
        self._ticks.append((ts_ms, price))
        win = (ts_ms // 1000) // self.s.window_sec * self.s.window_sec
        if win != self.window:
            # pencere donusu: bir onceki pencerenin kapanis fiyati ~ bu ilk tick
            if self.window:
                self._close_price[self.window] = self._prev_price if self._prev_price else price
            # yeni pencere price-to-beat = sinir civari ilk tick
            self.p2b = price
            self.window = win
            self._emit(f"yeni pencere {win} · price-to-beat={price:.2f}")
        self._prev_price = price
        self._prev_ts = ts_ms

    def _sigma(self) -> Optional[float]:
        if len(self._ticks) < self.s.MIN_TICKS:
            return None
        now = self.price_ts
        cut = now - self.s.VOL_WINDOW_SEC * 1000
        pts = [(t, p) for (t, p) in self._ticks if t >= cut]
        if len(pts) < self.s.MIN_TICKS:
            return None
        diffs = []
        for i in range(1, len(pts)):
            dt = (pts[i][0] - pts[i - 1][0]) / 1000.0
            if dt <= 0:
                continue
            diffs.append((pts[i][1] - pts[i - 1][1]) / math.sqrt(dt))
        if len(diffs) < self.s.MIN_TICKS - 1:
            return None
        m = sum(diffs) / len(diffs)
        var = sum((x - m) ** 2 for x in diffs) / len(diffs)
        sig = math.sqrt(var)
        return sig if sig > 0 else None

    def _emit(self, msg: str) -> None:
        self.events.appendleft(f"{time.strftime('%H:%M:%S')} {msg}")

    def _fee_share(self, price: float, fee: tuple[float, float]) -> float:
        rate, exp = fee
        if price <= 0 or price >= 1:
            return 0.0
        return rate * (price * (1 - price)) ** exp

    # ---- market ----
    async def refresh_market(self) -> None:
        if self.window == self.market_window and self.market is not None:
            return
        m = await asyncio.to_thread(self._discover, self.window)
        self.market = m
        self.market_window = self.window
        self.yes_ask = self.no_ask = None
        if m:
            self.yes_fee = await self.clob.fee(m["yes"])
            self.no_fee = await self.clob.fee(m["no"])
            self._emit(f"market: {m['name']}")

    def _discover(self, window: int) -> dict | None:
        w = self.s.window_sec
        for off in (0, -1, 1):
            ts = window + off * w
            for pat in ("{a}-updown-{m}m-{ts}", "{a}-up-or-down-{m}m-{ts}"):
                for a in ([self.s.ASSET, "bitcoin"] if self.s.ASSET == "btc" else [self.s.ASSET]):
                    slug = pat.format(a=a, m=self.s.TIMEFRAME_MIN, ts=ts)
                    try:
                        r = requests.get(f"{self.s.GAMMA_HOST}/markets/slug/{slug}", timeout=5)
                        if r.status_code != 200:
                            continue
                        p = r.json()
                    except Exception:
                        continue
                    if p.get("active") is not True:
                        continue
                    toks = p.get("clobTokenIds")
                    if isinstance(toks, str):
                        try:
                            toks = json.loads(toks)
                        except json.JSONDecodeError:
                            toks = []
                    if not isinstance(toks, list) or len(toks) < 2:
                        continue
                    outs = p.get("outcomes")
                    if isinstance(outs, str):
                        try:
                            outs = json.loads(outs)
                        except json.JSONDecodeError:
                            outs = []
                    outs = [str(o).strip().lower() for o in (outs or [])]
                    ui = next((i for i, o in enumerate(outs) if o in ("up", "yes", "long")), 0)
                    di = next((i for i, o in enumerate(outs) if o in ("down", "no", "short")), 1)
                    name = str(p.get("question") or p.get("title") or slug)
                    return {"slug": p.get("slug", slug), "name": name,
                            "yes": str(toks[ui]), "no": str(toks[di]),
                            "closes": ts + w, "window": window}
        return None

    def seconds_to_close(self) -> Optional[float]:
        if not self.market:
            return None
        return self.market["closes"] - time.time()

    # ---- ana dongu ----
    async def step(self) -> None:
        # 1) kapanan pencerelerin pozisyonlarini cozumle
        await self._resolve_closed()
        # 2) guncel market
        await self.refresh_market()
        m = self.market
        if not m or self.price is None or self.p2b is None:
            self.signal = "veri bekleniyor"
            return
        # 3) order book
        self.yes_ask, _ = await self.clob.best_ask(m["yes"])
        self.no_ask, _ = await self.clob.best_ask(m["no"])
        # 4) model
        self.sigma = self._sigma()
        stc = self.seconds_to_close() or 0.0
        elapsed = self.s.window_sec - stc
        d = self.price - self.p2b
        self.p_up = None
        self.ev_up = self.ev_down = None
        self.signal = "-"
        if self.sigma and stc > 0:
            z = d / (self.sigma * math.sqrt(stc))
            self.p_up = norm_cdf(z)
        if self.p_up is not None and self.yes_ask and self.no_ask:
            yf = self._fee_share(self.yes_ask, self.yes_fee)
            nf = self._fee_share(self.no_ask, self.no_fee)
            self.ev_up = self.p_up - self.yes_ask - yf
            self.ev_down = (1 - self.p_up) - self.no_ask - nf
            # 5) giris kosullari
            d_ok = (self.s.MAX_ABS_D <= 0) or (abs(d) <= self.s.MAX_ABS_D)
            can_enter = (stc >= self.s.MIN_SEC_TO_CLOSE and elapsed >= self.s.MIN_ELAPSED_SEC
                         and d_ok and self.window not in self.open_pos)
            best_side = "UP" if self.ev_up >= self.ev_down else "DOWN"
            best_ev = max(self.ev_up, self.ev_down)
            self.signal = f"{best_side} (EV={best_ev:+.3f})" if best_ev >= self.s.EDGE_MARGIN else \
                          f"bekle (en iyi EV={best_ev:+.3f})"
            if can_enter and best_ev >= self.s.EDGE_MARGIN:
                side = best_side
                if self.s.REVERSE:
                    side = "DOWN" if best_side == "UP" else "UP"  # sinyalin TERSI
                await self._enter(side)

    async def _enter(self, side: str) -> None:
        m = self.market
        token = m["yes"] if side == "UP" else m["no"]
        price = self.yes_ask if side == "UP" else self.no_ask
        fee_share = self._fee_share(price, self.yes_fee if side == "UP" else self.no_fee)
        live = self.live_armed
        order_id = ""
        if live:
            order_id = await self.clob.buy_fok(token, price, self.s.ORDER_SHARES) or ""
            if not order_id:
                self._emit(f"CANLI emir REDDEDILDI {side} @ {price:.3f}")
                live = False  # paper olarak kaydet
        pos = Position(window=self.window, side=side, token=token, entry_price=price,
                       shares=self.s.ORDER_SHARES, fee_share=fee_share, p_up=self.p_up or 0.0,
                       d=self.price - (self.p2b or 0.0), p2b=self.p2b or 0.0,
                       live=live, order_id=order_id, ts=time.time())
        self.open_pos[self.window] = pos
        tag = "CANLI" if live else "PAPER"
        self._emit(f"{tag} GIRIS {side} @ {price:.3f} (P_up={pos.p_up:.2f}, d={pos.d:+.1f})")
        logger.info("%s giris %s @ %.3f P_up=%.2f d=%+.1f", tag, side, price, pos.p_up, pos.d)

    async def _resolve_closed(self) -> None:
        """Kapanmis pencerelerin pozisyonlarini cozumle: kazanan = kapanis > acilis(p2b) ? UP : DOWN."""
        for win in list(self.open_pos.keys()):
            if win >= self.window:
                continue  # henuz kapanmadi
            pos = self.open_pos.pop(win)
            close = self._close_price.get(win, self.price or pos.entry_price)
            winner = "UP" if close > pos.p2b else "DOWN"
            won = (pos.side == winner)
            gross = (1.0 - pos.entry_price) if won else (-pos.entry_price)
            pnl = (gross - pos.fee_share) * pos.shares
            self._book(pos, won, pnl, close, winner)

    def _book(self, pos: Position, won: bool, pnl: float, close: float, winner: str) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "window": pos.window, "mode": ("live" if pos.live else "paper"),
               "side": pos.side, "winner": winner, "won": won, "entry": round(pos.entry_price, 4),
               "shares": pos.shares, "p_up": round(pos.p_up, 3), "d": round(pos.d, 2),
               "close": round(close, 2), "pnl": round(pnl, 4), "order_id": pos.order_id}
        try:
            with open(self.s.LEDGER_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass
        if pos.live:
            self.stats["live_trades"] += 1
            self.stats["live_wins"] += int(won)
            self.stats["live_pnl"] += pnl
        else:
            self.stats["paper_trades"] += 1
            self.stats["paper_wins"] += int(won)
            self.stats["paper_pnl"] += pnl
            self.equity.append(round(self.stats["paper_pnl"], 4))
        self._emit(f"SONUC {'CANLI' if pos.live else 'PAPER'} {pos.side} -> "
                   f"{'KAZANDI' if won else 'KAYBETTI'} (kazanan={winner}) PnL={pnl:+.3f}")

    # ---- dashboard snapshot ----
    def snapshot(self) -> dict:
        m = self.market
        pt = self.stats["paper_trades"]; lt = self.stats["live_trades"]
        return {
            "mode": ("LIVE ARMED" if self.live_armed else "PAPER") + (" · TERS" if self.s.REVERSE else ""),
            "reverse": self.s.REVERSE,
            "uptime": int(time.time() - self.started),
            "market_name": (m["name"] if m else None),
            "slug": (m["slug"] if m else None),
            "seconds_to_close": (round(self.seconds_to_close()) if self.seconds_to_close() is not None else None),
            "window_sec": self.s.window_sec,
            "btc_price": self.price,
            "price_to_beat": self.p2b,
            "distance": (round(self.price - self.p2b, 2) if (self.price and self.p2b) else None),
            "sigma": (round(self.sigma, 4) if self.sigma else None),
            "p_up": (round(self.p_up, 4) if self.p_up is not None else None),
            "yes_ask": self.yes_ask, "no_ask": self.no_ask,
            "ev_up": (round(self.ev_up, 4) if self.ev_up is not None else None),
            "ev_down": (round(self.ev_down, 4) if self.ev_down is not None else None),
            "edge_margin": self.s.EDGE_MARGIN,
            "signal": self.signal,
            "open_position": (None if self.window not in self.open_pos else {
                "side": self.open_pos[self.window].side,
                "entry": self.open_pos[self.window].entry_price,
                "live": self.open_pos[self.window].live}),
            "stats": {
                **self.stats,
                "paper_winrate": (round(100 * self.stats["paper_wins"] / pt, 1) if pt else None),
                "live_winrate": (round(100 * self.stats["live_wins"] / lt, 1) if lt else None),
            },
            "equity": list(self.equity),
            "events": list(self.events),
        }


# =====================================================================================
# Feed + strateji dongusu + dashboard
# =====================================================================================

async def feed_loop(strat: Strategy, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with websockets.connect(strat.s.RTDS_WS_URL, open_timeout=10, ping_interval=None) as ws:
                await ws.send(RTDS_SUB)
                strat._emit("feed baglandi (Chainlink RTDS)")

                async def keepalive():
                    while True:
                        await asyncio.sleep(5)
                        await ws.send("PING")
                ka = asyncio.create_task(keepalive())
                try:
                    async for raw in ws:
                        parsed = parse_rtds(raw)
                        if parsed:
                            strat.on_price(*parsed)
                finally:
                    ka.cancel()
        except Exception as exc:  # noqa: BLE001
            strat._emit(f"feed koptu: {type(exc).__name__}; 2sn sonra tekrar")
            await asyncio.sleep(2)


async def strategy_loop(strat: Strategy, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await strat.step()
        except Exception:  # noqa: BLE001
            logger.exception("strateji adimi hatasi")
        await asyncio.sleep(1.0)


DASH_HTML = """<!doctype html><html lang=tr><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Signal-Trader</title>
<style>
:root{--bg:#0b0f14;--card:#141b24;--line:#243040;--fg:#e6edf3;--mut:#8b98a5;--grn:#2ea043;--red:#f85149;--blu:#388bfd;--yel:#d29922}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Segoe UI,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:16px}
header{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
h1{font-size:17px;margin:0}.mut{color:var(--mut)}
.badge{padding:3px 10px;border-radius:999px;font-weight:700;font-size:12px}
.paper{background:rgba(56,139,253,.15);color:var(--blu);border:1px solid var(--blu)}
.live{background:rgba(248,81,73,.15);color:var(--red);border:1px solid var(--red)}
.mkt{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.mkt h2{margin:0 0 4px;font-size:24px}
.bar{height:8px;background:#0b0f14;border-radius:6px;overflow:hidden;margin-top:10px}.bar>i{display:block;height:100%;background:var(--blu)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.card h3{margin:0 0 6px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut)}
.big{font-size:22px;font-weight:700}
.sig{font-size:18px;font-weight:700;padding:12px;border-radius:10px;background:var(--card);border:1px solid var(--line);margin-bottom:14px}
.up{color:var(--grn)}.down{color:var(--red)}
button{font:inherit;font-weight:700;border:0;border-radius:10px;padding:12px 18px;cursor:pointer}
.arm{background:var(--red);color:#fff}.disarm{background:var(--line);color:var(--fg)}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.log{background:#0b0f14;border:1px solid var(--line);border-radius:10px;padding:10px;max-height:230px;overflow:auto;font-family:ui-monospace,Consolas,monospace;font-size:12px}
.log div{padding:2px 0;border-bottom:1px solid #161d27}
table{width:100%;border-collapse:collapse}td,th{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left}
svg{width:100%;height:60px}
</style></head><body><div class=wrap>
<header><h1>Signal-Trader · BTC 5dk yönlü</h1><span id=mode class="badge paper">...</span>
<span class=mut id=up></span><span class=mut style=margin-left:auto id=clk></span></header>

<div class=mkt><div class=mut>GÜNCEL MARKET</div><h2 id=mname>-</h2>
<div class=mut><span id=stc></span> · slug <span id=slug></span></div>
<div class=bar><i id=bar style=width:0%></i></div></div>

<div class=grid>
<div class=card><h3>BTC fiyat</h3><div class=big id=btc>-</div></div>
<div class=card><h3>Price-to-beat</h3><div class=big id=p2b>-</div></div>
<div class=card><h3>Mesafe (d)</h3><div class=big id=dist>-</div></div>
<div class=card><h3>σ (oynaklık)</h3><div class=big id=sig>-</div></div>
<div class=card><h3>Model P(Up)</h3><div class=big id=pup>-</div></div>
<div class=card><h3>YES / NO ask</h3><div class=big id=asks>-</div></div>
<div class=card><h3>EV Up / Down</h3><div class=big id=ev>-</div></div>
<div class=card><h3>Açık pozisyon</h3><div class=big id=pos>yok</div></div>
</div>

<div class=sig id=signal>sinyal: -</div>

<div class=row>
<button id=armbtn class=arm>▶ CANLIYA GEÇ (ARM)</button>
<span class=mut id=armnote>Paper modda — gerçek emir gitmiyor. Sicil pozitifse canlıya geç.</span>
</div>

<div class=grid>
<div class=card><h3>Paper işlem</h3><div class=big id=pt>0</div></div>
<div class=card><h3>Paper isabet</h3><div class=big id=pw>-</div></div>
<div class=card><h3>Paper PnL</h3><div class=big id=ppnl>$0</div></div>
<div class=card><h3>Canlı işlem</h3><div class=big id=lt>0</div></div>
<div class=card><h3>Canlı PnL</h3><div class=big id=lpnl>$0</div></div>
</div>

<div class=card style=margin-bottom:14px><h3>Paper PnL eğrisi</h3><svg id=eq viewBox="0 0 400 60" preserveAspectRatio=none></svg></div>

<div class=mut style=margin:6px 0>OLAY / SONUÇ AKIŞI</div><div class=log id=log></div>
</div>
<script>
const KEY=new URLSearchParams(location.search).get("key");
const q=i=>document.getElementById(i);const q2=(k)=>KEY?("?key="+encodeURIComponent(KEY)):"";
async function arm(on){await fetch("/api/"+(on?"arm":"disarm")+ q2(),{method:"POST"});tick();}
q("armbtn").onclick=()=>{const s=q("mode").textContent==="LIVE ARMED";
  if(!s){if(confirm("CANLI moda geçilecek — gerçek emir gönderilecek. Emin misin?"))arm(true);}else arm(false);};
function fmt(v,d=2){return v==null?"-":(+v).toFixed(d);}
async function tick(){try{
 const r=await fetch("/api/state"+q2());if(!r.ok){q("mode").textContent="ERİŞİM YOK";return;}
 const d=await r.json();const live=d.mode==="LIVE ARMED";
 q("mode").textContent=d.mode;q("mode").className="badge "+(live?"live":"paper");
 q("armbtn").textContent=live?"■ DURDUR (paper'a dön)":"▶ CANLIYA GEÇ (ARM)";
 q("armbtn").className=live?"disarm":"arm";
 q("armnote").textContent=live?"CANLI — sinyal gelince gerçek emir gidecek.":"Paper modda — gerçek emir gitmiyor.";
 q("up").textContent="çalışıyor "+Math.floor(d.uptime/60)+"d";q("clk").textContent=new Date().toLocaleTimeString();
 q("mname").textContent=d.market_name||"(market aranıyor…)";q("slug").textContent=d.slug||"";
 q("stc").textContent=d.seconds_to_close!=null?("kapanışa "+d.seconds_to_close+"sn"):"-";
 const pct=(d.seconds_to_close!=null&&d.window_sec)?Math.max(0,Math.min(100,100*d.seconds_to_close/d.window_sec)):0;
 q("bar").style.width=pct+"%";q("bar").style.background=(d.seconds_to_close!=null&&d.seconds_to_close<20)?"var(--red)":"var(--blu)";
 q("btc").textContent=fmt(d.btc_price);q("p2b").textContent=fmt(d.price_to_beat);
 q("dist").textContent=d.distance==null?"-":(d.distance>0?"+":"")+fmt(d.distance);
 q("dist").className="big "+(d.distance>0?"up":d.distance<0?"down":"");
 q("sig").textContent=fmt(d.sigma,3);
 q("pup").textContent=d.p_up==null?"-":(100*d.p_up).toFixed(1)+"%";
 q("asks").textContent=fmt(d.yes_ask,3)+" / "+fmt(d.no_ask,3);
 q("ev").textContent=(d.ev_up==null?"-":(d.ev_up>0?"+":"")+fmt(d.ev_up,3))+" / "+(d.ev_down==null?"-":(d.ev_down>0?"+":"")+fmt(d.ev_down,3));
 q("pos").textContent=d.open_position?(d.open_position.side+" @"+fmt(d.open_position.entry,3)+(d.open_position.live?" (canlı)":"")):"yok";
 const sg=d.signal||"-";q("signal").textContent="sinyal: "+sg;
 q("signal").className="sig "+(sg.startsWith("UP")?"up":sg.startsWith("DOWN")?"down":"");
 const s=d.stats;q("pt").textContent=s.paper_trades;q("pw").textContent=s.paper_winrate==null?"-":s.paper_winrate+"%";
 q("ppnl").textContent="$"+(s.paper_pnl||0).toFixed(2);q("lt").textContent=s.live_trades;
 q("lpnl").textContent="$"+(s.live_pnl||0).toFixed(2);
 // equity
 const e=d.equity||[];if(e.length>1){const mn=Math.min(...e,0),mx=Math.max(...e,0),rg=(mx-mn)||1;
  const pts=e.map((v,i)=>(400*i/(e.length-1)).toFixed(1)+","+(60-58*(v-mn)/rg).toFixed(1)).join(" ");
  q("eq").innerHTML='<polyline fill=none stroke='+(e[e.length-1]>=0?"#2ea043":"#f85149")+' stroke-width=2 points="'+pts+'"/>';}
 q("log").innerHTML=(d.events||[]).map(x=>"<div>"+x+"</div>").join("")||"<div class=mut>…</div>";
}catch(e){q("mode").textContent="BAĞLANTI YOK";}}
tick();setInterval(tick,1000);
</script></body></html>"""


async def dashboard(strat: Strategy, stop: asyncio.Event) -> None:
    try:
        from aiohttp import web
    except Exception:
        logger.warning("aiohttp yok; dashboard kapali.")
        return

    def ok(req) -> bool:
        return (not strat.s.DASHBOARD_TOKEN) or req.query.get("key") == strat.s.DASHBOARD_TOKEN

    async def index(req):
        return web.Response(text=DASH_HTML, content_type="text/html")

    async def state(req):
        if not ok(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(strat.snapshot())

    async def arm(req):
        if not ok(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        strat.live_armed = True
        strat._emit(">>> CANLI MOD AÇILDI (dashboard)")
        logger.warning("CANLI MOD ARM edildi")
        return web.json_response({"armed": True})

    async def disarm(req):
        if not ok(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        strat.live_armed = False
        strat._emit("<<< canli mod kapatildi (paper'a donuldu)")
        return web.json_response({"armed": False})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/arm", arm)
    app.router.add_post("/api/disarm", disarm)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", strat.s.DASHBOARD_PORT)
    await site.start()
    logger.info("Dashboard: http://0.0.0.0:%d%s", strat.s.DASHBOARD_PORT,
                " (?key=...)" if strat.s.DASHBOARD_TOKEN else "")
    try:
        while not stop.is_set():
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


async def amain(s: Settings) -> None:
    clob = Clob(s)
    await clob.connect()
    strat = Strategy(s, clob)
    stop = asyncio.Event()
    await asyncio.gather(feed_loop(strat, stop), strategy_loop(strat, stop), dashboard(strat, stop))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for n in ("websockets", "aiohttp", "httpx", "httpcore", "urllib3", "web3", "py_clob_client_v2"):
        logging.getLogger(n).setLevel(logging.WARNING)
    s = Settings.load()
    if args.check:
        print(f"OK · sig={s.SIGNATURE_TYPE} · dash port={s.DASHBOARD_PORT} · SDK={'kurulu' if _SDK else 'YOK'} "
              f"· creds={'set' if s.has_creds else 'turetilecek'} · EDGE_MARGIN={s.EDGE_MARGIN}")
        return
    logger.info("Signal-Trader basladi (PAPER%s). Canliya gecis dashboard'dan ARM ile.",
                " · TERS/REVERSE" if s.REVERSE else "")
    try:
        asyncio.run(amain(s))
    except KeyboardInterrupt:
        logger.info("durduruldu.")


if __name__ == "__main__":
    main()
