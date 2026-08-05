"""Polymarket Weather Botu -- "zaten oldu" (near-resolution) edge, 5 ABD sehri.

FIKIR: Market "Bugun {sehir} high >= T F mi?" diye soruyor. Gunun max sicakligi
ogleden sonra fiilen kilitlenir; ama market bunu gec fiyatlar. Gozlenen high (METAR,
istasyonun gece-yarisindan-beri max'i) esigi zaten gectiyse "EVET" KESIN -> ucuz YES'i
al. Zirve gecti ve gozlenen < esik ise "HAYIR" kesin -> ucuz NO'yu al. Aradaki
belirsizligi Open-Meteo ensemble (kalan gun) verir.

  P(YES) = (member_final_max >= T olan ensemble uye orani), final = max(H_obs, kalan_max)
  EV_yes = P(YES) - YES_ask - fee ;  EV_no = (1-P(YES)) - NO_ask - fee
  |EV| >= EDGE_MARGIN -> sinyal. Varsayilan PAPER; dashboard'dan tek-tik ARM ile canli.

Veri (hepsi UCRETSIZ, key gerekmez):
  - Gozlem: aviationweather METAR (resolution istasyonuyla birebir)
  - Ensemble: Open-Meteo Ensemble API (ECMWF/GFS/ICON uyeleri)
Resolution: NWS Daily Climate Report (CLI), gunluk HIGH, yerel gece yarisindan, F.

READ/WRITE: canli ARM edilmedikce hicbir gercek emir gitmez.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

logger = logging.getLogger("weather-bot")

# =====================================================================================
# Sehir / istasyon haritasi (resolution = NWS CLI, bu istasyonlar)
# =====================================================================================

CITIES: dict[str, dict] = {
    "nyc":     {"name": "New York City", "station": "KNYC", "lat": 40.78, "lon": -73.97,
                "tz": "America/New_York",    "aliases": ["new york city", "new york", "nyc"]},
    "chicago": {"name": "Chicago",       "station": "KMDW", "lat": 41.79, "lon": -87.75,
                "tz": "America/Chicago",     "aliases": ["chicago"]},
    "la":      {"name": "Los Angeles",   "station": "KLAX", "lat": 33.94, "lon": -118.41,
                "tz": "America/Los_Angeles", "aliases": ["los angeles", "la"]},
    "miami":   {"name": "Miami",         "station": "KMIA", "lat": 25.79, "lon": -80.29,
                "tz": "America/New_York",    "aliases": ["miami"]},
    "sf":      {"name": "San Francisco", "station": "KSFO", "lat": 37.62, "lon": -122.37,
                "tz": "America/Los_Angeles", "aliases": ["san francisco", "sf"]},
}


def _match_city(text: str) -> Optional[str]:
    t = text.lower()
    for key, c in CITIES.items():
        for a in c["aliases"]:
            if a in t:
                return key
    return None


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
    CLOB_API_KEY: Optional[str] = None
    CLOB_API_SECRET: Optional[str] = field(default=None, repr=False)
    CLOB_API_PASSPHRASE: Optional[str] = field(default=None, repr=False)

    CITIES: str = "nyc,chicago,la,miami,sf"     # hangi sehirler
    EDGE_MARGIN: float = 0.05                    # min fee-sonrasi EV -> sinyal
    ORDER_SHARES: float = 10.0                   # pozisyon boyutu (share)
    POLL_INTERVAL: float = 30.0                  # market/veri tarama araligi (sn)
    MAX_POS_PER_MARKET: int = 1                  # market basi max acik pozisyon
    FEE_BPS: float = 0.0                         # weather marketlerinde genelde fee yok
    FEE_EXP: float = 1.0

    DASHBOARD_PORT: int = 8095
    DASHBOARD_TOKEN: Optional[str] = None
    LEDGER_FILE: str = "weather_trades.jsonl"

    @property
    def has_creds(self) -> bool:
        return bool(self.CLOB_API_KEY and self.CLOB_API_SECRET and self.CLOB_API_PASSPHRASE)

    @property
    def city_list(self) -> list[str]:
        return [c.strip().lower() for c in self.CITIES.split(",") if c.strip() in CITIES]

    @classmethod
    def load(cls) -> "Settings":
        s = cls(
            PRIVATE_KEY=_req("PRIVATE_KEY", ""),
            PROXY_WALLET_ADDRESS=_req("PROXY_WALLET_ADDRESS", ""),
            SIGNATURE_TYPE=_i("SIGNATURE_TYPE", 3),
            CLOB_HOST=_req("CLOB_HOST", "https://clob.polymarket.com"),
            GAMMA_HOST=_req("GAMMA_HOST", "https://gamma-api.polymarket.com"),
            CLOB_API_KEY=os.getenv("CLOB_API_KEY"),
            CLOB_API_SECRET=os.getenv("CLOB_API_SECRET"),
            CLOB_API_PASSPHRASE=os.getenv("CLOB_API_PASSPHRASE"),
            CITIES=_req("CITIES", "nyc,chicago,la,miami,sf"),
            EDGE_MARGIN=_f("EDGE_MARGIN", 0.05),
            ORDER_SHARES=_f("ORDER_SHARES", 10.0),
            POLL_INTERVAL=_f("POLL_INTERVAL", 30.0),
            MAX_POS_PER_MARKET=_i("MAX_POS_PER_MARKET", 1),
            FEE_BPS=_f("FEE_BPS", 0.0),
            DASHBOARD_PORT=_i("DASHBOARD_PORT", 8095),
            DASHBOARD_TOKEN=os.getenv("DASHBOARD_TOKEN"),
            LEDGER_FILE=_req("LEDGER_FILE", "weather_trades.jsonl"),
        )
        if s.SIGNATURE_TYPE not in (0, 1, 2, 3):
            raise RuntimeError("SIGNATURE_TYPE 0/1/2/3 olmali (POLY_1271=3).")
        return s


def c2f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


# =====================================================================================
# Veri baglayicilari  (METAR gozlem + Open-Meteo ensemble)
# =====================================================================================

METAR_URL = "https://aviationweather.gov/api/data/metar"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ENSEMBLE_MODELS = "gfs_seamless,ecmwf_ifs025,icon_seamless"


def observed_high_f(city_key: str) -> Optional[float]:
    """Istasyonun gece-yarisindan (yerel) beri gozlenen MAX sicakligi (F). METAR."""
    c = CITIES[city_key]
    tz = ZoneInfo(c["tz"])
    now_local = datetime.now(tz)
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    mid_epoch = midnight.timestamp()
    try:
        r = requests.get(METAR_URL, params={"ids": c["station"], "format": "json", "hours": 24}, timeout=12)
        if r.status_code != 200:
            return None
        obs = r.json()
    except Exception:
        return None
    if not isinstance(obs, list):
        return None
    best = None
    for o in obs:
        try:
            ts = float(o.get("obsTime"))
            temp_c = o.get("temp")
            if temp_c is None:
                continue
            temp_c = float(temp_c)
        except (TypeError, ValueError):
            continue
        if ts < mid_epoch:
            continue
        f = c2f(temp_c)
        if best is None or f > best:
            best = f
    return round(best, 1) if best is not None else None


def ensemble_prob_ge(city_key: str, threshold_f: float, h_obs: Optional[float]) -> Optional[dict]:
    """P(gunun final high'i >= T) = final_max>=T olan ensemble uye orani.

    final_max = max(H_obs, uyenin kalan-gun saatlik max'i). Dondurur:
    {"p": olasilik, "members": n, "remaining_hours": k, "peak_passed": bool}
    """
    c = CITIES[city_key]
    tz = ZoneInfo(c["tz"])
    now_local = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    try:
        r = requests.get(ENSEMBLE_URL, params={
            "latitude": c["lat"], "longitude": c["lon"], "hourly": "temperature_2m",
            "models": ENSEMBLE_MODELS, "temperature_unit": "fahrenheit",
            "timezone": c["tz"], "forecast_days": 1}, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    hourly = data.get("hourly") if isinstance(data, dict) else None
    if not isinstance(hourly, dict):
        return None
    times = hourly.get("time") or []
    member_keys = [k for k in hourly if k.startswith("temperature_2m_member")]
    if not member_keys:
        # bazi modeller tek uye -> temperature_2m
        if "temperature_2m" in hourly:
            member_keys = ["temperature_2m"]
        else:
            return None
    # kalan gun (bugun, su andan itibaren) indexleri
    rem_idx = []
    for i, t in enumerate(times):
        try:
            dt = datetime.fromisoformat(t)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            if dt >= now_local:
                rem_idx.append(i)
        except Exception:
            continue
    base = h_obs if h_obs is not None else -999.0
    hits = 0
    total = 0
    any_reach = False
    for m in member_keys:
        vals = hourly.get(m) or []
        rem = [vals[i] for i in rem_idx if i < len(vals) and vals[i] is not None]
        rem_max = max(rem) if rem else -999.0
        if rem_max >= threshold_f:
            any_reach = True
        final = max(base, rem_max)
        total += 1
        if final >= threshold_f:
            hits += 1
    if total == 0:
        return None
    # zirve gecti mi: hicbir uye kalan gunde esige ulasamiyorsa (ve H_obs<T)
    peak_passed = (not any_reach)
    return {"p": round(hits / total, 3), "members": total,
            "remaining_hours": len(rem_idx), "peak_passed": peak_passed}


# =====================================================================================
# CLOB istemci (order book + fee + canli emir)  -- signal_bot deseni
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

CLOB_PUBLIC = "https://clob.polymarket.com"


def public_best_ask(token: str) -> tuple[Optional[float], float]:
    """Public /book ile en iyi ask (auth gerekmez)."""
    try:
        r = requests.get(f"{CLOB_PUBLIC}/book", params={"token_id": token}, timeout=10)
        if r.status_code != 200:
            return None, 0.0
        book = r.json()
    except Exception:
        return None, 0.0
    best = None
    sz = 0.0
    for a in (book.get("asks") or []):
        try:
            p = float(a["price"]); s = float(a["size"])
        except (KeyError, ValueError, TypeError):
            continue
        if best is None or p < best:
            best, sz = p, s
    return best, sz


class Clob:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self._c: Any = None

    async def connect(self) -> None:
        if not _SDK:
            raise RuntimeError("py-clob-client-v2 kurulu degil.")
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

    async def buy_fok(self, token: str, price: float, shares: float) -> Optional[str]:
        if self._c is None:
            logger.error("CLOB baglanmadi; canli emir gonderilemiyor (gecerli PRIVATE_KEY gerekir).")
            return None
        def go():
            tick = str(self._c.get_tick_size(token))
            neg = bool(self._c.get_neg_risk(token))
            tf = float(tick)
            limit = min(0.999, max(tf, round((price + 0.02) / tf) * tf))
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
# Market kesfi (weather high-temp binary marketleri)
# =====================================================================================

def _list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            p = json.loads(v)
            return p if isinstance(p, list) else []
        except json.JSONDecodeError:
            return []
    return []


# esik: "85F", "85 °F", "85 degrees", "above 85", "or higher ... 85", ">= 85"
_THRESH_RE = re.compile(r"(\d{2,3})\s*(?:°|degrees?|\bf\b|℉)", re.IGNORECASE)


def _parse_threshold(q: str) -> Optional[float]:
    m = _THRESH_RE.search(q)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    # yedek: "high of 85" / "reach 85"
    m2 = re.search(r"(?:high|reach|hit|exceed|above|over)\D{0,6}(\d{2,3})", q, re.IGNORECASE)
    return float(m2.group(1)) if m2 else None


@dataclass
class WxMarket:
    slug: str
    question: str
    city: str
    threshold: float
    yes_token: str
    no_token: str
    end_iso: str


def discover_weather_markets(s: Settings) -> list[WxMarket]:
    """Gamma'dan aktif weather high-temp binary marketlerini bul (secili sehirler)."""
    out: list[WxMarket] = []
    offset = 0
    page = 500
    safety = 0
    cities = set(s.city_list)
    while True:
        try:
            r = requests.get(f"{s.GAMMA_HOST}/markets",
                             params={"active": "true", "closed": "false", "limit": page, "offset": offset},
                             timeout=25)
            if r.status_code != 200:
                break
            data = r.json()
        except Exception:
            break
        if not isinstance(data, list) or not data:
            break
        for m in data:
            q = str(m.get("question") or m.get("title") or "")
            ql = q.lower()
            if not any(w in ql for w in ("temperature", "temp", "high", "hot", "degrees", "°", "warm")):
                continue
            ck = _match_city(q)
            if ck is None or ck not in cities:
                continue
            if any(w in ql for w in ("low temp", "lowest", "minimum", "average", "coldest")):
                continue  # simdilik sadece HIGH
            toks = _list(m.get("clobTokenIds"))
            if len(toks) != 2:
                continue
            T = _parse_threshold(q)
            if T is None:
                continue
            outs = [str(o).strip().lower() for o in _list(m.get("outcomes"))]
            yi = next((i for i, o in enumerate(outs) if o in ("yes", "up", "higher")), 0)
            ni = next((i for i, o in enumerate(outs) if o in ("no", "down", "lower")), 1)
            out.append(WxMarket(
                slug=str(m.get("slug", "")), question=q[:110], city=ck, threshold=T,
                yes_token=str(toks[yi]), no_token=str(toks[ni]),
                end_iso=str(m.get("endDate") or m.get("endDateIso") or ""),
            ))
        offset += len(data)
        safety += 1
        if safety > 60:
            break
    return out


# =====================================================================================
# Strateji (edge motoru + paper + resolution)
# =====================================================================================

@dataclass
class Position:
    slug: str
    city: str
    threshold: float
    side: str            # "YES" / "NO"
    token: str
    entry_price: float
    shares: float
    p_yes: float
    h_obs: float
    live: bool
    order_id: str = ""
    ts: float = 0.0
    date_local: str = ""


class Strategy:
    def __init__(self, s: Settings, clob: Clob) -> None:
        self.s = s
        self.clob = clob
        self.markets: list[WxMarket] = []
        self.view: dict[str, dict] = {}     # slug -> son hesap (dashboard)
        self.open_pos: dict[str, Position] = {}   # slug -> acik pozisyon
        self.live_armed = False
        self.events: deque = deque(maxlen=80)
        self.stats = {"paper_trades": 0, "paper_wins": 0, "paper_pnl": 0.0,
                      "live_trades": 0, "live_wins": 0, "live_pnl": 0.0}
        self.equity: deque = deque(maxlen=300)
        self.started = time.time()
        self._last_discover = 0.0
        self._obs_cache: dict[str, tuple[float, Optional[float]]] = {}

    def _emit(self, msg: str) -> None:
        self.events.appendleft(f"{time.strftime('%H:%M:%S')} {msg}")

    def _fee(self, price: float) -> float:
        rate = self.s.FEE_BPS / 10000.0
        if rate <= 0 or price <= 0 or price >= 1:
            return 0.0
        return rate * (price * (1 - price)) ** self.s.FEE_EXP

    def _local_date(self, city: str) -> str:
        return datetime.now(ZoneInfo(CITIES[city]["tz"])).strftime("%Y-%m-%d")

    async def step(self) -> None:
        # market listesini periyodik yenile
        if time.time() - self._last_discover > 300 or not self.markets:
            self.markets = await asyncio.to_thread(discover_weather_markets, self.s)
            self._last_discover = time.time()
            self._emit(f"kesif: {len(self.markets)} weather high-temp market")
        await self._resolve_closed()
        for m in self.markets:
            await self._eval_market(m)

    async def _obs(self, city: str) -> Optional[float]:
        # 5 dk cache (METAR saatlik)
        c = self._obs_cache.get(city)
        if c and time.time() - c[0] < 300:
            return c[1]
        val = await asyncio.to_thread(observed_high_f, city)
        self._obs_cache[city] = (time.time(), val)
        return val

    async def _eval_market(self, m: WxMarket) -> None:
        h_obs = await self._obs(m.city)
        ens = await asyncio.to_thread(ensemble_prob_ge, m.city, m.threshold, h_obs)
        if ens is None:
            return
        p_yes = ens["p"]
        ya, _ = await asyncio.to_thread(public_best_ask, m.yes_token)
        na, _ = await asyncio.to_thread(public_best_ask, m.no_token)
        ev_yes = ev_no = None
        signal = "-"
        if ya is not None:
            ev_yes = p_yes - ya - self._fee(ya)
        if na is not None:
            ev_no = (1 - p_yes) - na - self._fee(na)
        best_side = None
        best_ev = None
        if ev_yes is not None and (best_ev is None or ev_yes > best_ev):
            best_ev, best_side = ev_yes, "YES"
        if ev_no is not None and (best_ev is None or ev_no > best_ev):
            best_ev, best_side = ev_no, "NO"
        if best_ev is not None:
            signal = (f"{best_side} (EV={best_ev:+.3f})" if best_ev >= self.s.EDGE_MARGIN
                      else f"bekle (EV={best_ev:+.3f})")
        self.view[m.slug] = {
            "city": CITIES[m.city]["name"], "question": m.question, "threshold": m.threshold,
            "h_obs": h_obs, "p_yes": p_yes, "peak_passed": ens["peak_passed"],
            "yes_ask": ya, "no_ask": na, "ev_yes": (round(ev_yes, 3) if ev_yes is not None else None),
            "ev_no": (round(ev_no, 3) if ev_no is not None else None), "signal": signal,
            "open": m.slug in self.open_pos,
        }
        # giris
        if (best_side and best_ev is not None and best_ev >= self.s.EDGE_MARGIN
                and m.slug not in self.open_pos):
            token = m.yes_token if best_side == "YES" else m.no_token
            price = ya if best_side == "YES" else na
            if price is not None:
                await self._enter(m, best_side, token, price, p_yes, h_obs or 0.0)

    async def _enter(self, m: WxMarket, side: str, token: str, price: float,
                     p_yes: float, h_obs: float) -> None:
        live = self.live_armed
        order_id = ""
        if live:
            order_id = await self.clob.buy_fok(token, price, self.s.ORDER_SHARES) or ""
            if not order_id:
                self._emit(f"CANLI REDDEDILDI {m.city} {side} @ {price:.3f}")
                live = False
        pos = Position(slug=m.slug, city=m.city, threshold=m.threshold, side=side, token=token,
                       entry_price=price, shares=self.s.ORDER_SHARES, p_yes=p_yes, h_obs=h_obs,
                       live=live, order_id=order_id, ts=time.time(), date_local=self._local_date(m.city))
        self.open_pos[m.slug] = pos
        tag = "CANLI" if live else "PAPER"
        self._emit(f"{tag} GIRIS {CITIES[m.city]['name']} >= {m.threshold:.0f}F {side} "
                   f"@ {price:.3f} (P_yes={p_yes:.2f}, H_obs={h_obs:.1f})")
        logger.info("%s giris %s %s T=%.0f @ %.3f P=%.2f Hobs=%.1f", tag, m.city, side,
                    m.threshold, price, p_yes, h_obs)

    async def _resolve_closed(self) -> None:
        """Pozisyonun sehir-gunu bitince final gozlenen high ile cozumle."""
        for slug in list(self.open_pos.keys()):
            pos = self.open_pos[slug]
            today = self._local_date(pos.city)
            if today <= pos.date_local:
                continue  # pozisyonun gunu henuz bitmedi
            final_high = await self._final_high(pos.city, pos.date_local)
            if final_high is None:
                continue
            won_yes = final_high >= pos.threshold
            winner = "YES" if won_yes else "NO"
            won = (pos.side == winner)
            gross = (1.0 - pos.entry_price) if won else (-pos.entry_price)
            pnl = (gross - self._fee(pos.entry_price)) * pos.shares
            self._book(pos, won, pnl, final_high, winner)
            self.open_pos.pop(slug, None)

    async def _final_high(self, city: str, date_local: str) -> Optional[float]:
        """Belirli bir gunun final gozlenen high'i (Open-Meteo historical/archive)."""
        c = CITIES[city]
        try:
            r = await asyncio.to_thread(requests.get,
                "https://archive-api.open-meteo.com/v1/archive",
                params={"latitude": c["lat"], "longitude": c["lon"], "start_date": date_local,
                        "end_date": date_local, "daily": "temperature_2m_max",
                        "temperature_unit": "fahrenheit", "timezone": c["tz"]}, timeout=15)
            if r.status_code != 200:
                return None
            d = r.json()
            arr = (d.get("daily") or {}).get("temperature_2m_max") or []
            return float(arr[0]) if arr else None
        except Exception:
            return None

    def _book(self, pos: Position, won: bool, pnl: float, final_high: float, winner: str) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "slug": pos.slug, "city": pos.city,
               "threshold": pos.threshold, "date": pos.date_local, "mode": ("live" if pos.live else "paper"),
               "side": pos.side, "winner": winner, "won": won, "entry": round(pos.entry_price, 4),
               "shares": pos.shares, "p_yes": round(pos.p_yes, 3), "h_obs_at_entry": round(pos.h_obs, 1),
               "final_high": round(final_high, 1), "pnl": round(pnl, 4), "order_id": pos.order_id}
        try:
            with open(self.s.LEDGER_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass
        if pos.live:
            self.stats["live_trades"] += 1; self.stats["live_wins"] += int(won); self.stats["live_pnl"] += pnl
        else:
            self.stats["paper_trades"] += 1; self.stats["paper_wins"] += int(won); self.stats["paper_pnl"] += pnl
            self.equity.append(round(self.stats["paper_pnl"], 4))
        self._emit(f"SONUC {'CANLI' if pos.live else 'PAPER'} {CITIES[pos.city]['name']} "
                   f">={pos.threshold:.0f} {pos.side} -> {'KAZANDI' if won else 'KAYBETTI'} "
                   f"(final={final_high:.1f}, kazanan={winner}) PnL={pnl:+.2f}")

    def snapshot(self) -> dict:
        pt = self.stats["paper_trades"]; lt = self.stats["live_trades"]
        return {
            "mode": "LIVE ARMED" if self.live_armed else "PAPER",
            "uptime": int(time.time() - self.started),
            "markets": [self.view[m.slug] for m in self.markets if m.slug in self.view],
            "open_positions": [{"city": CITIES[p.city]["name"], "threshold": p.threshold,
                                "side": p.side, "entry": p.entry_price, "live": p.live}
                               for p in self.open_pos.values()],
            "stats": {**self.stats,
                      "paper_winrate": (round(100 * self.stats["paper_wins"] / pt, 1) if pt else None),
                      "live_winrate": (round(100 * self.stats["live_wins"] / lt, 1) if lt else None)},
            "equity": list(self.equity),
            "events": list(self.events),
            "edge_margin": self.s.EDGE_MARGIN,
        }


# =====================================================================================
# Dashboard (aiohttp)  -- signal_bot deseni
# =====================================================================================

DASH_HTML = """<!doctype html><html lang=tr><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Weather-Bot</title>
<style>
:root{--bg:#0b0f14;--card:#141b24;--line:#243040;--fg:#e6edf3;--mut:#8b98a5;--grn:#2ea043;--red:#f85149;--blu:#388bfd;--yel:#d29922}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Segoe UI,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
header{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
h1{font-size:17px;margin:0}.mut{color:var(--mut)}
.badge{padding:3px 10px;border-radius:999px;font-weight:700;font-size:12px}
.paper{background:rgba(56,139,253,.15);color:var(--blu);border:1px solid var(--blu)}
.live{background:rgba(248,81,73,.15);color:var(--red);border:1px solid var(--red)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.card h3{margin:0 0 6px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut)}
.big{font-size:20px;font-weight:700}
button{font:inherit;font-weight:700;border:0;border-radius:10px;padding:12px 18px;cursor:pointer}
.arm{background:var(--red);color:#fff}.disarm{background:var(--line);color:var(--fg)}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:8px 0 14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--mut)}.up{color:var(--grn)}.down{color:var(--red)}
.log{background:#0b0f14;border:1px solid var(--line);border-radius:10px;padding:10px;max-height:220px;overflow:auto;font-family:ui-monospace,Consolas,monospace;font-size:12px}
.log div{padding:2px 0;border-bottom:1px solid #161d27}
svg{width:100%;height:56px}
</style></head><body><div class=wrap>
<header><h1>Weather-Bot · Polymarket sicaklik ("zaten oldu" edge)</h1>
<span id=mode class="badge paper">...</span><span class=mut id=up></span>
<span class=mut style=margin-left:auto id=clk></span></header>

<div class=row>
<button id=armbtn class=arm>▶ CANLIYA GEÇ (ARM)</button>
<span class=mut id=armnote>Paper modda — gerçek emir gitmiyor.</span></div>

<div class=grid>
<div class=card><h3>Paper işlem</h3><div class=big id=pt>0</div></div>
<div class=card><h3>Paper isabet</h3><div class=big id=pw>-</div></div>
<div class=card><h3>Paper PnL</h3><div class=big id=ppnl>$0</div></div>
<div class=card><h3>Canlı işlem</h3><div class=big id=lt>0</div></div>
<div class=card><h3>Canlı PnL</h3><div class=big id=lpnl>$0</div></div>
</div>

<div class=card style=margin-bottom:14px><h3>Paper PnL eğrisi</h3><svg id=eq viewBox="0 0 400 56" preserveAspectRatio=none></svg></div>

<div class=mut style=margin:6px 0>GÜNCEL MARKETLER (P_yes = model olasılığı, EV = fee sonrası)</div>
<div class=card style=overflow-x:auto><table><thead><tr>
<th>Şehir</th><th>Eşik°F</th><th>Gözlenen high</th><th>Zirve</th><th>P_yes</th><th>YES/NO ask</th><th>EV Y/N</th><th>Sinyal</th></tr></thead>
<tbody id=mk></tbody></table></div>

<div class=mut style=margin:14px 0 6px>OLAY / SONUÇ AKIŞI</div><div class=log id=log></div>
</div>
<script>
const KEY=new URLSearchParams(location.search).get("key");
const q=i=>document.getElementById(i);const q2=()=>KEY?("?key="+encodeURIComponent(KEY)):"";
async function arm(on){await fetch("/api/"+(on?"arm":"disarm")+q2(),{method:"POST"});tick();}
q("armbtn").onclick=()=>{const live=q("mode").textContent==="LIVE ARMED";
 if(!live){if(confirm("CANLI moda geçilecek — gerçek emir gönderilecek. Emin misin?"))arm(true);}else arm(false);};
function fmt(v,d=2){return v==null?"-":(+v).toFixed(d);}
async function tick(){try{
 const r=await fetch("/api/state"+q2());if(!r.ok){q("mode").textContent="ERİŞİM YOK";return;}
 const d=await r.json();const live=d.mode==="LIVE ARMED";
 q("mode").textContent=d.mode;q("mode").className="badge "+(live?"live":"paper");
 q("armbtn").textContent=live?"■ DURDUR (paper'a dön)":"▶ CANLIYA GEÇ (ARM)";q("armbtn").className=live?"disarm":"arm";
 q("armnote").textContent=live?"CANLI — sinyal gelince gerçek emir gidecek.":"Paper modda — gerçek emir gitmiyor.";
 q("up").textContent="çalışıyor "+Math.floor(d.uptime/3600)+"s";q("clk").textContent=new Date().toLocaleTimeString();
 const s=d.stats;q("pt").textContent=s.paper_trades;q("pw").textContent=s.paper_winrate==null?"-":s.paper_winrate+"%";
 q("ppnl").textContent="$"+(s.paper_pnl||0).toFixed(2);q("lt").textContent=s.live_trades;q("lpnl").textContent="$"+(s.live_pnl||0).toFixed(2);
 const e=d.equity||[];if(e.length>1){const mn=Math.min(...e,0),mx=Math.max(...e,0),rg=(mx-mn)||1;
  const pts=e.map((v,i)=>(400*i/(e.length-1)).toFixed(1)+","+(56-54*(v-mn)/rg).toFixed(1)).join(" ");
  q("eq").innerHTML='<polyline fill=none stroke='+(e[e.length-1]>=0?"#2ea043":"#f85149")+' stroke-width=2 points="'+pts+'"/>';}
 q("mk").innerHTML=(d.markets||[]).map(m=>{const sg=m.signal||"-";const cl=sg.startsWith("YES")?"up":sg.startsWith("NO")?"down":"";
  return "<tr><td>"+m.city+"</td><td>"+fmt(m.threshold,0)+"</td><td>"+(m.h_obs==null?"-":fmt(m.h_obs,1))+"</td>"
  +"<td>"+(m.peak_passed?"geçti":"—")+"</td><td>"+(100*m.p_yes).toFixed(0)+"%</td>"
  +"<td>"+fmt(m.yes_ask,3)+" / "+fmt(m.no_ask,3)+"</td>"
  +"<td>"+(m.ev_yes==null?"-":(m.ev_yes>0?"+":"")+fmt(m.ev_yes,3))+" / "+(m.ev_no==null?"-":(m.ev_no>0?"+":"")+fmt(m.ev_no,3))+"</td>"
  +"<td class="+cl+">"+sg+"</td></tr>";}).join("")||"<tr><td colspan=8 class=mut>market bekleniyor…</td></tr>";
 q("log").innerHTML=(d.events||[]).map(x=>"<div>"+x+"</div>").join("")||"<div class=mut>…</div>";
}catch(e){q("mode").textContent="BAĞLANTI YOK";}}
tick();setInterval(tick,3000);
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
        return web.json_response({"armed": True})

    async def disarm(req):
        if not ok(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        strat.live_armed = False
        strat._emit("<<< canli mod kapatildi")
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


# =====================================================================================
# Dongu + giris
# =====================================================================================

async def strategy_loop(strat: Strategy, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await strat.step()
        except Exception:  # noqa: BLE001
            logger.exception("adim hatasi")
        await asyncio.sleep(strat.s.POLL_INTERVAL)


async def amain(s: Settings) -> None:
    clob = Clob(s)
    try:
        await clob.connect()   # sadece canli ARM icin gerekli; paper order book public'ten okur
        logger.info("CLOB baglandi (canli ARM hazir).")
    except Exception as exc:  # noqa: BLE001
        logger.warning("CLOB baglanamadi: %s -> sadece PAPER calisir "
                       "(canli ARM icin gecerli PRIVATE_KEY gerekir).", str(exc)[:120])
    strat = Strategy(s, clob)
    stop = asyncio.Event()
    await asyncio.gather(strategy_loop(strat, stop), dashboard(strat, stop))


def _check(s: Settings) -> None:
    print(f"OK · sig={s.SIGNATURE_TYPE} · SDK={'kurulu' if _SDK else 'YOK'} "
          f"· creds={'set' if s.has_creds else 'turetilecek'} · sehirler={s.city_list} "
          f"· EDGE_MARGIN={s.EDGE_MARGIN} · dash={s.DASHBOARD_PORT}")
    print("\nVeri baglaci testi (canli):")
    for ck in s.city_list:
        c = CITIES[ck]
        h = observed_high_f(ck)
        # ornek esik: gozlenen high civari
        T = (round(h) if h else 80)
        ens = ensemble_prob_ge(ck, T, h)
        p = ens["p"] if ens else None
        print(f"  {c['name']:<15} {c['station']}  gozlenen_high={h}F  "
              f"P(>= {T}F)={p}  {'(zirve gecti)' if ens and ens['peak_passed'] else ''}")


# sehir -> Polymarket slug adaylari
SLUG_NAMES = {
    "nyc": ["nyc", "new-york-city", "new-york"], "chicago": ["chicago"],
    "la": ["la", "los-angeles"], "miami": ["miami"], "sf": ["sf", "san-francisco"],
}
# event slug sablonlari (ay-gun-yil doldurulur)
SLUG_TEMPLATES = [
    "highest-temperature-in-{n}-on-{d}",
    "high-temperature-in-{n}-on-{d}",
    "what-will-the-high-temperature-be-in-{n}-on-{d}",
]


def _date_slugs() -> list[str]:
    from datetime import timedelta
    et = ZoneInfo("America/New_York")
    out: list[str] = []
    for off in (0, 1):  # bugun + yarin
        d = datetime.now(et) + timedelta(days=off)
        mon = d.strftime("%B").lower()
        out.append(f"{mon}-{d.day}-{d.year}")
        out.append(f"{mon}-{d.day}")
    return out


def fetch_weather_events(s: Settings) -> list[dict]:
    """Gamma /events?slug= ile sehir-basi weather event'lerini DOGRUDAN cek."""
    events: list[dict] = []
    seen: set[str] = set()
    dates = _date_slugs()
    for ck in s.city_list:
        for name in SLUG_NAMES.get(ck, [ck]):
            for tmpl in SLUG_TEMPLATES:
                for ds in dates:
                    slug = tmpl.format(n=name, d=ds)
                    try:
                        r = requests.get(f"{s.GAMMA_HOST}/events", params={"slug": slug}, timeout=12)
                        if r.status_code != 200:
                            continue
                        data = r.json()
                    except Exception:
                        continue
                    for ev in (data if isinstance(data, list) else [data] if isinstance(data, dict) else []):
                        eid = str(ev.get("id") or ev.get("slug") or slug)
                        if eid in seen:
                            continue
                        seen.add(eid)
                        ev["_city"] = ck
                        ev["_slug"] = slug
                        events.append(ev)
    return events


def _scan_diag(s: Settings) -> None:
    """Teshis: /events?slug= ile weather event'lerini bulup HAM yapisini dok."""
    print("Weather event TESHIS (slug-tabanli /events sorgusu) ...")
    print(f"  denenen tarih slug'lari: {_date_slugs()}")
    events = fetch_weather_events(s)
    if not events:
        print("\n  HIC EVENT BULUNAMADI. Slug sablonu/tarih formati yanlis olabilir.")
        print("  Polymarket'te bir weather marketi ac, URL'deki tam slug'i bana yaz;")
        print("  ornek: polymarket.com/event/<BURADAKI-SLUG> -> sablonu ona gore ayarlarim.")
        return
    for ev in events:
        city = CITIES[ev["_city"]]["name"]
        mkts = ev.get("markets") or []
        print(f"\n=== [{city}] event: {ev.get('title') or ev.get('slug')} ({ev['_slug']}) "
              f"-> {len(mkts)} market ===")
        for m in mkts[:20]:
            toks = _list(m.get("clobTokenIds"))
            outs = _list(m.get("outcomes"))
            prices = _list(m.get("outcomePrices"))
            gt = m.get("groupItemTitle") or ""
            q = str(m.get("question") or "")
            print(f"   tok={len(toks)} out={outs} price={prices} title={gt!r} | {q[:70]}")
    print("\nYorum: her market bir esik/bucket. groupItemTitle ('>= 90°F' / '83-84°')")
    print("       ve outcomePrices'i gorunce parser + edge motorunu buna gore yazacagim.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Polymarket weather botu")
    ap.add_argument("--check", action="store_true", help="config + veri baglaci self-test")
    ap.add_argument("--scan", action="store_true", help="aktif weather marketlerini listele")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for n in ("aiohttp", "httpx", "httpcore", "urllib3", "web3", "py_clob_client_v2"):
        logging.getLogger(n).setLevel(logging.WARNING)
    s = Settings.load()
    if args.check:
        _check(s)
        return
    if args.scan:
        _scan_diag(s)
        return
    logger.info("Weather-Bot basladi (PAPER). Canliya gecis dashboard'dan ARM ile.")
    try:
        asyncio.run(amain(s))
    except KeyboardInterrupt:
        logger.info("durduruldu.")


if __name__ == "__main__":
    main()
