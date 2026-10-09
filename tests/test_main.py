"""Exercise FastAPI lifespan, testnet enforcement, and real MCP HTTP requests."""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from app.confirmations import ConfirmationStore
from app.main import check_chain, create_app
from tests.conftest import ADDRESS, SECRET

HEADERS = {"Accept": "application/json, text/event-stream"}


async def rpc(client, method, params=None, host="localhost:8000"):
    """Send a JSON-RPC request through the mounted MCP HTTP transport.

    Args:
        client: The HTTP test client.
        method: The MCP method to call.
        params: Optional JSON-RPC method parameters.
        host: The Host header used for transport-security validation.

    Returns:
        httpx.Response: The unmodified MCP response.
    """
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return await client.post("/mcp", json=body, headers={**HEADERS, "Host": host})


async def test_mcp_discovery_two_step_flow_and_host_validation(settings, cdp, caplog):
    """Reach the exact MCP endpoint and exercise creation using the actual transport.

    Args:
        settings: Synthetic service settings.
        cdp: The observable fake provider.
        caplog: Pytest's captured logs.

    Returns:
        None.
    """
    app = create_app(settings, cdp_factory=lambda _: cdp, rpc_checker=AsyncMock())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
        ) as client:
            init = await rpc(
                client,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "phase1-acceptance", "version": "1.0"},
                },
            )
            assert init.status_code == 200
            assert "weekly budget or automatic purchase limit require fresh confirmation" in (
                init.json()["result"]["instructions"]
            )
            listed = await rpc(client, "tools/list", host=settings.public_host)
            assert listed.status_code == 200
            tools = {item["name"]: item for item in listed.json()["result"]["tools"]}
            assert set(tools) == {
                "create_agent_wallet",
                "fund_agent_wallet",
                "get_product",
                "get_spending_policy",
                "get_transactions",
                "get_wallet",
                "request_purchase",
                "search_products",
                "set_spending_policy",
            }
            assert tools["get_wallet"]["annotations"]["readOnlyHint"] is True
            assert tools["create_agent_wallet"]["annotations"]["readOnlyHint"] is False
            assert tools["fund_agent_wallet"]["annotations"]["readOnlyHint"] is False
            schema = tools["create_agent_wallet"]["inputSchema"]
            assert set(schema["properties"]) == {"confirmation_id"}
            assert schema["additionalProperties"] is False
            funding_schema = tools["fund_agent_wallet"]["inputSchema"]
            assert set(funding_schema["properties"]) == {"confirmation_id", "amount_usdc"}
            assert funding_schema["additionalProperties"] is False
            policy_schema = tools["set_spending_policy"]["inputSchema"]
            assert set(policy_schema["properties"]) == {
                "weekly_limit",
                "max_auto_transaction",
                "confirmation_id",
            }
            assert policy_schema["additionalProperties"] is False
            assert "new message" in tools["create_agent_wallet"]["description"]
            assert "top-up" in tools["fund_agent_wallet"]["description"]
            assert "tighten" in tools["set_spending_policy"]["description"]
            assert "automatic purchase limit increase" in (
                tools["set_spending_policy"]["description"]
            )
            assert "private key" in tools["get_wallet"]["description"]
            assert "product_id" in tools["request_purchase"]["description"]
            assert "max_price" in tools["search_products"]["inputSchema"]["properties"]
            assert tools["get_transactions"]["annotations"]["readOnlyHint"] is True
            initial = await rpc(client, "tools/call", {"name": "create_agent_wallet"})
            disclosure = initial.json()["result"]["structuredContent"]
            assert disclosure["status"] == "CONFIRMATION_REQUIRED"
            cdp.evm.create_account.assert_not_awaited()
            created = await rpc(
                client,
                "tools/call",
                {
                    "name": "create_agent_wallet",
                    "arguments": {"confirmation_id": disclosure["confirmation_id"]},
                },
            )
            assert created.json()["result"]["structuredContent"]["address"] == ADDRESS
            repeated = await rpc(client, "tools/call", {"name": "create_agent_wallet"})
            assert repeated.json()["result"]["structuredContent"]["status"] == "ALREADY_EXISTS"
            invalid = await rpc(
                client,
                "tools/call",
                {
                    "name": "create_agent_wallet",
                    "arguments": {"confirmation_id": "fake"},
                },
            )
            assert invalid.json()["result"]["isError"] is True
            assert "INVALID_CONFIRMATION" in invalid.text
            leaked = await rpc(
                client,
                "tools/call",
                {
                    "name": "fund_agent_wallet",
                    "arguments": {"amount": SECRET},
                },
            )
            assert leaked.json()["result"]["isError"] is True
            assert SECRET not in leaked.text
            assert SECRET not in caplog.text
            policy = await rpc(
                client,
                "tools/call",
                {
                    "name": "set_spending_policy",
                    "arguments": {"weekly_limit": "25", "max_auto_transaction": "12"},
                },
            )
            policy_disclosure = policy.json()["result"]["structuredContent"]
            assert policy_disclosure["status"] == "CONFIRMATION_REQUIRED"
            assert (await rpc(client, "tools/call", {"name": "get_spending_policy"})).json()[
                "result"
            ]["structuredContent"] == {"status": "NOT_SET"}
            active = await rpc(
                client,
                "tools/call",
                {
                    "name": "set_spending_policy",
                    "arguments": {
                        "weekly_limit": "25",
                        "max_auto_transaction": "12",
                        "confirmation_id": policy_disclosure["confirmation_id"],
                    },
                },
            )
            assert active.json()["result"]["structuredContent"]["status"] == "ACTIVE"
            tightened = await rpc(
                client,
                "tools/call",
                {
                    "name": "set_spending_policy",
                    "arguments": {"weekly_limit": "25", "max_auto_transaction": "10"},
                },
            )
            assert (
                tightened.json()["result"]["structuredContent"]["max_auto_transaction"] == "10.00"
            )
            proposed = await rpc(
                client,
                "tools/call",
                {
                    "name": "set_spending_policy",
                    "arguments": {"weekly_limit": "50", "max_auto_transaction": "10"},
                },
            )
            proposal = proposed.json()["result"]["structuredContent"]
            assert proposal["status"] == "CONFIRMATION_REQUIRED"
            before = await rpc(client, "tools/call", {"name": "get_spending_policy"})
            assert before.json()["result"]["structuredContent"]["weekly_limit"] == "25.00"
            increase_args = {
                "name": "set_spending_policy",
                "arguments": {
                    "weekly_limit": "50", "max_auto_transaction": "10",
                    "confirmation_id": proposal["confirmation_id"],
                },
            }
            increased = await rpc(client, "tools/call", increase_args)
            assert increased.json()["result"]["structuredContent"] == {
                "status": "ACTIVE", "weekly_limit": "50.00", "max_auto_transaction": "10.00",
            }
            replay = await rpc(client, "tools/call", increase_args)
            assert replay.json()["result"]["isError"] is True
            assert "CONFIRMATION_EXPIRED" in replay.text
            over_cap = await rpc(
                client,
                "tools/call",
                {
                    "name": "set_spending_policy",
                    "arguments": {"weekly_limit": "500", "max_auto_transaction": "10"},
                },
            )
            assert over_cap.json()["result"]["structuredContent"]["status"] == "ABOVE_CAP"
            auto_increase = await rpc(
                client, "tools/call",
                {
                    "name": "set_spending_policy",
                    "arguments": {"weekly_limit": "50", "max_auto_transaction": "11"},
                },
            )
            auto_proposal = auto_increase.json()["result"]["structuredContent"]
            assert auto_proposal["status"] == "CONFIRMATION_REQUIRED"
            before_auto = await rpc(client, "tools/call", {"name": "get_spending_policy"})
            assert before_auto.json()["result"]["structuredContent"][
                "max_auto_transaction"
            ] == "10.00"
            confirmed_auto = await rpc(
                client, "tools/call",
                {
                    "name": "set_spending_policy",
                    "arguments": {
                        "weekly_limit": "50", "max_auto_transaction": "11",
                        "confirmation_id": auto_proposal["confirmation_id"],
                    },
                },
            )
            assert confirmed_auto.json()["result"]["structuredContent"] == {
                "status": "ACTIVE", "weekly_limit": "50.00", "max_auto_transaction": "11.00",
            }
            auto_over_cap = await rpc(
                client, "tools/call",
                {
                    "name": "set_spending_policy",
                    "arguments": {"weekly_limit": "100", "max_auto_transaction": "50.01"},
                },
            )
            assert auto_over_cap.json()["result"]["structuredContent"]["status"] == "ABOVE_CAP"
            assert (await rpc(client, "tools/list", host="attacker.example")).status_code == 421
            assert (
                await rpc(client, "tools/list", host=settings.public_host + ".evil")
            ).status_code == 421
    assert cdp.entered and cdp.closed


