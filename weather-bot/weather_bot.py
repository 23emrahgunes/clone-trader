"""Polymarket Weather Botu -- gunluk-high BUCKET marketleri, "zaten oldu" edge.

YAPI (gercek): her sehir/gun bir EVENT; icinde ~11 BUCKET marketi (Yes/No):
  '77F or below', '78-79F', '80-81F', ..., '94-95F', '96F or higher'.
Sorun: gunun high'i hangi bucket'a duser?

EDGE ("zaten oldu"): gunun max'i ogleden sonra gozlemle kilitlenir; market gec
fiyatlar. Her bucket icin gercek olasilik:
  P(bucket) = final_high in [lo,hi] olan ensemble uye orani,  final=max(H_obs, kalan_gun_max)
Gun ilerledikce H_obs altindaki bucket'lar imkansizlasir (P=0), H_obs'un bucket'i
near-certain (P->1). Market bunu gec fiyatlarsa ucuz kesin tarafi al.
  EV_yes = P(bucket) - YES_fiyat - fee ; EV_no = (1-P) - NO_fiyat - fee
  en iyi EV >= EDGE_MARGIN -> sinyal. Varsayilan PAPER; dashboard'dan tek-tik ARM.

Veri (UCRETSIZ, key yok): METAR gozlem (resolution istasyonu) + Open-Meteo ensemble.
Kesif: Gamma /events?slug=highest-temperature-in-{sehir}-on-{ay}-{gun}-{yil}.
Resolution: NWS CLI gunluk HIGH (F). Paper cozumlemesi Open-Meteo archive.
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
from datetime import datetime, timedelta, timezone
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
# Sehir / istasyon / slug haritasi (resolution = NWS CLI, bu istasyonlar)
# =====================================================================================

CITIES: dict[str, dict] = {
    "nyc":     {"name": "New York City", "station": "KNYC", "lat": 40.78, "lon": -73.97,
                "tz": "America/New_York",    "slugs": ["nyc", "new-york-city", "new-york"]},
    "chicago": {"name": "Chicago",       "station": "KMDW", "lat": 41.79, "lon": -87.75,
                "tz": "America/Chicago",     "slugs": ["chicago"]},
    "la":      {"name": "Los Angeles",   "station": "KLAX", "lat": 33.94, "lon": -118.41,
                "tz": "America/Los_Angeles", "slugs": ["la", "los-angeles"]},
    "miami":   {"name": "Miami",         "station": "KMIA", "lat": 25.79, "lon": -80.29,
                "tz": "America/New_York",    "slugs": ["miami"]},
    "sf":      {"name": "San Francisco", "station": "KSFO", "lat": 37.62, "lon": -122.37,
                "tz": "America/Los_Angeles", "slugs": ["sf", "san-francisco"]},
}
SLUG_TEMPLATES = [
    "highest-temperature-in-{n}-on-{d}",
    "high-temperature-in-{n}-on-{d}",
    "what-will-the-high-temperature-be-in-{n}-on-{d}",
]


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

    CITIES: str = "nyc,chicago,la,miami,sf"
    EDGE_MARGIN: float = 0.06            # min fee-sonrasi EV -> sinyal
    PEAK_ONLY: bool = True              # sadece gunun zirvesi gectiyse (high kilitli) gir
    ORDER_SHARES: float = 10.0
    POLL_INTERVAL: float = 45.0
    FEE_BPS: float = 0.0
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
            EDGE_MARGIN=_f("EDGE_MARGIN", 0.06),
            PEAK_ONLY=_b("PEAK_ONLY", True),
            ORDER_SHARES=_f("ORDER_SHARES", 10.0),
            POLL_INTERVAL=_f("POLL_INTERVAL", 45.0),
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


# =====================================================================================
# Veri baglayicilari  (METAR gozlem + Open-Meteo ensemble)
# =====================================================================================

METAR_URL = "https://aviationweather.gov/api/data/metar"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ENSEMBLE_MODELS = "gfs_seamless,ecmwf_ifs025,icon_seamless"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def observed_high_f(city_key: str) -> Optional[float]:
    """Istasyonun gece-yarisindan (yerel) beri gozlenen MAX sicakligi (F). METAR."""
    c = CITIES[city_key]
    tz = ZoneInfo(c["tz"])
    mid_epoch = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
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
            tc = o.get("temp")
            if tc is None or ts < mid_epoch:
                continue
            f = c2f(float(tc))
        except (TypeError, ValueError):
            continue
        if best is None or f > best:
            best = f
    return round(best, 1) if best is not None else None


def ensemble_finals(city_key: str, h_obs: Optional[float]) -> Optional[dict]:
    """Her ensemble uyesi icin final_max = max(H_obs, uyenin kalan-gun saatlik max'i) (F).

    Dondurur {"finals": [...], "remaining_hours": k, "peak_passed": bool}
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
    keys = [k for k in hourly if k.startswith("temperature_2m_member")] or (
        ["temperature_2m"] if "temperature_2m" in hourly else [])
    if not keys:
        return None
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
    finals = []
    any_exceed = False
    for k in keys:
        vals = hourly.get(k) or []
        rem = [vals[i] for i in rem_idx if i < len(vals) and vals[i] is not None]
        rmax = max(rem) if rem else -999.0
        if h_obs is not None and rmax > h_obs:
            any_exceed = True
        finals.append(max(base, rmax))
    return {"finals": finals, "remaining_hours": len(rem_idx),
            "peak_passed": (h_obs is not None and not any_exceed)}


def bucket_prob(finals: list[float], lo: Optional[float], hi: Optional[float]) -> Optional[float]:
    """P(final_high (yuvarlanmis) bucket [lo,hi] icinde)."""
    if not finals:
        return None
    c = 0
    for f in finals:
        r = round(f)
        if (lo is None or r >= lo) and (hi is None or r <= hi):
            c += 1
    return round(c / len(finals), 3)


def final_high_f(city_key: str, date_local: str) -> Optional[float]:
    """Belirli gunun final gozlenen high'i (Open-Meteo archive)."""
    c = CITIES[city_key]
    try:
        r = requests.get(ARCHIVE_URL, params={
            "latitude": c["lat"], "longitude": c["lon"], "start_date": date_local,
            "end_date": date_local, "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit", "timezone": c["tz"]}, timeout=15)
        if r.status_code != 200:
            return None
        arr = (r.json().get("daily") or {}).get("temperature_2m_max") or []
        return round(float(arr[0]), 1) if arr else None
    except Exception:
        return None


# =====================================================================================
# CLOB istemci (order book + canli emir)
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


def public_best_ask(token: str) -> Optional[float]:
    try:
        r = requests.get(f"{CLOB_PUBLIC}/book", params={"token_id": token}, timeout=10)
        if r.status_code != 200:
            return None
        book = r.json()
    except Exception:
        return None
    best = None
    for a in (book.get("asks") or []):
        try:
            p = float(a["price"])
        except (KeyError, ValueError, TypeError):
            continue
        if best is None or p < best:
            best = p
    return best


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
            logger.error("CLOB baglanmadi; canli emir gonderilemiyor.")
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
            logger.error("Canli emir hatasi: %s", exc)
            return None
        for k in ("orderID", "orderId", "id", "order_id"):
            v = resp.get(k) if isinstance(resp, dict) else getattr(resp, k, None)
            if v:
                return str(v)
        return "posted"


# =====================================================================================
# Weather event / bucket kesfi (Gamma /events?slug=)
# =====================================================================================

def _today_date_slugs(tz_name: str = "America/New_York") -> list[str]:
    """Sehrin KENDI yerel tarihine gore slug adaylari (Pasifik/Dogu farki icin)."""
    d = datetime.now(ZoneInfo(tz_name))
    mon = d.strftime("%B").lower()
    return [f"{mon}-{d.day}-{d.year}", f"{mon}-{d.day}"]


# bucket etiketi -> (lo, hi). "77F or below"->(None,77) "78-79F"->(78,79) "96F or higher"->(96,None)
def parse_bucket(title: str) -> Optional[tuple[Optional[float], Optional[float]]]:
    t = title.lower().replace("°", "").replace("f", "").strip()
    m = re.search(r"(\d+)\s*or\s*(?:below|lower)", t)
    if m:
        return (None, float(m.group(1)))
    m = re.search(r"(\d+)\s*or\s*(?:higher|above)", t)
    if m:
        return (float(m.group(1)), None)
    m = re.search(r"(\d+)\s*-\s*(\d+)", t)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r"(\d+)", t)
    if m:
        return (float(m.group(1)), float(m.group(1)))
    return None


