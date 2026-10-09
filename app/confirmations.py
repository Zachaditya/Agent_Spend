"""Issue bounded, ten-minute, parameter-bound confirmations in process memory."""

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

CONFIRMATION_TTL_SECONDS = 600
MAX_PENDING_CONFIRMATIONS = 1024


class ConfirmationError(Exception):
    """Carry only a public confirmation failure code, never input parameters."""


@dataclass
class Confirmation:
    """Keep an action, parameter digest, and expiry for a pending confirmation."""

    action: str
    params_hash: str
    expires_at: float
    params_json: str


def hash_params(params: dict[str, Any]) -> str:
    """Hash JSON parameters deterministically, independent of dictionary key order.

    Args:
        params: JSON-serializable parameters bound to a protected action.

    Returns:
        str: The SHA-256 hex digest of the canonical JSON representation.

    Raises:
        TypeError: Parameters are not JSON serializable.
        ValueError: Parameters contain non-finite numbers.
    """
    encoded = json.dumps(params, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ConfirmationStore:
    """Atomically validate and consume opaque confirmation tokens for one process."""

    def __init__(self, max_pending: int = MAX_PENDING_CONFIRMATIONS) -> None:
        """Initialize a capacity-bounded store with a fixed ten-minute lifetime.

        Args:
            max_pending: Positive maximum number of unexpired pending confirmations.

        Returns:
            None.

        Raises:
            ValueError: The pending capacity is not positive.
        """
        if max_pending <= 0:
            raise ValueError("Confirmation capacity must be positive")
        self._max_pending = max_pending
        self._confirmations: dict[str, Confirmation] = {}
        self._signing_key = secrets.token_bytes(32)
        self._lock = threading.Lock()

    def create_confirmation(self, action: str, params: dict[str, Any]) -> str:
        """Create an unpredictable confirmation with no protected side effects.

        Args:
            action: The exact protected action name.
            params: Complete action parameters copied into an immutable JSON snapshot.

        Returns:
            str: The opaque identifier to return with the user disclosure.

        Raises:
            ConfirmationError: CONFIRMATION_LIMIT_REACHED when pending capacity is full.
            TypeError: Parameters cannot be serialized as JSON.
            ValueError: Parameters contain non-finite numbers.
        """
        nonce = secrets.token_urlsafe(32)
        signature = hmac.digest(self._signing_key, nonce.encode("ascii"), "sha256").hex()
        token = nonce + "." + signature
        params_hash = hash_params(params)
        with self._lock:
            now = time.monotonic()
            self._confirmations = {
                identifier: record
                for identifier, record in self._confirmations.items()
                if record.expires_at > now
            }
            if len(self._confirmations) >= self._max_pending:
                raise ConfirmationError("CONFIRMATION_LIMIT_REACHED")
            self._confirmations[token] = Confirmation(
                action,
                params_hash,
                now + CONFIRMATION_TTL_SECONDS,
                json.dumps(params, sort_keys=True, allow_nan=False),
            )
        return token

    def get_confirmation_params(self, confirmation_id: str, action: str) -> dict[str, Any]:
        """Resolve a pending intent without consuming it or exposing mutable state.

        Args:
            confirmation_id: Opaque identifier returned with the disclosure.
            action: Expected action name for this intent.

        Returns:
            dict[str, Any]: A fresh copy of the originally disclosed parameters.

        Raises:
            ConfirmationError: INVALID_CONFIRMATION, CONFIRMATION_EXPIRED, or
                CONFIRMATION_MISMATCH for an invalid identifier, expiry, or action.
        """
        with self._lock:
            confirmation = self._confirmations.get(confirmation_id)
            if confirmation is None:
                code = (
                    "CONFIRMATION_EXPIRED"
                    if self._was_issued(confirmation_id)
                    else "INVALID_CONFIRMATION"
                )
                raise ConfirmationError(code)
            if time.monotonic() >= confirmation.expires_at:
                del self._confirmations[confirmation_id]
                raise ConfirmationError("CONFIRMATION_EXPIRED")
            if confirmation.action != action:
                raise ConfirmationError("CONFIRMATION_MISMATCH")
            return json.loads(confirmation.params_json)

    def _was_issued(self, token: str | None) -> bool:
        """Recognize this store's retired tokens without retaining token history.

        Args:
            token: An opaque identifier that has no pending record.

        Returns:
            bool: Whether the identifier carries this process-local store's valid MAC.
                Restarted stores, forged identifiers, and malformed values return False.
        """
        if not token or not token.isascii():
            return False
        nonce, separator, signature = token.partition(".")
        expected = hmac.digest(self._signing_key, nonce.encode("ascii"), "sha256").hex()
        return bool(separator) and hmac.compare_digest(signature, expected)

    def consume_confirmation(
        self, confirmation_id: str | None, action: str, params: dict[str, Any]
    ) -> None:
        """Validate and remove a pending token before a protected action can start.

        Args:
            confirmation_id: The token from the disclosure, or an invalid missing token.
            action: The action being authorized.
            params: The current action parameters, which must match the original hash.

        Returns:
            None.

        Raises:
            ConfirmationError: INVALID_CONFIRMATION for unknown IDs,
                CONFIRMATION_EXPIRED for expired or used IDs, or
                CONFIRMATION_MISMATCH for changed actions or parameters.
        """
        with self._lock:
            confirmation = self._confirmations.get(confirmation_id)
            if confirmation is None:
                # A MAC preserves replay/expiry codes without an unbounded tombstone set.
                if self._was_issued(confirmation_id):
                    raise ConfirmationError("CONFIRMATION_EXPIRED")
                raise ConfirmationError("INVALID_CONFIRMATION")
            if time.monotonic() >= confirmation.expires_at:
                del self._confirmations[confirmation_id]
                raise ConfirmationError("CONFIRMATION_EXPIRED")
            if confirmation.action != action or not hmac.compare_digest(
                confirmation.params_hash, hash_params(params)
            ):
                raise ConfirmationError("CONFIRMATION_MISMATCH")
            del self._confirmations[confirmation_id]
