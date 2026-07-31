"""One-shot diagnostic: which SIGNATURE_TYPE sees your USDC?

Runs on the SERVER with your real .env. It builds a ClobClient for each
signature type (0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE), authenticates, and
reads the collateral (USDC.e) balance for each. Whichever prints ~your real
balance is the correct SIGNATURE_TYPE for clone-trader.

    python diag_balance.py

Read-only: it queries balances only, never signs or places an order.
"""

from __future__ import annotations

import os

from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

from config import settings


def _bal(sig_type: int) -> str:
    """Build a client at this signature type and return its collateral balance."""
    try:
        client = ClobClient(
            host=settings.CLOB_HOST,
            chain_id=settings.CHAIN_ID,
            key=settings.PRIVATE_KEY,
            signature_type=sig_type,
            funder=settings.PROXY_WALLET_ADDRESS or None,
        )
        creds = client.derive_api_key()
        client.set_api_creds(creds)
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=sig_type,
        )
        resp = client.get_balance_allowance(params)
        raw = resp.get("balance") if isinstance(resp, dict) else resp
        # CLOB returns USDC.e in 6-decimal base units as a string.
        try:
            usdc = float(raw) / 1_000_000
            return f"{usdc:.4f} USDC   (raw={raw})"
        except (TypeError, ValueError):
            return f"(unparsed) {resp!r}"
    except Exception as exc:  # noqa: BLE001 - diagnostic must show every failure
        return f"ERROR: {type(exc).__name__}: {exc}"


def main() -> None:
    eoa = Account.from_key(settings.PRIVATE_KEY).address
    print("=" * 64)
    print("clone-trader balance diagnostic")
    print("=" * 64)
    print(f"EOA (from PRIVATE_KEY)   : {eoa}")
    print(f"PROXY_WALLET_ADDRESS     : {settings.PROXY_WALLET_ADDRESS or '(unset)'}")
    print(f"Current SIGNATURE_TYPE   : {settings.SIGNATURE_TYPE}")
    print("-" * 64)
    names = {0: "EOA", 1: "POLY_PROXY (email/Magic)", 2: "POLY_GNOSIS_SAFE (MetaMask)"}
    for st in (0, 1, 2):
        print(f"  sig_type={st} {names[st]:<28}-> {_bal(st)}")
    print("-" * 64)
    print("Pick the sig_type above that shows your real balance, then set")
    print("SIGNATURE_TYPE=<that> in .env and restart. If ALL show 0.0000, your")
    print("10 USDC is likely native USDC (not USDC.e collateral) or in a")
    print("different wallet than PROXY_WALLET_ADDRESS.")


if __name__ == "__main__":
    main()