@dataclass
class Bucket:
    label: str
    lo: Optional[float]
    hi: Optional[float]
    yes_token: str
    no_token: str
    yes_price: float
    no_price: float


@dataclass
class WxEvent:
    city: str
    slug: str
    date: str            # sehrin yerel tarihi (YYYY-MM-DD)
    buckets: list[Bucket]


def _fetch_event(gamma: str, slug: str) -> Optional[dict]:
    try:
        r = requests.get(f"{gamma}/events", params={"slug": slug}, timeout=12)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    evs = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    return evs[0] if evs else None


def _parse_event(ev: dict, city: str, slug: str, date_local: str) -> Optional[WxEvent]:
    buckets: list[Bucket] = []
    for m in (ev.get("markets") or []):
        rng = parse_bucket(str(m.get("groupItemTitle") or m.get("question") or ""))
        toks = _list(m.get("clobTokenIds"))
        if rng is None or len(toks) != 2:
            continue
        outs = [str(o).strip().lower() for o in _list(m.get("outcomes"))]
        pr = _list(m.get("outcomePrices"))
        yi = outs.index("yes") if "yes" in outs else 0
        ni = 1 - yi if len(toks) == 2 else 1
        try:
            yp = float(pr[yi]); npx = float(pr[ni])
        except (IndexError, ValueError, TypeError):
            yp = npx = -1.0
        buckets.append(Bucket(
            label=str(m.get("groupItemTitle") or ""), lo=rng[0], hi=rng[1],
            yes_token=str(toks[yi]), no_token=str(toks[ni]), yes_price=yp, no_price=npx))
    if not buckets:
        return None
    return WxEvent(city=city, slug=slug, date=date_local, buckets=buckets)


