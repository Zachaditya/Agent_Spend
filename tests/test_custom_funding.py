"""Specify custom grants, repeated top-ups, and durable funding safeguards."""

import asyncio
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.confirmations import ConfirmationError, ConfirmationStore
from app.db import Database, FundingConflict
from app.wallet import WalletError, WalletService
from tests.conftest import ADDRESS, ETH_TX_HASH, SECRET, TREASURY_ADDRESS, TX_HASH

TOP_UP_HASH = "0x" + "ef" * 32


@pytest.fixture
def shopper(database, web3, cdp):
    """Prepare a shopper and enough fake treasury funds for custom grants.

    Args:
        database: Isolated SQLite repository.
        web3: Observable fake chain state.
        cdp: Observable fake signing provider.

    Returns:
        None.
    """
    database.create_wallet_pointer("shopper-1", ADDRESS)
    web3.usdc_balances[TREASURY_ADDRESS] = 1_000_000_000
    web3.eth_balances[TREASURY_ADDRESS] = 10**18
    cdp.treasury.transfer.side_effect = [TX_HASH, ETH_TX_HASH, TOP_UP_HASH, TX_HASH]


async def grant(service, amount="50"):
    """Request and confirm one grant through the public service entry point.

    Args:
        service: Wallet service under test.
        amount: Decimal-string USDC amount to request.

    Returns:
        dict: Confirmed operation or public failure result.
    """
    requested = await service.fund_agent_wallet(amount_usdc=amount)
    assert requested["status"] == "CONFIRMATION_REQUIRED"
    return await service.fund_agent_wallet(confirmation_id=requested["confirmation_id"])


async def test_custom_grant_discloses_and_transfers_exact_amount(shopper, service, database, cdp):
    """Bind a 50 USDC initial grant to consent, units, and audit records.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet orchestration service.
        database: Isolated funding audit repository.
        cdp: Observable fake provider.

    Returns:
        None.
    """
    requested = await service.fund_agent_wallet(amount_usdc="50")
    assert requested["usdc_amount"] == "50.00"
    assert requested["funding_kind"] == "INITIAL"
    assert "50.00 test USDC" in requested["disclosure"]
    assert "0.0005 test ETH" in requested["disclosure"]
    cdp.treasury.transfer.assert_not_awaited()
    assert database.get_latest_funding_event() is None
    result = await service.fund_agent_wallet(confirmation_id=requested["confirmation_id"])
    assert result["status"] == "FUNDED"
    assert result["usdc_amount"] == "50.00"
    assert result["funding_kind"] == "INITIAL"
    assert cdp.treasury.transfer.await_args_list[0].kwargs["amount"] == 50_000_000
    assert database.get_latest_funding_event()["id"] == result["operation_id"]
    assert database.get_latest_funding_event()["usdc_amount_cents"] == 5000


@pytest.mark.parametrize(
    "amount",
    ["0", "-1", "NaN", "Infinity", "0.001", "1.000", "abc", "$50", "1e2", "", True, 50, "9" * 200],
)
async def test_invalid_amounts_never_issue_consent_or_transfer(
    shopper, service, database, cdp, amount
):
    """Reject malformed, non-string, non-positive, or imprecise amount inputs.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        database: Isolated audit repository.
        cdp: Observable fake provider.
        amount: Invalid amount input.

    Returns:
        None.
    """
    with pytest.raises(WalletError, match="^INVALID_FUNDING_AMOUNT$"):
        await service.fund_agent_wallet(amount_usdc=amount)
    assert database.get_latest_funding_event() is None
    assert not service.confirmations._confirmations
    cdp.treasury.transfer.assert_not_awaited()


@pytest.mark.parametrize(
    "amount,units", [("0.01", 10_000), ("25.50", 25_500_000), ("100.00", 100_000_000)]
)
async def test_exact_cent_and_per_transfer_boundaries(shopper, service, cdp, amount, units):
    """Accept exact cents and the inclusive per-operation funding cap.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        cdp: Observable fake provider.
        amount: Valid decimal string.
        units: Expected token base units.

    Returns:
        None.
    """
    assert (await grant(service, amount))["status"] == "FUNDED"
    assert cdp.treasury.transfer.await_args_list[0].kwargs["amount"] == units


