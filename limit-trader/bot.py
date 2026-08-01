"""Polymarket BTC 5 Dakika CLOB Market-Maker Botu.

Strateji ozeti
--------------
1. Dinamik market kesfi: Polymarket Gamma API uzerinden guncel, aktif BTC 5 dakikalik
   (up/down) marketini otomatik bulur ve YES/NO CLOB token id'lerini cikarir.
2. Cift yonlu limit alim: YES ve NO taraflarina ayni anda ORDER_COUNT adet 1c limit
   alim (GTC) emri koyar.
3. Hizli takip + satis: POLL_INTERVAL (0.5 sn) araliklarla emir durumunu izler. Dolan
   alim emri icin dolan miktarda ANINDA 2c limit satis emri koyar.
4. Vade-sonu korumasi: kapanisa EXPIRY_CANCEL_SECONDS (30 sn) kala tum acik emirleri
   iptal eder (cancel-all). Market kapaninca sonraki 5 dk marketini bulup dongusu bastan
   baslar.

Guvenlik / mimari notlari
-------------------------
- Hassas bilgiler (.env) python-dotenv ile okunur; kaynak kodda anahtar tutulmaz.
- Emir imzalama ve L2 auth resmi ``py-clob-client`` SDK tarafindan yapilir.
- SDK senkron oldugundan tum ag cagrilari ``asyncio.to_thread`` ile ana event loop'u
  bloklamadan calisir; her cagride try/except + kisa exponential-backoff retry vardir.
- ``DRY_RUN=true`` ile gercek emir gonderilmeden tum dongu (kesif, fill, iptal) test edilir.
  Varsayilan ``DRY_RUN=false`` -> GERCEK CANLI emir gonderir.

UYARI: Bu bot gercek USDC ile canli emir gonderir. Calistirma ve fon sorumlulugu
kullanicidadir. 1c limit alimlar defterin dibindedir; cogu dolmaz, dolanlar vade sonunda
tam zarara aciktir. En kritik guvenlik parcasi vade-sonu cancel-all mekanizmasidir.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from dotenv import load_dotenv

# .env dosyasini (varsa) os.environ'a yukle. Gercek ortam degiskenleri her zaman oncelikli.
load_dotenv()

logger = logging.getLogger("btc5m-mm")


# =====================================================================================
# A. KONFIGURASYON  (clone-trader/config.py deseninden uyarlandi)
# =====================================================================================

class ConfigError(RuntimeError):
    """Gerekli konfigurasyon eksik veya gecersiz oldugunda firlatilir."""


def _require(name: str) -> str:
    """Zorunlu bir ortam degiskenini dondur; yoksa yuksek sesle basarisiz ol."""
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ConfigError(
            f"Zorunlu ortam degiskeni '{name}' eksik. .env.example'i .env olarak "
            f"kopyalayip degeri girin."
        )
    return value.strip()


def _optional(name: str, default: Optional[str] = None) -> Optional[str]:
    """Opsiyonel bir ortam degiskenini dondur; yoksa ``default``."""
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"'{name}' bir tam sayi olmali, alinan: {raw!r}.") from exc


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"'{name}' bir sayi olmali, alinan: {raw!r}.") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y", "evet")


@dataclass(frozen=True)
class Settings:
    """Botun calisma-zamani konfigurasyonunun degismez (immutable) gorunumu."""

    # --- Cuzdan / Kimlik (hassas) ---
    PRIVATE_KEY: str = field(repr=False)          # loglardan gizli
    PROXY_WALLET_ADDRESS: str = ""                 # CLOB funder adresi
    RPC_URL: str = ""
    CHAIN_ID: int = 137

    # --- Polymarket sunuculari ---
    CLOB_HOST: str = "https://clob.polymarket.com"
    GAMMA_HOST: str = "https://gamma-api.polymarket.com"

    # Imza tipi (py-clob-client-v2). Gecerli: 0, 1, 2, 3.
    #   0 = EOA               -> anahtarin kendi cuzdanindan islem (funder = self)
    #   1 = POLY_PROXY        -> Polymarket email / Magic login proxy cuzdani
    #   2 = POLY_GNOSIS_SAFE  -> MetaMask / tarayici cuzdani Polymarket hesabi (Safe)
    #   3 = POLY_1271         -> EIP-1271 akilli-kontrat cuzdan imzasi (v2 fork; senin .env.gercek setin)
    # NOT: Resmi py-clob-client 3'u reddeder; bu bot v2 fork kullandigi icin 3 gecerlidir.
    SIGNATURE_TYPE: int = 0

    # --- Opsiyonel L2 API kimlik bilgileri ---
    # Ucu de verilirse dogrudan ApiCreds olarak kullanilir; aksi halde ozel anahtardan turetilir.
    CLOB_API_KEY: Optional[str] = None
    CLOB_API_SECRET: Optional[str] = field(default=None, repr=False)
    CLOB_API_PASSPHRASE: Optional[str] = field(default=None, repr=False)

    # --- Strateji parametreleri ---
    ASSET: str = "btc"
    TIMEFRAME_MIN: int = 5
    BUY_PRICE: float = 0.01               # 1c limit alim
    SELL_PRICE: float = 0.02              # 2c limit satis
    ORDER_COUNT: int = 5                  # taraf basina alim emri sayisi
    SHARES_PER_ORDER: float = 5.0         # emir basina kontrat (Polymarket BTC 5dk'da min $1 YOK)
    POLL_INTERVAL: float = 0.5            # emir durumu izleme araligi (sn)
    EXPIRY_CANCEL_SECONDS: int = 30       # kapanisa bu kadar kala cancel-all
    DISCOVERY_RETRY_SECONDS: float = 2.0  # market bulunamazsa bekleme

    # --- Guvenlik anahtari ---
    DRY_RUN: bool = False                 # True: gercek emir gondermez, sadece simule + loglar

    @property
    def has_clob_creds(self) -> bool:
        """Uc L2 kimlik bilgisi de verilmisse True."""
        return bool(self.CLOB_API_KEY and self.CLOB_API_SECRET and self.CLOB_API_PASSPHRASE)

    @property
    def window_seconds(self) -> int:
        return self.TIMEFRAME_MIN * 60

    @classmethod
    def load(cls) -> "Settings":
        """Mevcut ortamdan dogrulanmis bir Settings ornegi olustur (baslangicta hizli-hata)."""
        dry_run = _get_bool("DRY_RUN", False)
        # DRY_RUN'da bile config yuklenebilsin diye ozel anahtar DRY_RUN'da opsiyonel.
        private_key = _optional("PRIVATE_KEY", "") if dry_run else _require("PRIVATE_KEY")
        inst = cls(
            PRIVATE_KEY=private_key or "",
            PROXY_WALLET_ADDRESS=_optional("PROXY_WALLET_ADDRESS", "") or "",
            RPC_URL=_optional("RPC_URL", "") or "",
            CHAIN_ID=_get_int("CHAIN_ID", 137),
            CLOB_HOST=_optional("CLOB_HOST", "https://clob.polymarket.com") or "https://clob.polymarket.com",
            GAMMA_HOST=_optional("GAMMA_HOST", "https://gamma-api.polymarket.com") or "https://gamma-api.polymarket.com",
            SIGNATURE_TYPE=_get_int("SIGNATURE_TYPE", 0),
            CLOB_API_KEY=_optional("CLOB_API_KEY"),
            CLOB_API_SECRET=_optional("CLOB_API_SECRET"),
            CLOB_API_PASSPHRASE=_optional("CLOB_API_PASSPHRASE"),
            ASSET=(_optional("ASSET", "btc") or "btc").lower(),
            TIMEFRAME_MIN=_get_int("TIMEFRAME_MIN", 5),
            BUY_PRICE=_get_float("BUY_PRICE", 0.01),
            SELL_PRICE=_get_float("SELL_PRICE", 0.02),
            ORDER_COUNT=_get_int("ORDER_COUNT", 5),
            SHARES_PER_ORDER=_get_float("SHARES_PER_ORDER", 5.0),
            POLL_INTERVAL=_get_float("POLL_INTERVAL", 0.5),
            EXPIRY_CANCEL_SECONDS=_get_int("EXPIRY_CANCEL_SECONDS", 30),
            DISCOVERY_RETRY_SECONDS=_get_float("DISCOVERY_RETRY_SECONDS", 2.0),
            DRY_RUN=dry_run,
        )
        if inst.SIGNATURE_TYPE not in (0, 1, 2, 3):
            raise ConfigError(
                f"SIGNATURE_TYPE={inst.SIGNATURE_TYPE} gecersiz. Gecerli degerler: 0 (EOA), "
                "1 (POLY_PROXY / email login), 2 (POLY_GNOSIS_SAFE / MetaMask), "
                "3 (POLY_1271 / EIP-1271 akilli-kontrat cuzdani). Hangisinin dogru oldugunu "
                "'python diag_balance.py' ile (read-only) bulun."
            )
        if not inst.DRY_RUN and not inst.PROXY_WALLET_ADDRESS:
            raise ConfigError(
                "Canli modda PROXY_WALLET_ADDRESS (CLOB funder adresi) zorunludur."
            )
        return inst


# =====================================================================================
# B. POLYMARKET CLOB ISTEMCISI  (clone-trader/trader.py + pyton-polymarket/micro_live.py)
# =====================================================================================

# py-clob-client-v2 (fork) kullanilir -- kullanicinin canli edge botlariyla ayni yigin.
# Bu fork SignatureTypeV2.POLY_1271=3'u destekler (resmi py-clob-client yalnizca 0/1/2 kabul eder).
_SDK_AVAILABLE = False
try:  # pragma: no cover - kurulum ortamina bagli
    from py_clob_client_v2 import (
        ApiCreds,
        ClobClient,
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        Side,
    )

    BUY, SELL = Side.BUY, Side.SELL
    _SDK_AVAILABLE = True
except Exception:  # pragma: no cover
    BUY, SELL = "BUY", "SELL"  # type: ignore
    ClobClient = None  # type: ignore
    Side = None  # type: ignore

_DEFAULT_TICK = 0.01
FILLED_STATUSES = {"filled", "matched"}


class ExecutionError(RuntimeError):
    """Bir emir kurulamaz, fiyatlanamaz veya kabul edilemezse firlatilir."""


class PolyClobClient:
    """Tek, uzun-omurlu ``ClobClient`` etrafinda ince async sarmalayici."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None
        self._connected = False
        self._meta_cache: dict[str, tuple[str, float, bool]] = {}

    # -- yasam dongusu -----------------------------------------------------------------

    def _build_client(self) -> Any:
        """ClobClient'i kur (ozel anahtari parse eder, bu yuzden yalnizca talep uzerine cagirilir)."""
        if not _SDK_AVAILABLE:
            raise ExecutionError(
                "py-clob-client kurulu degil. 'pip install -r requirements.txt' calistirin."
            )
        preset_creds = None
        if self.settings.has_clob_creds:
            preset_creds = ApiCreds(
                api_key=self.settings.CLOB_API_KEY,
                api_secret=self.settings.CLOB_API_SECRET,
                api_passphrase=self.settings.CLOB_API_PASSPHRASE,
            )
        client = ClobClient(
            host=self.settings.CLOB_HOST,
            chain_id=self.settings.CHAIN_ID,
            key=self.settings.PRIVATE_KEY,
            creds=preset_creds,
            signature_type=self.settings.SIGNATURE_TYPE,
            funder=self.settings.PROXY_WALLET_ADDRESS,
        )
        self._connected = preset_creds is not None
        return client

    async def connect(self) -> None:
        """Client'i kur ve L2 API kimlik bilgilerini ekle (gerektiginde ozel anahtardan turet)."""
        if self.settings.DRY_RUN:
            # DRY_RUN'da gercek imzalama/auth yapilmaz.
            self._connected = True
            logger.info("[DRY_RUN] Client baglantisi simule edildi (gercek auth yok).")
            return
        if self._client is None:
            self._client = await asyncio.to_thread(self._build_client)
        if self._connected:
            return
        try:
            creds = await asyncio.to_thread(self._client.derive_api_key)
            self._client.set_api_creds(creds)
            self._connected = True
            logger.info(
                "ClobClient turetilen kimlikle baglandi (funder=%s, sig_type=%s)",
                self.settings.PROXY_WALLET_ADDRESS, self.settings.SIGNATURE_TYPE,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("CLOB kimlik dogrulama basarisiz")
            raise ExecutionError(f"CLOB auth basarisiz: {exc}") from exc

    # -- retry yardimcisi ---------------------------------------------------------------

    async def _call(self, fn_name: str, *args: Any, retries: int = 3) -> Any:
        """Senkron SDK metodunu thread'de, kisa exponential-backoff retry ile cagir."""
        method = getattr(self._client, fn_name, None)
        if not callable(method):
            raise ExecutionError(f"ClobClient '{fn_name}' metodunu icermiyor.")
        delay = 0.3
        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                return await asyncio.to_thread(method, *args)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("%s deneme %d/%d basarisiz: %s", fn_name, attempt, retries, exc)
                if attempt < retries:
                    await asyncio.sleep(delay)
                    delay *= 2
        raise ExecutionError(f"{fn_name} {retries} denemede basarisiz: {last_exc}") from last_exc

    # -- market meta --------------------------------------------------------------------

    async def market_meta(self, token_id: str) -> tuple[str, float, bool]:
        """(tick_str, tick_float, neg_risk) dondur. neg_risk yanlissa emir reddedilir."""
        if token_id in self._meta_cache:
            return self._meta_cache[token_id]
        if self.settings.DRY_RUN:
            meta = ("0.01", 0.01, False)
            self._meta_cache[token_id] = meta
            return meta
        try:
            tick_str = str(await self._call("get_tick_size", token_id))
        except Exception:  # noqa: BLE001
            logger.warning("get_tick_size %s icin basarisiz; varsayilan 0.01", token_id)
            tick_str = "0.01"
        try:
            neg_risk = bool(await self._call("get_neg_risk", token_id))
        except Exception:  # noqa: BLE001
            logger.warning("get_neg_risk %s icin basarisiz; varsayilan False", token_id)
            neg_risk = False
        try:
            tick = float(tick_str)
        except (TypeError, ValueError):
            tick = _DEFAULT_TICK
        meta = (tick_str, tick, neg_risk)
        self._meta_cache[token_id] = meta
        return meta

    # -- emir islemleri -----------------------------------------------------------------

    async def place_limit(self, token_id: str, side: str, price: float, size: float) -> Optional[str]:
        """token_id icin GTC limit emri gonder; kabul edilen order_id'yi dondur (yoksa None)."""
        tick_str, tick, neg_risk = await self.market_meta(token_id)
        # Fiyati tick'e hizala (borsa aksi halde reddeder).
        aligned = max(tick, round(price / tick) * tick)
        aligned = round(aligned, 6)

        if self.settings.DRY_RUN:
            fake_id = f"dry-{uuid.uuid4().hex[:12]}"
            logger.info(
                "[DRY_RUN] EMIR %s token=%s fiyat=%.4f miktar=%.2f (order_id=%s)",
                side, token_id[:10], aligned, size, fake_id,
            )
            return fake_id

        order_args = OrderArgs(token_id=token_id, price=aligned, size=size, side=side)
        options = PartialCreateOrderOptions(tick_size=tick_str, neg_risk=neg_risk)
        try:
            # create_and_post_order tek adimda imzalar + gonderir (GTC).
            resp = await self._call(
                "create_and_post_order", order_args, options, OrderType.GTC,
            )
        except ExecutionError:
            # Bazi SDK surumlerinde create_and_post_order farkli imza ister; iki-adima dus.
            signed = await self._call("create_order", order_args, options)
            resp = await self._call("post_order", signed, OrderType.GTC)

        order_id = _extract_order_id(resp)
        accepted = _payload_get(resp, "success", True)
        logger.info(
            "EMIR %s token=%s fiyat=%.4f miktar=%.2f kabul=%s order_id=%s",
            side, token_id[:10], aligned, size, accepted, order_id,
        )
        return order_id or None

    async def get_order(self, order_id: str) -> Any:
        """Bir emrin guncel durumunu dondur (dolum takibi icin)."""
        if self.settings.DRY_RUN:
            return {"status": "open", "size_matched": "0"}
        return await self._call("get_order", order_id)

    async def cancel(self, order_id: str) -> Any:
        """Tek bir emri iptal et."""
        if self.settings.DRY_RUN:
            logger.info("[DRY_RUN] IPTAL order_id=%s", order_id)
            return {"canceled": [order_id]}
        try:
            return await self._call("cancel", order_id)
        except ExecutionError:
            return await self._call("cancel_order", order_id)

    async def cancel_all(self, tracked_ids: Optional[list[str]] = None) -> Any:
        """Tum acik emirleri iptal et (vade-sonu koruma). SDK cancel_all yoksa tekil dongu."""
        if self.settings.DRY_RUN:
            logger.info("[DRY_RUN] CANCEL_ALL (izlenen %d emir)", len(tracked_ids or []))
            return {"canceled": list(tracked_ids or [])}
        # Once toplu cancel_all dene.
        for fn in ("cancel_all", "cancel_orders"):
            method = getattr(self._client, fn, None)
            if callable(method):
                try:
                    if fn == "cancel_orders" and tracked_ids:
                        return await self._call(fn, tracked_ids)
                    if fn == "cancel_all":
                        return await self._call(fn)
                except Exception:  # noqa: BLE001
                    logger.exception("%s basarisiz; tekil iptale geciliyor", fn)
                    break
        # Fallback: izlenen her order_id icin tekil iptal (micro_live cancel_open_orders deseni).
        results = []
        for oid in list(tracked_ids or []):
            try:
                results.append(await self.cancel(oid))
            except Exception:  # noqa: BLE001
                logger.exception("Tekil iptal basarisiz order_id=%s", oid)
        return results


# -- payload yardimcilari (micro_live anahtar-toleransli cozumu) -------------------------

def _payload_get(payload: Any, key: str, default: Any) -> Any:
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        snake = _camel_to_snake(key)
        if snake in payload:
            return payload[snake]
        return default
    return getattr(payload, key, getattr(payload, _camel_to_snake(key), default))


def _camel_to_snake(value: str) -> str:
    out: list[str] = []
    for ch in value:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out).lstrip("_")


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def matched_size(payload: Any) -> float:
    """Bir emir payload'undan dolan (matched) miktari cikar."""
    for key in ("size_matched", "matched_size", "matchedAmount", "takingAmount", "filled_size"):
        v = _payload_get(payload, key, None)
        if v is not None and v != "":
            return _to_float(v)
    return 0.0


