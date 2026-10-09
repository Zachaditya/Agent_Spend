"""Specify the authenticated eshop client used by the Phase 5 purchase loop."""

import httpx

from app.store import StoreClient


def product(
    product_id: str,
    name: str,
    price: float,
    color: str = "Black",
    category: str = "Garment Upper body",
) -> dict[str, object]:
    """Build an eshop-shaped product payload for client normalization tests.

    Args:
        product_id: Store-owned product identifier.
        name: Product display name.
        price: Floating dollar price returned by the catalog API.
        color: Catalog color field.
        category: Product group/category field.

    Returns:
        dict[str, object]: Raw product JSON matching the eshop catalog endpoints.
    """
    return {
        "id": product_id,
        "name": name,
        "price": price,
        "colour_group_name": color,
        "product_group_name": category,
        "description": name + " description",
    }


async def test_search_prefers_semantic_then_filters_price_and_limit() -> None:
    """Fetch semantic results, normalize product fields, and apply max-price locally.

    Args:
        None.

    Returns:
        None. Assertions prove the public MCP shape and search request contract.
    """
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """Return a catalog response while recording the requested URL.

        Args:
            request: Incoming mock HTTP request.

        Returns:
            httpx.Response: Synthetic semantic-search response.
        """
        requests.append(request)
        assert request.url.path == "/products/semantic"
        assert request.url.params["q"] == "tee"
        assert request.url.params["limit"] == "100"
        return httpx.Response(
            200,
            json={
                "items": [
                    product("0591439009", "ROOT CLASSIC TEE", 12.00),
                    product("expensive", "Premium tee", 19.99),
                    product("0508638025", "Printed tee 9.99", 10.99, "White"),
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://eshop.test")
    store = StoreClient("http://eshop.test", "agent-key", client=client)

    results = await store.search_products("tee", max_price_cents=1200, limit=2)

    assert [item["product_id"] for item in results] == ["0591439009", "0508638025"]
    assert results[0] == {
        "product_id": "0591439009",
        "name": "ROOT CLASSIC TEE",
        "price": "12.00",
        "amount_cents": 1200,
        "color": "Black",
        "category": "Garment Upper body",
        "cashback": "0.00",
        "net_price": "12.00",
        "offer_note": None,
    }
    assert len(requests) == 1


async def test_search_falls_back_to_name_search_when_semantic_is_unavailable() -> None:
    """Use the name-search endpoint if semantic search returns an unavailable status.

    Args:
        None.

    Returns:
        None. Assertions prove fallback keeps the same normalized result shape.
    """
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """Return a semantic failure followed by name-search results.

        Args:
            request: Incoming mock HTTP request.

        Returns:
            httpx.Response: Failure for semantic search or a fallback result page.
        """
        paths.append(request.url.path)
        if request.url.path == "/products/semantic":
            return httpx.Response(503, json={"detail": "semantic disabled"})
        assert request.url.path == "/products"
        assert request.url.params["q"] == "sweater"
        return httpx.Response(
            200,
            json={"items": [product("0521471002", "Harmony College Sweater", 14.00)]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://eshop.test")
    store = StoreClient("http://eshop.test", "agent-key", client=client)

    results = await store.search_products("sweater", limit=5)

    assert paths == ["/products/semantic", "/products"]
    assert results[0]["product_id"] == "0521471002"
    assert results[0]["price"] == "14.00"


async def test_order_routes_send_agent_key_without_exposing_it() -> None:
    """Authenticate order creation, confirmation, and cancellation with X-Agent-Key.

    Args:
        None.

    Returns:
        None. Assertions inspect headers and response normalization.
    """
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """Validate checkout calls and return synthetic order lifecycle payloads.

        Args:
            request: Incoming mock HTTP request.

        Returns:
            httpx.Response: Order, confirmation, or cancellation response.
        """
        seen.append((request.method, request.url.path))
        assert request.headers["X-Agent-Key"] == "agent-key"
        if request.url.path == "/agent/orders":
            return httpx.Response(
                200,
                json={
                    "order_id": "cart-1",
                    "payment_intent_id": "intent-1",
                    "amount_cents": 1200,
                    "pay_to": "0x3333333333333333333333333333333333333333",
                    "currency": "USDC",
                    "network": "base-sepolia",
                    "status": "requires_payment",
                },
            )
        if request.url.path == "/agent/payment-intents/intent-1/confirm":
            return httpx.Response(200, json={"status": "paid", "order_id": "cart-1"})
        return httpx.Response(200, json={"status": "canceled", "order_id": "cart-1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://eshop.test")
    store = StoreClient("http://eshop.test", "agent-key", client=client)

    order = await store.create_order("0591439009")
    confirmed = await store.confirm_payment("intent-1", "0x" + "ab" * 32)
    canceled = await store.cancel_payment_intent("intent-1")

    assert order["amount_cents"] == 1200
    assert confirmed["status"] == "paid"
    assert canceled["status"] == "canceled"
    assert seen == [
        ("POST", "/agent/orders"),
        ("POST", "/agent/payment-intents/intent-1/confirm"),
        ("POST", "/agent/payment-intents/intent-1/cancel"),
    ]
