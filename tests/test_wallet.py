"""Test two-step creation, persistence, concurrency, and provider failures."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from cdp.openapi_client.errors import ApiError

from app.confirmations import ConfirmationError, ConfirmationStore
from app.db import Database
from app.wallet import WalletError, WalletService, get_explorer_url
from tests.conftest import ADDRESS, SECRET


async def test_first_call_has_no_side_effects(service, database, cdp):
    """Require a disclosure before any provider or persistence operation.

    Args:
        service: The wallet orchestration service.
        database: The test SQLite repository.
        cdp: The observable fake provider.

    Returns:
        None.
    """
    assert service.get_wallet() == {"status": "NOT_CREATED"}
    result = await service.create_agent_wallet()
    assert result["status"] == "CONFIRMATION_REQUIRED"
    assert len(result["confirmation_id"]) >= 32
    assert result["disclosure"] == (
        "Agent Spend will create a Base Sepolia testnet wallet for shopping. "
        "Coinbase CDP will hold the keys. No real funds are used. Reply yes to create it."
    )
    assert not database.wallet_exists()
    cdp.evm.create_account.assert_not_awaited()


async def test_confirmation_creates_one_wallet_and_survives_restart(
    service, database, cdp, settings, web3
):
    """Persist one public wallet pointer and recover it with fresh service state.

    Args:
        service: The original wallet service.
        database: The persistent test repository.
        cdp: The observable fake provider.
        settings: The synthetic validated application settings.
        web3: The deterministic fake Base Sepolia reader.

    Returns:
        None.
    """
    token = (await service.create_agent_wallet())["confirmation_id"]
    result = await service.create_agent_wallet(token)
    assert result == {
        "status": "CREATED",
        "address": ADDRESS,
        "network": "base-sepolia",
        "explorer_url": f"https://sepolia.basescan.org/address/{ADDRESS}",
    }
    cdp.evm.create_account.assert_awaited_once()
    assert cdp.evm.create_account.call_args.kwargs["name"].startswith("shopper-")
    assert database.connection.execute("SELECT COUNT(*) FROM wallet").fetchone()[0] == 1
    path = database.path
    database.close()
    reopened = Database(path)
    try:
        fresh = WalletService(reopened, cdp, ConfirmationStore(), settings=settings, web3=web3)
        assert fresh.get_wallet() == {
            **result,
            "status": "READY",
            "usdc_balance": "0.00",
            "eth_balance": "0",
            "funding_status": "NOT_FUNDED",
        }
        assert (await fresh.create_agent_wallet())["status"] == "ALREADY_EXISTS"
    finally:
        reopened.close()
    cdp.evm.create_account.assert_awaited_once()


async def test_repeat_creation_does_not_create_a_second_account(service, cdp):
    """Return the persisted wallet for repeat prompts after successful creation.

    Args:
        service: The wallet service.
        cdp: The observable fake provider.

    Returns:
        None.
    """
    token = (await service.create_agent_wallet())["confirmation_id"]
    await service.create_agent_wallet(token)
    assert (await service.create_agent_wallet())["status"] == "ALREADY_EXISTS"
    with pytest.raises(ConfirmationError, match="CONFIRMATION_EXPIRED"):
        await service.create_agent_wallet(token)
    with pytest.raises(ConfirmationError, match="INVALID_CONFIRMATION"):
        await service.create_agent_wallet("invented")
    cdp.evm.create_account.assert_awaited_once()


async def test_concurrent_confirmations_create_exactly_one_remote_account(service, cdp):
    """Serialize concurrent valid confirmations before creating a CDP account.

    Args:
        service: The wallet service.
        cdp: The observable fake provider.

    Returns:
        None.
    """
    first = (await service.create_agent_wallet())["confirmation_id"]
    second = (await service.create_agent_wallet())["confirmation_id"]
    results = await asyncio.gather(
        service.create_agent_wallet(first), service.create_agent_wallet(second)
    )
    assert sorted(result["status"] for result in results) == ["ALREADY_EXISTS", "CREATED"]
    cdp.evm.create_account.assert_awaited_once()


async def test_invalid_and_mismatched_confirmations_do_not_call_cdp(service, cdp):
    """Keep invalid or changed confirmations from reaching the wallet provider.

    Args:
        service: The wallet service.
        cdp: The observable fake provider.

    Returns:
        None.
    """
    with pytest.raises(ConfirmationError, match="INVALID_CONFIRMATION"):
        await service.create_agent_wallet("invented")
    token = service.confirmations.create_confirmation("create_agent_wallet", {"network": "mainnet"})
    with pytest.raises(ConfirmationError, match="CONFIRMATION_MISMATCH"):
        await service.create_agent_wallet(token)
    cdp.evm.create_account.assert_not_awaited()


async def test_provider_failure_is_sanitized_and_consumes_confirmation(
    service, database, cdp, caplog
):
    """Hide provider exception details and prevent retry with the same token.

    Args:
        service: The wallet service.
        database: The test repository.
        cdp: The fake provider configured to fail.
        caplog: Pytest's captured log records.

    Returns:
        None.
    """
    cdp.evm.create_account.side_effect = RuntimeError(SECRET)
    token = (await service.create_agent_wallet())["confirmation_id"]
    with pytest.raises(WalletError, match="WALLET_CREATION_FAILED") as error:
        await service.create_agent_wallet(token)
    assert SECRET not in str(error.value)
    assert SECRET not in caplog.text
    assert not database.wallet_exists()
    with pytest.raises(ConfirmationError, match="CONFIRMATION_EXPIRED"):
        await service.create_agent_wallet(token)
    cdp.evm.create_account.assert_awaited_once()


async def test_outputs_and_database_contain_only_public_wallet_fields(service, database):
    """Project public fields instead of serializing the SDK account object.

    Args:
        service: The wallet service.
        database: The test repository.

    Returns:
        None.
    """
    token = (await service.create_agent_wallet())["confirmation_id"]
    result = await service.create_agent_wallet(token)
    assert SECRET not in json.dumps(result)
    assert SECRET not in json.dumps(service.get_wallet())
    columns = database.connection.execute("PRAGMA table_info(wallet)").fetchall()
    assert {row["name"] for row in columns} == {"id", "account_name", "address", "created_at"}
    assert SECRET not in "\n".join(database.connection.iterdump())


def test_database_enforces_single_wallet(database):
    """Reject a second pointer and any non-singleton row at the SQL boundary.

    Args:
        database: The test repository.

    Returns:
        None.
    """
    database.create_wallet_pointer("shopper-1", ADDRESS)
    with pytest.raises(sqlite3.IntegrityError):
        database.create_wallet_pointer("shopper-2", ADDRESS)
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "INSERT INTO wallet VALUES (2, ?, ?, ?)", ("shopper-2", ADDRESS, "now")
        )


@pytest.mark.parametrize("value,kind", [(ADDRESS, "address"), ("0x" + "ab" * 32, "tx")])
def test_explorer_url_selects_the_public_resource(value, kind):
    """Build the appropriate Base Sepolia address or transaction URL.

    Args:
        value: A valid public EVM address or transaction hash.
        kind: The expected explorer resource type.

    Returns:
        None.
    """
    assert get_explorer_url(value) == f"https://sepolia.basescan.org/{kind}/{value}"


def test_explorer_url_rejects_arbitrary_strings():
    """Avoid interpolating arbitrary or secret values into explorer URLs.

    Args:
        None.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="Invalid public address or transaction hash"):
        get_explorer_url(SECRET)


