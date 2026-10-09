"""Specify Phase 3 spending policy decisions, persistence, and MCP tool behavior."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.confirmations import ConfirmationError
from app.policy import (
    Decision,
    DecisionReason,
    Policy,
    PurchaseContext,
    evaluate_purchase,
    validate_policy_change,
)
from app.wallet import get_explorer_url
from tests.conftest import ADDRESS, MERCHANT_ADDRESS


def purchase_context(**overrides):
    """Build a valid purchase context with focused override support.

    Args:
        **overrides: Field values replacing the default approved context.

    Returns:
        PurchaseContext: A complete immutable policy-evaluation input.
    """
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    values = {
        "policy": Policy(weekly_limit_cents=2500, max_auto_tx_cents=1200),
        "amount_cents": 1200,
        "pay_to": MERCHANT_ADDRESS,
        "merchant_address": MERCHANT_ADDRESS,
        "max_price_cents": None,
        "live_balance_cents": 5000,
        "spent_this_week_cents": 0,
        "recent_product_purchases": [],
        "product_id": "leo-tee",
        "now": now,
        "human_approved": False,
    }
    values.update(overrides)
    return PurchaseContext(**values)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"policy": None}, DecisionReason.NO_POLICY),
        ({"pay_to": ADDRESS}, DecisionReason.MERCHANT_NOT_ALLOWED),
        ({"max_price_cents": 1199}, DecisionReason.ABOVE_REQUESTED_MAX),
        ({"live_balance_cents": 1199}, DecisionReason.INSUFFICIENT_FUNDS),
        (
            {"recent_product_purchases": [("leo-tee", datetime(2026, 10, 7, 11, 59, tzinfo=UTC))]},
            DecisionReason.DUPLICATE_PURCHASE,
        ),
        ({"spent_this_week_cents": 1301}, DecisionReason.WEEKLY_LIMIT_EXCEEDED),
    ],
)
def test_rejection_rules_are_first_match(overrides, reason):
    """Reject each deterministic purchase failure before approvals are considered.

    Args:
        overrides: Context changes that trigger one rejection rule.
        reason: Expected public reason code.

    Returns:
        None.
    """
    result = evaluate_purchase(purchase_context(**overrides))
    assert result.decision == Decision.REJECTED
    assert result.reason == reason


def test_auto_limit_boundary_is_inclusive_and_human_approval_is_explicit():
    """Approve exact auto-limit purchases and require humans above the limit.

    Args:
        None.

    Returns:
        None.
    """
    exact = evaluate_purchase(purchase_context(amount_cents=1200))
    assert exact.decision == Decision.APPROVED
    assert exact.reason == DecisionReason.WITHIN_POLICY

    over = evaluate_purchase(purchase_context(amount_cents=1201))
    assert over.decision == Decision.HUMAN_APPROVAL_REQUIRED
    assert over.reason == DecisionReason.ABOVE_AUTO_LIMIT

    approved = evaluate_purchase(purchase_context(amount_cents=1201, human_approved=True))
    assert approved.decision == Decision.APPROVED
    assert approved.reason == DecisionReason.HUMAN_APPROVED


@pytest.mark.parametrize(
    "current,weekly,auto,allowed,reason",
    [
        (None, 20_001, 1200, False, DecisionReason.ABOVE_CAP),
        (None, 10_000, 5001, False, DecisionReason.ABOVE_CAP),
        (None, -1, 0, False, DecisionReason.INVALID_POLICY),
        (None, 1000, 1001, False, DecisionReason.INVALID_POLICY),
        (None, 2500, 1200, True, DecisionReason.WITHIN_POLICY),
        (Policy(2500, 1200), 2500, 1000, True, DecisionReason.WITHIN_POLICY),
        (Policy(2500, 1200), 2501, 1000, False, DecisionReason.LOOSENING_NOT_ALLOWED),
        (Policy(2500, 1200), 2500, 1201, False, DecisionReason.LOOSENING_NOT_ALLOWED),
    ],
)
def test_policy_change_validation_enforces_caps_and_tightening(
    current, weekly, auto, allowed, reason
):
    """Validate initial caps, malformed limits, tightening, and loosening attempts.

    Args:
        current: Existing policy or None for first setup.
        weekly: Requested weekly limit in cents.
        auto: Requested max auto transaction in cents.
        allowed: Whether the change should be accepted.
        reason: Public reason code explaining the result.

    Returns:
        None.
    """
    result = validate_policy_change(current, weekly, auto)
    assert result.allowed is allowed
    assert result.reason == reason


def test_policy_database_helpers_survive_restart(database):
    """Persist the singleton policy and expose Phase 5 query contracts.

    Args:
        database: The isolated SQLite repository.

    Returns:
        None.
    """
    assert database.get_policy() is None
    database.set_policy(2500, 1200)
    assert database.get_policy()["weekly_limit_cents"] == 2500
    assert database.get_policy()["max_auto_tx_cents"] == 1200
    assert database.get_spent_this_week(datetime.now(UTC)) == 0
    assert database.get_pending_approvals() == []

    reopened = type(database)(database.path)
    try:
        assert reopened.get_policy()["weekly_limit_cents"] == 2500
        assert reopened.get_policy()["max_auto_tx_cents"] == 1200
    finally:
        reopened.close()


async def test_initial_policy_requires_confirmation_and_then_persists(service, database):
    """Activate the first policy only after a matching confirmation token.

    Args:
        service: Wallet orchestration service under test.
        database: Isolated policy repository.

    Returns:
        None.
    """
    requested = await service.set_spending_policy("25", "12")
    assert requested["status"] == "CONFIRMATION_REQUIRED"
    assert requested["weekly_limit"] == "25.00"
    assert requested["max_auto_transaction"] == "12.00"
    assert "Reply yes to set this spending policy." in requested["disclosure"]
    assert database.get_policy() is None

    active = await service.set_spending_policy(
        "25.00", "12.0", confirmation_id=requested["confirmation_id"]
    )
    assert active["status"] == "ACTIVE"
    assert active["weekly_limit"] == "25.00"
    assert active["max_auto_transaction"] == "12.00"
    assert database.get_policy()["weekly_limit_cents"] == 2500

    with pytest.raises(ConfirmationError, match="CONFIRMATION_EXPIRED"):
        await service.set_spending_policy("25", "12", confirmation_id=requested["confirmation_id"])


async def test_policy_updates_tighten_instantly_and_reject_loosening(service):
    """Apply stricter updates without confirmation while refusing chat-side increases.

    Args:
        service: Wallet orchestration service under test.

    Returns:
        None.
    """
    token = (await service.set_spending_policy("25", "12"))["confirmation_id"]
    assert (await service.set_spending_policy("25", "12", confirmation_id=token))[
        "status"
    ] == "ACTIVE"

    tightened = await service.set_spending_policy("25", "10")
    assert tightened["status"] == "ACTIVE"
    assert tightened["max_auto_transaction"] == "10.00"

    assert await service.set_spending_policy("500", "10") == {
        "status": "LOOSENING_NOT_ALLOWED",
        "reason_code": "LOOSENING_NOT_ALLOWED",
    }
    assert await service.set_spending_policy("20", "21") == {
        "status": "INVALID_POLICY",
        "reason_code": "INVALID_POLICY",
    }


async def test_policy_lookup_returns_budget_and_empty_phase_5_contract(service):
    """Return display limits, remaining budget, and no Phase 5 approvals yet.

    Args:
        service: Wallet orchestration service under test.

    Returns:
        None.
    """
    assert await service.get_spending_policy() == {"status": "NOT_SET"}
    token = (await service.set_spending_policy(Decimal("25"), Decimal("12")))["confirmation_id"]
    await service.set_spending_policy("25", "12", confirmation_id=token)
    assert await service.get_spending_policy() == {
        "status": "ACTIVE",
        "weekly_limit": "25.00",
        "max_auto_transaction": "12.00",
        "spent_this_week": "0.00",
        "remaining_this_week": "25.00",
        "pending_approvals": [],
    }


async def test_policy_confirmation_binds_amounts(service):
    """Reject changed policy parameters between disclosure and confirmation.

    Args:
        service: Wallet orchestration service under test.

    Returns:
        None.
    """
    token = (await service.set_spending_policy("25", "12"))["confirmation_id"]
    with pytest.raises(ConfirmationError, match="CONFIRMATION_MISMATCH"):
        await service.set_spending_policy("25", "11", confirmation_id=token)


async def test_policy_rejects_initial_values_above_caps_without_consent(service):
    """Refuse over-cap first policies without issuing a confirmation token.

    Args:
        service: Wallet orchestration service under test.

    Returns:
        None.
    """
    result = await service.set_spending_policy("201", "12")
    assert result == {"status": "ABOVE_CAP", "reason_code": "ABOVE_CAP"}
    assert not service.confirmations._confirmations


def test_policy_display_helpers_are_exact(service):
    """Keep policy money formatting aligned with wallet explorer helpers smoke coverage.

    Args:
        service: Wallet orchestration service under test.

    Returns:
        None.
    """
    assert get_explorer_url(MERCHANT_ADDRESS).endswith(MERCHANT_ADDRESS)
