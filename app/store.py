"""Call the authenticated eshop catalog and agent-checkout HTTP API."""

from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

from app.wallet import format_cents


class StoreError(Exception):
    """Represent a sanitized merchant API failure for purchase orchestration."""


def _price_to_cents(item: dict[str, Any]) -> int:
    """Convert an eshop product price payload into whole cents.

    Args:
        item: Raw product JSON with either price_cents or price.

    Returns:
        int: Product price in whole cents.

    Raises:
        StoreError: The product price is absent, negative, or not cent-exact.
    """
    if item.get("price_cents") is not None:
        cents = int(item["price_cents"])
    else:
        try:
            price = Decimal(str(item["price"]))
        except (KeyError, InvalidOperation, ValueError):
            raise StoreError("PRODUCT_PRICE_UNAVAILABLE") from None
        cents_decimal = price * Decimal("100")
        if cents_decimal != cents_decimal.to_integral_value():
            raise StoreError("PRODUCT_PRICE_UNAVAILABLE")
        cents = int(cents_decimal)
    if cents < 0:
        raise StoreError("PRODUCT_PRICE_UNAVAILABLE")
    return cents


def _coalesce(*values: Any, default: str | None = "") -> str | None:
    """Return the first non-empty string value from a list of candidates.

    Args:
        *values: Candidate values to inspect.
        default: Fallback returned when every candidate is empty.

    Returns:
        str | None: First non-empty string, or the fallback.
    """
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def normalize_product(item: dict[str, Any], *, include_description: bool = False) -> dict[str, Any]:
    """Project eshop product JSON into the public Agent Spend tool shape.

    Args:
        item: Raw eshop product payload.
        include_description: Whether to include the detail-page description field.

    Returns:
        dict[str, Any]: Stable public product fields for MCP responses.

    Raises:
        StoreError: The product lacks an ID, name, or valid price.
    """
    product_id = _coalesce(item.get("id"), item.get("product_id"), default=None)
    name = _coalesce(item.get("name"), item.get("prod_name"), default=None)
    if product_id is None or name is None:
        raise StoreError("PRODUCT_UNAVAILABLE")
    cents = _price_to_cents(item)
    result: dict[str, Any] = {
        "product_id": product_id,
        "name": name,
        "price": format_cents(cents),
        "amount_cents": cents,
        "color": _coalesce(
            item.get("colour_group_name"), item.get("color_name"), item.get("color")
        ),
        "category": _coalesce(
            item.get("product_group_name"), item.get("category"), item.get("index_group_name")
        ),
        "cashback": "0.00",
        "net_price": format_cents(cents),
        "offer_note": None,
    }
    if include_description:
        result["description"] = _coalesce(
            item.get("description"), item.get("detail_desc"), default=""
        )
    return result


