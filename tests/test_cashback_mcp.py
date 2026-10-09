"""Exercise offer discovery, purchase, history, and strict inputs over MCP HTTP."""

from unittest.mock import AsyncMock

import httpx

from app.main import create_app
from tests import test_cashback, test_purchase
from tests.conftest import SECRET
from tests.test_main import rpc

purchase_service = test_purchase.purchase_service
store = test_purchase.store
cashback_setup = test_cashback.cashback_setup


async def test_offer_flow_and_override_rejection_over_mcp(
    settings, cdp, purchase_service, cashback_setup
):
    """Expose server-computed incentives without allowing model-supplied payout fields.

    Args:
        settings: Isolated application settings.
        cdp: Fake provider injected into the real app lifespan.
        purchase_service: Service with a funded shopper and full-price policy.
        cashback_setup: Escrow and exact receipt fixtures.

    Returns:
        None. Real MCP serialization, strict schemas, and receipt links are checked.
    """
    app = create_app(settings, cdp_factory=lambda _: cdp, rpc_checker=AsyncMock())
    async with app.router.lifespan_context(app):
        app.state.wallet_service.purchase_service = purchase_service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
        ) as client:
            listed = await rpc(client, "tools/list")
            tools = {t["name"]: t for t in listed.json()["result"]["tools"]}
            assert len(tools) == 9
            assert set(tools["request_purchase"]["inputSchema"]["properties"]) == {
                "product_id",
                "max_price",
            }
            assert "Never promise cashback" in tools["search_products"]["description"]
            search = await rpc(
                client,
                "tools/call",
                {"name": "search_products", "arguments": {"query": "tee", "max_price": "15"}},
            )
            assert not search.json()["result"].get("isError")
            assert "10.20" in search.text and "1.80" in search.text
            for field in ("offer_id", "cashback", "cashback_cents", "recipient"):
                rejected = await rpc(
                    client,
                    "tools/call",
                    {
                        "name": "request_purchase",
                        "arguments": {"product_id": test_cashback.PRODUCT, field: SECRET},
                    },
                )
                assert rejected.json()["result"]["isError"]
                assert SECRET not in rejected.text
            cashback_setup.transfer.assert_not_called()
            paid = await rpc(
                client,
                "tools/call",
                {"name": "request_purchase", "arguments": {"product_id": test_cashback.PRODUCT}},
            )
            result = paid.json()["result"]["structuredContent"]
            assert result["cashback_status"] == "PAID"
            assert result["cashback"] == "1.80"
            assert result["cashback_explorer_url"].endswith(test_cashback.CB_HASH)
            history = await rpc(client, "tools/call", {"name": "get_transactions"})
            assert (
                history.json()["result"]["structuredContent"]["transactions"][0]["cashback_status"]
                == "PAID"
            )
            assert SECRET not in paid.text + history.text + search.text