async def test_expired_confirmation_cannot_reach_cdp(service, cdp, monkeypatch):
    """Keep expiry enforcement in front of the provider side effect.

    Args:
        service: The wallet service.
        cdp: The observable fake provider.
        monkeypatch: Pytest's scoped clock replacement.

    Returns:
        None.
    """
    monkeypatch.setattr("app.confirmations.time.monotonic", lambda: 100.0)
    token = (await service.create_agent_wallet())["confirmation_id"]
    monkeypatch.setattr("app.confirmations.time.monotonic", lambda: 700.0)
    with pytest.raises(ConfirmationError, match="CONFIRMATION_EXPIRED"):
        await service.create_agent_wallet(token)
    cdp.evm.create_account.assert_not_awaited()


async def test_uncertain_provider_response_reuses_creation_identity_after_restart(service, cdp):
    """Reuse the remote idempotency key after a timeout and process restart.

    Args:
        service: The original wallet service and durable repository.
        cdp: The provider that first simulates a lost response.

    Returns:
        None.
    """
    cdp.evm.create_account.side_effect = TimeoutError(SECRET)
    token = (await service.create_agent_wallet())["confirmation_id"]
    with pytest.raises(WalletError, match="WALLET_CREATION_FAILED"):
        await service.create_agent_wallet(token)
    first_arguments = cdp.evm.create_account.call_args.kwargs.copy()
    path = service.database.path
    service.database.close()
    reopened = Database(path)
    try:
        fresh = WalletService(reopened, cdp, ConfirmationStore())
        cdp.evm.create_account.side_effect = None
        token = (await fresh.create_agent_wallet())["confirmation_id"]
        assert (await fresh.create_agent_wallet(token))["status"] == "CREATED"
        assert cdp.evm.create_account.call_args.kwargs == first_arguments
    finally:
        reopened.close()


