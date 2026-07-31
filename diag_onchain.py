"""On-chain diagnostic: WHERE is the 10 USDC and in WHICH token?

The CLOB balance came back 0 for every signature type, so the funds are not
sitting as USDC.e collateral. This queries the raw ERC-20 balances directly on
Polygon for both of your addresses and both USDC variants, so we can see
exactly where the money is and what needs to move.

    .venv/bin/python diag_onchain.py

Read-only: eth_call balanceOf only. No transactions.
"""

from __future__ import annotations

import sys

from eth_account import Account
from web3 import Web3

from config import settings

# Polygon token contracts.
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")  # bridged (Polymarket collateral)
USDC_NATIVE = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")  # Circle native
# Polymarket CTF Exchange (spender that must be approved on USDC.e).
CTF_EXCHANGE = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "o", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]


# Fallback public Polygon RPCs, tried in order if the configured one is dead.
_FALLBACK_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://1rpc.io/matic",
    "https://polygon.drpc.org",
]


def _connect() -> Web3:
    """Return a working Web3 on Polygon, trying the configured RPC then fallbacks."""
    candidates = [settings.RPC_URL] if settings.RPC_URL else []
    candidates += _FALLBACK_RPCS
    for url in candidates:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))
            if w3.is_connected() and w3.eth.chain_id == 137:
                print(f"RPC in use: {url}")
                return w3
        except Exception as exc:  # noqa: BLE001
            print(f"  (skip {url}: {type(exc).__name__})")
    raise SystemExit("No working Polygon RPC found. Set a valid RPC_URL in .env.")


def main() -> None:
    w3 = _connect()
    eoa = Account.from_key(settings.PRIVATE_KEY).address
    proxy = settings.PROXY_WALLET_ADDRESS
    addrs = {"EOA (PRIVATE_KEY)": eoa}
    if proxy:
        addrs["PROXY_WALLET_ADDRESS"] = Web3.to_checksum_address(proxy)
    # Any extra addresses passed on the command line (e.g. your Polymarket UI
    # deposit address) are checked too, so we can find where the 10 USDC really is.
    for i, extra in enumerate(sys.argv[1:], 1):
        try:
            addrs[f"CLI arg #{i}"] = Web3.to_checksum_address(extra)
        except Exception:  # noqa: BLE001
            print(f"  (ignored invalid address arg: {extra!r})")

    print("=" * 70)
    print("on-chain USDC diagnostic (Polygon)")
    print("=" * 70)
    print(f"RPC connected: {w3.is_connected()}")
    print(f"EOA   : {eoa}")
    print(f"PROXY : {proxy or '(unset)'}")
    print("-" * 70)

    for token_name, token_addr in (("USDC.e (collateral)", USDC_E), ("USDC native", USDC_NATIVE)):
        c = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
        print(f"[{token_name}]  {token_addr}")
        for label, addr in addrs.items():
            bal = c.functions.balanceOf(addr).call() / 1_000_000
            line = f"   {label:<22}: {bal:.4f}"
            if token_name.startswith("USDC.e"):
                allow = c.functions.allowance(addr, CTF_EXCHANGE).call() / 1_000_000
                line += f"   (allowance->Exchange: {allow:.2f})"
            print(line)
        print()

    print("-" * 70)
    print("Reading the result:")
    print(" - USDC.e in PROXY, allowance>0  -> set SIGNATURE_TYPE for that proxy; should trade.")
    print(" - USDC.e in EOA                 -> use SIGNATURE_TYPE=0 (trade from EOA), or move to proxy.")
    print(" - Money shows under 'USDC native' -> wrong token; Polymarket needs USDC.e. Swap/bridge it.")
    print(" - allowance 0 on the funded address -> approve the Exchange once via the Polymarket UI.")


if __name__ == "__main__":
    main()
