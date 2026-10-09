"""Test Phase 2 wallet funding, balance reads, and funding idempotency."""

from types import SimpleNamespace
from unittest.mock import call

import pytest

from app.confirmations import ConfirmationError
from app.db import Database
from app.wallet import (
    WalletService,
    cents_to_usdc_base_units,
    eth_to_wei,
    format_eth_balance,
    format_usdc_balance,
    get_explorer_url,
)
from tests.conftest import ADDRESS, ETH_TX_HASH, SECRET, TREASURY_ADDRESS, TX_HASH


def test_phase_2_unit_conversions_are_fixed():
    """Convert only the server-configured starter units used by funding.

    Args:
        None.

    Returns:
        None.
    """
    assert cents_to_usdc_base_units(2500) == 25_000_000
    assert eth_to_wei("0.0005") == 500_000_000_000_000
    assert format_usdc_balance(25_000_000) == "25.00"
    assert format_eth_balance(500_000_000_000_000) == "0.0005"


def test_funding_event_helpers_are_durable(database):
    """Persist submitted, failed, and confirmed funding event state.

    Args:
        database: The isolated SQLite repository.

    Returns:
        None.
    """
    event = database.insert_funding_event(ADDRESS, 2500, "500000000000000")
    assert event["status"] == "SUBMITTED"
    assert not database.has_confirmed_funding()
    database.record_funding_tx_hashes(event["id"], usdc_tx_hash=TX_HASH)
    latest = database.get_latest_funding_event()
    assert latest["usdc_tx_hash"] == TX_HASH
    assert latest["eth_tx_hash"] is None
    database.mark_funding_failed(event["id"], "Developer must inspect partial funding")
    assert database.get_latest_funding_event()["status"] == "FAILED"

    confirmed = database.insert_funding_event(ADDRESS, 2500, "500000000000000")
    database.mark_funding_confirmed(confirmed["id"], TX_HASH, ETH_TX_HASH)
    latest = database.get_latest_funding_event()
    assert database.has_confirmed_funding()
    assert latest["status"] == "CONFIRMED"
    assert latest["usdc_tx_hash"] == TX_HASH
    assert latest["eth_tx_hash"] == ETH_TX_HASH


def test_get_wallet_includes_live_balances_and_funding_status(database, cdp, settings, web3):
    """Read balances from Base Sepolia while keeping only public metadata in output.

    Args:
        database: The isolated SQLite repository.
        cdp: The fake wallet provider.
        settings: The synthetic validated application settings.
        web3: The deterministic fake Base Sepolia reader.

    Returns:
        None.
    """
    database.create_wallet_pointer("shopper-1", ADDRESS)
    web3.usdc_balances[ADDRESS] = 25_000_000
    web3.eth_balances[ADDRESS] = 500_000_000_000_000
    service = WalletService(database, cdp, confirmations=None, settings=settings, web3=web3)
    assert service.get_wallet() == {
        "status": "READY",
        "address": ADDRESS,
        "network": "base-sepolia",
        "usdc_balance": "25.00",
        "eth_balance": "0.0005",
        "funding_status": "NOT_FUNDED",
        "explorer_url": get_explorer_url(ADDRESS),
    }


async def test_funding_first_call_has_no_side_effects(service, database, cdp):
    """Return a disclosure and confirmation ID before any funding transfer.

    Args:
        service: The wallet orchestration service.
        database: The isolated SQLite repository.
        cdp: The observable fake provider.

    Returns:
        None.
    """
    database.create_wallet_pointer("shopper-1", ADDRESS)
    result = await service.fund_agent_wallet()
    assert result["status"] == "CONFIRMATION_REQUIRED"
    assert "25.00 test USDC" in result["disclosure"]
    assert "0.0005 test ETH" in result["disclosure"]
    assert len(result["confirmation_id"]) >= 32
    cdp.treasury.transfer.assert_not_awaited()
    assert database.get_latest_funding_event() is None


