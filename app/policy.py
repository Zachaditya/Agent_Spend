"""Evaluate Agent Spend purchase requests and spending-policy changes deterministically."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

MAX_WEEKLY_LIMIT_CENTS = 20_000
MAX_AUTO_TX_CENTS = 5_000
DUPLICATE_PURCHASE_WINDOW = timedelta(minutes=2)


class Decision(StrEnum):
    """Enumerate the public outcomes returned by the pure purchase policy engine."""

    APPROVED = "APPROVED"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
    REJECTED = "REJECTED"


class DecisionReason(StrEnum):
    """Enumerate stable public reason codes for purchases and policy updates."""

    ABOVE_AUTO_LIMIT = "ABOVE_AUTO_LIMIT"
    ABOVE_CAP = "ABOVE_CAP"
    ABOVE_REQUESTED_MAX = "ABOVE_REQUESTED_MAX"
    DUPLICATE_PURCHASE = "DUPLICATE_PURCHASE"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    INVALID_POLICY = "INVALID_POLICY"
    LOOSENING_NOT_ALLOWED = "LOOSENING_NOT_ALLOWED"
    MERCHANT_NOT_ALLOWED = "MERCHANT_NOT_ALLOWED"
    NO_POLICY = "NO_POLICY"
    WEEKLY_LIMIT_EXCEEDED = "WEEKLY_LIMIT_EXCEEDED"
    WITHIN_POLICY = "WITHIN_POLICY"


@dataclass(frozen=True)
class Policy:
    """Represent the active cents-denominated spending policy."""

    weekly_limit_cents: int
    max_auto_tx_cents: int


@dataclass(frozen=True)
class PurchaseContext:
    """Collect all externally-owned facts needed for one purchase decision."""

    policy: Policy | None
    amount_cents: int
    pay_to: str
    merchant_address: str
    max_price_cents: int | None
    live_balance_cents: int
    spent_this_week_cents: int
    recent_product_purchases: list[tuple[str, datetime]]
    product_id: str
    now: datetime
    human_approved: bool = False


@dataclass(frozen=True)
class PurchaseDecision:
    """Return a deterministic purchase outcome and public reason code."""

    decision: Decision
    reason: DecisionReason


@dataclass(frozen=True)
class PolicyChangeDecision:
    """Return whether a requested policy update is permitted and why."""

    allowed: bool
    reason: DecisionReason


def evaluate_purchase(context: PurchaseContext) -> PurchaseDecision:
    """Apply the fixed Phase 3 first-match purchase rules.

    Args:
        context: The complete purchase facts supplied by wallet, store, and history code.

    Returns:
        PurchaseDecision: APPROVED, HUMAN_APPROVAL_REQUIRED, or REJECTED with reason.
    """
    if context.policy is None:
        return PurchaseDecision(Decision.REJECTED, DecisionReason.NO_POLICY)
    if context.pay_to.lower() != context.merchant_address.lower():
        return PurchaseDecision(Decision.REJECTED, DecisionReason.MERCHANT_NOT_ALLOWED)
    if context.max_price_cents is not None and context.amount_cents > context.max_price_cents:
        return PurchaseDecision(Decision.REJECTED, DecisionReason.ABOVE_REQUESTED_MAX)
    if context.amount_cents > context.live_balance_cents:
        return PurchaseDecision(Decision.REJECTED, DecisionReason.INSUFFICIENT_FUNDS)
    if _is_duplicate_product_request(
        context.product_id, context.now, context.recent_product_purchases
    ):
        return PurchaseDecision(Decision.REJECTED, DecisionReason.DUPLICATE_PURCHASE)
    if context.spent_this_week_cents + context.amount_cents > context.policy.weekly_limit_cents:
        return PurchaseDecision(Decision.REJECTED, DecisionReason.WEEKLY_LIMIT_EXCEEDED)
    if context.amount_cents > context.policy.max_auto_tx_cents and not context.human_approved:
        return PurchaseDecision(Decision.HUMAN_APPROVAL_REQUIRED, DecisionReason.ABOVE_AUTO_LIMIT)
    if context.human_approved:
        return PurchaseDecision(Decision.APPROVED, DecisionReason.HUMAN_APPROVED)
    return PurchaseDecision(Decision.APPROVED, DecisionReason.WITHIN_POLICY)


def validate_policy_change(
    current: Policy | None, weekly_limit_cents: int, max_auto_tx_cents: int
) -> PolicyChangeDecision:
    """Validate initial policy creation, stricter updates, caps, and malformed limits.

    Args:
        current: Existing active policy, or None when creating the first one.
        weekly_limit_cents: Requested weekly limit in whole cents.
        max_auto_tx_cents: Requested automatic-transaction limit in whole cents.

    Returns:
        PolicyChangeDecision: Whether the change is allowed and its public reason code.
    """
    if weekly_limit_cents < 0 or max_auto_tx_cents < 0 or max_auto_tx_cents > weekly_limit_cents:
        return PolicyChangeDecision(False, DecisionReason.INVALID_POLICY)
    if current is not None and (
        weekly_limit_cents > current.weekly_limit_cents
        or max_auto_tx_cents > current.max_auto_tx_cents
    ):
        return PolicyChangeDecision(False, DecisionReason.LOOSENING_NOT_ALLOWED)
    if weekly_limit_cents > MAX_WEEKLY_LIMIT_CENTS or max_auto_tx_cents > MAX_AUTO_TX_CENTS:
        return PolicyChangeDecision(False, DecisionReason.ABOVE_CAP)
    return PolicyChangeDecision(True, DecisionReason.WITHIN_POLICY)


def _is_duplicate_product_request(
    product_id: str, now: datetime, recent_purchases: list[tuple[str, datetime]]
) -> bool:
    """Check whether the same product was requested in the two-minute rejection window.

    Args:
        product_id: Store-owned product identifier under evaluation.
        now: Current policy-evaluation time.
        recent_purchases: Prior product identifiers and request timestamps.

    Returns:
        bool: True when a matching product timestamp falls within the recent window.
    """
    for prior_product_id, requested_at in recent_purchases:
        age = now - requested_at
        if prior_product_id == product_id and timedelta(0) <= age <= DUPLICATE_PURCHASE_WINDOW:
            return True
    return False
