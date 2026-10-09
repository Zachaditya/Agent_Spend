"""Orchestrate Phase 5 product search, policy checks, payment, and history."""

import asyncio
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.db import Database
from app.policy import Decision, Policy, PurchaseContext, evaluate_purchase
from app.store import StoreClient, StoreError
from app.wallet import (
    NETWORK,
    cents_to_usdc_base_units,
    format_cents,
    get_explorer_url,
    get_usdc_balance,
)


class PurchaseError(Exception):
    """Represent a sanitized purchase-loop failure suitable for MCP output."""


def parse_optional_price(max_price: Decimal | int | str | None) -> int | None:
    """Normalize an optional decimal USDC price into whole cents.

    Args:
        max_price: Optional decimal string, Decimal, or integer dollar amount.

    Returns:
        int | None: Whole cents, or None when no maximum was supplied.

    Raises:
        PurchaseError: The supplied price is malformed, negative, or sub-cent.
    """
    if max_price is None:
        return None
    if isinstance(max_price, bool):
        raise PurchaseError("INVALID_PRICE")
    if isinstance(max_price, str) and not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,2})?", max_price):
        raise PurchaseError("INVALID_PRICE")
    try:
        parsed = Decimal(str(max_price))
    except (InvalidOperation, ValueError):
        raise PurchaseError("INVALID_PRICE") from None
    cents = parsed * Decimal("100")
    if parsed < 0 or cents != cents.to_integral_value():
        raise PurchaseError("INVALID_PRICE")
    return int(cents)


