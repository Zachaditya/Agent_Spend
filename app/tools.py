"""Expose strict MCP wallet, policy, catalog, and purchase tools."""

from collections.abc import Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import Tool
from mcp_types import ToolAnnotations
from pydantic import ConfigDict, create_model

from app.confirmations import ConfirmationError
from app.purchase import PurchaseError
from app.store import StoreError
from app.wallet import WalletError, WalletService


def strict_tool(function: Callable, annotations: ToolAnnotations) -> Tool:
    """Build a tool that rejects extra arguments without echoing rejected values.

    Args:
        function: The documented callable to expose through MCP.
        annotations: The read/write and external-interaction tool hints.

    Returns:
        Tool: A tool registration with a closed input schema and sanitized validation.
    """
    tool = Tool.from_function(function, annotations=annotations)
    model = create_model(
        function.__name__ + "StrictArguments",
        __base__=tool.fn_metadata.arg_model,
        __config__=ConfigDict(extra="forbid", hide_input_in_errors=True),
    )
    # SDK metadata customization: wire-level schema/rejection tests are the upgrade canary.
    tool.fn_metadata.arg_model = model
    tool.parameters = model.model_json_schema(by_alias=True)
    return tool


def build_mcp(service_provider: Callable[[], WalletService]) -> MCPServer:
    """Create the MCP server whose tools resolve the lifespan-owned service.

    Args:
        service_provider: A callable returning the initialized wallet service.

    Returns:
        MCPServer: Wallet creation, public balances, and confirmed custom funding/top-ups.
    """

    async def create_agent_wallet(confirmation_id: str | None = None) -> dict[str, str]:
        """Create the single Base Sepolia shopping wallet in two conversation steps.

        On CONFIRMATION_REQUIRED, show disclosure word for word and ask yes or no.
        Call again with the confirmation ID only after the user says yes in a new message.
        Never claim a wallet exists unless CREATED, ALREADY_EXISTS, or READY says so.
        If asked for a private key or seed phrase, explain that Coinbase CDP holds
        the keys and no Agent Spend tool can export them. Never invent key material.

        Args:
            confirmation_id: Omit for the disclosure; supply its ID after a new user yes.

        Returns:
            dict[str, str]: CONFIRMATION_REQUIRED with disclosure and ID, or CREATED /
                ALREADY_EXISTS with the public address, network, and Basescan link.

        Raises:
            ToolError: A public confirmation failure code or WALLET_CREATION_FAILED.
        """
        try:
            return await service_provider().create_agent_wallet(confirmation_id)
        except (ConfirmationError, WalletError) as error:
            raise ToolError(str(error)) from None
        except Exception:
            raise ToolError("WALLET_UNAVAILABLE") from None

    async def get_wallet() -> dict[str, str]:
        """Read the persisted shopping wallet's public address and Base Sepolia link.

        Never claim a wallet exists when status is NOT_CREATED. If asked for a
        private key or seed phrase, explain that Coinbase CDP holds the keys and
        no Agent Spend tool can export them. Never invent key material. Relay the
        balance and funding_status fields exactly as returned by the tool.

        Args:
            None.

        Returns:
            dict[str, str]: NOT_CREATED, or READY with public wallet, balances, funding
                status, network, and explorer URL.

        Raises:
            ToolError: WALLET_UNAVAILABLE when the public pointer cannot be read.
        """
        try:
            return service_provider().get_wallet()
        except Exception:
            raise ToolError("WALLET_UNAVAILABLE") from None

    async def fund_agent_wallet(
        confirmation_id: str | None = None, amount_usdc: str | None = None
    ) -> dict[str, object]:
        """Request a custom test USDC grant or repeated top-up within server caps.

        On CONFIRMATION_REQUIRED, show disclosure word for word and ask yes or no.
        Call again with the confirmation ID only after the user says yes in a new message.
        Relay the user's requested USDC amount as a decimal string, for example "50".
        Omit the amount for the configured default. On confirmation, omit amount_usdc
        to use the exact disclosed intent. Every top-up needs a fresh disclosure and
        a new user yes. The server controls caps, treasury, persisted destination,
        token, and Base Sepolia network. Initial funding includes configured gas ETH;
        top-ups send USDC only. Never invent an amount, split requests to bypass caps,
        or automatically retry failed or uncertain transfers. Relay exact amounts,
        explorer URLs, status, and recovery guidance word for word.

        Args:
            confirmation_id: Omit for disclosure; supply its ID after a new user yes.
            amount_usdc: Optional user-requested positive decimal string with at most
                two fractional places; omitted confirmation amounts use the stored intent.

        Returns:
            dict[str, object]: CONFIRMATION_REQUIRED, FUNDED, or a
                public failure status such as NO_WALLET, TREASURY_NOT_READY,
                INSUFFICIENT_TREASURY_FUNDS, FUNDING_IN_PROGRESS,
                FUNDING_AMOUNT_ABOVE_CAP, FUNDING_TOTAL_CAP_EXCEEDED, or FUNDING_FAILED.

        Raises:
            ToolError: A public confirmation failure code or WALLET_UNAVAILABLE.
        """
        try:
            return await service_provider().fund_agent_wallet(
                confirmation_id=confirmation_id, amount_usdc=amount_usdc
            )
        except (ConfirmationError, WalletError) as error:
            raise ToolError(str(error)) from None
        except Exception:
            raise ToolError("WALLET_UNAVAILABLE") from None

    async def set_spending_policy(
        weekly_limit: str,
        max_auto_transaction: str,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        """Set a policy or raise either spending limit with consent; tighten immediately.

        On CONFIRMATION_REQUIRED, show disclosure word for word and ask yes or no.
        Call again with the confirmation ID only after the user says yes in a new
        message. Use decimal-string test USDC amounts such as "25" and "12".
        Initial setup and every weekly budget or automatic purchase limit increase
        require fresh confirmation within server caps. Preserve any limit the user
        did not ask to change. The automatic limit must not exceed the weekly limit.
        Changes that only tighten limits apply immediately. If either limit increases,
        no part of the update takes effect before confirmation. Existing purchases
        still count toward the rolling seven-day budget. Relay INVALID_POLICY and
        ABOVE_CAP exactly. Never retry, split, or rephrase a refused change to bypass
        these rules. Funding the wallet never raises this policy.

        Args:
            weekly_limit: Requested weekly budget as a decimal test USDC string.
            max_auto_transaction: Requested automatic purchase limit as test USDC.
            confirmation_id: Omit for first disclosure; supply after a new user yes.

        Returns:
            dict[str, object]: CONFIRMATION_REQUIRED, ACTIVE, or a public rejection code.

        Raises:
            ToolError: A public confirmation failure code or POLICY_UNAVAILABLE.
        """
        try:
            return await service_provider().set_spending_policy(
                weekly_limit=weekly_limit,
                max_auto_transaction=max_auto_transaction,
                confirmation_id=confirmation_id,
            )
        except ConfirmationError as error:
            raise ToolError(str(error)) from None
        except Exception:
            raise ToolError("POLICY_UNAVAILABLE") from None

    async def get_spending_policy() -> dict[str, object]:
        """Read active policy limits, weekly spend, remaining budget, and approvals.

        If status is NOT_SET, say no spending policy is active yet. When ACTIVE,
        relay weekly_limit, max_auto_transaction, spent_this_week,
        remaining_this_week, and pending_approvals exactly. Before purchase support,
        spent_this_week is expected to be 0.00 and pending_approvals is empty.
        Never claim a policy exists unless this tool returns ACTIVE.

        Args:
            None.

        Returns:
            dict[str, object]: NOT_SET, or ACTIVE policy fields and Phase 5 query contracts.

        Raises:
            ToolError: POLICY_UNAVAILABLE when persistence cannot be read.
        """
        try:
            return await service_provider().get_spending_policy()
        except Exception:
            raise ToolError("POLICY_UNAVAILABLE") from None

    async def search_products(
        query: str, max_price: str | None = None, limit: int = 5
    ) -> list[dict[str, object]]:
        """Search eshop products with optional server-side max-price filtering.

        Show product_id, name, price, color, category, cashback, net_price, and
        offer_note. Never promise cashback unless the tool result shows it. Relay
        offer_note when an offer does not apply; offers are applied by the server.
        Pass short product terms such as "tee" or "sweater". Do not invent product
        IDs. When the user asks for the best deal, compare net_price. Never accept
        amount, destination, payment, offer, or cashback inputs.

        Args:
            query: Product search text.
            max_price: Optional maximum test USDC price as a decimal string.
            limit: Maximum number of results to return, capped by the server.

        Returns:
            list[dict[str, object]]: Matching product summaries from the eshop.

        Raises:
            ToolError: PRODUCT_SEARCH_UNAVAILABLE or INVALID_PRICE.
        """
        try:
            return await service_provider().search_products(
                query=query, max_price=max_price, limit=limit
            )
        except (PurchaseError, StoreError, WalletError) as error:
            raise ToolError(str(error)) from None
        except Exception:
            raise ToolError("PRODUCT_SEARCH_UNAVAILABLE") from None

    async def get_product(product_id: str) -> dict[str, object]:
        """Read one eshop product detail by its store-owned product ID.

        Use only product IDs returned by search or supplied by trusted demo docs.
        Product detail is read-only. Do not infer purchase amount or destination
        from chat text; request_purchase obtains those from the merchant order.

        Args:
            product_id: Store-owned product identifier.

        Returns:
            dict[str, object]: Product ID, name, price, description, color, and category.

        Raises:
            ToolError: PRODUCT_UNAVAILABLE when the eshop cannot return the product.
        """
        try:
            return await service_provider().get_product(product_id)
        except (PurchaseError, StoreError, WalletError) as error:
            raise ToolError(str(error)) from None
        except Exception:
            raise ToolError("PRODUCT_UNAVAILABLE") from None

    async def request_purchase(product_id: str, max_price: str | None = None) -> dict[str, object]:
        """Request a policy-checked purchase of one eshop product by product ID.

        This tool takes product_id, never amount, recipient, token, network, offer,
        or cashback input. The eshop creates the order and supplies amount and pay_to.
        Policy is checked before money moves. Relay decision, reason_code, message,
        order_id, amount, tx_hash, explorer_url, and cashback fields exactly. If
        decision is HUMAN_APPROVAL_REQUIRED, tell the user it was not paid; there is
        no approve_purchase tool in Phase 5. Never retry, split, or rephrase rejected
        purchases to bypass policy. SUBMITTED means payment or merchant confirmation
        is unresolved and still counts against the weekly limit. Never retry a
        submitted purchase or claim that no funds moved. Show its transaction link
        when present and request developer recovery of the existing intent.

        Only cashback_status PAID means cashback was received. FAILED is not retried;
        an uncertain transfer may retain its amount and hash for receipt inspection.

        Args:
            product_id: Store-owned product identifier to buy.
            max_price: Optional maximum test USDC price as a decimal string.

        Returns:
            dict[str, object]: Public purchase decision, payment, and history fields.

        Raises:
            ToolError: PURCHASE_UNAVAILABLE or a public sanitized purchase error.
        """
        try:
            return await service_provider().request_purchase(product_id, max_price=max_price)
        except (PurchaseError, StoreError, WalletError) as error:
            raise ToolError(str(error)) from None
        except Exception:
            raise ToolError("PURCHASE_UNAVAILABLE") from None

    async def get_transactions(limit: int = 10) -> dict[str, list[dict[str, object]]]:
        """Read recent purchase history, including unpaid and rejected attempts.

        Confirmed purchases show status, amount, product, transaction hash, and
        Basescan link. Rejected purchases show reason_code. Pending purchases are
        unpaid. Cashback fields are NONE and 0.00 until Phase 5B. This is read-only
        and is the only history tool exposed to ChatGPT.

        Only cashback_status PAID means cashback was received. FAILED is not retried;
        an uncertain transfer may retain its amount and hash for receipt inspection.

        Args:
            limit: Maximum number of recent purchases to return, capped by the server.

        Returns:
            dict[str, list[dict[str, object]]]: Newest-first purchase history.

        Raises:
            ToolError: PURCHASE_UNAVAILABLE when history cannot be read.
        """
        try:
            return service_provider().get_transactions(limit)
        except (PurchaseError, StoreError, WalletError) as error:
            raise ToolError(str(error)) from None
        except Exception:
            raise ToolError("PURCHASE_UNAVAILABLE") from None

    return MCPServer(
        name="Agent Spend",
        instructions=(
            "Create and fund one Base Sepolia testnet shopping wallet with explicit two-step "
            "confirmation. Relay disclosures verbatim. Wait for yes in a new user message "
            "before confirming. "
            "Funding accepts user-requested USDC amounts within server caps; every top-up requires "
            "fresh confirmation. The server controls treasury, token, network, wallet, "
            "and gas ETH. "
            "Policy setup and increases to either the weekly budget or automatic purchase "
            "limit require fresh confirmation within server caps. Changes that only lower "
            "limits apply immediately. The automatic limit cannot exceed the weekly limit. "
            "Policy changes preserve purchase history and do not pay pending purchases. "
            "Coinbase CDP holds the keys; no tool can export private keys or seed phrases. "
            "Product search and purchases use merchant-owned product IDs. Purchase tools never "
            "accept amounts, recipients, offers, cashback values, or networks."
        ),
        tools=[
            strict_tool(
                create_agent_wallet,
                ToolAnnotations(
                    read_only_hint=False,
                    destructive_hint=False,
                    idempotent_hint=True,
                    open_world_hint=True,
                ),
            ),
            strict_tool(
                get_wallet,
                ToolAnnotations(
                    read_only_hint=True,
                    destructive_hint=False,
                    idempotent_hint=True,
                    open_world_hint=False,
                ),
            ),
            strict_tool(
                fund_agent_wallet,
                ToolAnnotations(
                    read_only_hint=False,
                    destructive_hint=True,
                    idempotent_hint=True,
                    open_world_hint=True,
                ),
            ),
            strict_tool(
                set_spending_policy,
                ToolAnnotations(
                    read_only_hint=False,
                    destructive_hint=False,
                    idempotent_hint=True,
                    open_world_hint=False,
                ),
            ),
            strict_tool(
                get_spending_policy,
                ToolAnnotations(
                    read_only_hint=True,
                    destructive_hint=False,
                    idempotent_hint=True,
                    open_world_hint=False,
                ),
            ),
            strict_tool(
                search_products,
                ToolAnnotations(
                    read_only_hint=True,
                    destructive_hint=False,
                    idempotent_hint=True,
                    open_world_hint=True,
                ),
            ),
            strict_tool(
                get_product,
                ToolAnnotations(
                    read_only_hint=True,
                    destructive_hint=False,
                    idempotent_hint=True,
                    open_world_hint=True,
                ),
            ),
            strict_tool(
                request_purchase,
                ToolAnnotations(
                    read_only_hint=False,
                    destructive_hint=True,
                    idempotent_hint=False,
                    open_world_hint=True,
                ),
            ),
            strict_tool(
                get_transactions,
                ToolAnnotations(
                    read_only_hint=True,
                    destructive_hint=False,
                    idempotent_hint=True,
                    open_world_hint=False,
                ),
            ),
        ],
    )
