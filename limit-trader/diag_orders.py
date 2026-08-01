"""Teshis: acik emirler + gercek trade'ler (fill). READ-ONLY, emir gondermez.

Botun 1c alimlarindan biri gercekten doldu mu, dolduysa bot 2c satis koydu mu?
Bunu net gormek icin:
  - get_open_orders(): su an defterde bekleyen alim/satis emirlerin
  - get_trades():      hesabinin gercek trade/fill kayitlari (dolan var mi?)
Istersen bir order_id vererek o emrin ham /data/order cevabini da yazdirir:
  python diag_orders.py 0xORDER_ID
"""

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv
from py_clob_client_v2 import ApiCreds, ClobClient

load_dotenv()

HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
KEY = os.getenv("PRIVATE_KEY", "").strip()
FUNDER = (os.getenv("PROXY_WALLET_ADDRESS") or os.getenv("PM_EDGE_FUNDER_ADDRESS") or "").strip()
SIG = int(os.getenv("SIGNATURE_TYPE", "3"))
API_KEY = os.getenv("CLOB_API_KEY")
API_SECRET = os.getenv("CLOB_API_SECRET")
API_PASS = os.getenv("CLOB_API_PASSPHRASE")


def _client() -> ClobClient:
    creds = None
    if API_KEY and API_SECRET and API_PASS:
        creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASS)
    c = ClobClient(host=HOST, chain_id=CHAIN_ID, key=KEY, creds=creds,
                   signature_type=SIG, funder=FUNDER or None)
    if creds is None:
        c.set_api_creds(c.derive_api_key())
    return c


def _g(row, *keys):
    for k in keys:
        if isinstance(row, dict) and row.get(k) not in (None, ""):
            return row.get(k)
    return "-"


def main() -> None:
    if not KEY:
        print("PRIVATE_KEY .env'de yok."); raise SystemExit(1)
    c = _client()

    # Tek bir order_id verildiyse: ham cevabini yazdir (fill alan adlarini gormek icin)
    if len(sys.argv) > 1:
        oid = sys.argv[1]
        print(f"=== get_order({oid[:16]}...) ham cevap ===")
        try:
            print(json.dumps(c.get_order(oid), indent=2)[:2000])
        except Exception as exc:  # noqa: BLE001
            print(f"HATA (dolmus/iptal emir /data/order'da 404 verebilir): {type(exc).__name__}: {exc}")
        return

    print("=" * 70)
    print("ACIK EMIRLER (get_open_orders)")
    print("=" * 70)
    try:
        oo = c.get_open_orders()
        if not oo:
            print("  (acik emir yok)")
        for o in oo:
            print(f"  {_g(o,'side'):>4} fiyat={_g(o,'price')} orijinal={_g(o,'original_size','size')} "
                  f"dolan={_g(o,'size_matched')} durum={_g(o,'status')} outcome={_g(o,'outcome')} "
                  f"id={str(_g(o,'id','orderID'))[:12]}...")
    except Exception as exc:  # noqa: BLE001
        print(f"  HATA: {type(exc).__name__}: {exc}")

    print()
    print("=" * 70)
    print("GERCEK TRADE'LER / FILL'LER (get_trades) -- son 15")
    print("=" * 70)
    try:
        tr = c.get_trades()
        if not tr:
            print("  (hic trade yok -> hicbir 1c alim dolmamis; bu yuzden satis da yok)")
        for t in tr[:15]:
            print(f"  {_g(t,'side'):>4} fiyat={_g(t,'price')} miktar={_g(t,'size')} "
                  f"durum={_g(t,'status')} outcome={_g(t,'outcome')} "
                  f"zaman={_g(t,'match_time','matchTime','timestamp')} "
                  f"id={str(_g(t,'id','transaction_hash'))[:12]}...")
        print(f"\n  Toplam trade sayisi: {len(tr)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  HATA: {type(exc).__name__}: {exc}")

    print()
    print("Yorum: get_trades'te BUY satiri varsa o 1c alim dolmus demektir; ayni")
    print("outcome'da bir SELL satiri da varsa bot 2c satisi koymus/dolmus demektir.")


if __name__ == "__main__":
    main()
