"""Verify offer ranking and exactly-once cashback after merchant confirmation."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db import Database
from app.store import StoreError
from tests import test_purchase
from tests.conftest import ADDRESS, TX_HASH

purchase_service = test_purchase.purchase_service
store = test_purchase.store

PRODUCT = "0591439009"
CB_HASH = "0x" + "c" * 64


@pytest.fixture
def cashback_setup(database, purchase_service, cdp, web3):
    """Install an all-customer offer and a separate escrow account double.

    Args:
        database: Isolated SQLite repository.
        purchase_service: Service with a funded shopper and full-price policy.
        cdp: Fake provider whose accounts are selected by name.
        web3: Reader with exact cashback receipt evidence.

    Returns:
        Escrow transfer double for verifying side effects.
    """
    database.upsert_offer(
        PRODUCT, 1500, False, 500, (datetime.now(UTC) + timedelta(days=1)).isoformat()
    )
    web3.receipts[CB_HASH] = cashback_receipt()
    web3.receipts["0x" + "d" * 64] = cashback_receipt()
    shopper = cdp.evm.get_account.return_value
    escrow = SimpleNamespace(address="0x" + "e" * 40, transfer=AsyncMock(return_value=CB_HASH))

    async def account_by_name(*, name):
        """Resolve only the configured shopper or offer escrow.

        Args:
            name: Server-owned account name.

        Returns:
            Account double for the requested name.
        """
        return escrow if name == "offer-escrow" else shopper

    cdp.evm.get_account.side_effect = account_by_name
    return escrow


@pytest.mark.parametrize("returning", [False, True])
async def test_search_sorts_all_candidates_before_limiting(
    database, purchase_service, store, cashback_setup, returning
):
    """Rank the offer by net price even beyond the store's original top ten.

    Args:
        database: Isolated repository.
        purchase_service: Configured service.
        store: Catalog double.
        cashback_setup: Observable escrow double.
        returning: Whether a prior confirmed order exists.

    Returns:
        None. Full-price filtering, ranking, and transfer-free search are checked.
    """
    root = store.products[PRODUCT]
    store.products = {
        f"other-{n}": dict(
            root, product_id=f"other-{n}", name="Printed tee 9.99", amount_cents=1099, price="10.99"
        )
        for n in range(12)
    }
    store.products[PRODUCT] = root
    if returning:
        database.insert_purchase(
            product_id="old",
            product_name="Old",
            amount_cents=1,
            status="CONFIRMED",
            reason_code="WITHIN_POLICY",
            order_id="old",
            payment_intent_id="old",
        )
    results = await purchase_service.search_products("tee", "15", 2)
    assert results[0]["product_id"] == PRODUCT
    assert (results[0]["price"], results[0]["cashback"], results[0]["net_price"]) == (
        "12.00",
        "1.80",
        "10.20",
    )
    assert results[1]["cashback"] == "0.00"
    assert all(
        p["product_id"] != PRODUCT for p in await purchase_service.search_products("tee", "11")
    )
    cashback_setup.transfer.assert_not_called()


async def test_paid_cashback_follows_confirmation_and_is_idempotent(
    database, purchase_service, store, cashback_setup
):
    """Settle exactly once and retain both links without increasing policy allowance.

    Args:
        database: Isolated repository.
        purchase_service: Purchase service.
        store: Observable merchant double.
        cashback_setup: Escrow transfer double.

    Returns:
        None. Ordering, amount, budget, history, and repeated settlement are verified.
    """

    async def checked_transfer(**kwargs):
        """Assert merchant confirmation precedes any escrow transfer.

        Args:
            kwargs: Provider transfer parameters.

        Returns:
            Public cashback hash.
        """
        assert store.confirmed
        assert kwargs == dict(to=ADDRESS, amount=1_800_000, token="usdc", network="base-sepolia")
        return CB_HASH

    cashback_setup.transfer.side_effect = checked_transfer
    result = await purchase_service.request_purchase(PRODUCT)
    assert result["status"] == "CONFIRMED"
    assert result["cashback_status"] == "PAID"
    assert result["cashback"] == "1.80"
    assert result["cashback_explorer_url"].endswith(CB_HASH)
    assert result["explorer_url"].endswith(TX_HASH)
    assert database.get_offer(PRODUCT)["spent_cents"] == 180
    assert database.get_spent_this_week(datetime.now(UTC)) == 1200
    await asyncio.gather(
        *(purchase_service.settle_cashback(result["purchase_id"]) for _ in range(3))
    )
    cashback_setup.transfer.assert_awaited_once()
    assert database.get_offer(PRODUCT)["spent_cents"] == 180
    assert purchase_service.get_transactions()["transactions"][0]["cashback_status"] == "PAID"
    assert (await purchase_service.search_products("tee"))[0]["net_price"] == "10.20"


@pytest.mark.parametrize("failure", ["merchant", "revert", "timeout", "submit"])
async def test_cashback_failure_never_retries_or_undoes_purchase(
    database, purchase_service, store, cashback_setup, web3, failure
):
    """Keep confirmed orders intact and retain uncertain payout budget conservatively.

    Args:
        database: Isolated repository.
        purchase_service: Purchase service.
        store: Merchant double.
        cashback_setup: Escrow double.
        web3: Receipt double.
        failure: Controlled failure point.

    Returns:
        None. At-most-once payout and public error sanitization are checked.
    """
    if failure == "merchant":
        store.confirm_payment = AsyncMock(side_effect=StoreError("CONFIRM_FAILED"))
    elif failure == "submit":
        cashback_setup.transfer.side_effect = RuntimeError("secret-provider-detail")
    elif failure == "revert":
        web3.receipts[CB_HASH] = {"status": 0}
    else:
        original = purchase_service._wait_for_receipt

        async def wait(tx_hash, timeout=60):
            """Inject an ambiguous cashback receipt timeout.

            Args:
                tx_hash: Public transaction hash.
                timeout: Receipt timeout.

            Returns:
                Merchant receipt; cashback raises TimeoutError.
            """
            if tx_hash == CB_HASH:
                assert database.get_purchase_history()[0]["cashback_tx_hash"] == CB_HASH
                raise TimeoutError("secret-provider-detail")
            return await original(tx_hash, timeout)

        purchase_service._wait_for_receipt = wait
    result = await purchase_service.request_purchase(PRODUCT)
    assert "secret-provider-detail" not in str(result)
    await purchase_service.settle_cashback(result["purchase_id"])
    if failure == "merchant":
        assert result["status"] == "SUBMITTED"
        cashback_setup.transfer.assert_not_called()
        assert database.get_offer(PRODUCT)["spent_cents"] == 0
    else:
        assert result["status"] == "CONFIRMED"
        assert result["cashback_status"] == "FAILED"
        cashback_setup.transfer.assert_awaited_once()
        assert database.get_offer(PRODUCT)["spent_cents"] == (0 if failure == "revert" else 180)


@pytest.mark.parametrize(
    "budget,expired,note",
    [
        (179, False, "Offer budget used up"),
        (500, True, "Offer expired"),
        (180, False, "15% cashback"),
    ],
)
async def test_limits_apply_in_search_and_settlement(
    database, purchase_service, cashback_setup, budget, expired, note
):
    """Apply the same expiry and inclusive budget rules at discovery and payment.

    Args:
        database: Isolated repository.
        purchase_service: Service under test.
        cashback_setup: Escrow double.
        budget: Configured budget cents.
        expired: Whether the fixture expired yesterday.
        note: Expected public eligibility explanation.

    Returns:
        None. Ineligible offers cannot cause transfers.
    """
    database.upsert_offer(
        PRODUCT,
        1500,
        False,
        budget,
        (datetime.now(UTC) + timedelta(days=-1 if expired else 1)).isoformat(),
    )
    result = (await purchase_service.search_products("tee"))[0]
    assert result["offer_note"] == note
    paid = await purchase_service.request_purchase(PRODUCT)
    assert paid["offer_note"] == note
    assert paid["cashback_status"] == ("PAID" if budget == 180 else "NOT_ELIGIBLE")
    assert cashback_setup.transfer.await_count == (1 if budget == 180 else 0)


async def test_distinct_purchases_compete_atomically_for_budget(
    database, purchase_service, cashback_setup
):
    """Allow distinct returning purchases while preventing concurrent budget overspend.

    Args:
        database: Isolated repository.
        purchase_service: Service under test.
        cashback_setup: Escrow double.

    Returns:
        None. Only two 180-cent rewards consume a 500-cent budget across connections.
    """
    rows = [
        database.insert_purchase(
            product_id=PRODUCT,
            product_name="Tee",
            amount_cents=1200,
            status="CONFIRMED",
            reason_code="WITHIN_POLICY",
            order_id=str(n),
            payment_intent_id=str(n),
            tx_hash="0x" + str(n + 1) * 64,
        )
        for n in range(3)
    ]
    cashback_setup.transfer.side_effect = [CB_HASH, "0x" + "d" * 64]
    other = Database(database.path)
    sibling = type(purchase_service)(
        database=other,
        cdp=purchase_service.cdp,
        store=purchase_service.store,
        merchant_address=purchase_service.merchant_address,
        web3=purchase_service.web3,
    )
    try:
        await asyncio.gather(
            purchase_service.settle_cashback(rows[0]["id"]),
            sibling.settle_cashback(rows[1]["id"]),
            purchase_service.settle_cashback(rows[2]["id"]),
        )
        assert cashback_setup.transfer.await_count == 2
        assert database.get_offer(PRODUCT)["spent_cents"] == 360
        assert sorted(r["cashback_status"] for r in database.get_purchase_history()) == [
            "NOT_ELIGIBLE",
            "PAID",
            "PAID",
        ]
    finally:
        other.close()


@pytest.mark.parametrize("fault", ["missing", "sender", "recipient", "token", "amount"])
async def test_paid_requires_exact_escrow_transfer_receipt(
    database, purchase_service, cashback_setup, web3, fault
):
    """Reject successful receipts lacking the exact escrow-to-shopper token transfer.

    Args:
        database: Isolated ledger.
        purchase_service: Service under test.
        cashback_setup: Escrow account.
        web3: Receipt double.
        fault: Receipt evidence to corrupt.

    Returns:
        None. Unproven cashback remains failed with a conservative budget allocation.
    """
    receipt = cashback_receipt()
    if fault == "missing":
        receipt["logs"] = []
    elif fault == "token":
        receipt["logs"][0]["address"] = ADDRESS
    elif fault == "amount":
        receipt["logs"][0]["data"] = hex(1_800_001)
    else:
        receipt["logs"][0]["topics"][1 if fault == "sender" else 2] = "0x" + "0" * 64
    web3.receipts[CB_HASH] = receipt
    result = await purchase_service.request_purchase(PRODUCT)
    assert result["cashback_status"] == "FAILED"
    assert database.get_offer(PRODUCT)["spent_cents"] == 180


def cashback_receipt():
    """Build exact escrow transfer evidence for a 180-cent cashback.

    Args:
        None.

    Returns:
        dict: Successful USDC receipt with indexed escrow sender and shopper recipient.
    """
    return {
        "status": 1,
        "logs": [
            {
                "address": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + "0" * 24 + "e" * 40,
                    "0x" + "0" * 24 + ADDRESS[2:].lower(),
                ],
                "data": hex(1_800_000),
            }
        ],
    }


async def test_cashback_cannot_raise_full_price_weekly_limit(
    database, purchase_service, cashback_setup
):
    """Reject gross spend above policy even when its advertised net price would fit.

    Args:
        database: Isolated repository.
        purchase_service: Service under test.
        cashback_setup: Observable escrow account.

    Returns:
        None. Neither merchant nor escrow receives a transfer.
    """
    database.set_policy(1100, 1100)
    result = await purchase_service.request_purchase(PRODUCT)
    assert result["reason_code"] == "WEEKLY_LIMIT_EXCEEDED"
    assert result["cashback_status"] == "NONE"
    cashback_setup.transfer.assert_not_called()


async def test_reconfiguration_cannot_release_uncertain_payout(
    database, purchase_service, cashback_setup
):
    """Block a budget reset while a lost provider response may still settle.

    Args:
        database: Isolated repository.
        purchase_service: Configured service.
        cashback_setup: Escrow with a lost submission response.

    Returns:
        None. Durable allocation survives service restart and offer replacement attempts.
    """
    cashback_setup.transfer.side_effect = TimeoutError()
    result = await purchase_service.request_purchase(PRODUCT)
    reopened = Database(database.path)
    try:
        assert reopened.claim_cashback(result["purchase_id"], datetime.now(UTC))[1] is False
        with pytest.raises(ValueError, match="unresolved cashback"):
            reopened.upsert_offer(PRODUCT, 1500, False, 500, "2099-01-01T00:00:00+00:00")
        assert reopened.get_offer(PRODUCT)["spent_cents"] == 180
    finally:
        reopened.close()


async def test_opt_in_restriction_excludes_current_purchase_only(
    database, purchase_service, cashback_setup
):
    """Allow the first purchase under opt-in and exclude a subsequent distinct purchase.

    Args:
        database: Isolated repository.
        purchase_service: Configured service.
        cashback_setup: Escrow double.

    Returns:
        None. Current confirmation does not incorrectly make a new buyer ineligible.
    """
    database.upsert_offer(PRODUCT, 1500, True, 500, "2099-01-01T00:00:00+00:00")
    first = await purchase_service.request_purchase(PRODUCT)
    assert first["cashback_status"] == "PAID"
    second = database.insert_purchase(
        product_id=PRODUCT,
        product_name="Tee",
        amount_cents=1200,
        status="CONFIRMED",
        reason_code="WITHIN_POLICY",
        order_id="second",
        payment_intent_id="second",
    )
    settled = await purchase_service.settle_cashback(second["id"])
    assert settled["cashback_status"] == "NOT_ELIGIBLE"
    assert settled["offer_note"] == "Offer for new customers only"
    cashback_setup.transfer.assert_awaited_once()