class PurchaseService:
    """Coordinate the merchant checkout API, policy engine, CDP payment, and history."""

    def __init__(
        self,
        *,
        database: Database,
        cdp: Any,
        store: StoreClient,
        merchant_address: str,
        usdc_contract_address: str = "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        web3: Any | None = None,
    ) -> None:
        """Bind persistence, merchant client, CDP account access, and chain reads.

        Args:
            database: Initialized Agent Spend SQLite repository.
            cdp: Lifespan-managed CDP client used to load the shopper account.
            store: Authenticated eshop client.
            merchant_address: Only allowed merchant recipient for purchases.
            usdc_contract_address: Base Sepolia USDC token contract address.
            web3: Optional Web3-compatible reader for balances and receipts.

        Returns:
            None.
        """
        self.database = database
        self.cdp = cdp
        self.store = store
        self.merchant_address = merchant_address
        self.usdc_contract_address = usdc_contract_address
        self.web3 = web3

    async def search_products(
        self, query: str, max_price: Decimal | int | str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Search the merchant catalog with optional price filtering.

        Args:
            query: Product query to pass to semantic or name search.
            max_price: Optional maximum test-USDC price.
            limit: Maximum number of products to return.

        Returns:
            list[dict[str, Any]]: Normalized public product results.

        Raises:
            PurchaseError: The price is malformed or the store cannot be reached.
        """
        try:
            max_price_cents = parse_optional_price(max_price)
            return await self.store.search_products(query, max_price_cents, limit)
        except StoreError as error:
            raise PurchaseError(str(error)) from None

    async def get_product(self, product_id: str) -> dict[str, Any]:
        """Read a single merchant product detail by store-owned ID.

        Args:
            product_id: Store-owned product identifier.

        Returns:
            dict[str, Any]: Public product detail.

        Raises:
            PurchaseError: The store cannot return the product.
        """
        try:
            return await self.store.get_product(product_id)
        except StoreError as error:
            raise PurchaseError(str(error)) from None

    async def request_purchase(
        self, product_id: str, max_price: Decimal | int | str | None = None
    ) -> dict[str, Any]:
        """Create a merchant order, evaluate policy, and pay only when approved.

        Args:
            product_id: Store-owned product identifier; no amount or destination input.
            max_price: Optional user-requested maximum price in test USDC.

        Returns:
            dict[str, Any]: Public purchase decision, order, payment, and cashback fields.

        Raises:
            PurchaseError: Price parsing, store access, CDP, or chain verification fails
                before a public decision can be returned.
        """
        max_price_cents = parse_optional_price(max_price)
        wallet = self.database.get_wallet()
        if wallet is None:
            return {
                "status": "NO_WALLET",
                "message": "Create and fund the shopping wallet before requesting purchases.",
            }
        policy_row = self.database.get_policy()
        if policy_row is None:
            return {
                "decision": "REJECTED",
                "reason_code": "NO_POLICY",
                "message": "Set a spending policy before requesting purchases.",
            }

        unresolved = self.database.get_unresolved_product_purchase(product_id)
        if unresolved is not None:
            return self._decision_result(unresolved, Decision.APPROVED)

        product = await self._safe_product_detail(product_id)
        order = await self._create_order(product_id)
        now = datetime.now(UTC)
        decision = evaluate_purchase(
            PurchaseContext(
                policy=Policy(policy_row["weekly_limit_cents"], policy_row["max_auto_tx_cents"]),
                amount_cents=int(order["amount_cents"]),
                pay_to=str(order["pay_to"]),
                merchant_address=self.merchant_address,
                max_price_cents=max_price_cents,
                live_balance_cents=self._live_usdc_cents(wallet["address"]),
                spent_this_week_cents=self.database.get_spent_this_week(now),
                recent_product_purchases=self.database.get_recent_product_purchases(
                    product_id, now
                ),
                product_id=product_id,
                now=now,
            )
        )
        status = self._status_for_decision(decision.decision)
        purchase = self.database.insert_purchase(
            product_id=product_id,
            product_name=product.get("name"),
            amount_cents=int(order["amount_cents"]),
            status=status,
            reason_code=decision.reason.value,
            order_id=str(order["order_id"]),
            payment_intent_id=str(order["payment_intent_id"]),
        )
        if decision.decision == Decision.REJECTED:
            await self._cancel_intent(str(order["payment_intent_id"]))
            return self._decision_result(purchase, decision.decision)
        if decision.decision == Decision.HUMAN_APPROVAL_REQUIRED:
            result = self._decision_result(purchase, decision.decision)
            result["message"] = (
                "This purchase is above the automatic limit and was not paid. "
                "Approval is deferred in this demo."
            )
            return result
        return await self._pay_approved_purchase(purchase, order, wallet, decision.decision)

    def get_transactions(self, limit: int = 10) -> dict[str, list[dict[str, Any]]]:
        """Return recent purchase history with payment and cashback display fields.

        Args:
            limit: Maximum number of purchases to return, clamped to a small range.

        Returns:
            dict[str, list[dict[str, Any]]]: Newest-first transaction history.
        """
        bounded = min(max(int(limit), 0), 50)
        return {
            "transactions": [
                self._history_row(row) for row in self.database.get_purchase_history(bounded)
            ]
        }

    async def _safe_product_detail(self, product_id: str) -> dict[str, Any]:
        """Read product details without blocking order-owned price validation.

        Args:
            product_id: Store-owned product identifier.

        Returns:
            dict[str, Any]: Product detail, or a minimal fallback when unavailable.
        """
        try:
            return await self.store.get_product(product_id)
        except StoreError:
            return {"product_id": product_id, "name": None}

    async def _create_order(self, product_id: str) -> dict[str, Any]:
        """Create and validate a merchant payment intent for one product.

        Args:
            product_id: Store-owned product identifier.

        Returns:
            dict[str, Any]: Merchant order payload.

        Raises:
            PurchaseError: The store response is unavailable or malformed.
        """
        try:
            order = await self.store.create_order(product_id)
            for field in ("order_id", "payment_intent_id", "amount_cents", "pay_to"):
                if field not in order:
                    raise KeyError(field)
            if int(order["amount_cents"]) <= 0:
                raise ValueError("Invalid order amount")
            return order
        except (StoreError, KeyError, TypeError, ValueError):
            raise PurchaseError("ORDER_UNAVAILABLE") from None

    def _live_usdc_cents(self, address: str) -> int:
        """Read the shopper wallet's live USDC balance as whole cents.

        Args:
            address: Persisted shopper wallet address.

        Returns:
            int: Whole cents available for policy evaluation.

        Raises:
            PurchaseError: The balance cannot be read.
        """
        try:
            return get_usdc_balance(address, self.web3, self.usdc_contract_address) // 10_000
        except Exception:
            raise PurchaseError("BALANCE_UNAVAILABLE") from None

    def _status_for_decision(self, decision: Decision) -> str:
        """Map a pure policy decision to the purchase ledger status.

        Args:
            decision: Policy engine purchase decision.

        Returns:
            str: REJECTED, PENDING_APPROVAL, or SUBMITTED.
        """
        if decision == Decision.REJECTED:
            return "REJECTED"
        if decision == Decision.HUMAN_APPROVAL_REQUIRED:
            return "PENDING_APPROVAL"
        return "SUBMITTED"

    async def _cancel_intent(self, payment_intent_id: str) -> None:
        """Best-effort cancel an unpaid merchant payment intent.

        Args:
            payment_intent_id: Merchant payment intent identifier.

        Returns:
            None.
        """
        try:
            await self.store.cancel_payment_intent(payment_intent_id)
        except StoreError:
            return

    async def _pay_approved_purchase(
        self,
        purchase: dict[str, Any],
        order: dict[str, Any],
        wallet: dict[str, Any],
        decision: Decision,
    ) -> dict[str, Any]:
        """Submit once and retain payment evidence until its outcome is definitive.

        Args:
            purchase: Durable purchase row inserted before payment.
            order: Merchant order and payment-intent payload.
            wallet: Persisted shopper wallet row.
            decision: Approved policy decision.

        Returns:
            dict[str, Any]: Confirmed, reverted, or conservatively submitted purchase.
                Timeouts and merchant failures retain their hash and weekly reservation.
        """
        try:
            account = await self.cdp.evm.get_account(name=wallet["account_name"])
        except Exception:
            purchase = self.database.update_purchase(
                purchase["id"], status="FAILED", reason_code="PAYMENT_ACCOUNT_UNAVAILABLE"
            )
            return self._decision_result(purchase, decision)

        try:
            tx_hash = str(
                await account.transfer(
                    to=str(order["pay_to"]),
                    amount=cents_to_usdc_base_units(int(order["amount_cents"])),
                    token="usdc",
                    network=NETWORK,
                )
            )
            get_explorer_url(tx_hash)
        except Exception:
            purchase = self.database.update_purchase(
                purchase["id"], status="SUBMITTED", reason_code="PAYMENT_SUBMISSION_UNKNOWN"
            )
            return self._decision_result(purchase, decision)

        # Commit the hash before any receipt wait or merchant request can fail.
        purchase = self.database.update_purchase(
            purchase["id"], status="SUBMITTED", tx_hash=tx_hash
        )
        try:
            receipt = await self._wait_for_receipt(tx_hash)
            receipt_status = self._receipt_status(receipt)
        except Exception:
            receipt_status = None
        if receipt_status != 1:
            purchase = self.database.update_purchase(
                purchase["id"],
                status="FAILED" if receipt_status == 0 else "SUBMITTED",
                reason_code="PAYMENT_REVERTED"
                if receipt_status == 0
                else "PAYMENT_RECEIPT_PENDING",
            )
            return self._decision_result(purchase, decision)

        try:
            await self._confirm_with_store(str(order["payment_intent_id"]), tx_hash)
        except StoreError:
            purchase = self.database.update_purchase(
                purchase["id"], status="SUBMITTED", reason_code="MERCHANT_CONFIRMATION_PENDING"
            )
            return self._decision_result(purchase, decision)
        purchase = self.database.update_purchase(purchase["id"], status="CONFIRMED")
        return self._decision_result(purchase, decision)

    async def _wait_for_receipt(self, tx_hash: str, timeout: int = 60) -> Any:
        """Wait for the payment receipt using the configured Web3 reader.

        Args:
            tx_hash: Public payment transaction hash.
            timeout: Maximum receipt wait in seconds.

        Returns:
            Any: Web3 receipt object or receipt-like test double.
        """
        return await asyncio.to_thread(
            self.web3.eth.wait_for_transaction_receipt, tx_hash, timeout=timeout
        )

    def _receipt_status(self, receipt: Any) -> int:
        """Extract a transaction status from a receipt object or mapping.

        Args:
            receipt: Receipt object returned by Web3 or a test double.

        Returns:
            int: Numeric receipt status.

        Raises:
            PurchaseError: The receipt lacks a status field.
        """
        status = (
            receipt.get("status") if isinstance(receipt, dict) else getattr(receipt, "status", None)
        )
        if status is None:
            raise PurchaseError("PAYMENT_FAILED")
        return int(status)

    async def _confirm_with_store(self, payment_intent_id: str, tx_hash: str) -> dict[str, Any]:
        """Confirm a merchant payment with a small retry budget.

        Args:
            payment_intent_id: Merchant payment intent identifier.
            tx_hash: Public Base Sepolia payment transaction hash.

        Returns:
            dict[str, Any]: Paid merchant confirmation for the same intent and hash.

        Raises:
            StoreError: All confirmation attempts fail.
        """
        last_error: StoreError | None = None
        for _attempt in range(3):
            try:
                result = await self.store.confirm_payment(payment_intent_id, tx_hash)
                if (
                    not isinstance(result, dict)
                    or result.get("status") != "paid"
                    or result.get("payment_intent_id") != payment_intent_id
                    or str(result.get("tx_hash", "")).lower() != tx_hash.lower()
                ):
                    raise StoreError("PAYMENT_CONFIRMATION_FAILED")
                return result
            except StoreError as error:
                last_error = error
        raise last_error or StoreError("PAYMENT_CONFIRMATION_FAILED")

    def _decision_result(self, purchase: dict[str, Any], decision: Decision) -> dict[str, Any]:
        """Format one purchase row as a public request_purchase response.

        Args:
            purchase: Purchase row from SQLite.
            decision: Policy decision that created the row.

        Returns:
            dict[str, Any]: Public decision, order, payment, and cashback fields.
        """
        result = {
            "decision": decision.value,
            "reason_code": purchase["reason_code"],
            "status": purchase["status"],
            "message": self._message_for_purchase(purchase, decision),
            "purchase_id": purchase["id"],
            "order_id": purchase["order_id"],
            "payment_intent_id": purchase["payment_intent_id"],
            "product_id": purchase["product_id"],
            "product_name": purchase["product_name"],
            "amount": format_cents(purchase["amount_cents"]),
            "cashback_status": purchase["cashback_status"],
            "cashback": format_cents(purchase["cashback_cents"]),
        }
        if purchase["tx_hash"]:
            result["tx_hash"] = purchase["tx_hash"]
            result["explorer_url"] = get_explorer_url(purchase["tx_hash"])
        if purchase["cashback_tx_hash"]:
            result["cashback_tx_hash"] = purchase["cashback_tx_hash"]
            result["cashback_explorer_url"] = get_explorer_url(purchase["cashback_tx_hash"])
        return result

    def _message_for_purchase(self, purchase: dict[str, Any], decision: Decision) -> str:
        """Build a short public purchase message without hidden implementation detail.

        Args:
            purchase: Purchase row from SQLite.
            decision: Policy decision that created the row.

        Returns:
            str: Chat-ready message for the purchase response.
        """
        if purchase["status"] == "CONFIRMED":
            return "Purchase approved, paid, and verified by the merchant."
        if purchase["status"] == "SUBMITTED":
            if purchase["reason_code"] == "MERCHANT_CONFIRMATION_PENDING":
                return (
                    "Payment succeeded on-chain, but merchant confirmation is pending. "
                    "Do not retry this purchase."
                )
            return "The payment outcome is not yet verified. Do not retry this purchase."
        if purchase["status"] == "FAILED":
            if purchase["reason_code"] == "PAYMENT_ACCOUNT_UNAVAILABLE":
                return "Payment could not be prepared; no transfer was submitted."
            return "The payment transaction reverted; the merchant was not paid."
        if decision == Decision.REJECTED:
            return "Purchase was rejected by policy and the unpaid merchant intent was canceled."
        return "Purchase requires human approval and was not paid."

    def _history_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Format one purchase row for get_transactions.

        Args:
            row: Purchase history row from SQLite.

        Returns:
            dict[str, Any]: Public transaction-history item.
        """
        result = {
            "purchase_id": row["id"],
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "amount": format_cents(row["amount_cents"]),
            "status": row["status"],
            "reason_code": row["reason_code"],
            "order_id": row["order_id"],
            "payment_intent_id": row["payment_intent_id"],
            "cashback_status": row["cashback_status"],
            "cashback": format_cents(row["cashback_cents"]),
            "created_at": row["created_at"],
        }
        if row["tx_hash"]:
            result["tx_hash"] = row["tx_hash"]
            result["explorer_url"] = get_explorer_url(row["tx_hash"])
        if row["cashback_tx_hash"]:
            result["cashback_tx_hash"] = row["cashback_tx_hash"]
            result["cashback_explorer_url"] = get_explorer_url(row["cashback_tx_hash"])
        return result
