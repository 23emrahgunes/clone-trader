"""Polymarket TUM market arbitraj tarayici (READ-ONLY, auth gerekmez).

Tum aktif marketleri Gamma API'den ceker; her IKILI markette YES/NO en-iyi-ASK
toplamini CLOB'un public order book'undan bulur. `YES_ask + NO_ask < 1` ise:
iki tarafi da <$1'a alirsin, biri $1 oder -> GARANTI kar (arbitraj).

Isaretlenenler icin: brut kar/cift (1 - ask_toplam), alinabilir SIZE (min iki
bacak), toplam potansiyel (brut x size), kapanisa gun, hacim. Boylece "gercek mi
toz mu" (kucuk size / uzun vade) hemen gorunur.

    python arb_scanner.py

Ayarlar (ortam):
  ARB_MIN_GROSS (vars. 0.01) -> 1 - ask_toplam bunu asarsa isaretle (min kar/cift)
  ARB_MIN_SIZE  (vars. 0)    -> min alinabilir share (0 = hepsi)
  ARB_LIMIT     (vars. 0)    -> sadece ilk N market (0 = tumu; test icin kucuk ver)

Cikti: ekrana en iyi adaylar + 'arb_candidates.jsonl' (hepsi).
"""

from __future__ import annotations

import json
import os
import sys
import time

import requests

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

GAMMA = os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com")
CLOB = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
MIN_GROSS = float(os.getenv("ARB_MIN_GROSS", "0.01"))
MIN_SIZE = float(os.getenv("ARB_MIN_SIZE", "0"))
LIMIT = int(os.getenv("ARB_LIMIT", "0"))
OUT = "arb_candidates.jsonl"

S = requests.Session()
S.headers.update({"User-Agent": "arb-scanner"})


def fetch_markets() -> list:
    """Tum aktif, kapanmamis marketleri sayfali cek."""
    out: list = []
    offset = 0
    page = 500          # istenen; Gamma daha az dondurebilir (genelde 100)
    safety = 0
    while True:
        try:
            r = S.get(f"{GAMMA}/markets",
                      params={"active": "true", "closed": "false", "limit": page, "offset": offset},
                      timeout=25)
            if r.status_code != 200:
                print(f"  Gamma status {r.status_code}, duruyor", flush=True)
                break
            data = r.json()
        except Exception as exc:
            print(f"  Gamma hata: {exc}", flush=True)
            break
        got = len(data) if isinstance(data, list) else 0
        print(f"  offset={offset} -> {got} market (toplam {len(out) + got})", flush=True)
        if got == 0:                       # bos donunce bitti
            break
        out.extend(data)
        offset += got                      # GELEN KADAR ilerlet
        if LIMIT and len(out) >= LIMIT:
            break
        safety += 1
        if safety > 300:                   # guvenlik
            break
    return out[:LIMIT] if LIMIT else out


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


def get_books(tokens: list) -> dict:
    """Token id -> order book. Once toplu /books, olmazsa tekil /book."""
    books: dict = {}
    CH = 200
    n = len(tokens)
    for i in range(0, n, CH):
        chunk = tokens[i:i + CH]
        ok = False
        try:
            r = S.post(f"{CLOB}/books", json=[{"token_id": t} for t in chunk], timeout=40)
            if r.status_code == 200:
                for b in r.json():
                    tid = b.get("asset_id") or b.get("token_id")
                    if tid is not None:
                        books[str(tid)] = b
                ok = True
        except Exception:
            ok = False
        if not ok:
            for t in chunk:
                try:
                    rr = S.get(f"{CLOB}/book", params={"token_id": t}, timeout=12)
                    if rr.status_code == 200:
                        books[str(t)] = rr.json()
                except Exception:
                    pass
                time.sleep(0.02)
        print(f"  order book {min(i + CH, n)}/{n}", flush=True)
    return books