async def test_changed_amount_is_rejected_but_equivalent_amount_is_accepted(shopper, service, cdp):
    """Keep the disclosed amount immutable while accepting equivalent decimals.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        cdp: Observable fake provider.

    Returns:
        None.
    """
    token = (await service.fund_agent_wallet(amount_usdc="50"))["confirmation_id"]
    with pytest.raises(ConfirmationError, match="CONFIRMATION_MISMATCH"):
        await service.fund_agent_wallet(confirmation_id=token, amount_usdc="49")
    cdp.treasury.transfer.assert_not_awaited()
    assert (await service.fund_agent_wallet(confirmation_id=token, amount_usdc="50.00"))[
        "status"
    ] == "FUNDED"


async def test_topups_require_new_consent_and_never_repeat_gas(shopper, service, database, cdp):
    """Allow separately confirmed additions while rejecting confirmation replay.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        database: Isolated audit repository.
        cdp: Observable fake provider.

    Returns:
        None.
    """
    first = await grant(service)
    requested = await service.fund_agent_wallet(amount_usdc="10")
    assert requested["funding_kind"] == "TOP_UP"
    assert requested["eth_amount"] == "0"
    assert cdp.treasury.transfer.await_count == 2
    second = await service.fund_agent_wallet(confirmation_id=requested["confirmation_id"])
    assert second["status"] == "FUNDED"
    assert second["operation_id"] != first["operation_id"]
    assert second["usdc_amount"] == "10.00"
    assert "eth_tx_hash" not in second
    assert cdp.treasury.transfer.await_args_list[-1].kwargs == {
        "to": ADDRESS,
        "amount": 10_000_000,
        "token": "usdc",
        "network": "base-sepolia",
    }
    assert database.get_latest_funding_event()["eth_status"] == "NOT_REQUIRED"
    with pytest.raises(ConfirmationError, match="CONFIRMATION_EXPIRED"):
        await service.fund_agent_wallet(confirmation_id=requested["confirmation_id"])
    default = await service.fund_agent_wallet()
    assert default["usdc_amount"] == "25.00"
    assert default["funding_kind"] == "TOP_UP"
    assert cdp.treasury.transfer.await_count == 3


async def test_concurrent_replay_and_stale_disclosures_cannot_double_fund(shopper, service, cdp):
    """Serialize execution and invalidate other intents issued before funding.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        cdp: Observable fake provider.

    Returns:
        None.
    """
    first = (await service.fund_agent_wallet(amount_usdc="50"))["confirmation_id"]
    stale = (await service.fund_agent_wallet(amount_usdc="10"))["confirmation_id"]
    results = await asyncio.gather(
        service.fund_agent_wallet(confirmation_id=first),
        service.fund_agent_wallet(confirmation_id=first),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) and result["status"] == "FUNDED" for result in results) == 1
    assert sum(isinstance(result, ConfirmationError) for result in results) == 1
    with pytest.raises(ConfirmationError, match="CONFIRMATION_MISMATCH"):
        await service.fund_agent_wallet(confirmation_id=stale)
    assert cdp.treasury.transfer.await_count == 2


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("treasury_account_name", "other-treasury"),
        ("usdc_contract_address", ADDRESS),
        ("starter_eth_wei", 1),
    ],
)
async def test_configuration_changes_invalidate_confirmation(
    shopper, service, cdp, attribute, value
):
    """Bind confirmation to server-owned source, token, and gas parameters.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        cdp: Observable fake provider.
        attribute: Configuration field to change after disclosure.
        value: Replacement configuration value.

    Returns:
        None.
    """
    token = (await service.fund_agent_wallet(amount_usdc="50"))["confirmation_id"]
    setattr(service, attribute, value)
    with pytest.raises(ConfirmationError, match="CONFIRMATION_MISMATCH"):
        await service.fund_agent_wallet(confirmation_id=token)
    cdp.treasury.transfer.assert_not_awaited()