async def test_lifespan_tools_use_the_application_that_owns_the_resources(settings, cdp):
    """Run the factory's lifespan on another app and resolve tools from that app.

    Args:
        settings: Synthetic service settings.
        cdp: The observable fake provider.

    Returns:
        None.
    """
    original = create_app(settings, cdp_factory=lambda _: cdp, rpc_checker=AsyncMock())
    other = FastAPI(lifespan=original.router.lifespan_context)
    original_routes = list(original.router.routes)
    other_routes = list(other.router.routes)
    async with other.router.lifespan_context(other):
        assert hasattr(other.state, "wallet_service")
        assert not hasattr(original.state, "wallet_service")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=other), base_url="http://localhost:8000"
        ) as client:
            read = await rpc(client, "tools/call", {"name": "get_wallet"})
            assert read.json()["result"]["structuredContent"] == {"status": "NOT_CREATED"}
            policy = await rpc(client, "tools/call", {"name": "get_spending_policy"})
            assert policy.json()["result"]["structuredContent"] == {"status": "NOT_SET"}
            funding = await rpc(client, "tools/call", {"name": "fund_agent_wallet"})
            assert funding.json()["result"]["structuredContent"]["status"] == "NO_WALLET"
            created = await rpc(client, "tools/call", {"name": "create_agent_wallet"})
            assert (
                created.json()["result"]["structuredContent"]["status"] == "CONFIRMATION_REQUIRED"
            )
    assert original.router.routes == original_routes
    assert other.router.routes == other_routes
    assert not hasattr(other.state, "wallet_service")
    assert not hasattr(other.state, "mcp")
    assert cdp.closed


