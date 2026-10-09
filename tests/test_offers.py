"""Specify Phase 5B offer eligibility, persistence, search ranking, and payouts."""

from datetime import UTC, datetime, timedelta

import pytest

from app.policy import eligible


def test_expiry_boundary_and_first_match() -> None:
    """Keep expiry inclusive and prioritize expiry over other failing rules.

    Args:
        None.

    Returns:
        None. Assertions verify timestamps and deterministic rule ordering.
    """
    now = datetime.now(UTC)
    assert eligible(offer(expires_at=now), False, 1200, now) == (True, 180, "15% cashback")
    assert (
        eligible(
            offer(expires_at=now - timedelta(seconds=1), new_customer_only=1, budget_cents=0),
            True,
            1200,
            now,
        )[2]
        == "Offer expired"
    )
    assert eligible(offer(new_customer_only=1, budget_cents=0), True, 1200, now)[2] == (
        "Offer for new customers only"
    )


def test_offer_upsert_constraints_and_history(database) -> None:
    """Persist one offer per product and count only other confirmed purchases.

    Args:
        database: Isolated SQLite repository.

    Returns:
        None. Assertions verify defaults, reconfiguration, and purchase exclusion.
    """
    expiry = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    first = database.upsert_offer("tee", 1500, False, 500, expiry)
    database.add_offer_spend(first["id"], 180)
    second = database.upsert_offer("tee", 1000, True, 600, expiry)
    assert second["id"] == first["id"]
    assert second["spent_cents"] == 0
    assert database.get_offer("missing") is None
    assert database.get_offer("tee")["new_customer_only"] == 1
    assert not database.has_prior_purchase()
    row = database.insert_purchase(
        product_id="tee",
        product_name="Tee",
        amount_cents=1200,
        status="SUBMITTED",
        reason_code="WITHIN_POLICY",
        order_id="o",
        payment_intent_id="i",
    )
    assert not database.has_prior_purchase()
    database.update_purchase(row["id"], status="CONFIRMED")
    assert database.has_prior_purchase()
    assert not database.has_prior_purchase(exclude_purchase_id=row["id"])
    with pytest.raises((ValueError, __import__("sqlite3").IntegrityError)):
        database.upsert_offer("bad", 5001, False, 500, expiry)
    with pytest.raises(ValueError):
        database.add_offer_spend(first["id"], 601)


def offer(
    *,
    cashback_bps: int = 1500,
    new_customer_only: int = 0,
    budget_cents: int = 500,
    spent_cents: int = 0,
    expires_at: datetime | None = None,
) -> dict[str, object]:
    """Build an offer row compatible with the pure eligibility function.

    Params:
        cashback_bps: Cashback basis points configured by the merchant.
        new_customer_only: Whether prior confirmed purchases suppress eligibility.
        budget_cents: Total offer budget in cents.
        spent_cents: Already consumed offer budget in cents.
        expires_at: Optional expiry timestamp; defaults to tomorrow.

    Returns:
        Offer dictionary matching the database helper shape.
    """
    expiry = expires_at or datetime.now(UTC) + timedelta(days=1)
    return {
        "id": "offer-1",
        "product_id": "0591439009",
        "cashback_bps": cashback_bps,
        "new_customer_only": new_customer_only,
        "budget_cents": budget_cents,
        "spent_cents": spent_cents,
        "expires_at": expiry.isoformat(),
    }


def test_eligible_returns_no_cashback_without_offer() -> None:
    """Return no cashback and no note when the product has no offer.

    Params:
        None.

    Returns:
        None. Assertions define the no-offer default.
    """
    ok, cashback_cents, note = eligible(
        None, has_prior_purchase=False, amount_cents=1200, now=datetime.now(UTC)
    )

    assert (ok, cashback_cents, note) == (False, 0, None)


def test_eligible_rejects_expired_offers() -> None:
    """Explain that an expired offer cannot pay cashback.

    Params:
        None.

    Returns:
        None. Assertions define the expiry rule.
    """
    expired = offer(expires_at=datetime.now(UTC) - timedelta(seconds=1))

    assert eligible(expired, False, 1200, datetime.now(UTC)) == (False, 0, "Offer expired")


def test_default_offer_pays_new_and_returning_customers() -> None:
    """Keep default item offers available to customers with prior purchases.

    Params:
        None.

    Returns:
        None. Assertions define the all-customer default.
    """
    active = offer()

    assert eligible(active, False, 1200, datetime.now(UTC)) == (True, 180, "15% cashback")
    assert eligible(active, True, 1200, datetime.now(UTC)) == (True, 180, "15% cashback")


def test_new_customer_only_offer_rejects_prior_purchasers() -> None:
    """Apply the optional new-customer-only merchant restriction.

    Params:
        None.

    Returns:
        None. Assertions define explicit opt-in behavior.
    """
    restricted = offer(new_customer_only=1)

    assert eligible(restricted, False, 1200, datetime.now(UTC)) == (True, 180, "15% cashback")
    assert eligible(restricted, True, 1200, datetime.now(UTC)) == (
        False,
        0,
        "Offer for new customers only",
    )


def test_remaining_budget_boundary_is_inclusive() -> None:
    """Permit cashback when remaining budget exactly equals the payout.

    Params:
        None.

    Returns:
        None. Assertions define exact-budget and short-budget behavior.
    """
    exact = offer(budget_cents=500, spent_cents=320)
    short = offer(budget_cents=500, spent_cents=321)

    assert eligible(exact, False, 1200, datetime.now(UTC)) == (True, 180, "15% cashback")
    assert eligible(short, False, 1200, datetime.now(UTC)) == (
        False,
        0,
        "Offer budget used up",
    )


def test_cashback_rounds_down_to_whole_cents() -> None:
    """Round fractional cent cashback down without overpaying.

    Params:
        None.

    Returns:
        None. Assertions define basis-point integer math.
    """
    assert eligible(offer(), False, 1099, datetime.now(UTC)) == (True, 164, "15% cashback")