def best_ask(book) -> tuple:
    if not isinstance(book, dict):
        return None, 0.0
    best = None
    sz = 0.0
    for a in (book.get("asks") or []):
        try:
            p = float(a["price"] if isinstance(a, dict) else a[0])
            s = float(a["size"] if isinstance(a, dict) else a[1])
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        if best is None or p < best:
            best, sz = p, s
    return best, sz


def _days_to_close(m) -> str:
    for k in ("endDate", "endDateIso", "end_date_iso"):
        v = m.get(k)
        if v:
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                d = (dt - datetime.now(timezone.utc)).days
                return f"{d}g"
            except Exception:
                return str(v)[:10]
    return "?"


def main() -> None:
    print("=" * 74)
    print("Polymarket TUM market arbitraj tarayici (ikili: YES_ask + NO_ask < 1)")
    print(f"esik: kar/cift >= {MIN_GROSS}  min_size >= {MIN_SIZE}"
          + (f"  (ilk {LIMIT} market)" if LIMIT else ""))
    print("=" * 74)

    print("\n[1/3] Marketler cekiliyor...")
    markets = fetch_markets()
    bins = []
    alltok: set = set()
    for m in markets:
        toks = _list(m.get("clobTokenIds"))
        if len(toks) == 2:
            bins.append((m, [str(toks[0]), str(toks[1])]))
            alltok.update(str(t) for t in toks)
    print(f"  toplam {len(markets)} market, {len(bins)} ikili (2 token).")

    print(f"\n[2/3] {len(alltok)} token order book cekiliyor (biraz surer)...")
    books = get_books(list(alltok))

    print("\n[3/3] Arbitraj taraniyor...")
    cands = []
    for m, toks in bins:
        ay, sy = best_ask(books.get(toks[0], {}))
        an, sn = best_ask(books.get(toks[1], {}))
        if ay is None or an is None:
            continue
        s = ay + an
        gross = round(1.0 - s, 4)                 # cift basina brut kar (fee HARIC)
        if gross < MIN_GROSS:
            continue
        size = min(sy, sn)                          # alinabilir cift sayisi (min bacak)
        if size < MIN_SIZE:
            continue
        rec = {
            "question": str(m.get("question") or m.get("title") or m.get("slug") or "")[:90],
            "slug": m.get("slug", ""),
            "yes_ask": ay, "no_ask": an, "sum": round(s, 4),
            "gross_per_pair": gross, "size": round(size, 2),
            "total_gross": round(gross * size, 2),
            "kapanis": _days_to_close(m),
            "volume": round(float(m.get("volumeNum") or m.get("volume") or 0), 0),
        }
        cands.append(rec)

    cands.sort(key=lambda x: x["total_gross"], reverse=True)
    with open(OUT, "w", encoding="utf-8") as f:
        for c in cands:
            f.write(json.dumps(c) + "\n")

    print("\n" + "=" * 74)
    print(f"SONUC: {len(cands)} ikili arb adayi (fee haric brut). Detay -> {OUT}")
    print("=" * 74)
    if not cands:
        print("  Hic aday yok -> su an ikili YES+NO < 1 arbitraji bulunamadi.")
    else:
        print(f"{'kar/cift':>8} {'size':>8} {'toplam$':>8} {'sum':>6} {'kapanis':>7}  soru")
        for c in cands[:30]:
            print(f"{c['gross_per_pair']:>8.3f} {c['size']:>8.0f} {c['total_gross']:>8.2f} "
                  f"{c['sum']:>6.3f} {c['kapanis']:>7}  {c['question']}")
        print("\nYorum: 'total$' kucukse (size az) ya da 'kapanis' cok uzunsa (sermaye kilitli)")
        print("-> toz. Buyuk total$ + yakin kapanis + likit (volume) olan varsa GERCEK olabilir.")
        print("Not: fee bazi marketlerde (ozellikle 5dk kripto) brut kari yer; net degil bu.")


if __name__ == "__main__":
    main()
