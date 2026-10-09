"""Create, inspect, and fund one CDP-managed Base Sepolia shopper wallet."""

import asyncio
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from typing import Any
from uuid import uuid4

from cdp import CdpClient
from cdp.openapi_client.errors import ApiError
from web3 import Web3

from app.config import Settings
from app.confirmations import ConfirmationError, ConfirmationStore
from app.db import Database, FundingConflict
from app.policy import DecisionReason, Policy, validate_policy_change

NETWORK = "base-sepolia"
DEFAULT_USDC_CONTRACT_ADDRESS = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
DEFAULT_TREASURY_ACCOUNT_NAME = "agent-spend-treasury"
DEFAULT_STARTER_USDC = Decimal("25")
DEFAULT_STARTER_ETH = Decimal("0.0005")
FUNDING_FEE_RESERVE_WEI = 10**13
USDC_BALANCE_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]
USDC_TRANSFER_ABI = [
    {
        "type": "function",
        "name": "transfer",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
    }
]
DISCLOSURE = (
    "Agent Spend will create a Base Sepolia testnet wallet for shopping. "
    "Coinbase CDP will hold the keys. No real funds are used. Reply yes to create it."
)


class WalletError(Exception):
    """Represent a sanitized wallet operation failure suitable for tool output."""


def _decimal(value: Decimal | str) -> Decimal:
    """Convert a string or Decimal into a finite Decimal for unit math.

    Args:
        value: The numeric value supplied by settings or a helper caller.

    Returns:
        Decimal: The finite decimal value.

    Raises:
        ValueError: The value cannot be represented as a finite Decimal.
    """
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid decimal amount") from None
    if not parsed.is_finite():
        raise ValueError("Invalid decimal amount")
    return parsed


def cents_to_usdc_base_units(cents: int) -> int:
    """Convert whole cents to USDC base units with six token decimals.

    Args:
        cents: The dollar-cent amount selected by server configuration.

    Returns:
        int: The equivalent USDC base-unit amount.

    Raises:
        ValueError: The cent value is negative.
    """
    if cents < 0:
        raise ValueError("USDC cents cannot be negative")
    return cents * 10_000


def usdc_decimal_to_cents(amount: Decimal | str) -> int:
    """Convert a decimal USDC amount into exact whole cents.

    Args:
        amount: The configured USDC amount, such as 25 or 25.50.

    Returns:
        int: The amount in whole cents.

    Raises:
        ValueError: The amount is negative or has more than two decimal places.
    """
    parsed = _decimal(amount)
    if parsed < 0:
        raise ValueError("USDC amount cannot be negative")
    cents = parsed * Decimal("100")
    if cents != cents.to_integral_value():
        raise ValueError("USDC amount must resolve to whole cents")
    return int(cents)


def eth_to_wei(amount: Decimal | str) -> int:
    """Convert a decimal ETH amount into exact wei.

    Args:
        amount: The configured ETH amount, such as 0.0005.

    Returns:
        int: The amount in wei.

    Raises:
        ValueError: The amount is negative or has sub-wei precision.
    """
    parsed = _decimal(amount)
    if parsed < 0:
        raise ValueError("ETH amount cannot be negative")
    wei = parsed * Decimal(10) ** 18
    if wei != wei.to_integral_value():
        raise ValueError("ETH amount must resolve to whole wei")
    return int(wei)


def format_usdc_balance(base_units: int) -> str:
    """Format a USDC base-unit balance for chat display with two decimals.

    Args:
        base_units: The USDC balance in six-decimal base units.

    Returns:
        str: A dollar-style USDC string such as 25.00.
    """
    return f"{Decimal(base_units) / Decimal(1_000_000):.2f}"


def format_eth_balance(wei: int) -> str:
    """Format a wei balance as a compact decimal ETH string.

    Args:
        wei: The ETH balance in wei.

    Returns:
        str: A non-scientific decimal string without unnecessary trailing zeroes.
    """
    ether = Decimal(wei) / (Decimal(10) ** 18)
    if ether == 0:
        return "0"
    return format(ether.normalize(), "f")


def format_cents(cents: int) -> str:
    """Format a whole-cent amount as a two-decimal policy display string.

    Args:
        cents: Non-negative or already-clamped whole-cent amount.

    Returns:
        str: A fixed two-decimal string such as 25.00.
    """
    return f"{Decimal(cents) / Decimal(100):.2f}"


def build_web3(rpc_url: str) -> Web3:
    """Build the synchronous Web3 client used for public Base Sepolia reads.

    Args:
        rpc_url: The validated HTTP(S) JSON-RPC endpoint.

    Returns:
        Web3: A Web3 instance backed by the configured HTTP provider.
    """
    return Web3(Web3.HTTPProvider(rpc_url))


def get_explorer_url(address_or_tx: str) -> str:
    """Build a Base Sepolia explorer URL for validated public identifiers only.

    Args:
        address_or_tx: A 20-byte EVM address or 32-byte transaction hash in hex.

    Returns:
        str: A Basescan address or transaction URL on Base Sepolia.

    Raises:
        ValueError: The value is neither a public address nor a transaction hash.
    """
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", address_or_tx):
        kind = "address"
    elif re.fullmatch(r"0x[0-9a-fA-F]{64}", address_or_tx):
        kind = "tx"
    else:
        raise ValueError("Invalid public address or transaction hash")
    return f"https://sepolia.basescan.org/{kind}/{address_or_tx}"


