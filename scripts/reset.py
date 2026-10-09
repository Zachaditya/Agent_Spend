"""Reset local Agent Spend demo state before a fresh Phase 5 run."""

import argparse
import json
from pathlib import Path
from typing import Any

from app.config import load_settings
from app.db import Database


def _delete_table(database: Database, table_name: str) -> int:
    """Delete every row from a table when it exists.

    Args:
        database: Open Agent Spend SQLite repository.
        table_name: Table to clear.

    Returns:
        int: Number of rows deleted, or zero when the table is absent.
    """
    if not database._table_exists(table_name):
        return 0
    cursor = database.connection.execute(f"DELETE FROM {table_name}")
    return int(cursor.rowcount if cursor.rowcount is not None else 0)


def reset_database(path: Path) -> dict[str, Any]:
    """Wipe wallet, policy, funding, and purchase state from the local database.

    Args:
        path: SQLite database path to reset.

    Returns:
        dict[str, Any]: Row counts deleted from each local demo-state table.
    """
    database = Database(path)
    try:
        with database.connection:
            funding_attempts = _delete_table(database, "funding_attempts")
            funding_events = _delete_table(database, "funding_events")
            purchases = _delete_table(database, "purchases")
            offers = _delete_table(database, "offers")
            policy = _delete_table(database, "policy")
            wallet_creation = _delete_table(database, "wallet_creation")
            wallet = _delete_table(database, "wallet")
        return {
            "wallet_rows_deleted": wallet,
            "wallet_creation_rows_deleted": wallet_creation,
            "policy_rows_deleted": policy,
            "purchase_rows_deleted": purchases,
            "offer_rows_deleted": offers,
            "funding_event_rows_deleted": funding_events,
            "funding_attempt_rows_deleted": funding_attempts,
            "manual_follow_up": (
                "Escrow balance is retained. Sweep shopper test USDC, including cashback, "
                "and any merchant test USDC back to the treasury "
                "manually, then restart the service and recreate/fund the shopper wallet."
            ),
        }
    finally:
        database.close()


def main() -> None:
    """Parse reset options and print public reset evidence as JSON.

    Args:
        None: Reads command-line arguments.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        type=Path,
        help="Optional SQLite path; defaults to DB_PATH from Agent Spend settings.",
    )
    args = parser.parse_args()
    settings = load_settings()
    path = args.db_path or settings.db_path
    print(json.dumps(reset_database(path), indent=2))


if __name__ == "__main__":
    main()
