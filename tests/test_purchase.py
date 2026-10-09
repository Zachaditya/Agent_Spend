"""Specify Phase 5 purchase orchestration, persistence, and history output."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.purchase import PurchaseService
from app.store import StoreError
from app.wallet import cents_to_usdc_base_units
from tests.conftest import ADDRESS, MERCHANT_ADDRESS, TX_HASH


class FakeStore:
    """Provide deterministic eshop responses for purchase-service tests."""

    def __init__(self) -> None:
        """Initialize product, order, cancellation, and confirmation state.

        Args:
            None.

        Returns:
            None.
        """
        self.products: dict[str, dict[str, object]] = {}
        self.orders: list[dict[str, object]] = []
        self.cancelled: list[str] = []
        self.confirmed: list[tuple[str, str]] = []

    async def search_products(
        self, query: str, max_price_cents: int | None = None, limit: int = 5
    ) -> list[dict[str, object]]:
        """Return matching products in insertion order.

        Args:
            query: Product search string.
            max_price_cents: Optional maximum price in cents.
            limit: Maximum number of products to return.

        Returns:
            list[dict[str, object]]: Matching normalized products.
        """
        del query
        products = list(self.products.values())
        if max_price_cents is not None:
            products = [item for item in products if item["amount_cents"] <= max_price_cents]
        return products[:limit]

    async def get_product(self, product_id: str) -> dict[str, object]:
        """Return one configured product.

        Args:
            product_id: Store-owned product identifier.

        Returns:
            dict[str, object]: Normalized product details.
        """
        return self.products[product_id]

    async def create_order(self, product_id: str) -> dict[str, object]:
        """Create a fake payment intent for one configured product.

        Args:
            product_id: Store-owned product identifier.

        Returns:
            dict[str, object]: Order and payment-intent fields from the merchant API.
        """
        product = self.products[product_id]
        order = {
            "order_id": f"order-{len(self.orders) + 1}",
            "payment_intent_id": f"intent-{len(self.orders) + 1}",
            "amount_cents": product["amount_cents"],
            "pay_to": product.get("pay_to", MERCHANT_ADDRESS),
            "currency": "USDC",
            "network": "base-sepolia",
            "status": "requires_payment",
        }
        self.orders.append(order)
        return order

    async def confirm_payment(self, payment_intent_id: str, tx_hash: str) -> dict[str, object]:
        """Mark a fake payment intent paid.

        Args:
            payment_intent_id: Merchant payment intent identifier.
            tx_hash: Public Base Sepolia transaction hash.

        Returns:
            dict[str, object]: Paid order response.
        """
        self.confirmed.append((payment_intent_id, tx_hash))
        return {"status": "paid", "payment_intent_id": payment_intent_id, "tx_hash": tx_hash}

    async def cancel_payment_intent(self, payment_intent_id: str) -> dict[str, object]:
        """Record a fake cancellation request.

        Args:
            payment_intent_id: Merchant payment intent identifier.

        Returns:
            dict[str, object]: Canceled intent response.
        """
        self.cancelled.append(payment_intent_id)
        return {"status": "canceled", "payment_intent_id": payment_intent_id}


@pytest.fixture
def store() -> FakeStore:
    """Create a store with the demo products used by Phase 5.

    Args:
        None.

    Returns:
        FakeStore: Store double with normalized product rows.
    """
    fake = FakeStore()
    fake.products = {
        "0591439009": {
            "product_id": "0591439009",
            "name": "ROOT CLASSIC TEE",
            "price": "12.00",
            "amount_cents": 1200,
            "color": "Black",
            "category": "Garment Upper body",
            "description": "A classic tee.",
        },
        "0521471002": {
            "product_id": "0521471002",
            "name": "Harmony College Sweater",
            "price": "14.00",
            "amount_cents": 1400,
            "color": "Gray",
            "category": "Garment Upper body",
            "description": "A college sweater.",
        },
    }
    return fake


@pytest.fixture
def purchase_service(database, cdp, web3, store) -> PurchaseService:
    """Assemble a Phase 5 purchase service with a funded wallet and active policy.

    Args:
        database: Isolated SQLite repository.
        cdp: Fake CDP provider.
        web3: Fake Web3 reader.
        store: Fake merchant store client.

    Returns:
        PurchaseService: Service under test.
    """
    database.create_wallet_pointer("shopper-1", ADDRESS)
    database.set_policy(2500, 1500)
    web3.usdc_balances[ADDRESS] = cents_to_usdc_base_units(2500)
    shopper = SimpleNamespace(address=ADDRESS, transfer=AsyncMock(return_value=TX_HASH))
    cdp.evm.get_account = AsyncMock(return_value=shopper)
    return PurchaseService(
        database=database,
        cdp=cdp,
        store=store,
        merchant_address=MERCHANT_ADDRESS,
        web3=web3,
    )


async def test_approved_purchase_pays_merchant_confirms_order_and_records_history(
    purchase_service, database, cdp, store
) -> None:
    """Pay an within-policy product from the shopper wallet and persist confirmation.

    Args:
        purchase_service: Configured purchase service.
        database: SQLite repository inspected after execution.
        cdp: Fake CDP provider whose shopper transfer is observed.
        store: Fake merchant store client.

    Returns:
        None. Assertions prove payment, merchant confirmation, and history fields.
    """
    result = await purchase_service.request_purchase("0591439009")

    assert result["decision"] == "APPROVED"
    assert result["reason_code"] == "WITHIN_POLICY"
    assert result["status"] == "CONFIRMED"
    assert result["order_id"] == "order-1"
    assert result["amount"] == "12.00"
    assert result["tx_hash"] == TX_HASH
    assert result["explorer_url"].endswith(TX_HASH)
    assert result["cashback_status"] == "NONE"
    assert result["cashback"] == "0.00"
    cdp.evm.get_account.assert_awaited_once_with(name="shopper-1")
    cdp.evm.get_account.return_value.transfer.assert_awaited_once_with(
        to=MERCHANT_ADDRESS,
        amount=cents_to_usdc_base_units(1200),
        token="usdc",
        network="base-sepolia",
    )
    assert store.confirmed == [("intent-1", TX_HASH)]
    assert store.cancelled == []
    row = database.connection.execute("SELECT * FROM purchases").fetchone()
    assert row["status"] == "CONFIRMED"
    assert row["product_name"] == "ROOT CLASSIC TEE"
    assert row["tx_hash"] == TX_HASH

    history = purchase_service.get_transactions()
    assert history["transactions"][0]["status"] == "CONFIRMED"
    assert history["transactions"][0]["explorer_url"].endswith(TX_HASH)


async def test_transaction_hash_is_durable_before_receipt_wait(
    purchase_service, database, monkeypatch
) -> None:
    """Persist a submitted transfer before waiting on external confirmation.

    Args:
        purchase_service: Configured service with a successful fake transfer.
        database: SQLite ledger inspected while receipt waiting is in progress.
        monkeypatch: Fixture replacing the receipt wait with a ledger assertion.

    Returns:
        None. The hash and reserved spend exist before any receipt is available.
    """

    async def inspect_submission(tx_hash: str, timeout: int = 60) -> dict[str, int]:
        """Inspect the durable submission at the receipt boundary.

        Args:
            tx_hash: Hash returned by the fake CDP transfer.
            timeout: Receipt timeout supplied by the service.

        Returns:
            dict[str, int]: Successful receipt after the durability assertions.
        """
        row = database.get_purchase_history()[0]
        assert row["status"] == "SUBMITTED"
        assert row["tx_hash"] == tx_hash == TX_HASH
        return {"status": 1}

    monkeypatch.setattr(purchase_service, "_wait_for_receipt", inspect_submission)

    result = await purchase_service.request_purchase("0591439009")

    assert result["status"] == "CONFIRMED"


async def test_merchant_failure_preserves_paid_hash_and_reserves_weekly_spend(
    purchase_service, database, store, cdp
) -> None:
    """Keep a mined payment submitted when merchant verification is unavailable.

    Args:
        purchase_service: Configured purchase service.
        database: Ledger used to verify retained transaction and weekly spend.
        store: Fake merchant whose confirmation fails.
        cdp: Fake account whose transfer count proves no automatic repayment.

    Returns:
        None. A merchant failure retains the hash, reserved spend, and retry warning.
    """
    store.confirm_payment = AsyncMock(side_effect=StoreError("PAYMENT_CONFIRMATION_FAILED"))

    result = await purchase_service.request_purchase("0591439009")

    assert result["status"] == "SUBMITTED"
    assert result["reason_code"] == "MERCHANT_CONFIRMATION_PENDING"
    assert result["tx_hash"] == TX_HASH
    assert "Do not retry" in result["message"]
    assert database.get_spent_this_week(datetime.now(UTC)) == 1200
    assert purchase_service.get_transactions()["transactions"][0]["tx_hash"] == TX_HASH
    cdp.evm.get_account.return_value.transfer.assert_awaited_once()


async def test_receipt_timeout_preserves_hash_and_reservation(
    purchase_service, database, store, monkeypatch
) -> None:
    """Treat a receipt timeout as uncertain payment rather than a failed transfer.

    Args:
        purchase_service: Configured service whose transfer returns a hash.
        database: Ledger inspected for conservative spend accounting.
        store: Fake merchant that must not be asked to verify an unknown receipt.
        monkeypatch: Fixture replacing receipt waiting with a timeout.

    Returns:
        None. The hash and spend remain durable without merchant confirmation.
    """
    monkeypatch.setattr(purchase_service, "_wait_for_receipt", AsyncMock(side_effect=TimeoutError))

    result = await purchase_service.request_purchase("0591439009")

    assert result["status"] == "SUBMITTED"
    assert result["reason_code"] == "PAYMENT_RECEIPT_PENDING"
    assert result["tx_hash"] == TX_HASH
    assert database.get_spent_this_week(datetime.now(UTC)) == 1200
    assert store.confirmed == []


async def test_submission_exception_keeps_unknown_payment_reserved(
    purchase_service, database, cdp, store
) -> None:
    """Reserve uncertain submissions even when CDP returns no transaction hash.

    Args:
        purchase_service: Configured purchase service.
        database: Ledger whose reservation must survive the provider exception.
        cdp: Fake account simulating a timeout during transfer submission.
        store: Merchant double that must not confirm or cancel an uncertain payment.

    Returns:
        None. An unknown submission cannot release budget or claim no money moved.
    """
    cdp.evm.get_account.return_value.transfer.side_effect = TimeoutError

    result = await purchase_service.request_purchase("0591439009")

    assert result["status"] == "SUBMITTED"
    assert result["reason_code"] == "PAYMENT_SUBMISSION_UNKNOWN"
    assert "tx_hash" not in result
    assert "Do not retry" in result["message"]
    assert database.get_spent_this_week(datetime.now(UTC)) == 1200
    assert store.confirmed == store.cancelled == []


async def test_reverted_payment_retains_hash_but_releases_spend(
    purchase_service, database, store, web3
) -> None:
    """Release reserved spend only for a definitively reverted transaction.

    Args:
        purchase_service: Service submitting a fake transaction.
        database: Ledger used to verify reverted payment accounting.
        store: Merchant double that must not confirm a reverted transaction.
        web3: Fake chain reader returning receipt status zero.

    Returns:
        None. The failed row retains its hash while weekly spend returns to zero.
    """
    web3.receipts[TX_HASH] = {"status": 0}

    result = await purchase_service.request_purchase("0591439009")

    assert result["status"] == "FAILED"
    assert result["reason_code"] == "PAYMENT_REVERTED"
    assert result["tx_hash"] == TX_HASH
    assert database.get_spent_this_week(datetime.now(UTC)) == 0
    assert store.confirmed == []


async def test_unresolved_purchase_blocks_repeat_after_duplicate_window(
    purchase_service, database, cdp, store
) -> None:
    """Block repayment for an unresolved product even after two minutes have elapsed.

    Args:
        purchase_service: Service receiving the repeated product request.
        database: Ledger seeded with an older unresolved submitted purchase.
        cdp: Fake provider whose transfer must not be called.
        store: Merchant double that must not receive another order.

    Returns:
        None. The original purchase is returned without another order or transfer.
    """
    original = database.insert_purchase(
        product_id="0591439009",
        product_name="ROOT CLASSIC TEE",
        amount_cents=1200,
        status="SUBMITTED",
        reason_code="MERCHANT_CONFIRMATION_PENDING",
        order_id="original-order",
        payment_intent_id="original-intent",
        tx_hash=TX_HASH,
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    result = await purchase_service.request_purchase("0591439009")

    assert result["status"] == "SUBMITTED"
    assert result["purchase_id"] == original["id"]
    assert result["tx_hash"] == TX_HASH
    assert "Do not retry" in result["message"]
    assert store.orders == []
    cdp.evm.get_account.return_value.transfer.assert_not_awaited()


@pytest.mark.parametrize(
    "confirmation",
    [
        {"status": "requires_payment"},
        {"status": "paid", "payment_intent_id": "other-intent", "tx_hash": TX_HASH},
        {"status": "paid", "payment_intent_id": "intent-1", "tx_hash": "0x" + "cd" * 32},
        None,
    ],
)
async def test_unpaid_merchant_response_cannot_confirm_purchase(
    purchase_service, store, confirmation
) -> None:
    """Require a matching paid response before reporting purchase confirmation.

    Args:
        purchase_service: Service with a successfully mined fake payment.
        store: Merchant double returning an untrusted confirmation response.
        confirmation: Unpaid, mismatched, or malformed merchant response payload.

    Returns:
        None. An invalid merchant response leaves the purchase submitted.
    """
    store.confirm_payment = AsyncMock(return_value=confirmation)

    result = await purchase_service.request_purchase("0591439009")

    assert result["status"] == "SUBMITTED"
    assert result["reason_code"] == "MERCHANT_CONFIRMATION_PENDING"
    assert result["tx_hash"] == TX_HASH


async def test_account_lookup_failure_does_not_submit_or_reserve_payment(
    purchase_service, database, cdp
) -> None:
    """Release spend when account lookup fails before transfer submission begins.

    Args:
        purchase_service: Purchase service receiving an otherwise allowed request.
        database: Ledger used to check the absence of reserved spend.
        cdp: Fake provider failing while loading the existing shopper account.

    Returns:
        None. A known pre-submission failure reports no payment and moves no money.
    """
    account = cdp.evm.get_account.return_value
    cdp.evm.get_account.side_effect = RuntimeError("private-provider-detail")

    result = await purchase_service.request_purchase("0591439009")

    assert result["status"] == "FAILED"
    assert result["reason_code"] == "PAYMENT_ACCOUNT_UNAVAILABLE"
    assert "no transfer was submitted" in result["message"]
    assert "private-provider-detail" not in str(result)
    assert database.get_spent_this_week(datetime.now(UTC)) == 0
    account.transfer.assert_not_awaited()


async def test_weekly_limit_rejection_cancels_intent_and_moves_no_money(
    purchase_service, database, cdp, store
) -> None:
    """Reject a second purchase that would exceed the rolling weekly limit.

    Args:
        purchase_service: Configured purchase service.
        database: SQLite repository inspected after execution.
        cdp: Fake CDP provider whose transfer calls are observed.
        store: Fake merchant store client.

    Returns:
        None. Assertions prove cancellation and absence of a second transfer.
    """
    await purchase_service.request_purchase("0591439009")
    cdp.evm.get_account.return_value.transfer.reset_mock()

    result = await purchase_service.request_purchase("0521471002")

    assert result["decision"] == "REJECTED"
    assert result["reason_code"] == "WEEKLY_LIMIT_EXCEEDED"
    assert result["status"] == "REJECTED"
    assert store.cancelled == ["intent-2"]
    cdp.evm.get_account.return_value.transfer.assert_not_awaited()
    rows = database.connection.execute(
        "SELECT product_id, status, reason_code FROM purchases ORDER BY created_at"
    ).fetchall()
    assert [(row["product_id"], row["status"], row["reason_code"]) for row in rows] == [
        ("0591439009", "CONFIRMED", "WITHIN_POLICY"),
        ("0521471002", "REJECTED", "WEEKLY_LIMIT_EXCEEDED"),
    ]


async def test_above_auto_purchase_stays_unpaid_and_visible_to_policy(
    purchase_service, database, cdp, store
) -> None:
    """Leave above-auto purchases pending without exposing an approval tool.

    Args:
        purchase_service: Configured purchase service.
        database: SQLite repository inspected after execution.
        cdp: Fake CDP provider whose transfer calls are observed.
        store: Fake merchant store client.

    Returns:
        None. Assertions prove no payment or cancellation occurs for pending approval.
    """
    database.set_policy(2500, 1000)

    result = await purchase_service.request_purchase("0591439009")

    assert result["decision"] == "HUMAN_APPROVAL_REQUIRED"
    assert result["reason_code"] == "ABOVE_AUTO_LIMIT"
    assert result["status"] == "PENDING_APPROVAL"
    assert result["purchase_id"]
    assert "not paid" in result["message"]
    cdp.evm.get_account.return_value.transfer.assert_not_awaited()
    assert store.cancelled == []
    assert database.get_pending_approvals()[0]["id"] == result["purchase_id"]


async def test_duplicate_request_rejects_and_cancels_second_intent(
    purchase_service, cdp, store
) -> None:
    """Reject the same product requested again inside the two-minute window.

    Args:
        purchase_service: Configured purchase service.
        cdp: Fake CDP provider whose transfer calls are observed.
        store: Fake merchant store client.

    Returns:
        None. Assertions prove duplicate protection prevents the second payment.
    """
    await purchase_service.request_purchase("0591439009")
    cdp.evm.get_account.return_value.transfer.reset_mock()

    result = await purchase_service.request_purchase("0591439009")

    assert result["decision"] == "REJECTED"
    assert result["reason_code"] == "DUPLICATE_PURCHASE"
    assert store.cancelled == ["intent-2"]
    cdp.evm.get_account.return_value.transfer.assert_not_awaited()


async def test_no_policy_rejects_before_creating_merchant_order(database, cdp, web3, store) -> None:
    """Return NO_POLICY without creating an unpaid merchant payment intent.

    Args:
        database: SQLite repository without an active policy.
        cdp: Fake CDP provider.
        web3: Fake Web3 reader.
        store: Fake merchant store client.

    Returns:
        None. Assertions prove no merchant side effect happens.
    """
    database.create_wallet_pointer("shopper-1", ADDRESS)
    service = PurchaseService(
        database=database,
        cdp=cdp,
        store=store,
        merchant_address=MERCHANT_ADDRESS,
        web3=web3,
    )

    result = await service.request_purchase("0591439009")

    assert result == {
        "decision": "REJECTED",
        "reason_code": "NO_POLICY",
        "message": "Set a spending policy before requesting purchases.",
    }
    assert store.orders == []


async def test_search_and_product_tools_delegate_to_store_with_price_parsing(
    purchase_service,
) -> None:
    """Expose product search and details through the purchase service.

    Args:
        purchase_service: Configured purchase service.

    Returns:
        None. Assertions prove display values and max-price parsing are stable.
    """
    products = await purchase_service.search_products("tee", max_price="12", limit=10)
    detail = await purchase_service.get_product("0591439009")

    assert [item["product_id"] for item in products] == ["0591439009"]
    assert detail["name"] == "ROOT CLASSIC TEE"


def test_database_purchase_queries_drive_policy_lookup(database) -> None:
    """Persist purchase spend, pending approvals, duplicate windows, and history.

    Args:
        database: Isolated SQLite repository.

    Returns:
        None. Assertions prove Phase 5 query contracts survive restart.
    """
    first = database.insert_purchase(
        product_id="tee",
        product_name="ROOT CLASSIC TEE",
        amount_cents=1200,
        status="SUBMITTED",
        reason_code="WITHIN_POLICY",
        order_id="order-1",
        payment_intent_id="intent-1",
    )
    pending = database.insert_purchase(
        product_id="sweater",
        product_name="Harmony College Sweater",
        amount_cents=1400,
        status="PENDING_APPROVAL",
        reason_code="ABOVE_AUTO_LIMIT",
        order_id="order-2",
        payment_intent_id="intent-2",
    )

    assert database.get_spent_this_week(first["created_at_datetime"]) == 1200
    assert database.get_pending_approvals()[0]["id"] == pending["id"]
    assert database.get_recent_product_purchases("tee", first["created_at_datetime"])[0][0] == "tee"
    history = database.get_purchase_history(limit=10)
    assert [row["product_id"] for row in history] == ["sweater", "tee"]


def test_history_rejects_unsafe_limits(purchase_service) -> None:
    """Clamp transaction history limits to a small public range.

    Args:
        purchase_service: Configured purchase service.

    Returns:
        None. Assertions prove malformed limits do not reach SQLite.
    """
    assert purchase_service.get_transactions(limit=0)["transactions"] == []
    assert purchase_service.get_transactions(limit=100)["transactions"] == []