async def test_caps_count_history_and_allow_exact_lifetime_boundary(
    shopper, service, database, cdp
):
    """Enforce a cumulative cap independently of live balance or prior spending.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        database: Isolated audit repository.
        cdp: Observable fake provider.

    Returns:
        None.
    """
    assert (await service.fund_agent_wallet(amount_usdc="100.01"))[
        "status"
    ] == "FUNDING_AMOUNT_ABOVE_CAP"
    for _ in range(4):
        event = database.insert_funding_event(ADDRESS, 10000, "0")
        database.mark_funding_confirmed(event["id"], TX_HASH, None)
    assert (await grant(service, "100"))["status"] == "FUNDED"
    assert (await service.fund_agent_wallet(amount_usdc="0.01"))[
        "status"
    ] == "FUNDING_TOTAL_CAP_EXCEEDED"
    assert cdp.treasury.transfer.await_count == 1


async def test_fee_eth_is_required_beyond_the_initial_grant(shopper, service, cdp, web3):
    """Reject treasury ETH covering the grant but not transaction fees.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        cdp: Observable fake provider.
        web3: Fake fee and balance reader.

    Returns:
        None.
    """
    web3.eth_balances[TREASURY_ADDRESS] = service.starter_eth_wei
    assert (await grant(service))["status"] == "INSUFFICIENT_TREASURY_FUNDS"
    cdp.treasury.transfer.assert_not_awaited()


async def test_receipt_timeout_blocks_new_funding_across_restart(
    shopper, service, database, cdp, settings, web3, monkeypatch
):
    """Preserve uncertain submission and its cap reservation after restart.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        database: Isolated audit repository.
        cdp: Observable fake provider.
        settings: Synthetic application settings.
        web3: Fake public chain reader.
        monkeypatch: Scoped receipt failure replacement helper.

    Returns:
        None.
    """

    def timeout(*args, **kwargs):
        """Simulate a receipt unavailable before the timeout.

        Args:
            *args: Receipt lookup positional arguments.
            **kwargs: Receipt lookup keyword arguments.

        Returns:
            None: Always raises TimeoutError.
        """
        raise TimeoutError(SECRET)

    monkeypatch.setattr(web3.eth, "wait_for_transaction_receipt", timeout)
    result = await grant(service)
    assert result["status"] == "FUNDING_FAILED"
    assert SECRET not in str(result)
    latest = database.get_latest_funding_event()
    assert latest["status"] == "UNKNOWN"
    assert latest["usdc_tx_hash"] == TX_HASH
    assert latest["usdc_status"] == "UNKNOWN"
    reopened = Database(database.path)
    try:
        fresh = WalletService(reopened, cdp, None, settings=settings, web3=web3)
        assert (await fresh.fund_agent_wallet(amount_usdc="10"))["status"] == "FUNDING_IN_PROGRESS"
        assert reopened.reserved_funding_cents(ADDRESS) == 5000
    finally:
        reopened.close()
    assert cdp.treasury.transfer.await_count == 1


async def test_failed_topup_does_not_erase_funded_status(shopper, service, database, cdp, web3):
    """Keep wallet funding history separate from a failed later operation.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        database: Isolated audit repository.
        cdp: Observable fake provider.
        web3: Fake receipt reader.

    Returns:
        None.
    """
    await grant(service)
    web3.receipts[TOP_UP_HASH] = SimpleNamespace(status=0)
    assert (await grant(service, "10"))["status"] == "FUNDING_FAILED"
    assert database.get_latest_funding_event()["status"] == "FAILED"
    assert database.reserved_funding_cents(ADDRESS) == 5000
    assert service.get_wallet()["funding_status"] == "FUNDED"
    assert service.get_wallet()["latest_funding_status"] == "FAILED"
    assert (await service.fund_agent_wallet(amount_usdc="10"))["status"] == "CONFIRMATION_REQUIRED"