def get_usdc_balance(
    address: str,
    web3: Web3 | Any | None = None,
    usdc_contract_address: str = DEFAULT_USDC_CONTRACT_ADDRESS,
) -> int:
    """Read an address's Base Sepolia USDC balance from the token contract.

    Args:
        address: The public EVM address whose USDC balance should be read.
        web3: Optional Web3-compatible client; defaults to the public Base Sepolia RPC.
        usdc_contract_address: The Base Sepolia USDC contract address.

    Returns:
        int: The token balance in USDC base units.

    Raises:
        ValueError: The address or token contract address is malformed.
        Exception: The underlying RPC or contract call fails.
    """
    if not Web3.is_address(address):
        raise ValueError("Invalid wallet address")
    if not Web3.is_address(usdc_contract_address):
        raise ValueError("Invalid USDC contract address")
    chain = web3 or build_web3("https://sepolia.base.org")
    checksum = Web3.to_checksum_address(address)
    contract = chain.eth.contract(
        address=Web3.to_checksum_address(usdc_contract_address), abi=USDC_BALANCE_ABI
    )
    return int(contract.functions.balanceOf(checksum).call())


def get_eth_balance(address: str, web3: Web3 | Any | None = None) -> int:
    """Read an address's Base Sepolia ETH balance.

    Args:
        address: The public EVM address whose ETH balance should be read.
        web3: Optional Web3-compatible client; defaults to the public Base Sepolia RPC.

    Returns:
        int: The ETH balance in wei.

    Raises:
        ValueError: The address is malformed.
        Exception: The underlying RPC call fails.
    """
    if not Web3.is_address(address):
        raise ValueError("Invalid wallet address")
    chain = web3 or build_web3("https://sepolia.base.org")
    return int(chain.eth.get_balance(Web3.to_checksum_address(address)))


def wallet_result(status: str, address: str) -> dict[str, str]:
    """Project the fixed public wallet response fields.

    Args:
        status: The operation's public status code.
        address: The validated shopper account address.

    Returns:
        dict[str, str]: Status, address, fixed network, and public explorer URL.
    """
    return {
        "status": status,
        "address": address,
        "network": NETWORK,
        "explorer_url": get_explorer_url(address),
    }