class Discovery:
    """Sehir basi BUGUNku weather event'ini bulur; calisan slug'i cache'ler."""

    def __init__(self, s: Settings) -> None:
        self.s = s
        self._slug_cache: dict[str, str] = {}   # city -> calisan slug

    def _local_date(self, city: str) -> str:
        return datetime.now(ZoneInfo(CITIES[city]["tz"])).strftime("%Y-%m-%d")

    def find(self) -> dict[str, WxEvent]:
        out: dict[str, WxEvent] = {}
        for ck in self.s.city_list:
            date_local = self._local_date(ck)
            ev = None
            # once cache'lenmis slug
            cached = self._slug_cache.get(ck)
            if cached:
                raw = _fetch_event(self.s.GAMMA_HOST, cached)
                if raw:
                    ev = _parse_event(raw, ck, cached, date_local)
            if ev is None:
                # bugunku tarih slug'lariyla dene
                for name in CITIES[ck]["slugs"]:
                    for tmpl in SLUG_TEMPLATES:
                        for ds in _today_date_slugs(CITIES[ck]["tz"]):
                            slug = tmpl.format(n=name, d=ds)
                            raw = _fetch_event(self.s.GAMMA_HOST, slug)
                            if not raw:
                                continue
                            parsed = _parse_event(raw, ck, slug, date_local)
                            if parsed:
                                ev = parsed
                                self._slug_cache[ck] = slug
                                break
                        if ev:
                            break
                    if ev:
                        break
            if ev:
                out[ck] = ev
        return out