async def test_confirmation_capacity_error_is_safe_over_mcp_and_preserves_tokens(settings, cdp):
    """Reject disclosure floods at the wire boundary without losing pending consent.

    Args:
        settings: Synthetic service settings.
        cdp: The observable fake provider.

    Returns:
        None.
    """
    app = create_app(settings, cdp_factory=lambda _: cdp, rpc_checker=AsyncMock())
    async with app.router.lifespan_context(app):
        app.state.wallet_service.confirmations = ConfirmationStore(max_pending=1)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
        ) as client:
            initial = await rpc(client, "tools/call", {"name": "create_agent_wallet"})
            disclosure = initial.json()["result"]["structuredContent"]
            limited = await rpc(client, "tools/call", {"name": "create_agent_wallet"})
            assert limited.json()["result"]["isError"] is True
            assert "CONFIRMATION_LIMIT_REACHED" in limited.text
            assert SECRET not in limited.text
            cdp.evm.create_account.assert_not_awaited()
            created = await rpc(
                client,
                "tools/call",
                {
                    "name": "create_agent_wallet",
                    "arguments": {"confirmation_id": disclosure["confirmation_id"]},
                },
            )
            assert created.json()["result"]["structuredContent"]["status"] == "CREATED"
            cdp.evm.create_account.assert_awaited_once()