class StoreClient:
    """Wrap eshop catalog and agent-checkout endpoints with sanitized errors."""

    def __init__(
        self,
        base_url: str,
        agent_api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create a reusable HTTP client for the merchant store.

        Args:
            base_url: Root eshop URL, such as http://127.0.0.1:8001.
            agent_api_key: Shared secret sent only in X-Agent-Key for checkout routes.
            client: Optional caller-owned AsyncClient for tests.

        Returns:
            None.
        """
        self.base_url = base_url.rstrip("/")
        self.agent_api_key = agent_api_key
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=10)
        self._owns_client = client is None

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this store created it.

        Args:
            None.

        Returns:
            None.
        """
        if self._owns_client:
            await self._client.aclose()

    async def search_products(
        self, query: str, max_price_cents: int | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Search eshop products and normalize Phase 5 public result fields.

        Args:
            query: Product search query, usually a short term such as tee.
            max_price_cents: Optional whole-cent maximum applied inside Agent Spend.
            limit: Maximum number of products to return after filtering.

        Returns:
            list[dict[str, Any]]: Normalized products with Phase 5B cashback placeholders.

        Raises:
            StoreError: Both semantic and fallback catalog searches fail.
        """
        bounded_limit = min(max(int(limit), 1), 100)
        raw_items = await self._search_semantic_then_name(query)
        products = []
        for item in raw_items:
            normalized = normalize_product(item)
            if max_price_cents is None or normalized["amount_cents"] <= max_price_cents:
                products.append(normalized)
            if len(products) >= bounded_limit:
                break
        return products

    async def _search_semantic_then_name(self, query: str) -> list[dict[str, Any]]:
        """Fetch up to 100 results, falling back from semantic to name search.

        Args:
            query: Product search query.

        Returns:
            list[dict[str, Any]]: Raw eshop product payloads.

        Raises:
            StoreError: Both search endpoints fail or return malformed JSON.
        """
        params = {"q": query, "limit": 100}
        try:
            response = await self._client.get("/products/semantic", params=params)
            response.raise_for_status()
        except httpx.HTTPError:
            try:
                response = await self._client.get("/products", params=params)
                response.raise_for_status()
            except httpx.HTTPError:
                raise StoreError("PRODUCT_SEARCH_UNAVAILABLE") from None
        try:
            payload = response.json()
            items = payload["items"]
            if not isinstance(items, list):
                raise TypeError
            return items
        except (KeyError, TypeError, ValueError):
            raise StoreError("PRODUCT_SEARCH_UNAVAILABLE") from None

    async def get_product(self, product_id: str) -> dict[str, Any]:
        """Fetch and normalize one eshop product detail.

        Args:
            product_id: Store-owned product identifier.

        Returns:
            dict[str, Any]: Public product detail fields.

        Raises:
            StoreError: The product cannot be loaded or normalized.
        """
        try:
            response = await self._client.get(f"/products/{quote(product_id, safe='')}")
            response.raise_for_status()
            return normalize_product(response.json(), include_description=True)
        except (httpx.HTTPError, ValueError, StoreError):
            raise StoreError("PRODUCT_UNAVAILABLE") from None

    async def create_order(self, product_id: str) -> dict[str, Any]:
        """Create a one-item merchant payment intent for a product ID.

        Args:
            product_id: Store-owned product identifier selected by the user or agent.

        Returns:
            dict[str, Any]: Merchant order and payment intent details.

        Raises:
            StoreError: The checkout API rejects the order or is unavailable.
        """
        try:
            response = await self._client.post(
                "/agent/orders",
                headers=self._agent_headers(),
                json={"product_id": product_id},
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise StoreError("ORDER_UNAVAILABLE") from None

    async def confirm_payment(self, payment_intent_id: str, tx_hash: str) -> dict[str, Any]:
        """Ask the merchant to verify a transaction hash and mark the order paid.

        Args:
            payment_intent_id: Merchant payment intent identifier.
            tx_hash: Public Base Sepolia transaction hash submitted by CDP.

        Returns:
            dict[str, Any]: Merchant confirmation response.

        Raises:
            StoreError: Verification fails or the checkout API is unavailable.
        """
        try:
            response = await self._client.post(
                f"/agent/payment-intents/{quote(payment_intent_id, safe='')}/confirm",
                headers=self._agent_headers(),
                json={"tx_hash": tx_hash},
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise StoreError("PAYMENT_CONFIRMATION_FAILED") from None

    async def cancel_payment_intent(self, payment_intent_id: str) -> dict[str, Any]:
        """Cancel an unpaid merchant payment intent.

        Args:
            payment_intent_id: Merchant payment intent identifier.

        Returns:
            dict[str, Any]: Merchant cancellation response.

        Raises:
            StoreError: Cancellation fails or the checkout API is unavailable.
        """
        try:
            response = await self._client.post(
                f"/agent/payment-intents/{quote(payment_intent_id, safe='')}/cancel",
                headers=self._agent_headers(),
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise StoreError("PAYMENT_CANCELLATION_FAILED") from None

    def _agent_headers(self) -> dict[str, str]:
        """Build checkout authentication headers without exposing the secret.

        Args:
            None.

        Returns:
            dict[str, str]: Headers for authenticated eshop agent routes.
        """
        return {"X-Agent-Key": self.agent_api_key}
