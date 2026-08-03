"""Arb-logger: 5 varligi surekli tarar, FEE sonrasi net-pozitif anlari kaydeder.

READ-ONLY -- ASLA emir gondermez. Amac: "YES+NO ask + fee < 1.00 (garanti kar)"
durumu bu marketlerde gercekten oluyor mu, ne siklikla, hangi varlikta, kac saniye?
Bunu gunlerce veri toplayarak KANITLA gormek icin.

Her tarama, hedef boyut icin (SAMPLE_SHARES) derinlik-dogru "en kotu dolum" fiyatini
ve gercek fee'yi hesaplar; net > MIN_NET ise firsati arb_hits.jsonl'a yazar.

    python arb_logger.py

Ayarlar (.env veya ortam):
  ARB_SCAN_INTERVAL (sn, vars. 2) · ARB_SAMPLE_SHARES (vars. 5) · ARB_MIN_NET (USDC, vars. 0)
  ARB_ASSETS (vars. "btc,eth,sol,xrp,doge")
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from py_clob_client_v2 import ApiCreds, ClobClient

load_dotenv()

# nohup ile dosyaya yazarken print ciktisi tamponlanmasin -> arb.log aninda dolsun.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
GAMMA = os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
KEY = os.getenv("PRIVATE_KEY", "").strip()
FUNDER = (os.getenv("PROXY_WALLET_ADDRESS") or os.getenv("PM_EDGE_FUNDER_ADDRESS") or "").strip()
SIG = int(os.getenv("SIGNATURE_TYPE", "3"))
API_KEY, API_SECRET, API_PASS = (os.getenv("CLOB_API_KEY"), os.getenv("CLOB_API_SECRET"),
                                 os.getenv("CLOB_API_PASSPHRASE"))

INTERVAL = float(os.getenv("ARB_SCAN_INTERVAL", "2"))
SAMPLE = float(os.getenv("ARB_SAMPLE_SHARES", "5"))
MIN_NET = float(os.getenv("ARB_MIN_NET", "0"))
ASSET_LIST = [a.strip().lower() for a in os.getenv("ARB_ASSETS", "btc,eth,sol,xrp,doge").split(",") if a.strip()]
HITS_FILE = "arb_hits.jsonl"

WINDOW = 300
OFFSETS = (0, -1, 1)
PATTERNS = ("{a}-updown-5m-{ts}", "{a}-up-or-down-5m-{ts}")
ALIASES = {"btc": ["btc", "bitcoin"], "eth": ["eth", "ethereum"], "sol": ["sol", "solana"],
           "xrp": ["xrp", "ripple"], "doge": ["doge", "dogecoin"]}
UP = {"up", "yes", "long"}
DOWN = {"down", "no", "short"}


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
                        "closes": ts + WINDOW, "window": win}
    return None


def _asks(book) -> list[tuple[float, float]]:
    a = getattr(book, "asks", None)
    if a is None and isinstance(book, dict):
        a = book.get("asks")
    rows = []
    for e in (a or []):
        pr = getattr(e, "price", None) if not isinstance(e, dict) else e.get("price")
        sz = getattr(e, "size", None) if not isinstance(e, dict) else e.get("size")
        try:
            rows.append((float(pr), float(sz)))
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x[0])  # en dusuk ask once
    return rows


def _worst_fill(asks: list[tuple[float, float]], n: float) -> tuple[float | None, float]:
    acc = 0.0
    for price, size in asks:
        acc += size
        if acc >= n:
            return price, acc
    return None, acc


def _fee(shares: float, price: float, rate: float, exponent: float) -> float:
    if shares <= 0 or price <= 0 or price >= 1:
        return 0.0
    return shares * rate * (price * (1 - price)) ** exponent


class ArbLogger:
    def __init__(self, client: ClobClient) -> None:
        self.c = client
        self.markets: dict[str, dict | None] = {}
        self.window = -1
        self.fee_cache: dict[str, tuple[float, float]] = {}  # token -> (rate, exponent)
        self.scans = 0
        self.hits = 0
        self.per_asset_hits: dict[str, int] = {a: 0 for a in ASSET_LIST}
        self.best_net: dict[str, float] = {a: float("-inf") for a in ASSET_LIST}

    def _fee_for(self, token: str) -> tuple[float, float]:
        if token not in self.fee_cache:
            try:
                bps = self.c.get_fee_rate_bps(token)
                exp = self.c.get_fee_exponent(token)
                self.fee_cache[token] = (bps / 10000.0, float(exp))
            except Exception:
                self.fee_cache[token] = (0.0, 0.0)
        return self.fee_cache[token]

    def _refresh_markets(self) -> None:
        now = int(time.time())
        win = now - (now % WINDOW)
        if win == self.window:
            return
        self.window = win
        self.fee_cache.clear()
        for a in ASSET_LIST:
            self.markets[a] = _find_market(ALIASES.get(a, [a]))
        found = [a.upper() for a, m in self.markets.items() if m]
        print(f"[{_hhmmss()}] pencere yenilendi -> market bulunan: {', '.join(found) or 'YOK'}")

    def scan_once(self) -> None:
        self._refresh_markets()
        self.scans += 1
        for a, m in self.markets.items():
            if not m:
                continue
            try:
                ya = _asks(self.c.get_order_book(m["yes"]))
                na = _asks(self.c.get_order_book(m["no"]))
            except Exception:
                continue
            yw, ydepth = _worst_fill(ya, SAMPLE)
            nw, ndepth = _worst_fill(na, SAMPLE)
            if yw is None or nw is None:
                continue  # yeterli derinlik yok
            yr, ye = self._fee_for(m["yes"])
            nr, ne = self._fee_for(m["no"])
            cost = SAMPLE * yw + SAMPLE * nw
            fee = _fee(SAMPLE, yw, yr, ye) + _fee(SAMPLE, nw, nr, ne)
            net = SAMPLE - cost - fee  # kazanan taraf SAMPLE $ oder
            if net > self.best_net[a]:
                self.best_net[a] = net
            if net > MIN_NET:
                self.hits += 1
                self.per_asset_hits[a] += 1
                rec = {"ts": _iso(), "asset": a, "slug": m["slug"], "shares": SAMPLE,
                       "yes_ask": yw, "no_ask": nw, "sum": round(yw + nw, 4),
                       "fee": round(fee, 4), "net_usdc": round(net, 4),
                       "yes_depth": ydepth, "no_depth": ndepth,
                       "sec_to_close": round(m["closes"] - time.time())}
                with open(HITS_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
                print(f"[{_hhmmss()}] >>> ARB! {a.upper()} sum={yw + nw:.4f} fee={fee:.4f} "
                      f"NET={net:+.4f} USDC (yes={yw:.3f} no={nw:.3f})")

        if self.scans % 15 == 0:
            best = "  ".join(f"{a.upper()}={self.best_net[a]:+.3f}" for a in ASSET_LIST
                             if self.best_net[a] > float("-inf"))
            print(f"[{_hhmmss()}] tarama={self.scans} toplam-firsat={self.hits} | en iyi net: {best}")

    def run(self) -> None:
        print(f"Arb-logger basladi. Varliklar={ASSET_LIST} boyut={SAMPLE} min_net={MIN_NET} "
              f"aralik={INTERVAL}sn -> firsatlar '{HITS_FILE}' dosyasina yazilir. (Ctrl+C ile dur)")
        while True:
            t0 = time.time()
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001 - logger dususmesin
                print(f"[{_hhmmss()}] tarama hatasi: {type(exc).__name__}: {exc}")
            dt = time.time() - t0
            time.sleep(max(0.0, INTERVAL - dt))


def _hhmmss() -> str:
    return time.strftime("%H:%M:%S")


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    if not KEY:
        print("PRIVATE_KEY .env'de yok."); raise SystemExit(1)
    try:
        ArbLogger(_client()).run()
    except KeyboardInterrupt:
        print("\nArb-logger durduruldu.")


if __name__ == "__main__":
    main()
