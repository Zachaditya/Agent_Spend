"""Load validated Agent Spend settings while keeping credentials out of diagnostics."""

import os
import re
from dataclasses import MISSING, dataclass, field, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _positive_decimal(name: str, value: Decimal | str, precision: int | None = None) -> Decimal:
    """Parse and validate a positive decimal configuration value.

    Args:
        name: The environment variable name to include in sanitized errors.
        value: The Decimal or string value supplied by defaults, dotenv, or the process.
        precision: Optional maximum number of fractional decimal places.

    Returns:
        Decimal: The validated positive decimal value.

    Raises:
        ValueError: The value is not a finite positive decimal.
    """
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a positive decimal") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{name} must be a positive decimal")
    if precision is not None and parsed.as_tuple().exponent < -precision:
        raise ValueError(f"{name} exceeds supported decimal precision")
    return parsed


@dataclass(frozen=True)
class Settings:
    """Hold private provider credentials and the fixed Base Sepolia configuration."""

    cdp_api_key_id: str = field(repr=False)
    cdp_api_key_secret: str = field(repr=False)
    cdp_wallet_secret: str = field(repr=False)
    base_sepolia_rpc_url: str = field(default="https://sepolia.base.org", repr=False)
    usdc_contract_address: str = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    merchant_address: str = "0xa7BD909D765d9e93f75a0d76E77827a6EdC8A69D"
    treasury_account_name: str = "agent-spend-treasury"
    escrow_account_name: str = "offer-escrow"
    escrow_eth: Decimal = Decimal("0.0002")
    starter_usdc: Decimal = Decimal("25")
    starter_eth: Decimal = Decimal("0.0005")
    max_funding_usdc: Decimal = Decimal("100")
    max_total_funding_usdc: Decimal = Decimal("500")
    eshop_url: str = "http://127.0.0.1:8001"
    agent_api_key: str = field(default="", repr=False)
    public_host: str = ""
    db_path: Path = PROJECT_ROOT / "agent_spend.db"
    chain_id: int = field(default=84532, init=False)

    def __post_init__(self) -> None:
        """Validate credentials, transport settings, funding precision, and caps.

        Args:
            None.

        Returns:
            None.

        Raises:
            ValueError: Required configuration is missing or malformed; values are omitted.
        """
        required = {
            "CDP_API_KEY_ID": self.cdp_api_key_id,
            "CDP_API_KEY_SECRET": self.cdp_api_key_secret,
            "CDP_WALLET_SECRET": self.cdp_wallet_secret,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError("Missing required settings: " + ", ".join(missing))
        try:
            rpc = urlsplit(self.base_sepolia_rpc_url)
            valid_rpc = rpc.scheme in {"http", "https"} and bool(rpc.hostname)
        except ValueError:
            valid_rpc = False
        if not valid_rpc:
            raise ValueError("BASE_SEPOLIA_RPC_URL must be an HTTP(S) endpoint")
        try:
            shop = urlsplit(self.eshop_url)
            valid_shop = shop.scheme in {"http", "https"} and bool(shop.hostname)
        except ValueError:
            valid_shop = False
        if not valid_shop:
            raise ValueError("ESHOP_URL must be an HTTP(S) endpoint")
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", self.merchant_address):
            raise ValueError("MERCHANT_ADDRESS must be an EVM address")
        if self.public_host and not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
            r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*",
            self.public_host,
        ):
            raise ValueError(
                "PUBLIC_HOST must be a hostname without scheme, port, path, or wildcard"
            )
        path = Path(self.db_path).expanduser()
        object.__setattr__(self, "db_path", path if path.is_absolute() else PROJECT_ROOT / path)
        object.__setattr__(self, "public_host", self.public_host.lower())
        object.__setattr__(self, "eshop_url", self.eshop_url.rstrip("/"))
        object.__setattr__(
            self, "starter_usdc", _positive_decimal("STARTER_USDC", self.starter_usdc, 2)
        )
        object.__setattr__(
            self, "starter_eth", _positive_decimal("STARTER_ETH", self.starter_eth, 18)
        )
        object.__setattr__(self, "escrow_eth", _positive_decimal("ESCROW_ETH", self.escrow_eth, 18))
        if (
            not self.escrow_account_name.strip()
            or self.escrow_account_name == self.treasury_account_name
        ):
            raise ValueError("ESCROW_ACCOUNT_NAME must name a separate escrow account")
        for name in ("max_funding_usdc", "max_total_funding_usdc"):
            object.__setattr__(self, name, _positive_decimal(name.upper(), getattr(self, name), 2))
        if self.starter_usdc > min(self.max_funding_usdc, self.max_total_funding_usdc):
            raise ValueError("STARTER_USDC must not exceed funding caps")
        if self.max_total_funding_usdc > Decimal("1000000000"):
            raise ValueError("MAX_TOTAL_FUNDING_USDC exceeds supported storage range")


def load_settings(env_file: Path | None = None) -> Settings:
    """Read the project .env file, allowing process environment values to override it.

    Args:
        env_file: Optional dotenv file; defaults to .env in the project root.

    Returns:
        Settings: Validated, testnet-only service settings.

    Raises:
        ValueError: Required settings are missing or malformed, without their values.
    """
    values = {**dotenv_values(env_file or PROJECT_ROOT / ".env"), **os.environ}
    supplied = {}
    for setting in fields(Settings):
        if not setting.init:
            continue
        value = values.get(setting.name.upper())
        required = setting.default is MISSING and setting.default_factory is MISSING
        if value or required:
            supplied[setting.name] = value or ""
    return Settings(**supplied)