class WalletService:
    """Create one wallet and serialize confirmed, audited grants and repeated top-ups."""

    def __init__(
        self,
        database: Database,
        cdp: CdpClient,
        confirmations: ConfirmationStore | None,
        *,
        settings: Settings | None = None,
        web3: Web3 | Any | None = None,
    ) -> None:
        """Bind persistence, provider access, confirmation storage, and chain reads.

        Args:
            database: The initialized single-wallet SQLite repository.
            cdp: The lifespan-managed CDP client; private methods are never exposed.
            confirmations: The process-local confirmation store, or None to create one.
            settings: Optional validated runtime settings for funding and RPC values.
            web3: Optional Web3-compatible reader for tests or alternate transports.

        Returns:
            None.
        """
        self.database = database
        self.cdp = cdp
        self.confirmations = confirmations or ConfirmationStore()
        self.usdc_contract_address = (
            settings.usdc_contract_address if settings else DEFAULT_USDC_CONTRACT_ADDRESS
        )
        self.treasury_account_name = (
            settings.treasury_account_name if settings else DEFAULT_TREASURY_ACCOUNT_NAME
        )
        self.starter_usdc = settings.starter_usdc if settings else DEFAULT_STARTER_USDC
        self.starter_eth = settings.starter_eth if settings else DEFAULT_STARTER_ETH
        self.starter_usdc_cents = usdc_decimal_to_cents(self.starter_usdc)
        self.starter_eth_wei = eth_to_wei(self.starter_eth)
        self.max_funding_cents = usdc_decimal_to_cents(
            settings.max_funding_usdc if settings else "100"
        )
        self.max_total_funding_cents = usdc_decimal_to_cents(
            settings.max_total_funding_usdc if settings else "500"
        )
        self.web3 = web3 or build_web3(
            settings.base_sepolia_rpc_url if settings else "https://sepolia.base.org"
        )
        self.purchase_service: Any | None = None
        self._creation_lock = asyncio.Lock()
        self._funding_lock = asyncio.Lock()
        self._policy_lock = asyncio.Lock()

    def _balance_fields(self, address: str) -> dict[str, str]:
        """Read and format live USDC and ETH balances for one public address.

        Args:
            address: The persisted shopper wallet address.

        Returns:
            dict[str, str]: Chat-displayable USDC and ETH balances.
        """
        return {
            "usdc_balance": format_usdc_balance(
                get_usdc_balance(address, self.web3, self.usdc_contract_address)
            ),
            "eth_balance": format_eth_balance(get_eth_balance(address, self.web3)),
        }

    def _funding_status(self) -> str:
        """Separate a previously funded wallet from the latest operation's outcome.

        Args:
            None.

        Returns:
            str: FUNDED, FUNDING_FAILED, FUNDING_SUBMITTED, or NOT_FUNDED.
        """
        if self.database.has_confirmed_funding():
            return "FUNDED"
        latest = self.database.get_latest_funding_event()
        if latest is None:
            return "NOT_FUNDED"
        if latest["status"] == "CONFIRMED":
            return "FUNDED"
        if latest["status"] == "FAILED":
            return "FUNDING_FAILED"
        return "FUNDING_SUBMITTED"

    def get_wallet(self) -> dict[str, str]:
        """Read public wallet metadata, live balances, and separate funding outcomes.

        Args:
            None.

        Returns:
            dict[str, str]: NOT_CREATED, or READY with address, balances, funding status,
                network, and explorer URL.
        """
        wallet = self.database.get_wallet()
        if not wallet:
            return {"status": "NOT_CREATED"}
        result = {
            **wallet_result("READY", wallet["address"]),
            **self._balance_fields(wallet["address"]),
            "funding_status": self._funding_status(),
        }
        latest = self.database.get_latest_funding_event()
        if latest:
            result["latest_funding_status"] = latest["status"]
            result["latest_funding_operation_id"] = latest["id"]
        return result

    async def create_agent_wallet(self, confirmation_id: str | None = None) -> dict[str, str]:
        """Require a separate confirmation before creating the single shopper wallet.

        Args:
            confirmation_id: The prior disclosure token; omit to request a disclosure.

        Returns:
            dict[str, str]: CONFIRMATION_REQUIRED with disclosure and ID, CREATED with
                public wallet metadata, or ALREADY_EXISTS for a repeat creation prompt.

        Raises:
            ConfirmationError: The supplied confirmation is invalid, expired, used,
                bound to another action or parameter set, or pending capacity is full.
            WalletError: The provider or persistence operation fails, without details.
        """
        params = {"network": NETWORK}
        async with self._creation_lock:
            if confirmation_id is not None:
                self.confirmations.consume_confirmation(
                    confirmation_id, "create_agent_wallet", params
                )
            wallet = self.database.get_wallet()
            if wallet:
                return wallet_result("ALREADY_EXISTS", wallet["address"])
            if confirmation_id is None:
                return {
                    "status": "CONFIRMATION_REQUIRED",
                    "confirmation_id": self.confirmations.create_confirmation(
                        "create_agent_wallet", params
                    ),
                    "disclosure": DISCLOSURE,
                }
            return await self.create_shopper_account()

    async def create_shopper_account(self) -> dict[str, str]:
        """Create and persist a server-selected CDP EVM account after confirmation.

        Look up only the durable reserved name before creating. A successful but
        unrecorded account is recovered even after the provider's replay window.
        The request UUID is reused when creating; it is not a key or credential.

        Args:
            None.

        Returns:
            dict[str, str]: CREATED with the account's validated public address.

        Raises:
            WalletError: WALLET_CREATION_FAILED if CDP or SQLite fails; the consumed
                confirmation cannot be reused and raw provider errors are suppressed.
        """
        try:
            request = self.database.reserve_wallet_creation()
            try:
                account = await self.cdp.evm.get_account(name=request["account_name"])
            except ApiError as error:
                if error.http_code != HTTPStatus.NOT_FOUND:
                    raise
                account = await self.cdp.evm.create_account(
                    name=request["account_name"], idempotency_key=request["request_id"]
                )
            if not Web3.is_address(account.address):
                raise ValueError("Invalid provider address")
            address = Web3.to_checksum_address(account.address)
            self.database.create_wallet_pointer(request["account_name"], address)
        except Exception:
            raise WalletError("WALLET_CREATION_FAILED") from None
        return wallet_result("CREATED", address)

    def _funding_confirmation_params(
        self, address: str, amount_cents: int, intent_id: str
    ) -> dict[str, str | int]:
        """Build the parameter set bound to a funding confirmation token.

        Args:
            address: The persisted shopper wallet address.
            amount_cents: Validated requested USDC amount in cents.
            intent_id: Unique operation UUID generated before the disclosure.

        Returns:
            dict[str, str | int]: Immutable operation parameters and wallet funding sequence.
        """
        initial = not self.database.has_confirmed_funding()
        return {
            "intent_id": intent_id,
            "wallet_address": address,
            "network": NETWORK,
            "chain_id": 84532,
            "usdc_contract_address": self.usdc_contract_address,
            "treasury_account_name": self.treasury_account_name,
            "usdc_amount_cents": amount_cents,
            "eth_amount_wei": str(self.starter_eth_wei if initial else 0),
            "funding_kind": "INITIAL" if initial else "TOP_UP",
            "funding_sequence": self.database.funding_sequence(address),
            "max_funding_cents": self.max_funding_cents,
            "max_total_funding_cents": self.max_total_funding_cents,
        }

    def _parse_funding_amount(self, amount_usdc: str | None) -> int:
        """Normalize a user-requested decimal string to exact positive USDC cents.

        Args:
            amount_usdc: Optional plain decimal string; omission selects STARTER_USDC.

        Returns:
            int: Positive whole cents without rounding or float conversion.

        Raises:
            WalletError: INVALID_FUNDING_AMOUNT for malformed or unsupported precision.
        """
        if amount_usdc is None:
            return self.starter_usdc_cents
        if not isinstance(amount_usdc, str) or not re.fullmatch(
            r"[0-9]{1,12}(?:\.[0-9]{1,2})?", amount_usdc
        ):
            raise WalletError("INVALID_FUNDING_AMOUNT")
        cents = usdc_decimal_to_cents(amount_usdc)
        if cents <= 0:
            raise WalletError("INVALID_FUNDING_AMOUNT")
        return cents

    def _funding_limits(self, address: str, amount_cents: int) -> dict[str, Any] | None:
        """Check configured caps and unresolved operations before issuing consent.

        Args:
            address: Persisted shopper destination.
            amount_cents: Requested positive USDC cents.

        Returns:
            dict[str, Any] | None: Public rejection, or None when funding may proceed.
        """
        unresolved = self.database.get_unresolved_funding(address)
        if unresolved:
            return {
                "status": "FUNDING_IN_PROGRESS",
                "latest_funding_event": unresolved,
                "message": (
                    "Developer must reconcile the unresolved operation before new funding."
                ),
            }
        if amount_cents > self.max_funding_cents:
            return {"status": "FUNDING_AMOUNT_ABOVE_CAP"}
        if (
            self.database.reserved_funding_cents(address) + amount_cents
            > self.max_total_funding_cents
        ):
            return {"status": "FUNDING_TOTAL_CAP_EXCEEDED"}
        return None

    def _funding_amount_fields(self, intent: dict[str, Any]) -> dict[str, str]:
        """Format the exact amounts and funding kind for disclosure or execution.

        Args:
            intent: Confirmed parameters or a persisted funding event.

        Returns:
            dict[str, str]: Exact USDC, ETH, and INITIAL/TOP_UP display fields.
        """
        return {
            "usdc_amount": format_usdc_balance(
                cents_to_usdc_base_units(intent["usdc_amount_cents"])
            ),
            "eth_amount": format_eth_balance(int(intent["eth_amount_wei"])),
            "funding_kind": intent["funding_kind"],
        }

    def _funding_disclosure(self, intent: dict[str, Any]) -> str:
        """Describe the exact treasury addition authorized by a funding intent.

        Args:
            intent: Validated and immutable funding parameters.

        Returns:
            str: Verbatim disclosure for a new user yes/no response.
        """
        fields = self._funding_amount_fields(intent)
        gas = (
            f" and {fields['eth_amount']} test ETH for gas" if int(intent["eth_amount_wei"]) else ""
        )
        gas_note = "" if gas else "No additional ETH will be sent. "
        return (
            f"Agent Spend will add {fields['usdc_amount']} test USDC{gas} to your existing "
            "Base Sepolia shopping wallet from the configured treasury. "
            f"{gas_note}No real funds are used. "
            "Reply yes to fund it."
        )

    def _funding_failed_result(
        self, message: str, event: dict[str, str | int | None] | None = None
    ) -> dict[str, Any]:
        """Build a sanitized funding failure response for ChatGPT and developers.

        Args:
            message: Public recovery guidance without provider exception details.
            event: Optional funding event with any public transaction hashes.

        Returns:
            dict[str, Any]: FUNDING_FAILED with recovery guidance and event context.
        """
        result: dict[str, Any] = {"status": "FUNDING_FAILED", "message": message}
        if event is not None:
            result["latest_funding_event"] = event
        return result

    async def get_treasury_account(self):
        """Get or create the configured CDP treasury account by server-owned name.

        Args:
            None.

        Returns:
            object: The CDP treasury account object with address and transfer methods.

        Raises:
            WalletError: TREASURY_NOT_READY when CDP cannot return a valid account.
        """
        try:
            account = await self.cdp.evm.get_or_create_account(name=self.treasury_account_name)
            if not Web3.is_address(account.address):
                raise ValueError("Invalid treasury address")
            return account
        except Exception:
            raise WalletError("TREASURY_NOT_READY") from None

    async def wait_for_receipt(self, tx_hash: str, timeout: int = 60):
        """Wait for a public transaction receipt using the configured Web3 reader.

        Args:
            tx_hash: The public transaction hash to verify.
            timeout: Maximum seconds to wait for the receipt.

        Returns:
            object: A Web3 receipt object or receipt-like mapping.

        Raises:
            Exception: The receipt is unavailable before the timeout.
        """
        return await asyncio.to_thread(
            self.web3.eth.wait_for_transaction_receipt, tx_hash, timeout=timeout
        )

    def _receipt_status(self, receipt) -> int:
        """Extract a numeric status from a Web3 receipt or receipt-like mapping.

        Args:
            receipt: A receipt object, mapping, or test double.

        Returns:
            int: The receipt status as returned by the chain.

        Raises:
            ValueError: The receipt has no status field.
        """
        if isinstance(receipt, dict):
            status = receipt.get("status")
        else:
            status = getattr(receipt, "status", None)
        if status is None:
            raise ValueError("Receipt status missing")
        return int(status)

    def _treasury_has_required_funds(
        self, treasury_address: str, address: str, amount_cents: int, eth_wei: int
    ) -> bool:
        """Check token funds, gas grants, estimated fees, and a fee reserve.

        Args:
            treasury_address: Validated configured treasury address.
            address: Persisted shopper destination.
            amount_cents: USDC cents still needing transfer; zero for gas-only recovery.
            eth_wei: Gas ETH still needing transfer; zero for USDC-only top-ups.

        Returns:
            bool: Whether live treasury balances cover the grants and estimated fees.

        Raises:
            Exception: Public RPC estimates or balances are unavailable.
        """
        treasury_usdc = get_usdc_balance(treasury_address, self.web3, self.usdc_contract_address)
        treasury_eth = get_eth_balance(treasury_address, self.web3)
        if (
            treasury_usdc < cents_to_usdc_base_units(amount_cents)
            or treasury_eth < eth_wei + FUNDING_FEE_RESERVE_WEI
        ):
            return False
        gas = 0
        if amount_cents:
            contract = Web3().eth.contract(
                address=Web3.to_checksum_address(self.usdc_contract_address), abi=USDC_TRANSFER_ABI
            )
            data = contract.encode_abi(
                "transfer",
                args=[Web3.to_checksum_address(address), cents_to_usdc_base_units(amount_cents)],
            )
            gas += self.web3.eth.estimate_gas(
                {"from": treasury_address, "to": self.usdc_contract_address, "data": data}
            )
        if eth_wei:
            gas += self.web3.eth.estimate_gas(
                {"from": treasury_address, "to": address, "value": eth_wei}
            )
        required_eth = eth_wei + 2 * gas * int(self.web3.eth.gas_price) + FUNDING_FEE_RESERVE_WEI
        return treasury_eth >= required_eth

    async def _funding_treasury(
        self, address: str, amount_cents: int, eth_wei: int
    ) -> tuple[Any | None, dict[str, Any] | None]:
        """Resolve the treasury and sanitize balance or fee preflight failures.

        Args:
            address: Persisted shopper destination.
            amount_cents: USDC cents required by the remaining operation legs.
            eth_wei: Gas ETH required by the remaining operation legs.

        Returns:
            tuple: Treasury account and no error, or None and a public failure result.
        """
        try:
            treasury = await self.get_treasury_account()
            enough = await asyncio.to_thread(
                self._treasury_has_required_funds, treasury.address, address, amount_cents, eth_wei
            )
            if not enough:
                amount = format_usdc_balance(cents_to_usdc_base_units(amount_cents))
                return None, {
                    "status": "INSUFFICIENT_TREASURY_FUNDS",
                    "message": (
                        f"Treasury needs {amount} test USDC, {format_eth_balance(eth_wei)} "
                        "test ETH for the grant, and ETH for estimated fees and reserve."
                    ),
                }
            return treasury, None
        except WalletError as error:
            return None, {
                "status": str(error),
                "message": "Treasury is not ready for demo funding.",
            }
        except Exception:
            return None, {
                "status": "TREASURY_NOT_READY",
                "message": "Treasury balances or fee estimates are unavailable.",
            }

    async def _send_funding_leg(self, event: dict[str, Any], token: str, treasury: Any) -> bool:
        """Submit a reserved leg once, persisting its attempt before remote signing.

        Args:
            event: Durable operation with destination and exact transfer amounts.
            token: Server-owned usdc or eth leg.
            treasury: Configured CDP treasury account.

        Returns:
            bool: True only when the submitted transaction has a successful receipt.

        Raises:
            FundingConflict: Another executor already claimed the leg.
            sqlite3.Error: Audit writes fail; the durable reservation still blocks retries.
        """
        attempt_id = self.database.begin_funding_leg(event["id"], token)
        amount = (
            cents_to_usdc_base_units(event["usdc_amount_cents"])
            if token == "usdc"
            else int(event["eth_amount_wei"])
        )
        try:
            tx_hash = str(
                await treasury.transfer(
                    to=event["wallet_address"], amount=amount, token=token, network=NETWORK
                )
            )
            self.database.record_funding_leg(event["id"], token, "SUBMITTED", tx_hash, attempt_id)
            get_explorer_url(tx_hash)
            receipt = await self.wait_for_receipt(tx_hash)
            receipt_status = self._receipt_status(receipt)
            if receipt_status not in {0, 1}:
                raise ValueError("Invalid receipt status")
        except Exception:
            self.database.record_funding_leg(event["id"], token, "UNKNOWN", attempt_id=attempt_id)
            return False
        confirmed = receipt_status == 1
        self.database.record_funding_leg(
            event["id"], token, "CONFIRMED" if confirmed else "FAILED", attempt_id=attempt_id
        )
        return confirmed

    def _funding_result(self, event: dict[str, Any]) -> dict[str, Any]:
        """Report an audited outcome without confusing uncertain transfers with failure.

        Args:
            event: Finalized durable funding event.

        Returns:
            dict[str, Any]: FUNDED with actual amounts and links, or recovery guidance.
        """
        if event["status"] != "CONFIRMED":
            return self._funding_failed_result(
                "Funding is incomplete or uncertain. Developer must reconcile this operation "
                "before retrying; do not resend confirmed transfers.",
                event,
            )
        result = {
            "status": "FUNDED",
            "operation_id": event["id"],
            "address": event["wallet_address"],
            "network": NETWORK,
            **self._funding_amount_fields(event),
        }
        for token in ("usdc", "eth"):
            if event[token + "_tx_hash"]:
                result[token + "_tx_hash"] = event[token + "_tx_hash"]
                result[token + "_explorer_url"] = get_explorer_url(event[token + "_tx_hash"])
        try:
            result.update(self._balance_fields(event["wallet_address"]))
        except Exception:
            result["balance_status"] = "UNAVAILABLE"
        return result

    async def fund_wallet(self, intent: dict[str, Any]) -> dict[str, Any]:
        """Reserve and execute one confirmed initial grant or USDC-only top-up.

        Args:
            intent: Exact parameters already validated and consumed by the confirmation store.

        Returns:
            dict[str, Any]: Audited success, treasury rejection, or unresolved funding status.

        Raises:
            sqlite3.Error: Reservation or audit persistence fails; no implicit retry occurs.
        """
        treasury, error = await self._funding_treasury(
            intent["wallet_address"], intent["usdc_amount_cents"], int(intent["eth_amount_wei"])
        )
        if error:
            return error
        try:
            event = self.database.reserve_funding(intent, self.max_total_funding_cents)
        except FundingConflict as error:
            return {"status": str(error)}
        if await self._send_funding_leg(event, "usdc", treasury) and int(event["eth_amount_wei"]):
            await self._send_funding_leg(event, "eth", treasury)
        return self._funding_result(self.database.finalize_funding(event["id"]))

    async def fund_agent_wallet(
        self, confirmation_id: str | None = None, amount_usdc: str | None = None
    ) -> dict[str, Any]:
        """Require fresh, amount-bound consent for every initial grant and top-up.

        Args:
            confirmation_id: Omit to request disclosure; supply the ID after a new user yes.
            amount_usdc: Optional positive decimal string for a new request. On confirmation,
                omission uses the stored intent, while a supplied value must match it.

        Returns:
            dict[str, Any]: CONFIRMATION_REQUIRED, FUNDED, or a public funding rejection.

        Raises:
            WalletError: INVALID_FUNDING_AMOUNT for invalid decimal-string inputs.
            ConfirmationError: Invalid, expired, used, changed, or stale consent.
        """
        async with self._funding_lock:
            wallet = self.database.get_wallet()
            if not wallet:
                return {
                    "status": "NO_WALLET",
                    "message": "Create the shopping wallet before funding it.",
                }
            if confirmation_id is not None:
                intent = self.confirmations.get_confirmation_params(
                    confirmation_id, "fund_agent_wallet"
                )
                if (
                    amount_usdc is not None
                    and self._parse_funding_amount(amount_usdc) != intent["usdc_amount_cents"]
                ):
                    raise ConfirmationError("CONFIRMATION_MISMATCH")
                current = self._funding_confirmation_params(
                    wallet["address"], intent["usdc_amount_cents"], intent["intent_id"]
                )
                self.confirmations.consume_confirmation(
                    confirmation_id, "fund_agent_wallet", current
                )
                error = self._funding_limits(wallet["address"], intent["usdc_amount_cents"])
                return error if error else await self.fund_wallet(intent)
            amount_cents = self._parse_funding_amount(amount_usdc)
            error = self._funding_limits(wallet["address"], amount_cents)
            if error:
                return error
            intent = self._funding_confirmation_params(
                wallet["address"], amount_cents, str(uuid4())
            )
            return {
                "status": "CONFIRMATION_REQUIRED",
                "confirmation_id": self.confirmations.create_confirmation(
                    "fund_agent_wallet", intent
                ),
                "disclosure": self._funding_disclosure(intent),
                **self._funding_amount_fields(intent),
            }

    def _parse_policy_amount(self, amount: Decimal | int | str) -> int | None:
        """Normalize a policy amount to exact non-negative whole cents.

        Args:
            amount: A decimal string, Decimal, or integer dollar amount.

        Returns:
            int | None: Whole cents, or None when the input is malformed or imprecise.
        """
        if isinstance(amount, bool):
            return None
        if isinstance(amount, str):
            if not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,2})?", amount):
                return None
            try:
                parsed = _decimal(amount)
            except ValueError:
                return None
        elif isinstance(amount, int | Decimal):
            try:
                parsed = _decimal(amount)
            except ValueError:
                return None
        else:
            return None
        cents = parsed * Decimal("100")
        if parsed < 0 or cents != cents.to_integral_value():
            return None
        return int(cents)

    def _active_policy(self) -> Policy | None:
        """Read the active policy row and convert it to the pure policy dataclass.

        Args:
            None.

        Returns:
            Policy | None: Active policy limits, or None when policy is unset.
        """
        row = self.database.get_policy()
        if row is None:
            return None
        return Policy(
            weekly_limit_cents=row["weekly_limit_cents"],
            max_auto_tx_cents=row["max_auto_tx_cents"],
        )

    def _policy_rejection(self, reason: DecisionReason) -> dict[str, str]:
        """Build the structured public response for a refused policy change.

        Args:
            reason: Public reason code from the policy validator.

        Returns:
            dict[str, str]: Status and reason_code with identical stable codes.
        """
        return {"status": reason.value, "reason_code": reason.value}

    def _policy_disclosure(self, weekly_limit_cents: int, max_auto_tx_cents: int) -> str:
        """Describe policy setup or a limit increase before any durable change.

        Args:
            weekly_limit_cents: Requested weekly budget in cents.
            max_auto_tx_cents: Requested automatic transaction limit in cents.

        Returns:
            str: Verbatim disclosure ChatGPT must show before confirmation.
        """
        return (
            "Agent Spend will set a spending policy for this Base Sepolia shopping wallet: "
            f"weekly limit {format_cents(weekly_limit_cents)} test USDC and automatic "
            f"approval for purchases up to {format_cents(max_auto_tx_cents)} test USDC. "
            "Weekly and automatic approval limits can be raised within server caps "
            "after a new confirmation. Changes that only lower limits apply immediately. "
            "Existing purchases still count toward the rolling seven-day budget. "
            "Reply yes to set this spending policy."
        )

    def _policy_fields(self, weekly_limit_cents: int, max_auto_tx_cents: int) -> dict[str, str]:
        """Format exact policy amounts for MCP responses.

        Args:
            weekly_limit_cents: Weekly budget in whole cents.
            max_auto_tx_cents: Automatic transaction limit in whole cents.

        Returns:
            dict[str, str]: Display values with two decimal places.
        """
        return {
            "weekly_limit": format_cents(weekly_limit_cents),
            "max_auto_transaction": format_cents(max_auto_tx_cents),
        }

    def _policy_confirmation_params(
        self,
        weekly_limit_cents: int,
        max_auto_tx_cents: int,
        current_policy: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Bind requested limits and the current policy revision to confirmation.

        Args:
            weekly_limit_cents: Requested weekly budget in whole cents.
            max_auto_tx_cents: Requested automatic transaction limit in whole cents.
            current_policy: Persisted policy snapshot, including its update timestamp.

        Returns:
            dict[str, Any]: Canonical limits and baseline for confirmation hashing.
        """
        return {
            "weekly_limit_cents": weekly_limit_cents,
            "max_auto_tx_cents": max_auto_tx_cents,
            "current_policy": current_policy,
        }

    async def set_spending_policy(
        self,
        weekly_limit: Decimal | int | str,
        max_auto_transaction: Decimal | int | str,
        confirmation_id: str | None = None,
    ) -> dict[str, str]:
        """Confirm setup and increases to either limit, and apply tightening instantly.

        Args:
            weekly_limit: Requested weekly budget as a decimal amount of test USDC.
            max_auto_transaction: Requested automatic purchase ceiling as test USDC.
            confirmation_id: Optional token from the disclosure, supplied after user yes.

        Returns:
            dict[str, str]: CONFIRMATION_REQUIRED, ACTIVE, or a public rejection code.

        Raises:
            ConfirmationError: Invalid, expired, used, or parameter-mismatched consent.
        """
        weekly_limit_cents = self._parse_policy_amount(weekly_limit)
        max_auto_tx_cents = self._parse_policy_amount(max_auto_transaction)
        if weekly_limit_cents is None or max_auto_tx_cents is None:
            return self._policy_rejection(DecisionReason.INVALID_POLICY)
        async with self._policy_lock:
            params = self._policy_confirmation_params(
                weekly_limit_cents, max_auto_tx_cents, self.database.get_policy()
            )
            if confirmation_id is not None:
                self.confirmations.consume_confirmation(
                    confirmation_id, "set_spending_policy", params
                )
            current = self._active_policy()
            validation = validate_policy_change(current, weekly_limit_cents, max_auto_tx_cents)
            if not validation.allowed:
                return self._policy_rejection(validation.reason)
            needs_confirmation = (
                current is None
                or weekly_limit_cents > current.weekly_limit_cents
                or max_auto_tx_cents > current.max_auto_tx_cents
            )
            if needs_confirmation and confirmation_id is None:
                return {
                    "status": "CONFIRMATION_REQUIRED",
                    "confirmation_id": self.confirmations.create_confirmation(
                        "set_spending_policy", params
                    ),
                    "disclosure": self._policy_disclosure(weekly_limit_cents, max_auto_tx_cents),
                    **self._policy_fields(weekly_limit_cents, max_auto_tx_cents),
                }
            policy = self.database.set_policy(weekly_limit_cents, max_auto_tx_cents)
        return {
            "status": "ACTIVE",
            **self._policy_fields(policy["weekly_limit_cents"], policy["max_auto_tx_cents"]),
        }

    async def get_spending_policy(self) -> dict[str, object]:
        """Read the active policy with spend and pending-approval contract fields.

        Args:
            None.

        Returns:
            dict[str, object]: NOT_SET, or ACTIVE with limits, spent, remaining, and
                pending_approvals.
        """
        policy = self.database.get_policy()
        if policy is None:
            return {"status": "NOT_SET"}
        spent_cents = self.database.get_spent_this_week(datetime.now(UTC))
        remaining_cents = max(policy["weekly_limit_cents"] - spent_cents, 0)
        return {
            "status": "ACTIVE",
            **self._policy_fields(policy["weekly_limit_cents"], policy["max_auto_tx_cents"]),
            "spent_this_week": format_cents(spent_cents),
            "remaining_this_week": format_cents(remaining_cents),
            "pending_approvals": self.database.get_pending_approvals(),
        }

    async def search_products(
        self, query: str, max_price: Decimal | int | str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Delegate product search to the Phase 5 purchase service.

        Args:
            query: Merchant catalog search query.
            max_price: Optional maximum test-USDC price.
            limit: Maximum result count.

        Returns:
            list[dict[str, Any]]: Normalized product results.

        Raises:
            WalletError: PURCHASE_UNAVAILABLE when Phase 5 service is not attached.
            Exception: The underlying purchase service may raise a sanitized public error.
        """
        if self.purchase_service is None:
            raise WalletError("PURCHASE_UNAVAILABLE")
        return await self.purchase_service.search_products(query, max_price=max_price, limit=limit)

    async def get_product(self, product_id: str) -> dict[str, Any]:
        """Delegate product detail reads to the Phase 5 purchase service.

        Args:
            product_id: Store-owned product identifier.

        Returns:
            dict[str, Any]: Normalized product detail.

        Raises:
            WalletError: PURCHASE_UNAVAILABLE when Phase 5 service is not attached.
            Exception: The underlying purchase service may raise a sanitized public error.
        """
        if self.purchase_service is None:
            raise WalletError("PURCHASE_UNAVAILABLE")
        return await self.purchase_service.get_product(product_id)

    async def request_purchase(
        self, product_id: str, max_price: Decimal | int | str | None = None
    ) -> dict[str, Any]:
        """Delegate purchase orchestration to the Phase 5 purchase service.

        Args:
            product_id: Store-owned product identifier.
            max_price: Optional maximum test-USDC price.

        Returns:
            dict[str, Any]: Public purchase decision and payment result.

        Raises:
            WalletError: PURCHASE_UNAVAILABLE when Phase 5 service is not attached.
            Exception: The underlying purchase service may raise a sanitized public error.
        """
        if self.purchase_service is None:
            raise WalletError("PURCHASE_UNAVAILABLE")
        return await self.purchase_service.request_purchase(product_id, max_price=max_price)

    def get_transactions(self, limit: int = 10) -> dict[str, list[dict[str, Any]]]:
        """Delegate transaction history reads to the Phase 5 purchase service.

        Args:
            limit: Maximum result count.

        Returns:
            dict[str, list[dict[str, Any]]]: Recent purchase history.

        Raises:
            WalletError: PURCHASE_UNAVAILABLE when Phase 5 service is not attached.
        """
        if self.purchase_service is None:
            raise WalletError("PURCHASE_UNAVAILABLE")
        return self.purchase_service.get_transactions(limit=limit)

    async def reconcile_funding(
        self, operation_id: str, recover_missing: bool = False
    ) -> dict[str, Any]:
        """Recheck an operation and optionally recover only known unsent or reverted legs.

        This developer-only function is never exposed as an MCP tool. An unknown
        submission without a hash must be investigated externally before recovery.

        Args:
            operation_id: Existing funding event UUID to inspect.
            recover_missing: Explicit developer instruction to submit known missing legs.

        Returns:
            dict[str, Any]: Confirmed result, treasury failure, or unresolved recovery guidance.

        Raises:
            WalletError: FUNDING_NOT_FOUND or FUNDING_RECOVERY_MISMATCH for invalid context.
        """
        async with self._funding_lock:
            event = self.database.get_funding_event(operation_id)
            if event is None:
                raise WalletError("FUNDING_NOT_FOUND")
            wallet = self.database.get_wallet()
            if (
                not wallet
                or wallet["address"] != event["wallet_address"]
                or event["treasury_account_name"] != self.treasury_account_name
                or event["usdc_contract_address"] != self.usdc_contract_address
            ):
                raise WalletError("FUNDING_RECOVERY_MISMATCH")
            if event["status"] == "CONFIRMED":
                return self._funding_result(event)
            if event["status"] == "FAILED":
                return self._funding_failed_result(
                    "This operation definitively failed. Request fresh funding consent "
                    "instead of reusing its released reservation.",
                    event,
                )
            for token in ("usdc", "eth"):
                state = event[token + "_status"]
                tx_hash = event[token + "_tx_hash"]
                if tx_hash and state in {"SUBMITTED", "SUBMITTING", "UNKNOWN"}:
                    try:
                        receipt = await self.wait_for_receipt(tx_hash)
                        status = self._receipt_status(receipt)
                        state = {0: "FAILED", 1: "CONFIRMED"}.get(status, "UNKNOWN")
                    except Exception:
                        state = "UNKNOWN"
                    self.database.record_funding_leg(operation_id, token, state)
                elif state in {"SUBMITTING", "SUBMITTED"}:
                    self.database.record_funding_leg(operation_id, token, "UNKNOWN")
            event = self.database.finalize_funding(operation_id)
            if event["status"] in {"CONFIRMED", "FAILED", "UNKNOWN"} or not recover_missing:
                return self._funding_result(event)
            missing_usdc = event["usdc_status"] in {"PENDING", "FAILED"}
            missing_eth = event["eth_status"] in {"PENDING", "FAILED"}
            treasury, error = await self._funding_treasury(
                event["wallet_address"],
                event["usdc_amount_cents"] if missing_usdc else 0,
                int(event["eth_amount_wei"]) if missing_eth else 0,
            )
            if error:
                return error
            for token, missing in (("usdc", missing_usdc), ("eth", missing_eth)):
                if missing and not await self._send_funding_leg(event, token, treasury):
                    break
            return self._funding_result(self.database.finalize_funding(operation_id))