async def test_partial_initial_grant_is_reconciled_without_resending_usdc(
    shopper, service, database, cdp, web3
):
    """Recover only a reverted gas leg from the existing initial operation.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        database: Isolated audit repository.
        cdp: Observable fake provider.
        web3: Fake receipt reader.

    Returns:
        None.
    """
    web3.receipts[ETH_TX_HASH] = SimpleNamespace(status=0)
    assert (await grant(service))["status"] == "FUNDING_FAILED"
    event = database.get_latest_funding_event()
    assert event["status"] == "PARTIAL"
    assert event["usdc_status"] == "CONFIRMED"
    recovered = await service.reconcile_funding(event["id"], recover_missing=True)
    assert recovered["status"] == "FUNDED"
    assert recovered["operation_id"] == event["id"]
    assert [item.kwargs["token"] for item in cdp.treasury.transfer.await_args_list] == [
        "usdc",
        "eth",
        "eth",
    ]


def test_funding_caps_are_validated_in_configuration(settings):
    """Validate positive cent-denominated caps and compatible starter defaults.

    Args:
        settings: Synthetic application settings.

    Returns:
        None.
    """
    assert str(settings.max_funding_usdc) == "100"
    assert str(settings.max_total_funding_usdc) == "500"
    for changes in (
        {"max_funding_usdc": "0"},
        {"max_total_funding_usdc": "NaN"},
        {"starter_usdc": "100.01"},
        {"max_funding_usdc": "100.001"},
        {"max_total_funding_usdc": "20"},
        {"starter_eth": "0.0000000000000000001"},
    ):
        with pytest.raises(ValueError):
            replace(settings, **changes)


def test_legacy_database_migrates_without_losing_funding_history(tmp_path):
    """Preserve baseline audit rows and count them toward funding caps.

    Args:
        tmp_path: Pytest's isolated directory for the legacy database.

    Returns:
        None.
    """
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE funding_events (id TEXT PRIMARY KEY, wallet_address TEXT NOT NULL, "
            "usdc_amount_cents INTEGER NOT NULL, eth_amount_wei TEXT NOT NULL, "
            "usdc_tx_hash TEXT, eth_tx_hash TEXT, status TEXT NOT NULL "
            "CHECK (status IN ('SUBMITTED','CONFIRMED','FAILED')), created_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO funding_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy",
                ADDRESS,
                2500,
                "500000000000000",
                TX_HASH,
                ETH_TX_HASH,
                "CONFIRMED",
                "2026-10-07",
            ),
        )
    migrated = Database(path)
    try:
        event = migrated.get_latest_funding_event()
        assert event["id"] == "legacy"
        assert event["funding_kind"] == "INITIAL"
        assert event["usdc_status"] == "CONFIRMED"
        assert event["eth_status"] == "CONFIRMED"
        assert migrated.reserved_funding_cents(ADDRESS) == 2500
    finally:
        migrated.close()


def test_confirmation_snapshot_cannot_be_modified_by_the_caller():
    """Copy intent parameters at issuance and when resolving a confirmation.

    Args:
        None.

    Returns:
        None.
    """
    store = ConfirmationStore()
    params = {"amount": 5000, "nested": {"network": "base-sepolia"}}
    token = store.create_confirmation("fund_agent_wallet", params)
    params["nested"]["network"] = "mainnet"
    snapshot = store.get_confirmation_params(token, "fund_agent_wallet")
    snapshot["nested"]["network"] = "other"
    assert (
        store.get_confirmation_params(token, "fund_agent_wallet")["nested"]["network"]
        == "base-sepolia"
    )


async def test_two_topups_and_restart_preserve_history(
    shopper, service, database, cdp, settings, web3
):
    """Permit two fresh top-ups and another request after reopening SQLite.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        database: Isolated persistent repository.
        cdp: Observable fake provider.
        settings: Synthetic configuration.
        web3: Fake receipt and balance reader.

    Returns:
        None.
    """
    first = await grant(service, "50")
    second = await grant(service, "10")
    assert (await grant(service, "5"))["status"] == "FUNDED"
    pending = (await service.fund_agent_wallet(amount_usdc="1"))["confirmation_id"]
    reopened = Database(database.path)
    try:
        fresh = WalletService(reopened, cdp, None, settings=settings, web3=web3)
        with pytest.raises(ConfirmationError, match="INVALID_CONFIRMATION"):
            await fresh.fund_agent_wallet(confirmation_id=pending)
        requested = await fresh.fund_agent_wallet(amount_usdc="1")
        assert requested["funding_kind"] == "TOP_UP"
        assert reopened.reserved_funding_cents(ADDRESS) == 6500
        assert first["operation_id"] != second["operation_id"]
    finally:
        reopened.close()
    assert [call.kwargs["token"] for call in cdp.treasury.transfer.await_args_list] == [
        "usdc",
        "eth",
        "usdc",
        "usdc",
    ]


