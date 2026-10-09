"""Verify the developer-only reconciliation entry point and chain guard."""

from unittest.mock import AsyncMock, Mock

import pytest

from scripts.reconcile_funding import reconcile
from tests.conftest import SECRET


async def test_reconciliation_cli_rechecks_chain_and_closes_resources(settings, cdp, monkeypatch):
    """Default to receipt reconciliation while owning provider/database resources.

    Args:
        settings: Synthetic application configuration.
        cdp: Fake provider with observable resource lifecycle.
        monkeypatch: Scoped configuration and dependency replacement helper.

    Returns:
        None.
    """
    database = Mock()
    service = Mock(reconcile_funding=AsyncMock(return_value={"status": "FUNDED"}))
    chain_check = AsyncMock()
    monkeypatch.setattr("scripts.reconcile_funding.load_settings", lambda: settings)
    monkeypatch.setattr("scripts.reconcile_funding.check_chain", chain_check)
    monkeypatch.setattr("scripts.reconcile_funding.build_cdp_client", lambda _: cdp)
    monkeypatch.setattr("scripts.reconcile_funding.Database", lambda _: database)
    monkeypatch.setattr("scripts.reconcile_funding.WalletService", lambda *args, **kwargs: service)
    assert await reconcile("operation") == {"status": "FUNDED"}
    chain_check.assert_awaited_once_with(settings)
    service.reconcile_funding.assert_awaited_once_with("operation", recover_missing=False)
    database.close.assert_called_once()
    assert cdp.entered and cdp.closed


async def test_reconciliation_cli_sanitizes_failures_before_provider_access(settings, monkeypatch):
    """Stop on a bad chain without leaking RPC secrets or opening the provider.

    Args:
        settings: Synthetic application settings.
        monkeypatch: Scoped configuration and chain-failure replacement helper.

    Returns:
        None.
    """
    provider = Mock()
    monkeypatch.setattr("scripts.reconcile_funding.load_settings", lambda: settings)
    monkeypatch.setattr(
        "scripts.reconcile_funding.check_chain", AsyncMock(side_effect=RuntimeError(SECRET))
    )
    monkeypatch.setattr("scripts.reconcile_funding.build_cdp_client", provider)
    with pytest.raises(RuntimeError, match="^FUNDING_RECONCILIATION_UNAVAILABLE$"):
        await reconcile("operation", recover_missing=True)
    provider.assert_not_called()


async def test_reconciliation_cli_sanitizes_database_cleanup_errors(settings, cdp, monkeypatch):
    """Keep cleanup exceptions inside the same sanitized developer boundary.

    Args:
        settings: Synthetic settings.
        cdp: Observable fake provider.
        monkeypatch: Scoped dependency replacement helper.

    Returns:
        None.
    """
    database = Mock(close=Mock(side_effect=RuntimeError(SECRET)))
    service = Mock(reconcile_funding=AsyncMock(return_value={"status": "FUNDED"}))
    monkeypatch.setattr("scripts.reconcile_funding.load_settings", lambda: settings)
    monkeypatch.setattr("scripts.reconcile_funding.check_chain", AsyncMock())
    monkeypatch.setattr("scripts.reconcile_funding.build_cdp_client", lambda _: cdp)
    monkeypatch.setattr("scripts.reconcile_funding.Database", lambda _: database)
    monkeypatch.setattr("scripts.reconcile_funding.WalletService", lambda *args, **kwargs: service)
    with pytest.raises(RuntimeError, match="^FUNDING_RECONCILIATION_UNAVAILABLE$"):
        await reconcile("operation")