async def test_constructor_failure_is_sanitized_at_the_lifespan_boundary(
    settings, monkeypatch, caplog
):
    """Suppress private SDK constructor details without redundant inner wrappers.

    Args:
        settings: Synthetic service settings.
        monkeypatch: Pytest's scoped SDK constructor replacement helper.
        caplog: Pytest's captured log records.

    Returns:
        None.
    """
    monkeypatch.setattr("app.main.CdpClient", Mock(side_effect=RuntimeError(SECRET)))
    app = create_app(settings, rpc_checker=AsyncMock())
    with pytest.raises(RuntimeError, match="^CDP_INITIALIZATION_FAILED$") as error:
        async with app.router.lifespan_context(app):
            pytest.fail("Startup must reject a broken provider constructor")
    assert SECRET not in str(error.value)
    assert SECRET not in caplog.text
    assert not hasattr(app.state, "wallet_service")


async def test_wrong_chain_prevents_startup_and_closes_resources(settings, cdp):
    """Abort lifespan before wallet access when the RPC is not Base Sepolia.

    Args:
        settings: Synthetic service settings.
        cdp: The observable fake provider.

    Returns:
        None.
    """
    checker = AsyncMock(side_effect=RuntimeError("RPC_CHAIN_MISMATCH: expected 84532"))
    app = create_app(settings, cdp_factory=lambda _: cdp, rpc_checker=checker)
    with pytest.raises(RuntimeError, match="RPC_CHAIN_MISMATCH"):
        async with app.router.lifespan_context(app):
            pytest.fail("Startup should reject the wrong chain")
    cdp.evm.create_account.assert_not_awaited()


@pytest.mark.parametrize("chain", [1, 8453, 11155111])
async def test_chain_guard_rejects_other_networks(settings, chain):
    """Check the actual JSON-RPC response against the fixed testnet chain ID.

    Args:
        settings: Synthetic service settings.
        chain: A disallowed mainnet or alternate testnet chain ID.

    Returns:
        None.
    """
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(chain)})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="RPC_CHAIN_MISMATCH"):
            await check_chain(settings, client)


async def test_chain_guard_accepts_base_sepolia_and_sanitizes_rpc_errors(settings):
    """Accept 84532 and hide sensitive RPC response content on failure.

    Args:
        settings: Synthetic service settings.

    Returns:
        None.
    """
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x14a34"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        await check_chain(settings, client)
        assert not client.is_closed
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text=SECRET))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="RPC_UNAVAILABLE") as error:
            await check_chain(settings, client)
        assert SECRET not in str(error.value)


async def test_cleanup_failure_cannot_expose_provider_credentials(settings, cdp, caplog):
    """Sanitize provider shutdown exceptions while closing the database.

    Args:
        settings: Synthetic service settings.
        cdp: A fake CDP provider whose cleanup raises a secret-bearing exception.
        caplog: Pytest's captured log records.

    Returns:
        None.
    """

    class BrokenCleanup(type(cdp)):
        """Simulate an SDK cleanup failure after the service has run."""

        async def __aexit__(self, *exc):
            """Fail shutdown with private data that the application must suppress.

            Args:
                *exc: Context-manager exception information.

            Returns:
                None: Always raises the simulated failure.

            Raises:
                RuntimeError: The synthetic secret-bearing provider failure.
            """
            raise RuntimeError(SECRET)

    broken = BrokenCleanup()
    app = create_app(settings, cdp_factory=lambda _: broken, rpc_checker=AsyncMock())
    with pytest.raises(RuntimeError, match="CDP_CLEANUP_FAILED") as error:
        async with app.router.lifespan_context(app):
            database = app.state.wallet_service.database
    assert SECRET not in str(error.value)
    assert SECRET not in caplog.text
    with pytest.raises(Exception, match="closed database"):
        database.get_wallet()