async def test_database_claim_rejects_competing_intents_and_same_leg(shopper, service, database):
    """Enforce durable operation and leg claims across separate connections.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service used to prepare server-owned intents.
        database: First SQLite repository connection.

    Returns:
        None.
    """
    token = (await service.fund_agent_wallet(amount_usdc="50"))["confirmation_id"]
    intent = service.confirmations.get_confirmation_params(token, "fund_agent_wallet")
    second = Database(database.path)
    try:
        event = database.reserve_funding(intent, 50000)
        with pytest.raises(FundingConflict, match="FUNDING_ALREADY_SUBMITTED"):
            second.reserve_funding(intent, 50000)
        competing = {**intent, "intent_id": "other", "funding_sequence": 1}
        with pytest.raises(FundingConflict, match="FUNDING_IN_PROGRESS"):
            second.reserve_funding(competing, 50000)
        database.begin_funding_leg(event["id"], "usdc")
        with pytest.raises(FundingConflict, match="FUNDING_IN_PROGRESS"):
            second.begin_funding_leg(event["id"], "usdc")
        assert second.reserved_funding_cents(ADDRESS) == 5000
    finally:
        second.close()


async def test_cancellation_before_hash_blocks_recovery_and_new_funding(
    shopper, service, database, cdp, settings, web3
):
    """Leave a durable unresolved claim when execution ends during submission.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        database: Persistent funding repository.
        cdp: Provider that is cancelled before returning a hash.
        settings: Synthetic configuration.
        web3: Fake public chain reader.

    Returns:
        None.
    """
    cdp.treasury.transfer.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await grant(service)
    event = database.get_latest_funding_event()
    assert event["usdc_status"] == "SUBMITTING"
    reopened = Database(database.path)
    try:
        fresh = WalletService(reopened, cdp, None, settings=settings, web3=web3)
        assert (await fresh.fund_agent_wallet(amount_usdc="10"))["status"] == "FUNDING_IN_PROGRESS"
        assert (await fresh.reconcile_funding(event["id"], recover_missing=True))[
            "status"
        ] == "FUNDING_FAILED"
        assert reopened.get_latest_funding_event()["status"] == "UNKNOWN"
    finally:
        reopened.close()
    assert cdp.treasury.transfer.await_count == 1


async def test_receipt_only_reconciliation_never_sends_pending_gas(
    shopper, service, database, cdp, web3, monkeypatch
):
    """Resolve a timeout while leaving known missing ETH for explicit recovery.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        database: Audit repository.
        cdp: Observable fake provider.
        web3: Fake receipt reader.
        monkeypatch: Scoped timeout replacement helper.

    Returns:
        None.
    """
    original = web3.eth.wait_for_transaction_receipt
    monkeypatch.setattr(
        web3.eth,
        "wait_for_transaction_receipt",
        Mock(side_effect=TimeoutError()),
    )
    await grant(service)
    monkeypatch.setattr(web3.eth, "wait_for_transaction_receipt", original)
    event = database.get_latest_funding_event()
    result = await service.reconcile_funding(event["id"])
    assert result["latest_funding_event"]["status"] == "PARTIAL"
    assert cdp.treasury.transfer.await_count == 1
    recovered = await service.reconcile_funding(event["id"], recover_missing=True)
    assert recovered["status"] == "FUNDED"
    assert cdp.treasury.transfer.await_count == 2
    assert {
        row[0] for row in database.connection.execute("SELECT status FROM funding_attempts")
    } == {"CONFIRMED"}