def is_filled(payload: Any, submitted_size: float) -> bool:
    """Emrin tamamen dolup dolmadigi (status veya matched >= gonderilen)."""
    status = str(_payload_get(payload, "status", "")).lower()
    if status in FILLED_STATUSES:
        return True
    return submitted_size > 0 and matched_size(payload) + 1e-9 >= submitted_size


def _extract_order_id(payload: Any) -> str:
    for key in ("orderID", "orderId", "id", "order_id"):
        oid = _payload_get(payload, key, None)
        if oid:
            return str(oid)
    return ""


# =====================================================================================
# C. MARKET KESFI  (pyton-polymarket/market_discovery.py + reversal-trading/polymarket_feed.py)
# =====================================================================================

SLUG_PATTERNS = ("{asset}-updown-{tf}m-{ts}", "{asset}-up-or-down-{tf}m-{ts}")
OFFSETS = (0, -1, 1, 2)
UP_OUTCOMES = {"up", "long", "yes"}
DOWN_OUTCOMES = {"down", "short", "no"}


@dataclass
class DiscoveredMarket:
    slug: str
    yes_token: str      # up / yes tarafi
    no_token: str       # down / no tarafi
    window_start: int   # 5 dk pencere baslangici (unix sn)
    closes_at: int      # window_start + window_seconds

    def seconds_to_close(self) -> float:
        return self.closes_at - time.time()


