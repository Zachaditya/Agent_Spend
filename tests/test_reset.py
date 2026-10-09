"""Verify the developer-only reset script clears local Agent Spend state."""

from app.db import Database
from scripts.reset import reset_database
from tests.conftest import ADDRESS


def test_reset_database_wipes_demo_state_without_exposing_a_tool(database: Database) -> None:
    """Clear wallet, policy, funding, and purchase rows for a fresh demo run.

    Args:
        database: Isolated SQLite repository.

    Returns:
        None. Assertions prove every local Phase 5 state table is empty afterward.
    """
    database.create_wallet_pointer("shopper-1", ADDRESS)
    database.set_policy(2500, 1500)
    database.insert_purchase(
        product_id="0591439009",
        product_name="ROOT CLASSIC TEE",
        amount_cents=1200,
        status="CONFIRMED",
        reason_code="WITHIN_POLICY",
        order_id="order-1",
        payment_intent_id="intent-1",
    )
    database.insert_funding_event(ADDRESS, 2500, "0", status="CONFIRMED")

    result = reset_database(database.path)

    assert result["wallet_rows_deleted"] == 1
    assert result["policy_rows_deleted"] == 1
    assert result["purchase_rows_deleted"] == 1
    assert result["funding_event_rows_deleted"] == 1
    reopened = Database(database.path)
    try:
        assert reopened.get_wallet() is None
        assert reopened.get_policy() is None
        assert reopened.get_purchase_history() == []
        assert reopened.get_latest_funding_event() is None
    finally:
        reopened.close()