async def test_failed_pointer_write_keeps_remote_request_identity(service, cdp, monkeypatch):
    """Keep a successful remote account from being duplicated after a local write failure.

    Args:
        service: The wallet service.
        cdp: The observable fake provider.
        monkeypatch: Pytest's scoped method replacement.

    Returns:
        None.
    """
    original = service.database.create_wallet_pointer

    def fail_write(account_name, address):
        """Simulate a local disk failure without changing the durable request record.

        Args:
            account_name: The attempted public CDP account name.
            address: The attempted public EVM address.

        Returns:
            None: Always raises to simulate an unavailable database write.

        Raises:
            sqlite3.OperationalError: The simulated disk is full.
        """
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(service.database, "create_wallet_pointer", fail_write)
    token = (await service.create_agent_wallet())["confirmation_id"]
    with pytest.raises(WalletError, match="WALLET_CREATION_FAILED"):
        await service.create_agent_wallet(token)
    first_arguments = cdp.evm.create_account.call_args.kwargs.copy()
    monkeypatch.setattr(service.database, "create_wallet_pointer", original)
    token = (await service.create_agent_wallet())["confirmation_id"]
    assert (await service.create_agent_wallet(token))["status"] == "CREATED"
    assert cdp.evm.create_account.call_args.kwargs == first_arguments


async def test_recovery_reads_reserved_account_without_creating_again(service, cdp):
    """Recover a remotely created account even after CDP stops replaying old requests.

    Args:
        service: The wallet service with a durable creation identity.
        cdp: The provider simulating a lost creation response followed by recovery.

    Returns:
        None.
    """
    cdp.evm.create_account.side_effect = TimeoutError(SECRET)
    token = (await service.create_agent_wallet())["confirmation_id"]
    with pytest.raises(WalletError, match="WALLET_CREATION_FAILED"):
        await service.create_agent_wallet(token)
    reserved_name = cdp.evm.create_account.call_args.kwargs["name"]
    cdp.evm.get_account.side_effect = None
    cdp.evm.get_account.return_value = SimpleNamespace(address=ADDRESS)
    token = (await service.create_agent_wallet())["confirmation_id"]
    assert (await service.create_agent_wallet(token))["status"] == "CREATED"
    cdp.evm.get_account.assert_awaited_with(name=reserved_name)
    cdp.evm.create_account.assert_awaited_once()


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_lookup_errors_do_not_fall_through_to_creation(service, cdp, status):
    """Create only after a genuine 404, never after auth or provider failures.

    Args:
        service: The wallet service.
        cdp: The observable fake provider.
        status: The non-404 HTTP failure from the SDK's account lookup.

    Returns:
        None.
    """
    cdp.evm.get_account.side_effect = ApiError(status, "unknown", SECRET)
    token = (await service.create_agent_wallet())["confirmation_id"]
    with pytest.raises(WalletError, match="WALLET_CREATION_FAILED"):
        await service.create_agent_wallet(token)
    cdp.evm.create_account.assert_not_awaited()
