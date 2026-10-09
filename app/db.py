"""Persist wallet pointers, creation requests, and auditable funding events in SQLite."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    account_name TEXT NOT NULL,
    address TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_creation (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    account_name TEXT NOT NULL,
    request_id TEXT NOT NULL
);
"""

FUNDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS funding_events (
    id TEXT PRIMARY KEY,
    wallet_address TEXT NOT NULL,
    usdc_amount_cents INTEGER NOT NULL,
    eth_amount_wei TEXT NOT NULL,
    usdc_tx_hash TEXT,
    eth_tx_hash TEXT,
    status TEXT NOT NULL CHECK (status IN
        ('RESERVED','SUBMITTED','CONFIRMED','FAILED','PARTIAL','UNKNOWN')),
    created_at TEXT NOT NULL,
    funding_kind TEXT NOT NULL DEFAULT 'INITIAL' CHECK (funding_kind IN ('INITIAL','TOP_UP')),
    usdc_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    eth_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    treasury_account_name TEXT NOT NULL DEFAULT 'agent-spend-treasury',
    usdc_contract_address TEXT NOT NULL DEFAULT '0x036CbD53842c5426634e7929541eC2318f3dCF7e'
);
"""

POLICY_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    weekly_limit_cents INTEGER NOT NULL CHECK (weekly_limit_cents >= 0),
    max_auto_tx_cents INTEGER NOT NULL CHECK (max_auto_tx_cents >= 0),
    updated_at TEXT NOT NULL
);
"""

PURCHASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    product_name TEXT,
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('PENDING_APPROVAL','REJECTED','SUBMITTED','CONFIRMED','FAILED')),
    reason_code TEXT,
    order_id TEXT,
    payment_intent_id TEXT,
    tx_hash TEXT UNIQUE,
    created_at TEXT NOT NULL,
    offer_id TEXT,
    cashback_cents INTEGER NOT NULL DEFAULT 0,
    cashback_status TEXT NOT NULL DEFAULT 'NONE'
        CHECK (cashback_status IN ('NONE','PAID','NOT_ELIGIBLE','FAILED')),
    cashback_tx_hash TEXT UNIQUE
);
"""

ATTEMPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS funding_attempts (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES funding_events(id),
    token TEXT NOT NULL CHECK (token IN ('usdc','eth')),
    status TEXT NOT NULL,
    tx_hash TEXT,
    created_at TEXT NOT NULL
);
"""


class FundingConflict(Exception):
    """Carry a public reservation conflict without exposing database internals."""


