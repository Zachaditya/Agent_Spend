"""Verify the developer smoke checker follows the MCP 2.x client contract."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

from scripts.check_mcp import verify


async def test_checker_uses_two_stream_client_contract(monkeypatch):
    """Accept the SDK 2.x two-stream context and return public verification results.

    Args:
        monkeypatch: Pytest's scoped dependency replacement.

    Returns:
        None.
    """

    @asynccontextmanager
    async def streams(url):
        """Provide the exact read/write pair returned by MCP 2.x.

        Args:
            url: The MCP URL supplied to the checker.

        Yields:
            tuple[None, None]: Fake read and write streams.
        """
        yield None, None

    session = SimpleNamespace(
        initialize=AsyncMock(),
        list_tools=AsyncMock(
            return_value=SimpleNamespace(
                tools=[
                    SimpleNamespace(name="create_agent_wallet"),
                    SimpleNamespace(name="fund_agent_wallet"),
                    SimpleNamespace(name="get_product"),
                    SimpleNamespace(name="get_spending_policy"),
                    SimpleNamespace(name="get_transactions"),
                    SimpleNamespace(name="get_wallet"),
                    SimpleNamespace(name="request_purchase"),
                    SimpleNamespace(name="search_products"),
                    SimpleNamespace(name="set_spending_policy"),
                ]
            )
        ),
        call_tool=AsyncMock(
            side_effect=[
                SimpleNamespace(is_error=False, structured_content={"status": "NOT_CREATED"}),
                SimpleNamespace(is_error=True, content="INVALID_CONFIRMATION"),
                SimpleNamespace(is_error=False, structured_content={"status": "NO_WALLET"}),
                SimpleNamespace(is_error=False, structured_content={"status": "NOT_CREATED"}),
            ]
        ),
    )

    @asynccontextmanager
    async def client_session(read, write):
        """Supply an initialized fake MCP session for the smoke checker.

        Args:
            read: The fake read stream.
            write: The fake write stream.

        Yields:
            SimpleNamespace: An observable fake protocol session.
        """
        yield session

    monkeypatch.setattr("scripts.check_mcp.streamable_http_client", streams)
    monkeypatch.setattr("scripts.check_mcp.ClientSession", client_session)
    result = await verify("http://127.0.0.1:8077/mcp")
    assert result["tools"] == [
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
    assert result["wallet_after"] == {"status": "NOT_CREATED"}


async def test_checker_custom_grant_and_topup_allow_already_funded_wallet(monkeypatch):
    """Verify custom additions and replay without rejecting an existing funded wallet.

    Args:
        monkeypatch: Scoped MCP transport/session replacement helper.

    Returns:
        None.
    """
    wallet = {"status": "READY", "funding_status": "FUNDED", "latest_funding_operation_id": "old"}
    disclosure = {"status": "CONFIRMATION_REQUIRED", "confirmation_id": "consent"}
    ok = SimpleNamespace(is_error=False, structured_content=wallet)
    consent = SimpleNamespace(is_error=False, structured_content=disclosure)
    funded = SimpleNamespace(is_error=False, structured_content={"status": "FUNDED"})
    replay = SimpleNamespace(is_error=True, content="CONFIRMATION_EXPIRED")
    invalid = SimpleNamespace(is_error=True, content="INVALID_CONFIRMATION")
    session = SimpleNamespace(
        initialize=AsyncMock(),
        list_tools=AsyncMock(
            return_value=SimpleNamespace(
                tools=[
                    SimpleNamespace(name=name)
                    for name in (
                        "create_agent_wallet",
                        "fund_agent_wallet",
                        "get_product",
                        "get_spending_policy",
                        "get_transactions",
                        "get_wallet",
                        "request_purchase",
                        "search_products",
                        "set_spending_policy",
                    )
                ]
            )
        ),
        call_tool=AsyncMock(
            side_effect=[
                ok,
                ok,
                consent,
                ok,
                funded,
                replay,
                ok,
                consent,
                ok,
                funded,
                replay,
                invalid,
                invalid,
                ok,
            ]
        ),
    )

    @asynccontextmanager
    async def streams(url):
        """Provide fake HTTP streams.

        Args:
            url: Requested MCP URL.

        Yields:
            tuple: Fake read/write streams.
        """
        yield None, None

    @asynccontextmanager
    async def client_session(read, write):
        """Provide the observable fake session.

        Args:
            read: Fake read stream.
            write: Fake write stream.

        Yields:
            SimpleNamespace: Mock protocol session.
        """
        yield session

    monkeypatch.setattr("scripts.check_mcp.streamable_http_client", streams)
    monkeypatch.setattr("scripts.check_mcp.ClientSession", client_session)
    result = await verify(
        "http://localhost:8000/mcp", fund_wallet=True, amount_usdc="50", top_up_usdc="10"
    )
    assert result["funded"]["status"] == "FUNDED"
    assert result["top_up"]["status"] == "FUNDED"
    assert call("fund_agent_wallet", {"amount_usdc": "50"}) in session.call_tool.await_args_list
    assert call("fund_agent_wallet", {"amount_usdc": "10"}) in session.call_tool.await_args_list
