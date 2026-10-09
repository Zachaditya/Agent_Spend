"""Specify merchant offer configuration, testnet funding, and safe reruns."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.create_offer import create_offer, parse_args, stage_output
from tests import test_purchase
from tests.conftest import ADDRESS, ETH_TX_HASH, TX_HASH

store = test_purchase.store


@pytest.mark.parametrize("opt_in", [False, True])
async def test_script_defaults_opt_in_shortfall_and_rerun(
    database, settings, cdp, web3, store, opt_in, tmp_path
):
    """Fund only shortfalls, persist the explicit restriction, and safely repeat.

    Args:
        database: Isolated offer repository.
        settings: Testnet configuration.
        cdp: Provider double.
        web3: Balance and receipt double.
        store: Catalog double.
        opt_in: Whether merchant explicitly restricts customers.
        tmp_path: Isolated funding journal directory.

    Returns:
        None. Transfer amounts, stage output, and idempotency are verified.
    """
    argv = ["--product", "0591439009", "--cashback-pct", "15", "--budget", "5"]
    if opt_in:
        argv.append("--new-customers-only")
    args = parse_args(argv)
    escrow = SimpleNamespace(address=ADDRESS)
    cdp.evm.get_or_create_account.return_value = escrow
    cdp.evm.get_account.return_value = cdp.treasury
    cdp.evm.get_account.side_effect = None
    web3.eth.chain_id = 84532
    web3.usdc_balances[ADDRESS] = 2_000_000
    web3.eth_balances[ADDRESS] = 100_000_000_000_000

    async def transfer(**kwargs):
        """Credit the exact requested fake transfer to escrow.

        Args:
            kwargs: CDP transfer parameters.

        Returns:
            Public token-specific transfer hash.
        """
        assert kwargs["to"] == ADDRESS
        assert kwargs["network"] == "base-sepolia"
        if kwargs["token"] == "usdc":
            assert kwargs["amount"] == 3_000_000
            web3.usdc_balances[ADDRESS] += kwargs["amount"]
            return TX_HASH
        assert kwargs["amount"] == 100_000_000_000_000
        web3.eth_balances[ADDRESS] += kwargs["amount"]
        return ETH_TX_HASH

    cdp.treasury.transfer = AsyncMock(side_effect=transfer)
    journal = tmp_path / "funding.json"
    for _ in range(2):
        result = await create_offer(args, settings, database, cdp, web3, store, journal)
        assert result["new_customer_only"] == int(opt_in)
        assert result["cashback_bps"] == 1500
        assert result["budget_cents"] == 500
        assert result["spent_cents"] == 0
    assert cdp.treasury.transfer.await_count == 2
    output = stage_output(result)
    assert output.startswith("Offer live: ROOT CLASSIC TEE\n  15% cashback, ")
    assert ("new customers only" if opt_in else "all customers") in output
    assert "$5.00 budget" in output
    assert "https://sepolia.basescan.org/address/" in output


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--cashback-pct", "50.01"),
        ("--cashback-pct", "0"),
        ("--cashback-pct", "nan"),
        ("--budget", "1.001"),
        ("--budget", "-1"),
        ("--days", "0"),
    ],
)
def test_invalid_arguments_fail_before_any_side_effect(flag, value):
    """Reject malformed merchant rates, precision, budgets, and expiry durations.

    Args:
        flag: CLI argument being validated.
        value: Invalid string value.

    Returns:
        None. Parsing exits before provider or database access.
    """
    with pytest.raises(SystemExit):
        parse_args(["--product", "tee", "--cashback-pct", "15", "--budget", "5", flag, value])


async def test_wrong_chain_cannot_create_or_fund(database, settings, cdp, web3, store, tmp_path):
    """Reject a mainnet RPC before any account, offer, or transfer side effect.

    Args:
        database: Isolated repository.
        settings: Testnet configuration.
        cdp: Provider double.
        web3: Deliberately mainnet reader.
        store: Catalog double.
        tmp_path: Journal directory.

    Returns:
        None. No account or transfer is created.
    """
    web3.eth.chain_id = 8453
    args = parse_args(["--product", "0591439009", "--cashback-pct", "15", "--budget", "5"])
    with pytest.raises(RuntimeError, match="Base Sepolia"):
        await create_offer(args, settings, database, cdp, web3, store, tmp_path / "journal.json")
    cdp.evm.get_or_create_account.assert_not_called()
    assert database.get_offer("0591439009") is None


async def test_lost_submission_response_cannot_double_fund(
    database, settings, cdp, web3, store, tmp_path
):
    """Retain an unresolved submission journal when the provider response is lost.

    Args:
        database: Isolated repository.
        settings: Testnet configuration.
        cdp: Provider double.
        web3: Testnet reader.
        store: Catalog double.
        tmp_path: Persistent journal directory.

    Returns:
        None. Reruns cannot resubmit an ambiguous leg or activate an unfunded offer.
    """
    web3.eth.chain_id = 84532
    cdp.evm.get_or_create_account.return_value = SimpleNamespace(address=ADDRESS)
    cdp.evm.get_account = AsyncMock(return_value=cdp.treasury)
    cdp.treasury.transfer = AsyncMock(side_effect=TimeoutError("secret-provider-detail"))
    args = parse_args(["--product", "0591439009", "--cashback-pct", "15", "--budget", "5"])
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await create_offer(
                args, settings, database, cdp, web3, store, tmp_path / "journal.json"
            )
    cdp.treasury.transfer.assert_awaited_once()
    assert database.get_offer("0591439009") is None
    assert "secret-provider-detail" not in (tmp_path / "journal.json").read_text()


async def test_receipt_timeout_rerun_checks_saved_hash_without_resubmitting(
    database, settings, cdp, web3, store, tmp_path
):
    """Resume a receipt check without submitting its treasury transfer twice.

    Args:
        database: Isolated offer repository.
        settings: Testnet configuration.
        cdp: Provider double.
        web3: Fake reader which times out once after the USDC submission.
        store: Catalog double.
        tmp_path: Persistent journal directory.

    Returns:
        None. One USDC transfer activates the offer only after receipt recovery.
    """
    web3.eth.chain_id = 84532
    web3.eth_balances[ADDRESS] = 200_000_000_000_000
    cdp.evm.get_or_create_account.return_value = SimpleNamespace(address=ADDRESS)
    cdp.evm.get_account = AsyncMock(return_value=cdp.treasury)

    async def transfer(**kwargs):
        """Simulate a confirmed token credit before its receipt is observed.

        Args:
            kwargs: CDP transfer arguments.

        Returns:
            str: Submitted transaction hash.
        """
        web3.usdc_balances[ADDRESS] = kwargs["amount"]
        return TX_HASH

    cdp.treasury.transfer = AsyncMock(side_effect=transfer)
    from unittest.mock import Mock

    web3.eth.wait_for_transaction_receipt = Mock(side_effect=[TimeoutError(), {"status": 1}])
    args = parse_args(["--product", "0591439009", "--cashback-pct", "15", "--budget", "5"])
    journal = tmp_path / "journal.json"
    with pytest.raises(RuntimeError, match="receipt unresolved"):
        await create_offer(args, settings, database, cdp, web3, store, journal)
    assert database.get_offer("0591439009") is None
    assert TX_HASH in journal.read_text()
    await create_offer(args, settings, database, cdp, web3, store, journal)
    cdp.treasury.transfer.assert_awaited_once()


def test_configuration_has_escrow_defaults(settings):
    """Expose a separate escrow account and precise gas target by default.

    Args:
        settings: Valid testnet settings.

    Returns:
        None. The default escrow cannot share the treasury account name.
    """
    from dataclasses import replace
    from decimal import Decimal

    assert settings.escrow_account_name == "offer-escrow"
    assert settings.escrow_eth == Decimal("0.0002")
    with pytest.raises(ValueError):
        replace(settings, escrow_account_name=settings.treasury_account_name)
    with pytest.raises(ValueError):
        replace(settings, escrow_eth="NaN")
