"""Verify real MCP discovery and optionally create or fund the testnet shopper wallet."""

import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = [
    "create_agent_wallet",
    "fund_agent_wallet",
    "get_product",
    "get_spending_policy",
    "get_transactions",
    "get_wallet",
    "request_purchase",
    "search_products",
    "set_spending_policy",
]


async def verify(
    url: str,
    create_wallet: bool = False,
    fund_wallet: bool = False,
    amount_usdc: str | None = None,
    top_up_usdc: str | None = None,
) -> dict:
    """Exercise the MCP protocol against a running local or tunneled service.

    This is an automated transport check, not evidence that ChatGPT waited for
    a human's separate yes message. Creation requires the explicit CLI flag.

    Args:
        url: The service's exact Streamable HTTP MCP URL ending in /mcp.
        create_wallet: Whether to perform both wallet tool calls if none exists.
        fund_wallet: Whether to perform both funding calls when a wallet exists.
        amount_usdc: Optional custom USDC amount for the explicitly requested grant.
        top_up_usdc: Optional second amount requiring a distinct confirmation sequence.

    Returns:
        dict: Public tool names and wallet results suitable for an acceptance record.

    Raises:
        RuntimeError: Discovery, tool execution, or the two-call contract fails.
    """
    if not fund_wallet and (amount_usdc is not None or top_up_usdc is not None):
        raise ValueError("Custom amounts require explicit --fund-wallet")
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            if names != EXPECTED_TOOLS:
                raise RuntimeError("Unexpected Agent Spend tool inventory")
            result = await session.call_tool("get_wallet", {})
            if result.is_error:
                raise RuntimeError("Wallet read failed")
            before = result.structured_content
            evidence = {"tools": names, "wallet_before": before}
            if create_wallet and before["status"] == "NOT_CREATED":
                initial = await session.call_tool("create_agent_wallet", {})
                if initial.is_error:
                    raise RuntimeError("Disclosure request failed")
                disclosure = initial.structured_content
                unchanged = await session.call_tool("get_wallet", {})
                if disclosure["status"] != "CONFIRMATION_REQUIRED" or (
                    unchanged.structured_content["status"] != "NOT_CREATED"
                ):
                    raise RuntimeError("The initial tool call caused a protected side effect")
                evidence["disclosure_status"] = disclosure["status"]
                arguments = {"confirmation_id": disclosure["confirmation_id"]}
                created = await session.call_tool("create_agent_wallet", arguments)
                if created.is_error or created.structured_content["status"] != "CREATED":
                    raise RuntimeError("Confirmed wallet creation failed")
                evidence["created"] = created.structured_content
                replay = await session.call_tool("create_agent_wallet", arguments)
                if not replay.is_error or "CONFIRMATION_EXPIRED" not in str(replay.content):
                    raise RuntimeError("Confirmation replay was not rejected")
                evidence["replay"] = "CONFIRMATION_EXPIRED"
            if create_wallet:
                repeated = await session.call_tool("create_agent_wallet", {})
                if repeated.is_error or repeated.structured_content["status"] != "ALREADY_EXISTS":
                    raise RuntimeError("Repeat creation did not return the existing wallet")
                evidence["repeat"] = repeated.structured_content
            if fund_wallet:
                requests = [("funded", amount_usdc)]
                if top_up_usdc is not None:
                    requests.append(("top_up", top_up_usdc))
                for label, amount in requests:
                    wallet = await session.call_tool("get_wallet", {})
                    if wallet.is_error or wallet.structured_content["status"] == "NOT_CREATED":
                        raise RuntimeError("Cannot fund before creating the wallet")
                    arguments = {"amount_usdc": amount} if amount is not None else {}
                    initial = await session.call_tool("fund_agent_wallet", arguments)
                    if initial.is_error:
                        raise RuntimeError("Funding disclosure request failed")
                    disclosure = initial.structured_content
                    if disclosure["status"] != "CONFIRMATION_REQUIRED":
                        raise RuntimeError(
                            "Funding disclosure was not issued: " + disclosure["status"]
                        )
                    unchanged = await session.call_tool("get_wallet", {})
                    if unchanged.is_error or unchanged.structured_content.get(
                        "latest_funding_operation_id"
                    ) != wallet.structured_content.get("latest_funding_operation_id"):
                        raise RuntimeError(
                            "The initial funding call caused a protected side effect"
                        )
                    funded = await session.call_tool(
                        "fund_agent_wallet",
                        {"confirmation_id": disclosure["confirmation_id"]},
                    )
                    if funded.is_error or funded.structured_content["status"] != "FUNDED":
                        raise RuntimeError("Confirmed funding failed")
                    evidence[label] = funded.structured_content
                    replay = await session.call_tool(
                        "fund_agent_wallet",
                        {"confirmation_id": disclosure["confirmation_id"]},
                    )
                    if not replay.is_error or "CONFIRMATION_EXPIRED" not in str(replay.content):
                        raise RuntimeError("Funding confirmation replay was not rejected")
                    evidence["funding_replay" if label == "funded" else "top_up_replay"] = (
                        "CONFIRMATION_EXPIRED"
                    )
            invalid = await session.call_tool("create_agent_wallet", {"confirmation_id": "made-up"})
            if not invalid.is_error or "INVALID_CONFIRMATION" not in str(invalid.content):
                raise RuntimeError("Invented confirmation was not rejected")
            invalid_funding = await session.call_tool(
                "fund_agent_wallet", {"confirmation_id": "made-up"}
            )
            if not invalid_funding.is_error and (
                invalid_funding.structured_content["status"] != "NO_WALLET"
            ):
                raise RuntimeError("Invented funding confirmation was not rejected")
            evidence["invalid"] = "INVALID_CONFIRMATION"
            final = await session.call_tool("get_wallet", {})
            if final.is_error:
                raise RuntimeError("Final wallet read failed")
            evidence["wallet_after"] = final.structured_content
            return evidence


def main() -> None:
    """Parse developer verification options and print public-only JSON evidence.

    Args:
        None: Reads command-line arguments.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:8000/mcp")
    parser.add_argument(
        "--create-wallet",
        action="store_true",
        help="Perform both confirmation calls and create the single CDP testnet wallet.",
    )
    parser.add_argument(
        "--fund-wallet",
        action="store_true",
        help="Perform both confirmation calls for a grant or top-up; defaults to STARTER_USDC.",
    )
    parser.add_argument("--amount-usdc", help="Decimal-string USDC amount; requires --fund-wallet.")
    parser.add_argument(
        "--top-up-usdc", help="Second separately confirmed USDC addition; requires --fund-wallet."
    )
    args = parser.parse_args()
    if not args.fund_wallet and (args.amount_usdc is not None or args.top_up_usdc is not None):
        parser.error("--amount-usdc and --top-up-usdc require --fund-wallet")
    print(
        json.dumps(
            asyncio.run(
                verify(
                    args.url,
                    args.create_wallet,
                    args.fund_wallet,
                    args.amount_usdc,
                    args.top_up_usdc,
                )
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