class MarketDiscovery:
    """Guncel aktif BTC 5 dk marketini Gamma slug lookup ile bulur."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def current_window(self, now: Optional[int] = None) -> int:
        now = int(time.time()) if now is None else now
        w = self.settings.window_seconds
        return now - (now % w)

    def slug_candidates(self) -> list[tuple[str, int]]:
        """(slug, window_start) adaylari; en olasi (offset 0) once."""
        w = self.settings.window_seconds
        window = self.current_window()
        out: list[tuple[str, int]] = []
        seen: set[str] = set()
        for offset in OFFSETS:
            ts = window + offset * w
            for pattern in SLUG_PATTERNS:
                slug = pattern.format(asset=self.settings.ASSET, tf=self.settings.TIMEFRAME_MIN, ts=ts)
                if slug in seen:
                    continue
                seen.add(slug)
                out.append((slug, ts))
        return out

    def _fetch_slug(self, slug: str) -> Optional[dict]:
        url = f"{self.settings.GAMMA_HOST}/markets/slug/{slug}"
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200:
                return None
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError):
            return None

    async def discover(self) -> Optional[DiscoveredMarket]:
        """Ilk aktif, henuz kapanmamis BTC 5 dk marketini dondur (yoksa None)."""
        for slug, window_start in self.slug_candidates():
            payload = await asyncio.to_thread(self._fetch_slug, slug)
            if not payload or payload.get("active") is not True:
                continue
            tokens = _coerce_list(payload.get("clobTokenIds"))
            if len(tokens) < 2:
                continue
            outcomes = _coerce_list(payload.get("outcomes"))
            up_idx, down_idx = _resolve_outcome_indexes(outcomes)
            try:
                yes_token = str(tokens[up_idx])
                no_token = str(tokens[down_idx])
            except IndexError:
                continue
            closes_at = window_start + self.settings.window_seconds
            if closes_at - time.time() <= 0:
                continue  # zaten kapanmis pencere
            market = DiscoveredMarket(
                slug=str(payload.get("slug") or slug),
                yes_token=yes_token,
                no_token=no_token,
                window_start=window_start,
                closes_at=closes_at,
            )
            logger.info(
                "MARKET BULUNDU slug=%s kapanisa=%.0fsn YES=%s NO=%s",
                market.slug, market.seconds_to_close(), yes_token[:10], no_token[:10],
            )
            return market
        return None


def _coerce_list(value: Any) -> list[Any]:
    """clobTokenIds/outcomes JSON-icinde-JSON string olabilir; listeye cevir."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _resolve_outcome_indexes(outcomes: list[Any]) -> tuple[int, int]:
    up_idx = _find_outcome_index(outcomes, UP_OUTCOMES)
    down_idx = _find_outcome_index(outcomes, DOWN_OUTCOMES)
    if up_idx is None or down_idx is None or up_idx == down_idx:
        return 0, 1
    return up_idx, down_idx


