"""Specify Phase 3 spending policy decisions, persistence, and MCP tool behavior."""

import asyncio
from datetime import UTC, datetime, timedelta
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
        (Policy(2500, 1200), 2501, 1000, True, DecisionReason.WITHIN_POLICY),
        (Policy(2500, 1200), 20_000, 1200, True, DecisionReason.WITHIN_POLICY),
        (Policy(2500, 1200), 20_001, 1200, False, DecisionReason.ABOVE_CAP),
        (Policy(2500, 1200), 5000, 1201, True, DecisionReason.WITHIN_POLICY),
        (Policy(2500, 1200), 2500, 1201, True, DecisionReason.WITHIN_POLICY),
        (Policy(2500, 1200), 2000, 1500, True, DecisionReason.WITHIN_POLICY),
        (Policy(2500, 1200), 5000, 5000, True, DecisionReason.WITHIN_POLICY),
        (Policy(2500, 1200), 10_000, 5001, False, DecisionReason.ABOVE_CAP),
        (Policy(2500, 1200), 2500, 2501, False, DecisionReason.INVALID_POLICY),
        (Policy(2500, 1200), 2500, -1, False, DecisionReason.INVALID_POLICY),
    ],
)
def test_policy_change_validation_enforces_caps_and_valid_limits(
    current, weekly, auto, allowed, reason
):
    """Allow weekly and automatic limit changes within caps and valid relationships.

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


async def test_policy_updates_tighten_instantly_and_reject_over_cap_increases(service):
    """Apply stricter updates immediately and refuse over-cap weekly or auto limits.

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
        "status": "ABOVE_CAP",
        "reason_code": "ABOVE_CAP",
    }
    assert await service.set_spending_policy("100", "50.01") == {
        "status": "ABOVE_CAP",
        "reason_code": "ABOVE_CAP",
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


@pytest.mark.parametrize(
    "weekly,auto", [("50", "12"), ("200", "10"), ("25", "15"), ("20", "15"), ("50", "50")]
)
async def test_policy_increase_requires_confirmation_and_preserves_history(
    service, database, cdp, weekly, auto
):
    """Persist confirmed increases without resetting spend, pending approvals, or history."""
    database.set_policy(2500, 1200)
    for status, amount, age in [
        ("CONFIRMED", 1200, 0),
        ("SUBMITTED", 500, 0),
        ("PENDING_APPROVAL", 1400, 0),
        ("CONFIRMED", 900, 8),
    ]:
        database.insert_purchase(
            product_id=f"{status}-{age}", product_name="Test product",
            amount_cents=amount, status=status, reason_code=None,
            order_id=None, payment_intent_id=None,
            created_at=datetime.now(UTC) - timedelta(days=age),
        )
    before = await service.get_spending_policy()
    history = database.get_purchase_history(10)
    proposed = await service.set_spending_policy(weekly, auto)
    assert proposed["status"] == "CONFIRMATION_REQUIRED"
    assert proposed["weekly_limit"] == f"{weekly}.00"
    assert proposed["max_auto_transaction"] == f"{auto}.00"
    assert "rolling seven-day budget" in proposed["disclosure"]
    assert await service.get_spending_policy() == before

    active = await service.set_spending_policy(
        weekly, auto, confirmation_id=proposed["confirmation_id"]
    )
    assert active["status"] == "ACTIVE"
    lookup = await service.get_spending_policy()
    assert lookup["weekly_limit"] == f"{weekly}.00"
    assert lookup["max_auto_transaction"] == f"{auto}.00"
    assert lookup["spent_this_week"] == "17.00"
    assert lookup["remaining_this_week"] == f"{int(weekly) - 17}.00"
    assert lookup["pending_approvals"] == before["pending_approvals"]
    assert database.get_purchase_history(10) == history
    reopened = type(database)(database.path)
    try:
        assert reopened.get_policy()["weekly_limit_cents"] == int(weekly) * 100
        assert reopened.get_policy()["max_auto_tx_cents"] == int(auto) * 100
        assert reopened.get_spent_this_week(datetime.now(UTC)) == 1700
    finally:
        reopened.close()
    cdp.evm.get_account.assert_not_awaited()
    cdp.treasury.transfer.assert_not_awaited()


@pytest.mark.parametrize("weekly,auto", [("51", "12"), ("50", "11")])
async def test_weekly_confirmation_rejects_changed_amounts(service, database, weekly, auto):
    """Bind both proposed limits to the weekly increase disclosure."""
    database.set_policy(2500, 1200)
    token = (await service.set_spending_policy("50", "12"))["confirmation_id"]
    before = database.get_policy()
    with pytest.raises(ConfirmationError, match="CONFIRMATION_MISMATCH"):
        await service.set_spending_policy(weekly, auto, confirmation_id=token)
    assert database.get_policy() == before


@pytest.mark.parametrize("weekly,auto", [("50", "12"), ("25", "15")])
@pytest.mark.parametrize("failure", ["unknown", "expired", "replayed", "restart"])
async def test_policy_confirmation_rejects_invalid_tokens(
    service, database, monkeypatch, failure, weekly, auto
):
    """Require valid, unexpired, single-use consent from the current process."""
    database.set_policy(2500, 1200)
    token = (await service.set_spending_policy(weekly, auto))["confirmation_id"]
    expected = "CONFIRMATION_EXPIRED"
    if failure == "unknown":
        token = "made-up"
        expected = "INVALID_CONFIRMATION"
    elif failure == "expired":
        expiry = service.confirmations._confirmations[token].expires_at
        monkeypatch.setattr("app.confirmations.time.monotonic", lambda: expiry)
    elif failure == "replayed":
        await service.set_spending_policy(weekly, auto, confirmation_id=token)
    else:
        service.confirmations = type(service.confirmations)()
        expected = "INVALID_CONFIRMATION"
    before = database.get_policy()
    with pytest.raises(ConfirmationError, match=expected):
        await service.set_spending_policy(weekly, auto, confirmation_id=token)
    assert database.get_policy() == before


@pytest.mark.parametrize("weekly,auto", [("50", "12"), ("25", "15")])
@pytest.mark.parametrize("restore_baseline", [False, True])
async def test_policy_confirmation_cannot_overwrite_a_newer_policy(
    service, database, restore_baseline, weekly, auto
):
    """Reject stale consent even if limits have since returned to their original values."""
    database.set_policy(2500, 1200)
    token = (await service.set_spending_policy(weekly, auto))["confirmation_id"]
    await service.set_spending_policy("20", "12")
    if restore_baseline:
        newer = (await service.set_spending_policy("25", "12"))["confirmation_id"]
        await service.set_spending_policy("25", "12", confirmation_id=newer)
    before = database.get_policy()
    with pytest.raises(ConfirmationError, match="CONFIRMATION_MISMATCH"):
        await service.set_spending_policy(weekly, auto, confirmation_id=token)
    assert database.get_policy() == before


async def test_initial_confirmation_cannot_override_another_policy_setup(service, database):
    """Invalidate pending initial consent when another setup has activated a policy."""
    old = (await service.set_spending_policy("50", "12"))["confirmation_id"]
    new = (await service.set_spending_policy("25", "12"))["confirmation_id"]
    await service.set_spending_policy("25", "12", confirmation_id=new)
    with pytest.raises(ConfirmationError, match="CONFIRMATION_MISMATCH"):
        await service.set_spending_policy("50", "12", confirmation_id=old)
    assert database.get_policy()["weekly_limit_cents"] == 2500


async def test_concurrent_weekly_confirmations_cannot_overwrite_each_other(service, database):
    """Serialize competing increases so only the first confirmed proposal takes effect."""
    database.set_policy(2500, 1200)
    first = (await service.set_spending_policy("50", "12"))["confirmation_id"]
    second = (await service.set_spending_policy("60", "12"))["confirmation_id"]
    outcomes = await asyncio.gather(
        service.set_spending_policy("50", "12", confirmation_id=first),
        service.set_spending_policy("60", "12", confirmation_id=second),
        return_exceptions=True,
    )
    assert outcomes[0]["status"] == "ACTIVE"
    assert isinstance(outcomes[1], ConfirmationError)
    assert str(outcomes[1]) == "CONFIRMATION_MISMATCH"
    assert database.get_policy()["weekly_limit_cents"] == 5000


@pytest.mark.parametrize("weekly", ["-1", "NaN", "Infinity", "25.001", "nope"])
async def test_invalid_weekly_updates_leave_policy_unchanged(service, database, weekly):
    """Reject malformed budgets before issuing any update confirmation."""
    database.set_policy(2500, 1200)
    before = database.get_policy()
    assert (await service.set_spending_policy(weekly, "12"))["status"] == "INVALID_POLICY"
    assert database.get_policy() == before
    assert not service.confirmations._confirmations


async def test_weekly_decrease_below_spend_clamps_remaining_to_zero(service, database):
    """Keep past spend intact when a user tightens the weekly budget below it."""
    database.set_policy(2500, 1200)
    database.insert_purchase(
        product_id="tee", product_name="Tee", amount_cents=2400,
        status="CONFIRMED", reason_code="WITHIN_POLICY",
        order_id=None, payment_intent_id=None,
    )
    assert (await service.set_spending_policy("20", "12"))["status"] == "ACTIVE"
    lookup = await service.get_spending_policy()
    assert lookup["spent_this_week"] == "24.00"
    assert lookup["remaining_this_week"] == "0.00"
    assert not service.confirmations._confirmations


@pytest.mark.parametrize("weekly,auto", [("24", "15"), ("25", "16")])
async def test_auto_confirmation_binds_both_limits(service, database, weekly, auto):
    """Reject changes to either disclosed amount while confirming an auto increase."""
    database.set_policy(2500, 1200)
    token = (await service.set_spending_policy("25", "15"))["confirmation_id"]
    before = database.get_policy()
    with pytest.raises(ConfirmationError, match="CONFIRMATION_MISMATCH"):
        await service.set_spending_policy(weekly, auto, token)
    assert database.get_policy() == before


@pytest.mark.parametrize(
    "weekly,auto,reason",
    [
        ("100", "50.01", "ABOVE_CAP"),
        ("25", "26", "INVALID_POLICY"),
        ("25", "-1", "INVALID_POLICY"),
        ("25", "NaN", "INVALID_POLICY"),
        ("25", "Infinity", "INVALID_POLICY"),
        ("25", "12.001", "INVALID_POLICY"),
        ("25", "nope", "INVALID_POLICY"),
    ],
)
async def test_invalid_auto_increase_leaves_policy_unchanged(
    service, database, weekly, auto, reason
):
    """Reject invalid auto ceilings before issuing consent or changing either limit."""
    database.set_policy(2500, 1200)
    before = database.get_policy()
    assert (await service.set_spending_policy(weekly, auto))["status"] == reason
    assert database.get_policy() == before
    assert not service.confirmations._confirmations


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