# =====================================================================================
# Strateji (bucket edge motoru + paper + resolution)
# =====================================================================================

@dataclass
class Position:
    city: str
    date: str
    bucket: str
    lo: Optional[float]
    hi: Optional[float]
    side: str
    token: str
    entry_price: float
    shares: float
    p_bucket: float
    h_obs: float
    live: bool
    order_id: str = ""


class Strategy:
    def __init__(self, s: Settings, clob: Clob) -> None:
        self.s = s
        self.clob = clob
        self.disc = Discovery(s)
        self.events: dict[str, WxEvent] = {}
        self.view: dict[str, dict] = {}       # city -> dashboard gorunumu
        self.open_pos: dict[str, Position] = {}   # "city:date" -> pozisyon
        self.live_armed = False
        self.events_log: deque = deque(maxlen=80)
        self.stats = {"paper_trades": 0, "paper_wins": 0, "paper_pnl": 0.0,
                      "live_trades": 0, "live_wins": 0, "live_pnl": 0.0}
        self.equity: deque = deque(maxlen=300)
        self.started = time.time()
        self._last_disc = 0.0
        self._obs_cache: dict[str, tuple[float, Optional[float]]] = {}
        self._ens_cache: dict[str, tuple[float, Optional[dict]]] = {}

    def _emit(self, msg: str) -> None:
        self.events_log.appendleft(f"{time.strftime('%H:%M:%S')} {msg}")

    def _fee(self, price: float) -> float:
        rate = self.s.FEE_BPS / 10000.0
        if rate <= 0 or price <= 0 or price >= 1:
            return 0.0
        return rate * (price * (1 - price)) ** self.s.FEE_EXP

    def _local_date(self, city: str) -> str:
        return datetime.now(ZoneInfo(CITIES[city]["tz"])).strftime("%Y-%m-%d")

    async def _obs(self, city: str) -> Optional[float]:
        c = self._obs_cache.get(city)
        if c and time.time() - c[0] < 240:
            return c[1]
        v = await asyncio.to_thread(observed_high_f, city)
        self._obs_cache[city] = (time.time(), v)
        return v

    async def _ens(self, city: str, h_obs: Optional[float]) -> Optional[dict]:
        c = self._ens_cache.get(city)
        if c and time.time() - c[0] < 300:
            return c[1]
        v = await asyncio.to_thread(ensemble_finals, city, h_obs)
        self._ens_cache[city] = (time.time(), v)
        return v

    async def step(self) -> None:
        if time.time() - self._last_disc > 120 or not self.events:
            self.events = await asyncio.to_thread(self.disc.find)
            self._last_disc = time.time()
            self._emit(f"kesif: {len(self.events)} sehir event bulundu "
                       f"({', '.join(CITIES[c]['name'] for c in self.events)})")
        await self._resolve_closed()
        for ck, ev in self.events.items():
            await self._eval_event(ck, ev)

    async def _eval_event(self, city: str, ev: WxEvent) -> None:
        h_obs = await self._obs(city)
        ens = await self._ens(city, h_obs)
        if ens is None:
            return
        finals = ens["finals"]
        rh = round(h_obs) if h_obs is not None else None  # yuvarlanmis gozlenen high (resolution birimi)
        rows = []
        best = None         # (ev_value, bucket, side, token, price, p) -- global en iyi EV
        best_locked = None  # gozlem TEK BASINA kesinlestirmis taraf (zirveden bagimsiz "zaten oldu")
        for b in ev.buckets:
            p = bucket_prob(finals, b.lo, b.hi)
            if p is None:
                continue
            # fiyat: market outcomePrice (mid) -- hizli; canli girise gercek ask cekilir
            ev_yes = p - b.yes_price - self._fee(b.yes_price) if b.yes_price >= 0 else None
            ev_no = (1 - p) - b.no_price - self._fee(b.no_price) if b.no_price >= 0 else None
            row_locked = False
            for side, evv, tok, price in (("YES", ev_yes, b.yes_token, b.yes_price),
                                          ("NO", ev_no, b.no_token, b.no_price)):
                if evv is None:
                    continue
                if best is None or evv > best[0]:
                    best = (evv, b, side, tok, price, p)
                # "zaten oldu" KILIDI -- sonuc gozlemle belli, kalan forecast onemsiz:
                #   NO kesin  -> bucket tamamen gozlenen high'in ALTINDA (round(H) > hi)
                #   YES kesin -> acik-ust bucket ("T ve uzeri") ve round(H) >= T
                locked = rh is not None and (
                    (side == "NO" and b.hi is not None and rh > b.hi)
                    or (side == "YES" and b.hi is None and b.lo is not None and rh >= b.lo))
                if locked:
                    row_locked = True
                    if best_locked is None or evv > best_locked[0]:
                        best_locked = (evv, b, side, tok, price, p)
            rows.append({"label": b.label, "p": p, "yes": b.yes_price, "no": b.no_price,
                         "ev_yes": (round(ev_yes, 3) if ev_yes is not None else None),
                         "ev_no": (round(ev_no, 3) if ev_no is not None else None),
                         "locked": row_locked})
        # sinyal: gozlem-kilitli aday varsa onu one cikar (kesin), yoksa global en iyi
        pick = (best_locked if best_locked is not None and best_locked[0] >= self.s.EDGE_MARGIN
                else best)
        sig = "-"
        if pick is not None:
            lk = " (kilit)" if pick is best_locked else ""
            sig = (f"{pick[2]} {pick[1].label} (EV={pick[0]:+.3f}){lk}"
                   if pick[0] >= self.s.EDGE_MARGIN else f"bekle (en iyi EV={pick[0]:+.3f})")
        self.view[city] = {"city": CITIES[city]["name"], "date": ev.date, "h_obs": h_obs,
                           "peak_passed": ens["peak_passed"], "remaining_hours": ens["remaining_hours"],
                           "buckets": rows, "signal": sig,
                           "open": f"{city}:{ev.date}" in self.open_pos}
        key = f"{city}:{ev.date}"
        # Giris kapisi:
        #  (a) GOZLEM-KILITLI taraf -> zirve beklemeden gir (gercek "zaten oldu" edge'i).
        #  (b) kilit yoksa ve zirve gectiyse -> global en iyi (ensemble-destekli).
        # Her iki durumda _enter gercek order-book ask'ini teyit eder (sahte 0.000/1.000 elenir).
        peak_ok = ens["peak_passed"] or not self.s.PEAK_ONLY
        cand = None
        if best_locked is not None and best_locked[0] >= self.s.EDGE_MARGIN:
            cand = best_locked
        elif peak_ok and best is not None and best[0] >= self.s.EDGE_MARGIN:
            cand = best
        if cand is not None and h_obs is not None and key not in self.open_pos:
            await self._enter(city, ev, cand)

    async def _enter(self, city: str, ev: WxEvent, best) -> None:
        evv, b, side, token, price, p = best
        # PAPER + CANLI: gercek ASK'i teyit et (outcomePrice/mid bayat olabilir).
        real = await asyncio.to_thread(public_best_ask, token)
        p_side = p if side == "YES" else (1 - p)
        if real is None or (p_side - real - self._fee(real)) < self.s.EDGE_MARGIN:
            self._emit(f"{CITIES[city]['name']} {side} '{b.label}': gercek ask'ta edge yok (mid bayatmis)")
            return
        price = real
        live = False
        order_id = ""
        if self.live_armed:
            oid = await self.clob.buy_fok(token, price, self.s.ORDER_SHARES) or ""
            if oid:
                live = True; order_id = oid
            else:
                self._emit(f"CANLI REDDEDILDI {CITIES[city]['name']} {side} {b.label}")
        h_obs = (await self._obs(city)) or 0.0
        pos = Position(city=city, date=ev.date, bucket=b.label, lo=b.lo, hi=b.hi, side=side,
                       token=token, entry_price=price, shares=self.s.ORDER_SHARES, p_bucket=p,
                       h_obs=h_obs, live=live, order_id=order_id)
        self.open_pos[f"{city}:{ev.date}"] = pos
        tag = "CANLI" if live else "PAPER"
        self._emit(f"{tag} GIRIS {CITIES[city]['name']} {side} '{b.label}' @ {price:.3f} "
                   f"(P={p:.2f}, H_obs={h_obs:.1f})")
        logger.info("%s giris %s %s %s @ %.3f P=%.2f Hobs=%.1f", tag, city, side, b.label, price, p, h_obs)

    async def _resolve_closed(self) -> None:
        for key in list(self.open_pos.keys()):
            pos = self.open_pos[key]
            if self._local_date(pos.city) <= pos.date:
                continue  # gunu bitmedi
            fh = await asyncio.to_thread(final_high_f, pos.city, pos.date)
            if fh is None:
                continue
            r = round(fh)
            bucket_won = (pos.lo is None or r >= pos.lo) and (pos.hi is None or r <= pos.hi)
            won = bucket_won if pos.side == "YES" else (not bucket_won)
            gross = (1.0 - pos.entry_price) if won else (-pos.entry_price)
            pnl = (gross - self._fee(pos.entry_price)) * pos.shares
            self._book(pos, won, pnl, fh, bucket_won)
            self.open_pos.pop(key, None)

    def _book(self, pos: Position, won: bool, pnl: float, final_high: float, bucket_won: bool) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "city": pos.city, "date": pos.date,
               "bucket": pos.bucket, "mode": ("live" if pos.live else "paper"), "side": pos.side,
               "bucket_won": bucket_won, "won": won, "entry": round(pos.entry_price, 4),
               "shares": pos.shares, "p_bucket": round(pos.p_bucket, 3),
               "h_obs_at_entry": round(pos.h_obs, 1), "final_high": round(final_high, 1),
               "pnl": round(pnl, 4), "order_id": pos.order_id}
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
        self._emit(f"SONUC {'CANLI' if pos.live else 'PAPER'} {CITIES[pos.city]['name']} {pos.side} "
                   f"'{pos.bucket}' -> {'KAZANDI' if won else 'KAYBETTI'} (final={final_high:.1f}) PnL={pnl:+.2f}")

    def snapshot(self) -> dict:
        pt = self.stats["paper_trades"]; lt = self.stats["live_trades"]
        return {
            "mode": "LIVE ARMED" if self.live_armed else "PAPER",
            "uptime": int(time.time() - self.started),
            "cities": [self.view[c] for c in self.events if c in self.view],
            "open_positions": [{"city": CITIES[p.city]["name"], "bucket": p.bucket, "side": p.side,
                                "entry": p.entry_price, "live": p.live} for p in self.open_pos.values()],
            "stats": {**self.stats,
                      "paper_winrate": (round(100 * self.stats["paper_wins"] / pt, 1) if pt else None),
                      "live_winrate": (round(100 * self.stats["live_wins"] / lt, 1) if lt else None)},
            "equity": list(self.equity), "events": list(self.events_log),
            "edge_margin": self.s.EDGE_MARGIN,
        }


