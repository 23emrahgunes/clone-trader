"""Centralised, secure configuration loader for the Polymarket clone trading bot.

Loads every secret and tunable from the environment via ``python-dotenv`` so that
private keys and API credentials are NEVER hardcoded in source (see .skills rule 6).

Usage:
    from config import settings
    client = ClobClient(host=..., key=settings.PRIVATE_KEY, funder=settings.PROXY_WALLET_ADDRESS)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

# Load variables from a local .env file (if present) into os.environ.
# Real environment variables always take precedence over the file.
load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _require(name: str) -> str:
    """Return a required environment variable or fail loudly (never returns empty)."""
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ConfigError(
            f"Missing required environment variable '{name}'. "
            f"Copy .env.example to .env and set it."
        )
    return value.strip()


def _optional(name: str, default: Optional[str] = None) -> Optional[str]:
    """Return an optional environment variable, falling back to ``default``."""
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _get_float(name: str, default: float) -> float:
    """Parse a float env var, falling back to ``default`` and validating the type."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"Environment variable '{name}' must be a number, got {raw!r}.") from exc


def _get_int(name: str, default: int) -> int:
    """Parse an int env var, falling back to ``default`` and validating the type."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"Environment variable '{name}' must be an integer, got {raw!r}.") from exc


def _require_int(name: str) -> int:
    """Return a required integer environment variable or fail loudly."""
    raw = _require(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable '{name}' must be an integer, got {raw!r}.") from exc


def _get_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var (true/1/yes/on), falling back to ``default``."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


@dataclass(frozen=True)
class Settings:
    """Immutable, type-hinted view of the bot's runtime configuration."""

    # --- Wallet / Authentication (sensitive) ---
    PRIVATE_KEY: str = field(repr=False)  # hidden from repr/logs
    PROXY_WALLET_ADDRESS: str = ""

    # --- Blockchain ---
    RPC_URL: str = ""
    CHAIN_ID: int = 137

    # --- Polymarket CLOB ---
    CLOB_HOST: str = "https://clob.polymarket.com"
    # Proxy wallet signature type: 1 = POLY_PROXY (email/magic), 2 = POLY_GNOSIS_SAFE,
    # 3 = POLY_PROXY (browser/relayer). Default 3 to match the user's working setup.
    SIGNATURE_TYPE: int = 3

    # --- CLOB L2 API credentials (optional) ---
    # If all three are present they are used directly as ApiCreds; otherwise they
    # are derived from the private key at runtime.
    CLOB_API_KEY: Optional[str] = None
    CLOB_API_SECRET: Optional[str] = field(default=None, repr=False)      # hidden from repr/logs
    CLOB_API_PASSPHRASE: Optional[str] = field(default=None, repr=False)  # hidden from repr/logs

    # --- Telegram ---
    TELEGRAM_TOKEN: str = field(default="", repr=False)  # hidden from repr/logs
    TELEGRAM_CHAT_ID: Optional[str] = None
    # Only this Telegram user id may command the bot (admin double-lock security).
    TELEGRAM_ADMIN_ID: int = 0

    # --- Copy-trade targets ---
    # Single wallet (kept for backwards compatibility) …
    TARGET_WALLET: str = ""
    # … plus an optional comma-separated list of additional wallets to mirror.
    TARGET_WALLETS: str = ""

    # --- Safety switch ---
    # When True the bot only tracks/logs and never sends real orders (paper mode).
    DRY_RUN: bool = True

    # --- Paper PnL dashboard ---
    DASHBOARD_PORT: int = 8080
    # If set, the dashboard requires ?key=<token>. Recommended on a public IP.
    DASHBOARD_TOKEN: Optional[str] = None

    # --- Business rules (see .skills) ---
    FIXED_ALLOCATION_USDC: float = 1.0
    MAX_SLIPPAGE_PCT: float = 3.0
    MIN_ORDER_USDC: float = 1.0

    @property
    def has_clob_creds(self) -> bool:
        """True only when all three L2 API credentials are provided in the env."""
        return bool(self.CLOB_API_KEY and self.CLOB_API_SECRET and self.CLOB_API_PASSPHRASE)

    @property
    def target_wallet_list(self) -> list:
        """De-duplicated, lower-cased list of every wallet to mirror.

        Merges TARGET_WALLET with the comma/semicolon-separated TARGET_WALLETS.
        """
        raw: list = []
        if self.TARGET_WALLETS:
            raw.extend(self.TARGET_WALLETS.replace(";", ",").split(","))
        if self.TARGET_WALLET:
            raw.append(self.TARGET_WALLET)
        out: list = []
        for w in raw:
            w = w.strip().lower()
            if w and w not in out:
                out.append(w)
        return out

    @classmethod
    def load(cls) -> "Settings":
        """Build a validated Settings instance from the current environment."""
        inst = cls(
            PRIVATE_KEY=_require("PRIVATE_KEY"),
            PROXY_WALLET_ADDRESS=_require("PROXY_WALLET_ADDRESS"),
            RPC_URL=_require("RPC_URL"),
            CHAIN_ID=_get_int("CHAIN_ID", 137),
            CLOB_HOST=_optional("CLOB_HOST", "https://clob.polymarket.com") or "https://clob.polymarket.com",
            SIGNATURE_TYPE=_get_int("SIGNATURE_TYPE", 3),
            CLOB_API_KEY=_optional("CLOB_API_KEY"),
            CLOB_API_SECRET=_optional("CLOB_API_SECRET"),
            CLOB_API_PASSPHRASE=_optional("CLOB_API_PASSPHRASE"),
            TELEGRAM_TOKEN=_require("TELEGRAM_TOKEN"),
            TELEGRAM_CHAT_ID=_optional("TELEGRAM_CHAT_ID"),
            TELEGRAM_ADMIN_ID=_require_int("TELEGRAM_ADMIN_ID"),
            TARGET_WALLET=_optional("TARGET_WALLET", "") or "",
            TARGET_WALLETS=_optional("TARGET_WALLETS", "") or "",
            DRY_RUN=_get_bool("DRY_RUN", True),
            DASHBOARD_PORT=_get_int("DASHBOARD_PORT", 8080),
            DASHBOARD_TOKEN=_optional("DASHBOARD_TOKEN"),
            FIXED_ALLOCATION_USDC=_get_float("FIXED_ALLOCATION_USDC", 1.0),
            MAX_SLIPPAGE_PCT=_get_float("MAX_SLIPPAGE_PCT", 3.0),
            MIN_ORDER_USDC=_get_float("MIN_ORDER_USDC", 1.0),
        )
        if not inst.target_wallet_list:
            raise ConfigError(
                "No copy targets configured. Set TARGET_WALLET or TARGET_WALLETS "
                "(comma-separated) in .env."
            )
        return inst


# Import-time singleton. Fails fast at startup if configuration is incomplete.
settings: Settings = Settings.load()


if __name__ == "__main__":
    # Safe self-check: prints non-sensitive config only, masks secrets.
    print("Configuration loaded successfully.")
    print(f"  RPC_URL              : {settings.RPC_URL}")
    print(f"  CHAIN_ID             : {settings.CHAIN_ID}")
    print(f"  PROXY_WALLET_ADDRESS : {settings.PROXY_WALLET_ADDRESS}")
    print(f"  TARGET_WALLET        : {settings.TARGET_WALLET}")
    print(f"  FIXED_ALLOCATION_USDC: {settings.FIXED_ALLOCATION_USDC}")
    print(f"  MAX_SLIPPAGE_PCT     : {settings.MAX_SLIPPAGE_PCT}")
    print(f"  MIN_ORDER_USDC       : {settings.MIN_ORDER_USDC}")
    print(f"  PRIVATE_KEY          : {'*** set ***' if settings.PRIVATE_KEY else 'MISSING'}")
    print(f"  TELEGRAM_TOKEN       : {'*** set ***' if settings.TELEGRAM_TOKEN else 'MISSING'}")
