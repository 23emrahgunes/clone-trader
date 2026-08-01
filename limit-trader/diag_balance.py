"""Tek-seferlik teshis: hangi SIGNATURE_TYPE senin USDC'ni goruyor?

py-clob-client-v2 (fork) ile calisir. Her imza tipi icin (0=EOA, 1=POLY_PROXY,
2=POLY_GNOSIS_SAFE, 3=POLY_1271) bir ClobClient kurar, kimlik dogrular ve teminat
(USDC.e) bakiyesini okur. Hangisi gercek bakiyeni yazdirirsa, .env'de kullanman
gereken SIGNATURE_TYPE odur.

    python diag_balance.py

READ-ONLY: yalnizca bakiye sorgular, ASLA imzalamaz veya emir gondermez.
Not: 3 (POLY_1271) yalnizca py-clob-client-v2 fork'unda gecerlidir (resmi kutuphane 3'u
"Invalid order inputs" ile reddeder); senin canli edge botlarin bu fork'u kullaniyor.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from eth_account import Account
from py_clob_client_v2 import ClobClient, AssetType, BalanceAllowanceParams

load_dotenv()

CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip()
# Funder olarak once PROXY_WALLET_ADDRESS, yoksa eski PM_EDGE_FUNDER_ADDRESS aliasi.
FUNDER = (os.getenv("PROXY_WALLET_ADDRESS") or os.getenv("PM_EDGE_FUNDER_ADDRESS") or "").strip()


def _bal(sig_type: int) -> str:
    """Bu imza tipinde client kur ve teminat bakiyesini dondur."""
    try:
        client = ClobClient(
            host=CLOB_HOST,
            chain_id=CHAIN_ID,
            key=PRIVATE_KEY,
            signature_type=sig_type,
            funder=FUNDER or None,
        )
        creds = client.derive_api_key()
        client.set_api_creds(creds)
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
        resp = client.get_balance_allowance(params)
        raw = resp.get("balance") if isinstance(resp, dict) else resp
        try:
            usdc = float(raw) / 1_000_000  # USDC.e 6 ondalik, base unit string
            return f"{usdc:.4f} USDC   (raw={raw})"
        except (TypeError, ValueError):
            return f"(cozulemedi) {resp!r}"
    except Exception as exc:  # noqa: BLE001 - teshis her hatayi gostermeli
        return f"HATA: {type(exc).__name__}: {exc}"


def main() -> None:
    if not PRIVATE_KEY:
        print("PRIVATE_KEY .env'de yok. Once .env.example'i .env olarak kopyalayip doldurun.")
        raise SystemExit(1)
    eoa = Account.from_key(PRIVATE_KEY).address
    print("=" * 66)
    print("limit-trader bakiye teshisi (py-clob-client-v2)")
    print("=" * 66)
    print(f"EOA (PRIVATE_KEY'ten)   : {eoa}")
    print(f"FUNDER (proxy) adresi   : {FUNDER or '(bos)'}")
    print(f"Mevcut .env SIGNATURE   : {os.getenv('SIGNATURE_TYPE', '(bos)')}")
    print("-" * 66)
    names = {
        0: "EOA",
        1: "POLY_PROXY (email/Magic)",
        2: "POLY_GNOSIS_SAFE (MetaMask)",
        3: "POLY_1271 (EIP-1271 kontrat)",
    }
    for st in (0, 1, 2, 3):
        print(f"  sig_type={st} {names[st]:<30}-> {_bal(st)}")
    print("-" * 66)
    print("Gercek bakiyeni gosteren sig_type'i .env'de SIGNATURE_TYPE=<o> yapip")
    print("botu baslatin. Hepsi 0.0000 ise: USDC muhtemelen USDC.e teminat degil")
    print("(native USDC) ya da FUNDER'dan farkli bir cuzdanda.")


if __name__ == "__main__":
    main()