def _find_outcome_index(outcomes: list[Any], keywords: set[str]) -> Optional[int]:
    for idx, outcome in enumerate(outcomes):
        if str(outcome).strip().lower() in keywords:
            return idx
    return None


# =====================================================================================
# D. STRATEJI DONGUSU
# =====================================================================================

@dataclass
class OpenBuy:
    """Acik bir alim emrinin izleme kaydi."""
    order_id: str
    token_id: str
    side_label: str          # "YES" / "NO" (log icin)
    size: float
    matched_sold: float = 0.0  # bu emrin satisa cikarilmis dolan miktari


class MarketMaker:
    """Ana market-maker stratejisi: kesif -> cift-yon alim -> fill/satis -> vade-sonu iptal."""

    def __init__(self, settings: Settings, client: PolyClobClient, discovery: MarketDiscovery) -> None:
        self.settings = settings
        self.client = client
        self.discovery = discovery
        # order_id -> OpenBuy
        self._open_buys: dict[str, OpenBuy] = {}
        # order_id -> gonderilen satis miktari
        self._open_sells: dict[str, float] = {}
        self._all_order_ids: set[str] = set()  # cancel-all icin izlenen tum emirler

    async def run_forever(self) -> None:
        """Sonsuz dongu: guncel marketi bul, isle, kapaninca sonrakine gec."""
        await self.client.connect()
        while True:
            market = await self.discovery.discover()
            if market is None:
                logger.info("Aktif BTC 5 dk market yok; %.1fsn sonra tekrar denenecek",
                            self.settings.DISCOVERY_RETRY_SECONDS)
                await asyncio.sleep(self.settings.DISCOVERY_RETRY_SECONDS)
                continue
            try:
                await self.trade_market(market)
            except Exception:  # noqa: BLE001 - tek market hatasi tum botu dusurmez
                logger.exception("Market islenirken hata slug=%s; guvenlik icin cancel-all", market.slug)
                await self._sweep_cancel_all()
            # Market kapanana kadar kisa bekle, sonra sonraki pencereye gec.
            while market.seconds_to_close() > 0:
                await asyncio.sleep(0.5)

    async def trade_market(self, market: DiscoveredMarket) -> None:
        """Tek bir market icin: cift-yon alim koy, fill/satis takip et, vade-sonu iptal."""
        self._open_buys.clear()
        self._open_sells.clear()
        self._all_order_ids.clear()

        # Kapanisa cok az kalmissa yeni emir koyma.
        if market.seconds_to_close() <= self.settings.EXPIRY_CANCEL_SECONDS:
            logger.info("Market %s kapanisa cok yakin (%.0fsn); atlaniyor",
                        market.slug, market.seconds_to_close())
            return

        # 1) YES ve NO taraflarina ORDER_COUNT adet 1c limit alim.
        await self._place_side_buys(market.yes_token, "YES")
        await self._place_side_buys(market.no_token, "NO")

        # 2) Hizli takip dongusu.
        while True:
            # Vade-sonu korumasi: kapanisa EXPIRY_CANCEL_SECONDS kala her seyi iptal et.
            if market.seconds_to_close() <= self.settings.EXPIRY_CANCEL_SECONDS:
                logger.warning("VADE-SONU (%.0fsn kaldi) -> cancel-all", market.seconds_to_close())
                await self._sweep_cancel_all()
                return

            await self._poll_buys()
            await self._poll_sells()

            # Is bittiyse (acik alim/satis kalmadiysa) kapanisa kadar bekle.
            if not self._open_buys and not self._open_sells:
                # Yeni emir koymuyoruz; sadece vade-sonu bekleniyor.
                pass

            await asyncio.sleep(self.settings.POLL_INTERVAL)

    async def _place_side_buys(self, token_id: str, side_label: str) -> None:
        for _ in range(self.settings.ORDER_COUNT):
            order_id = await self.client.place_limit(
                token_id, BUY, self.settings.BUY_PRICE, self.settings.SHARES_PER_ORDER,
            )
            if order_id:
                self._open_buys[order_id] = OpenBuy(
                    order_id=order_id, token_id=token_id,
                    side_label=side_label, size=self.settings.SHARES_PER_ORDER,
                )
                self._all_order_ids.add(order_id)

    async def _poll_buys(self) -> None:
        """Acik alimlari izle; dolan miktar icin 2c satis emri koy."""
        for order_id in list(self._open_buys.keys()):
            buy = self._open_buys[order_id]
            try:
                payload = await self.client.get_order(order_id)
            except Exception:  # noqa: BLE001
                continue
            matched = matched_size(payload)
            unsold = matched - buy.matched_sold
            fully = is_filled(payload, buy.size)

            # Dolan (henuz satisa cikmamis) her miktar icin 2c satis koy (min $1 kurali yok).
            if unsold > 0:
                logger.info("DOLUM %s order=%s dolan=%.2f -> %.2f share 2c satis",
                            buy.side_label, order_id, matched, unsold)
                sell_id = await self.client.place_limit(
                    buy.token_id, SELL, self.settings.SELL_PRICE, unsold,
                )
                if sell_id:
                    self._open_sells[sell_id] = unsold
                    self._all_order_ids.add(sell_id)
                    buy.matched_sold += unsold

            if fully:
                # Alim tamamen doldu ve satisa cikarildi; izlemeden dusur.
                self._open_buys.pop(order_id, None)

    async def _poll_sells(self) -> None:
        """Acik satislari izle; dolan satislari kar realize ederek dusur."""
        for order_id in list(self._open_sells.keys()):
            size = self._open_sells[order_id]
            try:
                payload = await self.client.get_order(order_id)
            except Exception:  # noqa: BLE001
                continue
            if is_filled(payload, size):
                logger.info("SATIS TAMAM order=%s miktar=%.2f (kar realize)", order_id, size)
                self._open_sells.pop(order_id, None)

    async def _sweep_cancel_all(self) -> None:
        """Tum acik alim + satis emirlerini iptal et ve yerel durumu temizle."""
        await self.client.cancel_all(list(self._all_order_ids))
        self._open_buys.clear()
        self._open_sells.clear()
        self._all_order_ids.clear()


