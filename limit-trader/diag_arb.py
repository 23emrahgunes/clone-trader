"""Teshis: 5 varlik icin 5dk market var mi + YES+NO ask toplami + GERCEK fee.

READ-ONLY (emir gondermez). Her varlik (BTC/ETH/SOL/XRP/DOGE) icin:
  - Guncel 5dk up/down marketini Gamma slug ile bulur (varsa),
  - YES ve NO en-iyi-ask'ini order book'tan ceker -> toplam,
  - Marketin gercek taker fee oranini (base_fee bps + exponent) ceker,
  - Fee sonrasi net (arb var mi) hesaplar.

Boylece "hangi varliklarda 5dk market var" ve "limit(FOK) emirde fee yeniyor mu,
<0.98 fee sonrasi kar birakiyor mu" sorulari net cevaplanir.

    python diag_arb.py
"""

from __future__ import annotations

import os
import time

import requests
from dotenv import load_dotenv
from py_clob_client_v2 import ApiCreds, ClobClient

load_dotenv()

HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
GAMMA = os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
KEY = os.getenv("PRIVATE_KEY", "").strip()
FUNDER = (os.getenv("PROXY_WALLET_ADDRESS") or os.getenv("PM_EDGE_FUNDER_ADDRESS") or "").strip()
SIG = int(os.getenv("SIGNATURE_TYPE", "3"))
API_KEY, API_SECRET, API_PASS = (os.getenv("CLOB_API_KEY"), os.getenv("CLOB_API_SECRET"),
                                 os.getenv("CLOB_API_PASSPHRASE"))

WINDOW = 300
OFFSETS = (0, -1, 1)
PATTERNS = ("{a}-updown-5m-{ts}", "{a}-up-or-down-5m-{ts}")
# varlik -> denenecek slug adlari
ASSETS = {
    "BTC": ["btc", "bitcoin"],
    "ETH": ["eth", "ethereum"],
    "SOL": ["sol", "solana"],
    "XRP": ["xrp", "ripple"],
    "DOGE": ["doge", "dogecoin"],
}
UP = {"up", "yes", "long"}
DOWN = {"down", "no", "short"}
SAMPLE_SHARES = 5.0


def _client() -> ClobClient:
    creds = None
    if API_KEY and API_SECRET and API_PASS:
        creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASS)
    c = ClobClient(host=HOST, chain_id=CHAIN_ID, key=KEY, creds=creds,
                   signature_type=SIG, funder=FUNDER or None)
    if creds is None and KEY:
        c.set_api_creds(c.derive_api_key())
    return c


def _coerce_list(v):
    import json
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            p = json.loads(v)
            return p if isinstance(p, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _find_market(names: list[str]) -> dict | None:
    now = int(time.time())
    win = now - (now % WINDOW)
    for off in OFFSETS:
        ts = win + off * WINDOW
        for a in names:
            for pat in PATTERNS:
                slug = pat.format(a=a, ts=ts)
                try:
                    r = requests.get(f"{GAMMA}/markets/slug/{slug}", timeout=5)
                    if r.status_code != 200:
                        continue
                    p = r.json()
                except Exception:
                    continue
                if p.get("active") is not True:
                    continue
                toks = _coerce_list(p.get("clobTokenIds"))
                if len(toks) < 2:
                    continue
                outs = [str(o).strip().lower() for o in _coerce_list(p.get("outcomes"))]
                ui = next((i for i, o in enumerate(outs) if o in UP), 0)
                di = next((i for i, o in enumerate(outs) if o in DOWN), 1)
                return {"slug": p.get("slug", slug), "yes": str(toks[ui]), "no": str(toks[di]),
                        "closes": ts + WINDOW}
    return None


def _best_ask(book) -> tuple[float | None, float]:
    asks = getattr(book, "asks", None)
    if asks is None and isinstance(book, dict):
        asks = book.get("asks")
    if not asks:
        return None, 0.0
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
    rows.sort(key=lambda x: x[0])  # en dusuk ask = en iyi
    return rows[0]


def _fee(shares: float, price: float, rate: float, exponent: float) -> float:
    if shares <= 0 or price <= 0 or price >= 1:
        return 0.0
    return shares * rate * (price * (1 - price)) ** exponent


def main() -> None:
    if not KEY:
        print("PRIVATE_KEY .env'de yok."); raise SystemExit(1)
    c = _client()
    print("=" * 78)
    print("5dk ARB TESPIT TESHISI  (YES+NO ask toplami + gercek fee)")
    print("=" * 78)

    for label, names in ASSETS.items():
        m = _find_market(names)
        if not m:
            print(f"\n[{label}]  5dk market BULUNAMADI (bu varlikta 5dk up/down yok olabilir)")
            continue
        stc = m["closes"] - time.time()
        print(f"\n[{label}]  {m['slug']}  (kapanisa {stc:.0f}sn)")
        try:
            yb = c.get_order_book(m["yes"])
            nb = c.get_order_book(m["no"])
        except Exception as exc:
            print(f"   order book alinamadi: {type(exc).__name__}: {exc}")
            continue
        ya, ysz = _best_ask(yb)
        na, nsz = _best_ask(nb)
        if ya is None or na is None:
            print("   YES/NO ask yok (defter bos)")
            continue
        total = ya + na
        # gercek fee
        try:
            y_bps = c.get_fee_rate_bps(m["yes"]); n_bps = c.get_fee_rate_bps(m["no"])
            y_exp = c.get_fee_exponent(m["yes"]); n_exp = c.get_fee_exponent(m["no"])
        except Exception as exc:
            y_bps = n_bps = 0; y_exp = n_exp = 0.0
            print(f"   (fee bilgisi alinamadi: {exc})")
        y_rate, n_rate = y_bps / 10000.0, n_bps / 10000.0

        cost = SAMPLE_SHARES * ya + SAMPLE_SHARES * na
        fee = _fee(SAMPLE_SHARES, ya, y_rate, y_exp) + _fee(SAMPLE_SHARES, na, n_rate, n_exp)
        payout = SAMPLE_SHARES  # kazanan taraf 1$/share oder
        net = payout - cost - fee

        print(f"   YES ask={ya:.3f} (size {ysz:.0f})   NO ask={na:.3f} (size {nsz:.0f})")
        print(f"   TOPLAM (YES+NO ask) = {total:.4f}   {'<0.98 (ham arb VAR)' if total < 0.98 else '>=0.98 (ham arb yok)'}")
        print(f"   FEE: YES base={y_bps}bps exp={y_exp}  |  NO base={n_bps}bps exp={n_exp}"
              + ("   -> FEE YOK (0 bps)" if y_bps == 0 and n_bps == 0 else ""))
        print(f"   {SAMPLE_SHARES:.0f} share: maliyet={cost:.4f}  fee={fee:.4f}  net(fee sonrasi)={net:+.4f} USDC"
              f"   {'>> KARLI' if net > 0 else '>> ZARAR'}")

    print("\n" + "=" * 78)
    print("Yorum: net(fee sonrasi) > 0 olan varliklar GERCEK arb firsati. FEE 0 bps ise")
    print("bu markette taker fee yok demektir (o zaman <0.98 dogrudan kar). base>0 ise fee var.")


if __name__ == "__main__":
    main()
