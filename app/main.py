"""Serve MCP at /mcp with managed CDP, SQLite, and Base Sepolia startup checks."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext

import httpx
from cdp import CdpClient
from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Mount

from app.config import Settings, load_settings
from app.confirmations import ConfirmationStore
from app.db import Database
from app.purchase import PurchaseService
from app.store import StoreClient
from app.tools import build_mcp
from app.wallet import WalletService


async def check_chain(settings: Settings, client: httpx.AsyncClient | None = None) -> None:
    """Fail closed unless the configured RPC reports Base Sepolia chain ID 84532.

    Args:
        settings: Validated settings containing the RPC endpoint and fixed chain ID.
        client: Optional caller-owned HTTP client, primarily for isolated verification.

    Returns:
        None.

    Raises:
        RuntimeError: RPC_UNAVAILABLE on transport or malformed-response failure,
            or RPC_CHAIN_MISMATCH if a different chain is returned. No URL or body is echoed.
    """
    context = nullcontext(client) if client is not None else httpx.AsyncClient(timeout=10)
    async with context as rpc_client:
        try:
            response = await rpc_client.post(
                settings.base_sepolia_rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
            )
            response.raise_for_status()
            result = response.json()["result"]
            if not isinstance(result, str) or not result.startswith("0x"):
                raise ValueError("Malformed chain ID")
            chain_id = int(result, 16)
        except Exception:
            raise RuntimeError("RPC_UNAVAILABLE: could not verify Base Sepolia") from None
        if chain_id != settings.chain_id:
            raise RuntimeError("RPC_CHAIN_MISMATCH: expected chain ID 84532")


def build_cdp_client(settings: Settings) -> CdpClient:
    """Initialize the server-side CDP SDK with credential logging disabled.

    Constructor failures are sanitized by the lifespan's initialization boundary.

    Args:
        settings: Validated provider credentials kept within the application process.

    Returns:
        CdpClient: An unentered client to manage with FastAPI's lifespan.
    """
    return CdpClient(
        api_key_id=settings.cdp_api_key_id,
        api_key_secret=settings.cdp_api_key_secret,
        wallet_secret=settings.cdp_wallet_secret,
        debugging=False,
    )


async def close_cdp_client(client: CdpClient) -> None:
    """Close CDP without allowing SDK cleanup errors to disclose private material.

    Args:
        client: The provider client entered during application startup.

    Returns:
        None.

    Raises:
        RuntimeError: CDP_CLEANUP_FAILED with no underlying provider error text.
    """
    try:
        await client.__aexit__(None, None, None)
    except Exception:
        raise RuntimeError("CDP_CLEANUP_FAILED") from None


def transport_security(public_host: str) -> TransportSecuritySettings:
    """Allow loopback development hosts and only the explicitly configured public host.

    Args:
        public_host: The validated ngrok hostname, or empty for local development.

    Returns:
        TransportSecuritySettings: DNS rebinding protection with explicit host/origin lists.
    """
    hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if public_host:
        hosts.extend([public_host, public_host + ":443"])
        origins.append("https://" + public_host)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def create_app(
    settings: Settings | None = None,
    *,
    cdp_factory: Callable[[Settings], CdpClient] = build_cdp_client,
    rpc_checker: Callable[[Settings], Awaitable[None]] = check_chain,
) -> FastAPI:
    """Build the MCP-only FastAPI application without opening resources at import time.

    Args:
        settings: Optional validated settings; otherwise load the project .env at startup.
        cdp_factory: Provider factory, replaceable by a fake in acceptance tests.
        rpc_checker: Async testnet guard, replaceable for isolated transport tests.

    Returns:
        FastAPI: An application serving exactly /mcp through Streamable HTTP.
    """

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """Check the chain, open resources, and run the MCP session manager together.

        Args:
            application: The FastAPI instance whose resources and routes are managed.

        Yields:
            None: Startup is complete and the MCP transport can accept requests.

        Returns:
            AsyncIterator[None]: Lifespan context yielding once while resources are active.

        Raises:
            RuntimeError: Testnet verification or CDP initialization fails.
            sqlite3.Error: The public-pointer database cannot be initialized.
        """
        config = settings or load_settings()
        await rpc_checker(config)
        async with AsyncExitStack() as stack:
            database = Database(config.db_path)
            stack.callback(database.close)
            try:
                cdp = await cdp_factory(config).__aenter__()
            except Exception:
                raise RuntimeError("CDP_INITIALIZATION_FAILED") from None
            stack.push_async_callback(close_cdp_client, cdp)
            wallet_service = WalletService(database, cdp, ConfirmationStore(), settings=config)
            store_client = StoreClient(config.eshop_url, config.agent_api_key)
            stack.push_async_callback(store_client.aclose)
            wallet_service.purchase_service = PurchaseService(
                database=database,
                cdp=cdp,
                store=store_client,
                merchant_address=config.merchant_address,
                usdc_contract_address=config.usdc_contract_address,
                web3=wallet_service.web3,
            )
            application.state.wallet_service = wallet_service
            application.state.store_client = store_client

            def get_service() -> WalletService:
                """Resolve the service from the instance managed by this lifespan.

                Args:
                    None.

                Returns:
                    WalletService: The service owned by the active FastAPI instance.

                Raises:
                    AttributeError: Called outside the application's active lifespan.
                """
                return application.state.wallet_service

            mcp = build_mcp(get_service)
            http_app = mcp.streamable_http_app(
                streamable_http_path="/mcp",
                json_response=True,
                stateless_http=True,
                transport_security=transport_security(config.public_host),
            )
            route = Mount("/", app=http_app)
            application.router.routes.append(route)
            try:
                async with mcp.session_manager.run():
                    yield
            finally:
                application.router.routes.remove(route)
                del application.state.store_client
                del application.state.wallet_service

    return FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


app = create_app()