# =====================================================================================
# E. GIRIS NOKTASI
# =====================================================================================

def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Gurultulu HTTP kutuphane loglarini sustur; sadece botun kendi INFO mesajlari kalsin
    # (EMIR / DOLUM / SATIS / MARKET BULUNDU / VADE-SONU). bot.log okunur kalir ve sismez.
    for noisy in ("httpx", "httpcore", "urllib3", "web3", "py_clob_client_v2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _print_config_check(settings: Settings) -> None:
    """Hassas degerleri maskeleyerek konfigurasyonu yazdir (self-check)."""
    print("Konfigurasyon basariyla yuklendi.")
    print(f"  DRY_RUN              : {settings.DRY_RUN}")
    print(f"  ASSET / TIMEFRAME    : {settings.ASSET} / {settings.TIMEFRAME_MIN}m")
    print(f"  CLOB_HOST            : {settings.CLOB_HOST}")
    print(f"  GAMMA_HOST           : {settings.GAMMA_HOST}")
    print(f"  CHAIN_ID             : {settings.CHAIN_ID}")
    print(f"  SIGNATURE_TYPE       : {settings.SIGNATURE_TYPE}")
    print(f"  PROXY_WALLET_ADDRESS : {settings.PROXY_WALLET_ADDRESS or '(bos)'}")
    print(f"  BUY/SELL PRICE       : {settings.BUY_PRICE} / {settings.SELL_PRICE}")
    print(f"  ORDER_COUNT x SHARES : {settings.ORDER_COUNT} x {settings.SHARES_PER_ORDER}")
    print(f"  POLL_INTERVAL        : {settings.POLL_INTERVAL}s")
    print(f"  EXPIRY_CANCEL_SECONDS: {settings.EXPIRY_CANCEL_SECONDS}s")
    print(f"  PRIVATE_KEY          : {'*** set ***' if settings.PRIVATE_KEY else 'MISSING'}")
    print(f"  L2 API creds         : {'*** set ***' if settings.has_clob_creds else '(turetilecek)'}")
    print(f"  py-clob-client SDK   : {'kurulu' if _SDK_AVAILABLE else 'KURULU DEGIL'}")


async def _amain(settings: Settings) -> None:
    client = PolyClobClient(settings)
    discovery = MarketDiscovery(settings)
    maker = MarketMaker(settings, client, discovery)
    try:
        await maker.run_forever()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.warning("Kapatma sinyali; acik emirler iptal ediliyor (cancel-all)...")
        await maker._sweep_cancel_all()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m CLOB market-maker botu")
    parser.add_argument("--check", action="store_true",
                        help="Sadece konfigurasyonu yukle ve dogrula, bot baslatma.")
    args = parser.parse_args()

    _configure_logging()
    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"KONFIGURASYON HATASI: {exc}")
        raise SystemExit(1)

    if args.check:
        _print_config_check(settings)
        return

    if not settings.DRY_RUN:
        logger.warning("CANLI MOD: gercek USDC ile emir gonderilecek. (Durdurmak icin Ctrl+C)")

    try:
        asyncio.run(_amain(settings))
    except KeyboardInterrupt:
        logger.info("Bot durduruldu.")


if __name__ == "__main__":
    main()
