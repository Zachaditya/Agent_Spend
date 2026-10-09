"""Validate configuration without exposing credential values."""

from dataclasses import fields
from decimal import Decimal

import pytest

from app.config import Settings, load_settings
from tests.conftest import SECRET


def test_missing_credentials_fail_with_names_only(tmp_path, monkeypatch):
    """Fail closed when required CDP configuration is absent.

    Args:
        tmp_path: Pytest's isolated temporary directory.
        monkeypatch: Pytest's scoped environment helper.

    Returns:
        None.
    """
    for key in ("CDP_API_KEY_ID", "CDP_API_KEY_SECRET", "CDP_WALLET_SECRET"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="CDP_API_KEY_ID"):
        load_settings(tmp_path / "missing.env")


def test_settings_repr_omits_credentials(settings):
    """Keep dataclass diagnostics from exposing credentials or RPC secrets.

    Args:
        settings: Synthetic test settings.

    Returns:
        None.
    """
    assert SECRET not in repr(settings)
    assert "84532" in repr(settings)


@pytest.mark.parametrize("host", ["https://x.ngrok.app", "*.ngrok.app", "x.ngrok.app/mcp"])
def test_public_host_rejects_url_path_and_wildcards(host):
    """Require a literal public hostname instead of unsafe allowlist entries.

    Args:
        host: An invalid PUBLIC_HOST value.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="PUBLIC_HOST"):
        Settings(SECRET, SECRET, SECRET, public_host=host)


@pytest.mark.parametrize("empty_optional", [False, True])
def test_loader_uses_dataclass_defaults_and_ignores_chain_overrides(
    tmp_path, monkeypatch, empty_optional
):
    """Keep absent or empty optional environment values on the Settings defaults.

    Args:
        tmp_path: Pytest's isolated directory for the missing dotenv file.
        monkeypatch: Pytest's scoped environment replacement helper.
        empty_optional: Whether optional variables are empty rather than absent.

    Returns:
        None.
    """
    for setting in fields(Settings):
        key = setting.name.upper()
        monkeypatch.delenv(key, raising=False)
        if setting.name.startswith("cdp_"):
            monkeypatch.setenv(key, SECRET)
        elif empty_optional:
            monkeypatch.setenv(key, "")
    monkeypatch.setenv("CHAIN_ID", "1")
    assert load_settings(tmp_path / "missing.env") == Settings(SECRET, SECRET, SECRET)


def test_environment_overrides_dotenv_without_losing_optional_settings(tmp_path, monkeypatch):
    """Retain all supported dotenv settings while honoring process overrides.

    Args:
        tmp_path: Pytest's isolated directory for the synthetic dotenv file.
        monkeypatch: Pytest's scoped environment replacement helper.

    Returns:
        None.
    """
    for setting in fields(Settings):
        monkeypatch.delenv(setting.name.upper(), raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CDP_API_KEY_ID=test-id\nCDP_API_KEY_SECRET=test-secret\n"
        "CDP_WALLET_SECRET=test-wallet-secret\n"
        "BASE_SEPOLIA_RPC_URL=https://rpc.example\nPUBLIC_HOST=demo.example\n"
        "USDC_CONTRACT_ADDRESS=test-contract\nTREASURY_ACCOUNT_NAME=test-treasury\n"
        "STARTER_USDC=25.50\nSTARTER_ETH=0.001\n"
        f"DB_PATH={tmp_path / 'custom.db'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CDP_API_KEY_ID", "process-id")
    settings = load_settings(env_file)
    assert settings.cdp_api_key_id == "process-id"
    assert settings.base_sepolia_rpc_url == "https://rpc.example"
    assert settings.public_host == "demo.example"
    assert settings.usdc_contract_address == "test-contract"
    assert settings.treasury_account_name == "test-treasury"
    assert settings.starter_usdc == Decimal("25.50")
    assert settings.starter_eth == Decimal("0.001")
    assert settings.db_path == tmp_path / "custom.db"


def test_phase_2_starter_amounts_are_positive_decimals():
    """Validate fixed starter funding amounts at configuration load time.

    Args:
        None.

    Returns:
        None.
    """
    settings = Settings(SECRET, SECRET, SECRET, starter_usdc="25", starter_eth="0.0005")
    assert settings.starter_usdc == Decimal("25")
    assert settings.starter_eth == Decimal("0.0005")
    with pytest.raises(ValueError, match="STARTER_USDC"):
        Settings(SECRET, SECRET, SECRET, starter_usdc="0")
    with pytest.raises(ValueError, match="STARTER_ETH"):
        Settings(SECRET, SECRET, SECRET, starter_eth="-0.1")
    with pytest.raises(ValueError, match="STARTER_USDC"):
        Settings(SECRET, SECRET, SECRET, starter_usdc="abc")