async def test_topup_requires_fee_eth_and_failures_release_only_definite_reservations(
    shopper, service, database, cdp, web3
):
    """Apply fee coverage and cumulative reservations to subsequent additions.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        database: Audit repository.
        cdp: Observable fake provider.
        web3: Fake balances and receipts.

    Returns:
        None.
    """
    await grant(service)
    web3.eth_balances[TREASURY_ADDRESS] = 0
    assert (await grant(service, "10"))["status"] == "INSUFFICIENT_TREASURY_FUNDS"
    assert cdp.treasury.transfer.await_count == 2
    web3.eth_balances[TREASURY_ADDRESS] = 10**18
    cdp.treasury.transfer.side_effect = RuntimeError(SECRET)
    assert (await grant(service, "10"))["status"] == "FUNDING_FAILED"
    assert database.reserved_funding_cents(ADDRESS) == 6000
    assert (await service.fund_agent_wallet(amount_usdc="1"))["status"] == "FUNDING_IN_PROGRESS"


async def test_reservation_failure_never_calls_provider(
    shopper, service, database, cdp, monkeypatch
):
    """Stop before signing when the durable funding reservation cannot commit.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        database: Repository with a simulated write failure.
        cdp: Observable fake provider.
        monkeypatch: Scoped database replacement helper.

    Returns:
        None.
    """

    def unavailable(*args, **kwargs):
        """Simulate an audit persistence failure.

        Args:
            *args: Reservation positional arguments.
            **kwargs: Reservation keyword arguments.

        Returns:
            None: Always raises sqlite3.OperationalError.
        """
        raise sqlite3.OperationalError("disk unavailable")

    monkeypatch.setattr(database, "reserve_funding", unavailable)
    with pytest.raises(sqlite3.OperationalError):
        await grant(service)
    cdp.treasury.transfer.assert_not_awaited()


async def test_insufficient_usdc_is_reported_before_reverting_gas_estimate(
    shopper, service, cdp, web3, monkeypatch
):
    """Avoid misreporting an insufficient token balance as an unavailable treasury.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        cdp: Observable fake provider.
        web3: Fake treasury balances.
        monkeypatch: Scoped fee-estimation failure replacement helper.

    Returns:
        None.
    """

    def reverting_estimate(transaction):
        """Simulate ERC-20 gas estimation reverting for insufficient token funds.

        Args:
            transaction: Proposed treasury transfer.

        Returns:
            None: Always raises RuntimeError.
        """
        raise RuntimeError(SECRET)

    web3.usdc_balances[TREASURY_ADDRESS] = 0
    monkeypatch.setattr(web3.eth, "estimate_gas", reverting_estimate)
    assert (await grant(service))["status"] == "INSUFFICIENT_TREASURY_FUNDS"
    cdp.treasury.transfer.assert_not_awaited()


async def test_custom_funding_adds_to_live_balance_instead_of_setting_it(
    shopper, service, cdp, web3
):
    """Read updated on-chain balances after adding custom USDC to an existing balance.

    Args:
        shopper: Prepared shopper fixture.
        service: Wallet service under test.
        cdp: Provider double updating fake chain state.
        web3: Fake live balances read by the service.

    Returns:
        None.
    """
    web3.usdc_balances[ADDRESS] = 20_000_000

    async def transfer(*, to, amount, token, network):
        """Apply one exact fake transfer to its recipient's chain balance.

        Args:
            to: Persisted shopper recipient.
            amount: Token base units or ETH wei.
            token: USDC or ETH symbol chosen by the server.
            network: Server-selected Base Sepolia network.

        Returns:
            str: Deterministic transaction hash for receipt verification.
        """
        balances = web3.usdc_balances if token == "usdc" else web3.eth_balances
        balances[to] = balances.get(to, 0) + amount
        return TX_HASH if token == "usdc" else ETH_TX_HASH

    cdp.treasury.transfer.side_effect = transfer
    assert (await grant(service, "50"))["usdc_balance"] == "70.00"
    topped_up = await grant(service, "10")
    assert topped_up["usdc_balance"] == "80.00"
    assert topped_up["eth_balance"] == "0.0005"