async def test_default_funding_is_confirmed_and_repeats_require_fresh_consent(
    service, database, cdp, settings, web3
):
    """Use configured default amounts and require fresh consent after funding/restart.

    Args:
        service: The wallet orchestration service.
        database: The isolated SQLite repository.
        cdp: The observable fake provider.
        settings: The synthetic validated application settings.
        web3: The deterministic fake Base Sepolia reader.

    Returns:
        None.
    """
    database.create_wallet_pointer("shopper-1", ADDRESS)
    web3.usdc_balances[ADDRESS] = 25_000_000
    web3.eth_balances[ADDRESS] = 500_000_000_000_000
    token = (await service.fund_agent_wallet())["confirmation_id"]
    result = await service.fund_agent_wallet(token)
    assert (
        result.items()
        >= {
            "status": "FUNDED",
            "address": ADDRESS,
            "network": "base-sepolia",
            "usdc_balance": "25.00",
            "eth_balance": "0.0005",
            "usdc_tx_hash": TX_HASH,
            "eth_tx_hash": ETH_TX_HASH,
            "usdc_explorer_url": get_explorer_url(TX_HASH),
            "eth_explorer_url": get_explorer_url(ETH_TX_HASH),
        }.items()
    )
    assert cdp.treasury.transfer.await_args_list == [
        call(to=ADDRESS, amount=25_000_000, token="usdc", network="base-sepolia"),
        call(to=ADDRESS, amount=500_000_000_000_000, token="eth", network="base-sepolia"),
    ]
    latest = database.get_latest_funding_event()
    assert latest["status"] == "CONFIRMED"
    assert latest["usdc_amount_cents"] == 2500
    assert latest["eth_amount_wei"] == "500000000000000"

    repeat = await service.fund_agent_wallet()
    assert repeat["status"] == "CONFIRMATION_REQUIRED"
    assert repeat["funding_kind"] == "TOP_UP"
    assert cdp.treasury.transfer.await_count == 2
    with pytest.raises(ConfirmationError, match="CONFIRMATION_EXPIRED"):
        await service.fund_agent_wallet(token)

    reopened = Database(database.path)
    try:
        fresh = WalletService(
            reopened,
            cdp,
            confirmations=None,
            settings=settings,
            web3=web3,
        )
        after_restart = await fresh.fund_agent_wallet()
        assert after_restart["status"] == "CONFIRMATION_REQUIRED"
        assert after_restart["funding_kind"] == "TOP_UP"
        assert fresh.get_wallet()["funding_status"] == "FUNDED"
    finally:
        reopened.close()
    assert cdp.treasury.transfer.await_count == 2


async def test_funding_before_wallet_creation_returns_no_wallet(service, cdp):
    """Refuse funding before the shopper wallet exists.

    Args:
        service: The wallet orchestration service.
        cdp: The observable fake provider.

    Returns:
        None.
    """
    assert await service.fund_agent_wallet() == {
        "status": "NO_WALLET",
        "message": "Create the shopping wallet before funding it.",
    }
    cdp.treasury.transfer.assert_not_awaited()


async def test_insufficient_treasury_funds_return_clear_status(service, database, cdp, web3):
    """Check treasury balances before transfer and report insufficient funds.

    Args:
        service: The wallet orchestration service.
        database: The isolated SQLite repository.
        cdp: The observable fake provider.
        web3: The deterministic fake Base Sepolia reader.

    Returns:
        None.
    """
    database.create_wallet_pointer("shopper-1", ADDRESS)
    web3.usdc_balances[TREASURY_ADDRESS] = 24_990_000
    token = (await service.fund_agent_wallet())["confirmation_id"]
    result = await service.fund_agent_wallet(token)
    assert result["status"] == "INSUFFICIENT_TREASURY_FUNDS"
    assert "25.00" in result["message"]
    cdp.treasury.transfer.assert_not_awaited()
    assert database.get_latest_funding_event() is None


async def test_partial_transfer_failure_records_failed_event_without_secret(
    service, database, cdp, web3, caplog
):
    """Preserve uncertain submission when a provider raises after one successful leg.

    Args:
        service: The wallet orchestration service.
        database: The isolated SQLite repository.
        cdp: The observable fake provider.
        web3: The deterministic fake Base Sepolia reader.
        caplog: Pytest's captured log records.

    Returns:
        None.
    """
    database.create_wallet_pointer("shopper-1", ADDRESS)
    cdp.treasury.transfer.side_effect = [TX_HASH, RuntimeError(SECRET)]
    token = (await service.fund_agent_wallet())["confirmation_id"]
    result = await service.fund_agent_wallet(token)
    assert result["status"] == "FUNDING_FAILED"
    assert "developer" in result["message"].lower()
    assert SECRET not in str(result)
    assert SECRET not in caplog.text
    latest = database.get_latest_funding_event()
    assert latest["status"] == "UNKNOWN"
    assert latest["usdc_tx_hash"] == TX_HASH
    assert latest["eth_tx_hash"] is None


async def test_failed_receipt_records_both_hashes_and_reports_failure(service, database, cdp, web3):
    """Preserve the successful USDC leg when its gas ETH receipt reverts.

    Args:
        service: The wallet orchestration service.
        database: The isolated SQLite repository.
        cdp: The observable fake provider.
        web3: The deterministic fake Base Sepolia reader.

    Returns:
        None.
    """
    database.create_wallet_pointer("shopper-1", ADDRESS)
    web3.receipts[ETH_TX_HASH] = SimpleNamespace(status=0)
    token = (await service.fund_agent_wallet())["confirmation_id"]
    result = await service.fund_agent_wallet(token)
    assert result["status"] == "FUNDING_FAILED"
    latest = database.get_latest_funding_event()
    assert latest["status"] == "PARTIAL"
    assert latest["usdc_tx_hash"] == TX_HASH
    assert latest["eth_tx_hash"] == ETH_TX_HASH
