"""Check and prepare the CDP treasury account used for Phase 2 demo funding."""

import argparse
import asyncio
import json
from typing import Any

from app.config import load_settings
from app.main import build_cdp_client, check_chain
from app.wallet import (
    NETWORK,
    build_web3,
    format_eth_balance,
    format_usdc_balance,
    get_eth_balance,
    get_usdc_balance,
)


async def maybe_request_faucet(account: Any, token: str) -> str | None:
    """Optionally request testnet faucet funds when the SDK account supports it.

    Args:
        account: The CDP account returned for the treasury name.
        token: The faucet token symbol, such as eth or usdc.

    Returns:
        str | None: The faucet transaction hash when requested, otherwise None.
    """
    request_faucet = getattr(account, "request_faucet", None)
    if request_faucet is None:
        return None
    return await request_faucet(network=NETWORK, token=token)


async def inspect_treasury(request_faucet: bool = False) -> dict[str, Any]:
    """Create or fetch the treasury account and report its public balances.

    Args:
        request_faucet: Whether to request supported faucet funds before reading balances.

    Returns:
        dict[str, Any]: Public treasury address, balances, and optional faucet hashes.

    Raises:
        RuntimeError: The configured RPC is unavailable or not Base Sepolia.
        Exception: CDP account lookup or balance reads fail.
    """
    settings = load_settings()
    await check_chain(settings)
    web3 = build_web3(settings.base_sepolia_rpc_url)
    faucet_hashes = {}
    async with build_cdp_client(settings) as cdp:
        account = await cdp.evm.get_or_create_account(name=settings.treasury_account_name)
        if request_faucet:
            for token in ("eth", "usdc"):
                tx_hash = await maybe_request_faucet(account, token)
                if tx_hash is not None:
                    faucet_hashes[token] = tx_hash
        usdc_balance = get_usdc_balance(account.address, web3, settings.usdc_contract_address)
        eth_balance = get_eth_balance(account.address, web3)
        return {
            "treasury_account_name": settings.treasury_account_name,
            "treasury_address": account.address,
            "network": NETWORK,
            "usdc_balance": format_usdc_balance(usdc_balance),
            "eth_balance": format_eth_balance(eth_balance),
            "faucet_tx_hashes": faucet_hashes,
        }


def main() -> None:
    """Parse bootstrap flags and print public treasury readiness JSON.

    Args:
        None: Reads command-line arguments.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--request-faucet",
        action="store_true",
        help="Request faucet funds first when the CDP SDK supports the token.",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(inspect_treasury(args.request_faucet)), indent=2))


if __name__ == "__main__":
    main()
