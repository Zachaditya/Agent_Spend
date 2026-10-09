"""Specify Phase 5B offer eligibility, persistence, search ranking, and payouts."""

from datetime import UTC, datetime, timedelta

from app.policy import eligible


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
    ok, cashback_cents, note = eligible(None, has_prior_purchase=False, amount_cents=1200, now=datetime.now(UTC))

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

