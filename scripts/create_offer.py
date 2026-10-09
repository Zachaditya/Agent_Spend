"""Fund Base Sepolia offer escrow and publish a product-specific merchant cashback offer."""

import argparse
import asyncio
import fcntl
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# Support the documented `python scripts/create_offer.py` entry point.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, load_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.main import build_cdp_client  # noqa: E402
from app.store import StoreClient  # noqa: E402
from app.wallet import (  # noqa: E402
    NETWORK,
    build_web3,
    cents_to_usdc_base_units,
    eth_to_wei,
    format_cents,
    get_eth_balance,
    get_explorer_url,
    get_usdc_balance,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Validate merchant input before credentials, accounts, or money are accessed.

    Args:
        argv: Optional CLI argument list; None reads the current process arguments.

    Returns:
        argparse.Namespace: Product, integer rate/budget, duration, and explicit opt-in.

    Raises:
        SystemExit: Arguments are invalid or help was requested.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", required=True)
    parser.add_argument("--cashback-pct", required=True)
    parser.add_argument("--budget", required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--new-customers-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        rate = Decimal(args.cashback_pct) * 100
        budget = Decimal(args.budget) * 100
        valid = (
            rate.is_finite()
            and budget.is_finite()
            and rate == rate.to_integral_value()
            and 1 <= rate <= 5000
            and budget == budget.to_integral_value()
            and 0 <= budget <= 100_000_000_000
            and 1 <= args.days <= 3650
            and bool(args.product.strip())
        )
    except InvalidOperation:
        valid = False
    if not valid:
        parser.error("Use 0.01–50% cashback, a nonnegative cent-exact budget, and 1–3650 days.")
    args.cashback_bps, args.budget_cents = int(rate), int(budget)
    return args


def _save_journal(path: Path, state: dict[str, Any]) -> None:
    """Atomically save public funding state before and after remote submission.

    Args:
        path: Local sidecar path associated with this SQLite database.
        state: Public account, amount, token, and transaction-hash evidence.

    Returns:
        None. Data is flushed before the atomic replacement.
    """
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as output:
        json.dump(state, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


async def _finish_leg(state: dict[str, Any], path: Path, web3: Any) -> None:
    """Resolve a submitted funding leg before allowing another transfer.

    Args:
        state: Durable funding journal, optionally containing a pending leg.
        path: Journal path to update after a definitive receipt.
        web3: Configured Base Sepolia chain reader.

    Returns:
        None. An unresolved outcome stays journaled and blocks resubmission.

    Raises:
        RuntimeError: Submission is uncertain, the receipt is unavailable, or it reverted.
    """
    pending = state.get("pending")
    if not pending:
        return
    if not pending.get("tx_hash"):
        raise RuntimeError("Funding submission unresolved; inspect treasury before any retry")
    try:
        receipt = await asyncio.to_thread(
            web3.eth.wait_for_transaction_receipt, pending["tx_hash"], timeout=60
        )
        status = receipt.get("status") if isinstance(receipt, dict) else receipt.status
    except Exception:
        raise RuntimeError("Funding receipt unresolved; rerun to check the saved hash") from None
    if status not in {0, 1}:
        raise RuntimeError("Funding receipt unresolved; rerun to check the saved hash")
    state.setdefault("completed", []).append({**pending, "status": status})
    del state["pending"]
    _save_journal(path, state)
    if status == 0:
        raise RuntimeError("Funding transfer reverted; offer was not activated")


async def create_offer(
    args: argparse.Namespace,
    settings: Settings,
    database: Database,
    cdp: Any,
    web3: Any,
    store: StoreClient,
    journal_path: Path,
) -> dict[str, Any]:
    """Top up escrow shortfalls and activate an offer only after successful receipts.

    Args:
        args: Validated CLI arguments returned by parse_args.
        settings: Server-owned testnet, treasury, token, and escrow configuration.
        database: Initialized SQLite offer repository.
        cdp: Authenticated provider client; no credentials enter the returned data.
        web3: Base Sepolia balance and receipt reader.
        store: Merchant client resolving the product's stage-ready name.
        journal_path: Public funding journal and sibling lock location.

    Returns:
        dict[str, Any]: Persisted offer plus product name and escrow address/link.

    Raises:
        RuntimeError: Wrong chain, concurrent script, unresolved funding, or changed offer.
    """
    if await asyncio.to_thread(lambda: web3.eth.chain_id) != 84532:
        raise RuntimeError("Offers require Base Sepolia (84532)")
    with journal_path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another merchant offer script is running") from None
        product = await store.get_product(args.product)
        before = database.get_offer(args.product)
        if database.connection.execute(
            "SELECT 1 FROM purchases WHERE product_id=? AND cashback_status='FAILED' "
            "AND cashback_cents>0",
            (args.product,),
        ).fetchone():
            raise RuntimeError(
                "Offer has unresolved cashback; inspect receipts before replacing it"
            )
        escrow = await cdp.evm.get_or_create_account(name=settings.escrow_account_name)
        if escrow.address.lower() == settings.merchant_address.lower():
            raise RuntimeError("Escrow must be separate from the merchant")
        state = json.loads(journal_path.read_text()) if journal_path.exists() else {}
        if state.get("escrow_address", escrow.address).lower() != escrow.address.lower():
            raise RuntimeError("Funding journal belongs to a different escrow")
        state["escrow_address"] = escrow.address
        await _finish_leg(state, journal_path, web3)
        targets = {
            "usdc": cents_to_usdc_base_units(args.budget_cents),
            "eth": eth_to_wei(settings.escrow_eth),
        }
        treasury = None
        for token, target in targets.items():
            balance = (
                get_usdc_balance(escrow.address, web3, settings.usdc_contract_address)
                if token == "usdc"
                else get_eth_balance(escrow.address, web3)
            )
            shortfall = max(target - balance, 0)
            if not shortfall:
                continue
            if treasury is None:
                treasury = await cdp.evm.get_account(name=settings.treasury_account_name)
                if treasury.address.lower() == escrow.address.lower():
                    raise RuntimeError("Treasury and escrow must be separate accounts")
            state["pending"] = {
                "token": token,
                "amount": shortfall,
                "from": treasury.address,
                "to": escrow.address,
                "tx_hash": None,
            }
            _save_journal(journal_path, state)
            try:
                tx_hash = str(
                    await treasury.transfer(
                        to=escrow.address, amount=shortfall, token=token, network=NETWORK
                    )
                )
                get_explorer_url(tx_hash)
            except Exception:
                raise RuntimeError(
                    "Funding submission unresolved; inspect treasury before retry"
                ) from None
            state["pending"]["tx_hash"] = tx_hash
            _save_journal(journal_path, state)
            await _finish_leg(state, journal_path, web3)
        offer = database.upsert_offer(
            args.product,
            args.cashback_bps,
            args.new_customers_only,
            args.budget_cents,
            (datetime.now(UTC) + timedelta(days=args.days)).isoformat(),
            expected_offer=before,
        )
        return {
            **offer,
            "product_name": product["name"],
            "escrow_address": escrow.address,
            "escrow_explorer_url": get_explorer_url(escrow.address),
        }


def stage_output(offer: dict[str, Any]) -> str:
    """Format the merchant's first demo beat using public offer metadata only.

    Args:
        offer: Persisted offer and public escrow fields returned by create_offer.

    Returns:
        str: Three stage-ready lines in the PRD's required order.
    """
    audience = "new customers only" if offer["new_customer_only"] else "all customers"
    return (
        f"Offer live: {offer['product_name']}\n"
        f"  {offer['cashback_bps'] / 100:g}% cashback, {audience}, "
        f"${format_cents(offer['budget_cents'])} budget, expires {offer['expires_at']}\n"
        f"  Escrow funded: {offer['escrow_address']} {offer['escrow_explorer_url']}"
    )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Open configured dependencies and ensure all local/provider resources close.

    Args:
        args: Validated merchant arguments.

    Returns:
        dict[str, Any]: Public offer activation result.
    """
    settings = load_settings()
    database = Database(settings.db_path)
    store = StoreClient(settings.eshop_url, settings.agent_api_key)
    try:
        async with build_cdp_client(settings) as cdp:
            return await create_offer(
                args,
                settings,
                database,
                cdp,
                build_web3(settings.base_sepolia_rpc_url),
                store,
                settings.db_path.with_suffix(".offer-funding.json"),
            )
    finally:
        await store.aclose()
        database.close()


def main() -> None:
    """Run the merchant command without exposing provider exception details.

    Args:
        None. Arguments come from the command line.

    Returns:
        None. Success prints stage output; failure exits with sanitized guidance.
    """
    args = parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception:
        print(
            "Offer not activated. Check testnet, balances, and the public funding journal; "
            "do not delete unresolved funding evidence.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(stage_output(result))


if __name__ == "__main__":
    main()
