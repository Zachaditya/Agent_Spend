"""Exercise custom amounts and repeated top-ups through the real MCP transport."""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.main import create_app
from tests.conftest import ADDRESS, ETH_TX_HASH, SECRET, TREASURY_ADDRESS, TX_HASH
from tests.test_main import rpc


async def test_custom_grant_then_topup_over_mcp(settings, cdp, web3):
    """Verify discovery, disclosures, exact sends, top-ups, and replay over HTTP.

    Args:
        settings: Synthetic application settings.
        cdp: Fake provider recording each transfer.
        web3: Fake chain exposing balances and receipts.

    Returns:
        None.
    """
    web3.usdc_balances[TREASURY_ADDRESS] = 100_000_000
    cdp.treasury.transfer.side_effect = [TX_HASH, ETH_TX_HASH, "0x" + "ef" * 32]
    app = create_app(settings, cdp_factory=lambda _: cdp, rpc_checker=AsyncMock())
    async with app.router.lifespan_context(app):
        service = app.state.wallet_service
        service.web3 = web3
        service.database.create_wallet_pointer("shopper-1", ADDRESS)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
        ) as client:
            for amount, kind in (("50", "INITIAL"), ("10", "TOP_UP")):
                response = await rpc(
                    client,
                    "tools/call",
                    {"name": "fund_agent_wallet", "arguments": {"amount_usdc": amount}},
                )
                assert not response.json()["result"].get("isError")
                disclosure = response.json()["result"]["structuredContent"]
                assert disclosure["funding_kind"] == kind
                assert disclosure["usdc_amount"] == amount + ".00"
                before = cdp.treasury.transfer.await_count
                arguments = {"confirmation_id": disclosure["confirmation_id"]}
                funded = await rpc(
                    client, "tools/call", {"name": "fund_agent_wallet", "arguments": arguments}
                )
                result = funded.json()["result"]["structuredContent"]
                assert result["status"] == "FUNDED"
                assert result["usdc_amount"] == amount + ".00"
                assert cdp.treasury.transfer.await_count == before + (2 if kind == "INITIAL" else 1)
                replay = await rpc(
                    client, "tools/call", {"name": "fund_agent_wallet", "arguments": arguments}
                )
                assert replay.json()["result"]["isError"]
                assert "CONFIRMATION_EXPIRED" in replay.text


@pytest.mark.parametrize(
    "arguments",
    [
        {"recipient": ADDRESS},
        {"network": "mainnet"},
        {"token": "eth"},
        {"treasury": "other"},
        {"amount_usdc": 50},
        {"amount_usdc": SECRET},
    ],
)
async def test_funding_rejects_override_arguments_without_leaking_values(
    settings, cdp, arguments, caplog
):
    """Reject unsupported schema fields and invalid amounts without exposing inputs.

    Args:
        settings: Synthetic application settings.
        cdp: Fake provider recording transfers.
        arguments: Invalid or unauthorized funding arguments.
        caplog: Captured logs used to check secret redaction.

    Returns:
        None.
    """
    app = create_app(settings, cdp_factory=lambda _: cdp, rpc_checker=AsyncMock())
    async with app.router.lifespan_context(app):
        app.state.wallet_service.database.create_wallet_pointer("shopper-1", ADDRESS)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
        ) as client:
            result = await rpc(
                client, "tools/call", {"name": "fund_agent_wallet", "arguments": arguments}
            )
            assert result.json()["result"]["isError"]
            assert SECRET not in result.text
            assert SECRET not in caplog.text
            cdp.treasury.transfer.assert_not_awaited()