# =====================================================================================
# Dashboard (aiohttp)
# =====================================================================================

DASH_HTML = """<!doctype html><html lang=tr><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Weather-Bot</title>
<style>
:root{--bg:#0b0f14;--card:#141b24;--line:#243040;--fg:#e6edf3;--mut:#8b98a5;--grn:#2ea043;--red:#f85149;--blu:#388bfd}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Segoe UI,sans-serif}
.wrap{max-width:1150px;margin:0 auto;padding:16px}
header{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
h1{font-size:17px;margin:0}.mut{color:var(--mut)}
.badge{padding:3px 10px;border-radius:999px;font-weight:700;font-size:12px}
.paper{background:rgba(56,139,253,.15);color:var(--blu);border:1px solid var(--blu)}
.live{background:rgba(248,81,73,.15);color:var(--red);border:1px solid var(--red)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.card h3{margin:0 0 6px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut)}
.big{font-size:20px;font-weight:700}
button{font:inherit;font-weight:700;border:0;border-radius:10px;padding:12px 18px;cursor:pointer}
.arm{background:var(--red);color:#fff}.disarm{background:var(--line);color:var(--fg)}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:8px 0 12px}
.city{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px}
.city h2{margin:0 0 2px;font-size:16px}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{padding:4px 8px;border-bottom:1px solid #1c2530;text-align:right}th:first-child,td:first-child{text-align:left}
th{color:var(--mut)}.hot{background:rgba(210,153,34,.12)}.up{color:var(--grn)}.down{color:var(--red)}
.log{background:#0b0f14;border:1px solid var(--line);border-radius:10px;padding:10px;max-height:200px;overflow:auto;font-family:ui-monospace,Consolas,monospace;font-size:12px}
.log div{padding:2px 0;border-bottom:1px solid #161d27}
svg{width:100%;height:54px}
</style></head><body><div class=wrap>
<header><h1>Weather-Bot · Polymarket sıcaklık ("zaten oldu" edge)</h1>
<span id=mode class="badge paper">...</span><span class=mut id=up></span>
<span class=mut style=margin-left:auto id=clk></span></header>

<div class=row><button id=armbtn class=arm>▶ CANLIYA GEÇ (ARM)</button>
<span class=mut id=armnote>Paper modda — gerçek emir gitmiyor.</span></div>

<div class=grid>
<div class=card><h3>Paper işlem</h3><div class=big id=pt>0</div></div>
<div class=card><h3>Paper isabet</h3><div class=big id=pw>-</div></div>
<div class=card><h3>Paper PnL</h3><div class=big id=ppnl>$0</div></div>
<div class=card><h3>Canlı işlem</h3><div class=big id=lt>0</div></div>
<div class=card><h3>Canlı PnL</h3><div class=big id=lpnl>$0</div></div>
</div>
<div class=card style=margin-bottom:12px><h3>Paper PnL eğrisi</h3><svg id=eq viewBox="0 0 400 54" preserveAspectRatio=none></svg></div>

<div id=cities></div>

<div class=mut style=margin:12px 0 6px>OLAY / SONUÇ AKIŞI</div><div class=log id=log></div>
</div>
<script>
const KEY=new URLSearchParams(location.search).get("key");
const q=i=>document.getElementById(i);const q2=()=>KEY?("?key="+encodeURIComponent(KEY)):"";
async function arm(on){await fetch("/api/"+(on?"arm":"disarm")+q2(),{method:"POST"});tick();}
q("armbtn").onclick=()=>{const live=q("mode").textContent==="LIVE ARMED";
 if(!live){if(confirm("CANLI moda geçilecek — gerçek emir gönderilecek. Emin misin?"))arm(true);}else arm(false);};
function f(v,d=2){return v==null?"-":(+v).toFixed(d);}
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
  const pts=e.map((v,i)=>(400*i/(e.length-1)).toFixed(1)+","+(54-52*(v-mn)/rg).toFixed(1)).join(" ");
  q("eq").innerHTML='<polyline fill=none stroke='+(e[e.length-1]>=0?"#2ea043":"#f85149")+' stroke-width=2 points="'+pts+'"/>';}
 q("cities").innerHTML=(d.cities||[]).map(c=>{
  const rows=(c.buckets||[]).map(b=>{const hit=b.p>=0.5;const evy=b.ev_yes,evn=b.ev_no;
   return "<tr class="+(hit?"hot":"")+"><td>"+(b.locked?"🔒 ":"")+b.label+"</td><td>"+(100*b.p).toFixed(0)+"%</td>"
   +"<td>"+f(b.yes,3)+"</td><td>"+f(b.no,3)+"</td>"
   +"<td class="+(evy>0.06?"up":"")+">"+(evy==null?"-":(evy>0?"+":"")+f(evy,3))+"</td>"
   +"<td class="+(evn>0.06?"up":"")+">"+(evn==null?"-":(evn>0?"+":"")+f(evn,3))+"</td></tr>";}).join("");
  const sg=c.signal||"-";const scl=sg.startsWith("YES")||sg.startsWith("NO")?"up":"";
  return "<div class=city><h2>"+c.city+" <span class=mut style=font-size:12px>· "+c.date
   +" · gözlenen high "+(c.h_obs==null?"-":f(c.h_obs,1)+"°F")+(c.peak_passed?" (zirve geçti)":"")
   +" · sinyal: <b class="+scl+">"+sg+"</b></span></h2>"
   +"<table><thead><tr><th>Bucket</th><th>P</th><th>YES</th><th>NO</th><th>EV_Y</th><th>EV_N</th></tr></thead><tbody>"+rows+"</tbody></table></div>";
 }).join("")||"<div class=mut>event bekleniyor…</div>";
 q("log").innerHTML=(d.events||[]).map(x=>"<div>"+x+"</div>").join("")||"<div class=mut>…</div>";
}catch(e){q("mode").textContent="BAĞLANTI YOK";}}
tick();setInterval(tick,4000);
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
        strat.live_armed = True; strat._emit(">>> CANLI MOD AÇILDI (dashboard)")
        return web.json_response({"armed": True})

    async def disarm(req):
        if not ok(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        strat.live_armed = False; strat._emit("<<< canli mod kapatildi")
        return web.json_response({"armed": False})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/arm", arm)
    app.router.add_post("/api/disarm", disarm)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", strat.s.DASHBOARD_PORT).start()
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
        await clob.connect()
        logger.info("CLOB baglandi (canli ARM hazir).")
    except Exception as exc:  # noqa: BLE001
        logger.warning("CLOB baglanamadi: %s -> sadece PAPER.", str(exc)[:120])
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
        ens = ensemble_finals(ck, h)
        if ens:
            fs = ens["finals"]
            lo, hi = (min(fs), max(fs)) if fs else (None, None)
            print(f"  {c['name']:<15} {c['station']}  gozlenen_high={h}F  "
                  f"ensemble final araligi={lo:.0f}-{hi:.0f}F ({len(fs)} uye)"
                  f"{'  (zirve gecti)' if ens['peak_passed'] else ''}")
        else:
            print(f"  {c['name']:<15} {c['station']}  gozlenen_high={h}F  (ensemble alinamadi)")


def _scan_diag(s: Settings) -> None:
    print("Weather event TESHIS (slug-tabanli /events) ...")
    d = Discovery(s)
    events = d.find()
    if not events:
        print("  HIC EVENT BULUNAMADI. Bir Polymarket sicaklik marketinin tam slug'ini yaz.")
        return
    for ck, ev in events.items():
        print(f"\n=== [{CITIES[ck]['name']}] {ev.slug} ({ev.date}) -> {len(ev.buckets)} bucket ===")
        for b in ev.buckets:
            print(f"   {b.label:<16} lo={b.lo} hi={b.hi}  YES={b.yes_price}  NO={b.no_price}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Polymarket weather botu (bucket edge)")
    ap.add_argument("--check", action="store_true", help="config + veri baglaci self-test")
    ap.add_argument("--scan", action="store_true", help="aktif weather event/bucket'lari listele")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for n in ("aiohttp", "httpx", "httpcore", "urllib3", "web3", "py_clob_client_v2"):
        logging.getLogger(n).setLevel(logging.WARNING)
    s = Settings.load()
    if args.check:
        _check(s); return
    if args.scan:
        _scan_diag(s); return
    logger.info("Weather-Bot basladi (PAPER). Canliya gecis dashboard'dan ARM ile.")
    try:
        asyncio.run(amain(s))
    except KeyboardInterrupt:
        logger.info("durduruldu.")


if __name__ == "__main__":
    main()
