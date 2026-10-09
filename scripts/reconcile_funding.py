"""Recheck funding receipts and explicitly recover only known missing transfer legs."""

import argparse
import asyncio
import json
from contextlib import closing

from app.config import load_settings
from app.db import Database
from app.main import build_cdp_client, check_chain
from app.wallet import WalletService


async def reconcile(operation_id: str, recover_missing: bool = False) -> dict:
    """Own testnet resources while reconciling one existing funding operation.

    Args:
        operation_id: Public funding UUID returned in the audit/result fields.
        recover_missing: Explicit developer instruction to send known missing or reverted legs.

    Returns:
        dict: Public confirmed result or recovery guidance without provider diagnostics.

    Raises:
        RuntimeError: FUNDING_RECONCILIATION_UNAVAILABLE for configuration, chain,
            provider, persistence, or cleanup failure; underlying details are suppressed.
    """
    try:
        settings = load_settings()
        await check_chain(settings)
        with closing(Database(settings.db_path)) as database:
            async with build_cdp_client(settings) as cdp:
                service = WalletService(database, cdp, None, settings=settings)
                return await service.reconcile_funding(
                    operation_id, recover_missing=recover_missing
                )
    except Exception:
        raise RuntimeError("FUNDING_RECONCILIATION_UNAVAILABLE") from None


def main() -> None:
    """Parse explicit recovery flags and print public-only reconciliation evidence.

    Args:
        None: Reads command-line arguments.

    Returns:
        None.

    Raises:
        SystemExit: The operation cannot be inspected or arguments are invalid.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation_id", help="Existing funding operation UUID.")
    parser.add_argument(
        "--recover-missing",
        action="store_true",
        help="Send only known unsent or reverted legs. Stop the service first.",
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(reconcile(args.operation_id, args.recover_missing))
    except RuntimeError:
        parser.exit(1, "FUNDING_RECONCILIATION_UNAVAILABLE\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
