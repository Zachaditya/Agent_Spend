"""Shared fake CDP resources and isolated Agent Spend test settings."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cdp.openapi_client.errors import ApiError

from app.config import Settings
from app.confirmations import ConfirmationStore
from app.db import Database
from app.wallet import WalletService

ADDRESS = "0x1111111111111111111111111111111111111111"
TREASURY_ADDRESS = "0x2222222222222222222222222222222222222222"
MERCHANT_ADDRESS = "0x3333333333333333333333333333333333333333"
SECRET = "test-credential-must-never-appear"
TX_HASH = "0x" + "ab" * 32
ETH_TX_HASH = "0x" + "cd" * 32


class FakeFunction:
    """Represent a minimal synchronous Web3 contract function call."""

    def __init__(self, value):
        """Store the deterministic call result.

        Args:
            value: The value returned when the fake function is called.

        Returns:
            None.
        """
        self.value = value

    def call(self):
        """Return the stored fake chain value.

        Args:
            None.

        Returns:
            object: The deterministic fake call result.
        """
        return self.value


class FakeContractFunctions:
    """Expose only the ERC-20 balanceOf function used by Agent Spend."""

    def __init__(self, balances):
        """Bind the address-indexed fake token balances.

        Args:
            balances: Mapping of checksum or lowercase addresses to token base units.

        Returns:
            None.
        """
        self.balances = balances

    def balanceOf(self, address):
        """Build a fake balanceOf call for the requested address.

        Args:
            address: The address whose fake token balance should be returned.

        Returns:
            FakeFunction: A call object returning the configured balance or zero.
        """
        return FakeFunction(self.balances.get(address, self.balances.get(address.lower(), 0)))


class FakeContract:
    """Provide the subset of a Web3 contract needed by balance reads."""

    def __init__(self, balances):
        """Create a fake contract with token balance functions.

        Args:
            balances: Mapping of addresses to token base-unit balances.

        Returns:
            None.
        """
        self.functions = FakeContractFunctions(balances)


class FakeEth:
    """Provide deterministic ETH balances, receipts, and ERC-20 contracts."""

    def __init__(self, web3):
        """Bind fake chain state to the parent fake Web3 object.

        Args:
            web3: The parent fake Web3 object.

        Returns:
            None.
        """
        self.web3 = web3
        self.gas_price = 1_000_000_000

    def estimate_gas(self, transaction):
        """Estimate deterministic transaction gas for treasury preflight tests.

        Args:
            transaction: Server-generated token or native transfer transaction.

        Returns:
            int: Fixed token-call or native-transfer gas estimate.
        """
        return 65_000 if "data" in transaction else 21_000

    def get_balance(self, address):
        """Return the configured wei balance for an address.

        Args:
            address: The address whose fake ETH balance should be returned.

        Returns:
            int: The configured wei balance, or zero.
        """
        return self.web3.eth_balances.get(address, self.web3.eth_balances.get(address.lower(), 0))

    def contract(self, address, abi):
        """Return a fake ERC-20 contract without inspecting ABI details.

        Args:
            address: The token contract address.
            abi: The ABI passed by production code.

        Returns:
            FakeContract: A contract exposing balanceOf.
        """
        return FakeContract(self.web3.usdc_balances)

    def wait_for_transaction_receipt(self, tx_hash, timeout=60):
        """Return the configured fake receipt for a transaction hash.

        Args:
            tx_hash: The public transaction hash to look up.
            timeout: The timeout value forwarded by production code.

        Returns:
            SimpleNamespace: A fake receipt with a status attribute.
        """
        return self.web3.receipts.get(tx_hash, SimpleNamespace(status=1))


class FakeWeb3:
    """Provide the tiny synchronous Web3 surface needed for local tests."""

    def __init__(self):
        """Initialize empty fake chain state and an eth namespace.

        Args:
            None.

        Returns:
            None.
        """
        self.usdc_balances = {}
        self.eth_balances = {}
        self.receipts = {}
        self.eth = FakeEth(self)


class FakeCdp:
    """Provide the CDP account API without credentials or network access."""

    def __init__(self):
        """Initialize observable account creation and resource cleanup.

        Args:
            None.

        Returns:
            None.
        """
        self.treasury = SimpleNamespace(
            address=TREASURY_ADDRESS,
            secret=SECRET,
            transfer=AsyncMock(side_effect=[TX_HASH, ETH_TX_HASH]),
            request_faucet=AsyncMock(return_value=TX_HASH),
        )
        self.evm = SimpleNamespace(
            create_account=AsyncMock(return_value=SimpleNamespace(address=ADDRESS, secret=SECRET)),
            get_account=AsyncMock(side_effect=ApiError(404, "not_found", "Account not found")),
            get_or_create_account=AsyncMock(return_value=self.treasury),
        )
        self.entered = False
        self.closed = False

    async def __aenter__(self):
        """Mark the fake provider as open.

        Args:
            None.

        Returns:
            FakeCdp: This provider.
        """
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        """Record resource cleanup, including failed startup.

        Args:
            *exc: Context manager exception information.

        Returns:
            None.
        """
        self.closed = True


@pytest.fixture
def settings(tmp_path):
    """Build settings that cannot access real credentials or production data.

    Args:
        tmp_path: Pytest's isolated temporary directory.

    Returns:
        Settings: A testnet configuration with synthetic credentials.
    """
    return Settings(
        cdp_api_key_id=SECRET,
        cdp_api_key_secret=SECRET,
        cdp_wallet_secret=SECRET,
        db_path=tmp_path / "wallet.db",
        public_host="agent-spend-test.ngrok-free.app",
    )


@pytest.fixture
def web3():
    """Provide deterministic fake Base Sepolia balances and receipts.

    Args:
        None.

    Returns:
        FakeWeb3: A synchronous fake Web3 object.
    """
    chain = FakeWeb3()
    chain.usdc_balances[TREASURY_ADDRESS] = 25_000_000
    chain.eth_balances[TREASURY_ADDRESS] = 1_000_000_000_000_000
    return chain


@pytest.fixture
def cdp():
    """Provide a fresh observable fake wallet provider.

    Args:
        None.

    Returns:
        FakeCdp: A provider that never makes external calls.
    """
    return FakeCdp()


@pytest.fixture
def database(tmp_path):
    """Open and close a separate persistent wallet database per test.

    Args:
        tmp_path: Pytest's isolated temporary directory.

    Yields:
        Database: An initialized SQLite repository.
    """
    db = Database(tmp_path / "wallet.db")
    yield db
    db.close()


@pytest.fixture
def service(database, cdp, settings, web3):
    """Assemble wallet orchestration with isolated confirmation state.

    Args:
        database: The test SQLite repository.
        cdp: The fake wallet provider.
        settings: The synthetic validated application settings.
        web3: The deterministic fake Base Sepolia reader.

    Returns:
        WalletService: The service under test.
    """
    return WalletService(database, cdp, ConfirmationStore(), settings=settings, web3=web3)