class Database:
    """Own a single-process SQLite connection and Agent Spend persistence helpers."""

    def __init__(self, path: Path) -> None:
        """Open SQLite and initialize the public-pointer and request-id schema.

        Args:
            path: The SQLite file path; its parent directory must exist.

        Returns:
            None.

        Raises:
            sqlite3.Error: The database cannot be opened or initialized.
        """
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self._migrate_funding_events()
        self.connection.execute(ATTEMPT_SCHEMA)
        self.connection.execute(POLICY_SCHEMA)
        self._migrate_purchases()

    def _migrate_funding_events(self) -> None:
        """Upgrade baseline funding rows transactionally without losing hashes.

        Args:
            None.

        Returns:
            None.

        Raises:
            sqlite3.Error: Schema creation or the atomic migration fails.
        """
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(funding_events)")
        }
        with self.connection:
            if columns and "funding_kind" not in columns:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    "ALTER TABLE funding_events RENAME TO funding_events_legacy"
                )
                self.connection.execute(FUNDING_SCHEMA)
                self.connection.execute(
                    "INSERT INTO funding_events "
                    "(id, wallet_address, usdc_amount_cents, eth_amount_wei, usdc_tx_hash, "
                    "eth_tx_hash, status, created_at, funding_kind, usdc_status, eth_status) "
                    "SELECT id, wallet_address, usdc_amount_cents, eth_amount_wei, usdc_tx_hash, "
                    "eth_tx_hash, CASE WHEN status = 'CONFIRMED' "
                    "THEN 'CONFIRMED' ELSE 'UNKNOWN' END, "
                    "created_at, 'INITIAL', "
                    "CASE WHEN status = 'CONFIRMED' THEN 'CONFIRMED' ELSE 'UNKNOWN' END, "
                    "CASE WHEN status = 'CONFIRMED' THEN 'CONFIRMED' ELSE 'UNKNOWN' END "
                    "FROM funding_events_legacy"
                )
                self.connection.execute("DROP TABLE funding_events_legacy")
            else:
                self.connection.execute(FUNDING_SCHEMA)

    def _migrate_purchases(self) -> None:
        """Create the Phase 5 purchase ledger and add cashback columns when absent.

        Args:
            None.

        Returns:
            None.

        Raises:
            sqlite3.Error: Schema creation or additive migration fails.
        """
        self.connection.execute(PURCHASE_SCHEMA)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(purchases)")}
        additions = {
            "offer_id": "ALTER TABLE purchases ADD COLUMN offer_id TEXT",
            "cashback_cents": (
                "ALTER TABLE purchases ADD COLUMN cashback_cents INTEGER NOT NULL DEFAULT 0"
            ),
            "cashback_status": (
                "ALTER TABLE purchases ADD COLUMN cashback_status TEXT NOT NULL DEFAULT 'NONE'"
            ),
            "cashback_tx_hash": "ALTER TABLE purchases ADD COLUMN cashback_tx_hash TEXT",
        }
        with self.connection:
            for column, statement in additions.items():
                if column not in columns:
                    self.connection.execute(statement)

    def funding_sequence(self, address: str) -> int:
        """Count durable operations to invalidate earlier pending disclosures.

        Args:
            address: Persisted shopper destination.

        Returns:
            int: Number of funding operations recorded for this wallet.
        """
        return self.connection.execute(
            "SELECT COUNT(*) FROM funding_events WHERE wallet_address = ?", (address,)
        ).fetchone()[0]

    def reserved_funding_cents(self, address: str) -> int:
        """Count confirmed funds and reservations with possible chain side effects.

        Args:
            address: Shopper wallet whose lifetime funding allowance is queried.

        Returns:
            int: Confirmed and unresolved USDC cents, excluding definitively failed grants.
        """
        return self.connection.execute(
            "SELECT COALESCE(SUM(usdc_amount_cents), 0) FROM funding_events "
            "WHERE wallet_address = ? AND status != 'FAILED'",
            (address,),
        ).fetchone()[0]

    def get_unresolved_funding(self, address: str) -> dict[str, Any] | None:
        """Find a reservation, partial transfer, or uncertain operation blocking funding.

        Args:
            address: Persisted shopper wallet destination.

        Returns:
            dict[str, Any] | None: Oldest unresolved event, if any.
        """
        row = self.connection.execute(
            "SELECT * FROM funding_events WHERE wallet_address = ? "
            "AND status IN ('RESERVED','SUBMITTED','PARTIAL','UNKNOWN') "
            "ORDER BY created_at LIMIT 1",
            (address,),
        ).fetchone()
        return dict(row) if row else None

    def get_funding_event(self, event_id: str) -> dict[str, Any] | None:
        """Read one durable operation by its immutable funding intent identifier.

        Args:
            event_id: Public operation UUID.

        Returns:
            dict[str, Any] | None: Funding audit row, or None when unknown.
        """
        row = self.connection.execute(
            "SELECT * FROM funding_events WHERE id = ?", (event_id,)
        ).fetchone()
        return dict(row) if row else None

    def reserve_funding(self, intent: dict[str, Any], total_cap_cents: int) -> dict[str, Any]:
        """Atomically reserve an intent, its lifetime allowance, and the wallet lock.

        Args:
            intent: Confirmed immutable parameters including operation ID and sequence.
            total_cap_cents: Maximum cumulative funding permitted by server configuration.

        Returns:
            dict[str, Any]: Committed funding event before any remote transfer.

        Raises:
            FundingConflict: Duplicate intent, stale sequence, unresolved funding, or cap breach.
            sqlite3.Error: Reservation persistence fails before provider submission.
        """
        address = intent["wallet_address"]
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.get_funding_event(intent["intent_id"]):
                raise FundingConflict("FUNDING_ALREADY_SUBMITTED")
            wallet = self.get_wallet()
            if (
                not wallet
                or wallet["address"] != address
                or self.funding_sequence(address) != intent["funding_sequence"]
            ):
                raise FundingConflict("CONFIRMATION_MISMATCH")
            if self.get_unresolved_funding(address):
                raise FundingConflict("FUNDING_IN_PROGRESS")
            if self.reserved_funding_cents(address) + intent["usdc_amount_cents"] > total_cap_cents:
                raise FundingConflict("FUNDING_TOTAL_CAP_EXCEEDED")
            self.connection.execute(
                "INSERT INTO funding_events "
                "(id, wallet_address, usdc_amount_cents, eth_amount_wei, "
                "status, created_at, funding_kind, usdc_status, eth_status, treasury_account_name, "
                "usdc_contract_address) VALUES (?, ?, ?, ?, 'RESERVED', ?, ?, 'PENDING', ?, ?, ?)",
                (
                    intent["intent_id"],
                    address,
                    intent["usdc_amount_cents"],
                    intent["eth_amount_wei"],
                    datetime.now(UTC).isoformat(),
                    intent["funding_kind"],
                    "PENDING" if int(intent["eth_amount_wei"]) else "NOT_REQUIRED",
                    intent["treasury_account_name"],
                    intent["usdc_contract_address"],
                ),
            )
        return self.get_funding_event(intent["intent_id"])

    def begin_funding_leg(self, event_id: str, token: str) -> str:
        """Persist a transfer attempt before invoking the remote signing provider.

        Args:
            event_id: Existing reserved funding operation ID.
            token: Server-owned leg name, usdc or eth.

        Returns:
            str: Unique attempt UUID retained across receipt checks and recovery.

        Raises:
            ValueError: The token is unsupported.
            FundingConflict: The leg is confirmed, uncertain, or already being submitted.
        """
        if token not in {"usdc", "eth"}:
            raise ValueError("Unsupported funding leg")
        attempt_id = str(uuid4())
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            event = self.get_funding_event(event_id)
            if not event or event[token + "_status"] not in {"PENDING", "FAILED"}:
                raise FundingConflict("FUNDING_IN_PROGRESS")
            self.connection.execute(
                "INSERT INTO funding_attempts (id, event_id, token, status, created_at) "
                "VALUES (?, ?, ?, 'SUBMITTING', ?)",
                (attempt_id, event_id, token, datetime.now(UTC).isoformat()),
            )
            self.connection.execute(
                f"UPDATE funding_events SET status = 'SUBMITTED', {token}_status = 'SUBMITTING', "
                f"{token}_tx_hash = NULL WHERE id = ?",
                (event_id,),
            )
        return attempt_id

    def record_funding_leg(
        self,
        event_id: str,
        token: str,
        status: str,
        tx_hash: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        """Update a leg and retain every attempted hash for developer audit.

        Args:
            event_id: Funding operation UUID.
            token: Server-owned usdc or eth leg.
            status: Leg lifecycle state to persist.
            tx_hash: Optional submitted transaction hash.
            attempt_id: Optional attempt UUID; omitted for read-only reconciliation.

        Returns:
            None.

        Raises:
            ValueError: Token or status is unsupported.
            sqlite3.Error: Audit persistence fails.
        """
        if token not in {"usdc", "eth"} or status not in {
            "PENDING",
            "SUBMITTING",
            "SUBMITTED",
            "CONFIRMED",
            "FAILED",
            "UNKNOWN",
            "NOT_REQUIRED",
        }:
            raise ValueError("Invalid funding leg state")
        with self.connection:
            self.connection.execute(
                f"UPDATE funding_events SET {token}_status = ?, "
                f"{token}_tx_hash = COALESCE(?, {token}_tx_hash) WHERE id = ?",
                (status, tx_hash, event_id),
            )
            if attempt_id is not None:
                self.connection.execute(
                    "UPDATE funding_attempts SET status = ?, "
                    "tx_hash = COALESCE(?, tx_hash) WHERE id = ?",
                    (status, tx_hash, attempt_id),
                )
            else:
                self.connection.execute(
                    "UPDATE funding_attempts SET status = ? WHERE event_id = ? AND token = ? "
                    f"AND tx_hash = (SELECT {token}_tx_hash FROM funding_events WHERE id = ?)",
                    (status, event_id, token, event_id),
                )

    def finalize_funding(self, event_id: str) -> dict[str, Any]:
        """Derive the operation outcome from confirmed, failed, and uncertain legs.

        Args:
            event_id: Durable funding operation ID.

        Returns:
            dict[str, Any]: Updated CONFIRMED, PARTIAL, UNKNOWN, FAILED, or RESERVED event.
        """
        event = self.get_funding_event(event_id)
        states = {event["usdc_status"], event["eth_status"]}
        if event["usdc_status"] == "CONFIRMED" and event["eth_status"] in {
            "CONFIRMED",
            "NOT_REQUIRED",
        }:
            status = "CONFIRMED"
        elif states & {"UNKNOWN", "SUBMITTING", "SUBMITTED"}:
            status = "UNKNOWN"
        elif "CONFIRMED" in states:
            status = "PARTIAL"
        elif event["usdc_status"] == "FAILED":
            status = "FAILED"
        else:
            status = "RESERVED"
        with self.connection:
            self.connection.execute(
                "UPDATE funding_events SET status = ? WHERE id = ?", (status, event_id)
            )
        return self.get_funding_event(event_id)

    def get_wallet(self) -> dict[str, str] | None:
        """Read the only shopper wallet, excluding the internal singleton identifier.

        Args:
            None.

        Returns:
            dict[str, str] | None: Account name, address, and creation time, or no wallet.
        """
        row = self.connection.execute(
            "SELECT account_name, address, created_at FROM wallet WHERE id = 1"
        ).fetchone()
        return dict(row) if row else None

    def create_wallet_pointer(self, account_name: str, address: str) -> None:
        """Commit public CDP account metadata without replacing an existing wallet.

        Args:
            account_name: The server-selected CDP account name.
            address: The validated public EVM account address.

        Returns:
            None.

        Raises:
            sqlite3.IntegrityError: A wallet already exists.
            sqlite3.Error: Persistence fails.
        """
        with self.connection:
            self.connection.execute(
                "INSERT INTO wallet (id, account_name, address, created_at) VALUES (1, ?, ?, ?)",
                (account_name, address, datetime.now(UTC).isoformat()),
            )

    def wallet_exists(self) -> bool:
        """Check whether the singleton shopper wallet has been persisted.

        Args:
            None.

        Returns:
            bool: True when the wallet pointer exists.
        """
        return self.get_wallet() is not None

    def has_confirmed_funding(self) -> bool:
        """Check whether the shopper has at least one fully confirmed funding operation.

        Args:
            None.

        Returns:
            bool: True when any funding event has status CONFIRMED.
        """
        row = self.connection.execute(
            "SELECT 1 FROM funding_events WHERE status = 'CONFIRMED' LIMIT 1"
        ).fetchone()
        return row is not None

    def insert_funding_event(
        self,
        wallet_address: str,
        usdc_amount_cents: int,
        eth_amount_wei: str,
        status: str = "SUBMITTED",
    ) -> dict[str, str | int | None]:
        """Insert the durable audit row before any funding transfer is attempted.

        Args:
            wallet_address: The persisted shopper wallet destination.
            usdc_amount_cents: The USDC amount in cents.
            eth_amount_wei: The ETH gas amount in wei as a decimal string.
            status: The initial funding event status; defaults to SUBMITTED.

        Returns:
            dict[str, str | int | None]: The inserted funding event.

        Raises:
            sqlite3.Error: The event cannot be persisted.
        """
        event_id = str(uuid4())
        with self.connection:
            self.connection.execute(
                "INSERT INTO funding_events "
                "(id, wallet_address, usdc_amount_cents, eth_amount_wei, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    wallet_address,
                    usdc_amount_cents,
                    eth_amount_wei,
                    status,
                    datetime.now(UTC).isoformat(),
                ),
            )
        row = self.connection.execute(
            "SELECT * FROM funding_events WHERE id = ?", (event_id,)
        ).fetchone()
        return dict(row)

    def record_funding_tx_hashes(
        self,
        event_id: str,
        *,
        usdc_tx_hash: str | None = None,
        eth_tx_hash: str | None = None,
    ) -> None:
        """Record funding transfer hashes as soon as each transaction is submitted.

        Args:
            event_id: The funding event identifier to update.
            usdc_tx_hash: Optional USDC transfer hash to persist.
            eth_tx_hash: Optional ETH transfer hash to persist.

        Returns:
            None.

        Raises:
            sqlite3.Error: The funding event cannot be updated.
        """
        with self.connection:
            self.connection.execute(
                "UPDATE funding_events "
                "SET usdc_tx_hash = COALESCE(?, usdc_tx_hash), "
                "eth_tx_hash = COALESCE(?, eth_tx_hash) "
                "WHERE id = ?",
                (usdc_tx_hash, eth_tx_hash, event_id),
            )

    def mark_funding_confirmed(
        self, event_id: str, usdc_tx_hash: str, eth_tx_hash: str | None
    ) -> None:
        """Mark a funding event confirmed after all required receipts succeed.

        Args:
            event_id: The funding event identifier to update.
            usdc_tx_hash: The confirmed USDC transfer transaction hash.
            eth_tx_hash: Confirmed ETH hash, or None for a USDC-only operation.

        Returns:
            None.

        Raises:
            sqlite3.Error: The funding event cannot be updated.
        """
        with self.connection:
            self.connection.execute(
                "UPDATE funding_events "
                "SET status = 'CONFIRMED', usdc_tx_hash = ?, eth_tx_hash = ?, "
                "usdc_status = 'CONFIRMED', eth_status = CASE WHEN eth_amount_wei = '0' "
                "THEN 'NOT_REQUIRED' ELSE 'CONFIRMED' END "
                "WHERE id = ?",
                (usdc_tx_hash, eth_tx_hash, event_id),
            )

    def mark_funding_failed(self, event_id: str, message: str | None = None) -> None:
        """Mark a funding event failed without persisting provider error details.

        Args:
            event_id: The funding event identifier to update.
            message: Optional developer-facing context intentionally not stored.

        Returns:
            None.

        Raises:
            sqlite3.Error: The funding event cannot be updated.
        """
        with self.connection:
            self.connection.execute(
                "UPDATE funding_events SET status = 'FAILED' WHERE id = ?", (event_id,)
            )

    def get_latest_funding_event(self) -> dict[str, str | int | None] | None:
        """Read the most recently created funding event for status and audit output.

        Args:
            None.

        Returns:
            dict[str, str | int | None] | None: The latest event, or None when absent.
        """
        row = self.connection.execute(
            "SELECT * FROM funding_events ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def reserve_wallet_creation(self) -> dict[str, str]:
        """Reuse a durable request identity before any remote account creation.

        The stable CDP idempotency key and unique account name prevent an uncertain
        provider response or failed pointer write from creating another account.

        Args:
            None.

        Returns:
            dict[str, str]: The public account name and non-secret request UUID.

        Raises:
            sqlite3.Error: The creation identity cannot be persisted.
        """
        request_id = str(uuid4())
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO wallet_creation (id, account_name, request_id) "
                "VALUES (1, ?, ?)",
                ("shopper-" + request_id.replace("-", "")[:24], request_id),
            )
        row = self.connection.execute(
            "SELECT account_name, request_id FROM wallet_creation WHERE id = 1"
        ).fetchone()
        return dict(row)

    def get_policy(self) -> dict[str, Any] | None:
        """Read the singleton spending policy, excluding the fixed internal row ID.

        Args:
            None.

        Returns:
            dict[str, Any] | None: Weekly and automatic transaction limits, or no policy.
        """
        row = self.connection.execute(
            "SELECT weekly_limit_cents, max_auto_tx_cents, updated_at FROM policy WHERE id = 1"
        ).fetchone()
        return dict(row) if row else None

    def set_policy(self, weekly_limit_cents: int, max_auto_tx_cents: int) -> dict[str, Any]:
        """Persist the singleton spending policy with deterministic cents values.

        Args:
            weekly_limit_cents: Non-negative weekly budget in whole cents.
            max_auto_tx_cents: Non-negative automatic approval limit in whole cents.

        Returns:
            dict[str, Any]: The committed policy row without the internal singleton ID.

        Raises:
            sqlite3.Error: The policy cannot be inserted or updated.
        """
        with self.connection:
            self.connection.execute(
                "INSERT INTO policy "
                "(id, weekly_limit_cents, max_auto_tx_cents, updated_at) "
                "VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "weekly_limit_cents = excluded.weekly_limit_cents, "
                "max_auto_tx_cents = excluded.max_auto_tx_cents, "
                "updated_at = excluded.updated_at",
                (weekly_limit_cents, max_auto_tx_cents, datetime.now(UTC).isoformat()),
            )
        return self.get_policy()

    def get_spent_this_week(self, now: datetime) -> int:
        """Sum submitted and confirmed purchase spend over the rolling seven-day window.

        Args:
            now: Current time used to compute the rolling seven-day lower bound.

        Returns:
            int: Whole cents counted against the active weekly policy.
        """
        cutoff = (now - timedelta(days=7)).isoformat()
        row = self.connection.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM purchases "
            "WHERE status IN ('SUBMITTED','CONFIRMED') AND created_at >= ?",
            (cutoff,),
        ).fetchone()
        return int(row[0])

    def get_pending_approvals(self) -> list[dict[str, Any]]:
        """Read purchase rows waiting for human approval.

        Args:
            None.

        Returns:
            list[dict[str, Any]]: Pending approval rows in creation order.
        """
        rows = self.connection.execute(
            "SELECT * FROM purchases WHERE status = 'PENDING_APPROVAL' ORDER BY created_at"
        ).fetchall()
        return [dict(row) for row in rows]

    def insert_purchase(
        self,
        *,
        product_id: str,
        product_name: str | None,
        amount_cents: int,
        status: str,
        reason_code: str | None,
        order_id: str | None,
        payment_intent_id: str | None,
        tx_hash: str | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Insert one purchase decision before any Phase 5 payment side effect.

        Args:
            product_id: Store-owned product identifier.
            product_name: Optional store-owned product display name.
            amount_cents: Store-owned amount in whole cents.
            status: Purchase lifecycle status to persist.
            reason_code: Public policy reason code, if a decision has one.
            order_id: Merchant order/cart identifier, when an order exists.
            payment_intent_id: Merchant payment intent identifier, when created.
            tx_hash: Optional public payment transaction hash.
            created_at: Optional deterministic timestamp for tests.

        Returns:
            dict[str, Any]: Inserted purchase row plus created_at_datetime for callers.

        Raises:
            ValueError: Amount or status is invalid.
            sqlite3.Error: Persistence fails.
        """
        if amount_cents < 0:
            raise ValueError("Purchase amount cannot be negative")
        if status not in {"PENDING_APPROVAL", "REJECTED", "SUBMITTED", "CONFIRMED", "FAILED"}:
            raise ValueError("Invalid purchase status")
        purchase_id = str(uuid4())
        timestamp = created_at or datetime.now(UTC)
        with self.connection:
            self.connection.execute(
                "INSERT INTO purchases "
                "(id, product_id, product_name, amount_cents, status, reason_code, "
                "order_id, payment_intent_id, tx_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    purchase_id,
                    product_id,
                    product_name,
                    amount_cents,
                    status,
                    reason_code,
                    order_id,
                    payment_intent_id,
                    tx_hash,
                    timestamp.isoformat(),
                ),
            )
        return self._purchase_with_datetime(purchase_id)

    def update_purchase(
        self,
        purchase_id: str,
        *,
        status: str,
        reason_code: str | None = None,
        tx_hash: str | None = None,
    ) -> dict[str, Any]:
        """Update one purchase after payment submission, receipt, or verification.

        Args:
            purchase_id: Purchase UUID returned by insert_purchase.
            status: New lifecycle status.
            reason_code: Optional updated public reason code.
            tx_hash: Optional public payment transaction hash.

        Returns:
            dict[str, Any]: Updated purchase row plus created_at_datetime.

        Raises:
            ValueError: Status is invalid.
            sqlite3.Error: Persistence fails.
        """
        if status not in {"PENDING_APPROVAL", "REJECTED", "SUBMITTED", "CONFIRMED", "FAILED"}:
            raise ValueError("Invalid purchase status")
        with self.connection:
            self.connection.execute(
                "UPDATE purchases SET status = ?, "
                "reason_code = COALESCE(?, reason_code), "
                "tx_hash = COALESCE(?, tx_hash) WHERE id = ?",
                (status, reason_code, tx_hash, purchase_id),
            )
        return self._purchase_with_datetime(purchase_id)

    def get_unresolved_product_purchase(self, product_id: str) -> dict[str, Any] | None:
        """Find a submitted purchase whose payment or merchant outcome is unresolved.

        Args:
            product_id: Store-owned product identifier being requested again.

        Returns:
            dict[str, Any] | None: Newest unresolved purchase, or None when absent.
                No time cutoff applies because an uncertain payment may still settle.
        """
        row = self.connection.execute(
            "SELECT * FROM purchases WHERE product_id = ? AND status = 'SUBMITTED' "
            "ORDER BY created_at DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_recent_product_purchases(
        self, product_id: str, now: datetime
    ) -> list[tuple[str, datetime]]:
        """Read same-product purchase attempts inside the duplicate window.

        Args:
            product_id: Store-owned product identifier under evaluation.
            now: Current time used to compute the two-minute lower bound.

        Returns:
            list[tuple[str, datetime]]: Product IDs and parsed creation timestamps.
        """
        cutoff = (now - timedelta(minutes=2)).isoformat()
        rows = self.connection.execute(
            "SELECT product_id, created_at FROM purchases "
            "WHERE product_id = ? AND created_at >= ? ORDER BY created_at DESC",
            (product_id, cutoff),
        ).fetchall()
        return [(row["product_id"], datetime.fromisoformat(row["created_at"])) for row in rows]

    def get_purchase_history(self, limit: int = 10) -> list[dict[str, Any]]:
        """Read recent purchase rows for ChatGPT transaction history.

        Args:
            limit: Maximum number of purchases to return in newest-first order.

        Returns:
            list[dict[str, Any]]: Purchase rows including cashback Phase 5B placeholders.
        """
        if limit <= 0:
            return []
        rows = self.connection.execute(
            "SELECT * FROM purchases ORDER BY created_at DESC LIMIT ?", (min(limit, 50),)
        ).fetchall()
        return [dict(row) for row in rows]

    def _purchase_with_datetime(self, purchase_id: str) -> dict[str, Any]:
        """Read one purchase row and attach a parsed timestamp for policy callers.

        Args:
            purchase_id: Purchase UUID to load.

        Returns:
            dict[str, Any]: Purchase row plus created_at_datetime.

        Raises:
            LookupError: The purchase row does not exist.
        """
        row = self.connection.execute(
            "SELECT * FROM purchases WHERE id = ?", (purchase_id,)
        ).fetchone()
        if row is None:
            raise LookupError("Purchase not found")
        result = dict(row)
        result["created_at_datetime"] = datetime.fromisoformat(result["created_at"])
        return result

    def _table_exists(self, table_name: str) -> bool:
        """Check for an application table before querying future-phase contracts.

        Args:
            table_name: Exact SQLite table name to look up.

        Returns:
            bool: True when the table exists in the current database.
        """
        row = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    def close(self) -> None:
        """Release the SQLite connection; repeated cleanup is harmless.

        Args:
            None.

        Returns:
            None.
        """
        self.connection.close()
