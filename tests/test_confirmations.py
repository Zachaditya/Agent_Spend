"""Verify expiry, parameter binding, and single-use confirmation semantics."""

import pytest

from app.confirmations import ConfirmationError, ConfirmationStore


def test_confirmation_is_parameter_order_independent_and_single_use():
    """Consume equivalent parameters once and reject replay.

    Args:
        None.

    Returns:
        None.
    """
    store = ConfirmationStore()
    token = store.create_confirmation("wallet", {"network": "base-sepolia", "count": 1})
    store.consume_confirmation(token, "wallet", {"count": 1, "network": "base-sepolia"})
    with pytest.raises(ConfirmationError, match="^CONFIRMATION_EXPIRED$"):
        store.consume_confirmation(token, "wallet", {"network": "base-sepolia", "count": 1})


@pytest.mark.parametrize("token", [None, "", "made-up-id"])
def test_unknown_confirmation_is_invalid(token):
    """Reject missing and invented confirmation identifiers.

    Args:
        token: An invalid identifier supplied by the caller.

    Returns:
        None.
    """
    with pytest.raises(ConfirmationError, match="^INVALID_CONFIRMATION$"):
        ConfirmationStore().consume_confirmation(token, "wallet", {})


@pytest.mark.parametrize("action,params", [("payment", {}), ("wallet", {"network": "mainnet"})])
def test_changed_action_or_params_is_rejected(action, params):
    """Bind a confirmation to both its action and canonical parameters.

    Args:
        action: The substituted action.
        params: The substituted parameters.

    Returns:
        None.
    """
    store = ConfirmationStore()
    token = store.create_confirmation("wallet", {})
    with pytest.raises(ConfirmationError, match="^CONFIRMATION_MISMATCH$"):
        store.consume_confirmation(token, action, params)
    store.consume_confirmation(token, "wallet", {})


def test_confirmation_expires_at_exactly_ten_minutes(monkeypatch):
    """Reject a token at the ten-minute boundary without sleeping.

    Args:
        monkeypatch: Pytest's scoped patch helper.

    Returns:
        None.
    """
    monkeypatch.setattr("app.confirmations.time.monotonic", lambda: 100.0)
    store = ConfirmationStore()
    token = store.create_confirmation("wallet", {})
    monkeypatch.setattr("app.confirmations.time.monotonic", lambda: 700.0)
    with pytest.raises(ConfirmationError, match="^CONFIRMATION_EXPIRED$"):
        store.consume_confirmation(token, "wallet", {})


def test_confirmation_is_valid_just_before_expiry(monkeypatch):
    """Accept a token just before its expiry boundary.

    Args:
        monkeypatch: Pytest's scoped patch helper.

    Returns:
        None.
    """
    monkeypatch.setattr("app.confirmations.time.monotonic", lambda: 100.0)
    store = ConfirmationStore()
    token = store.create_confirmation("wallet", {})
    monkeypatch.setattr("app.confirmations.time.monotonic", lambda: 699.999)
    store.consume_confirmation(token, "wallet", {})


def test_consumed_confirmation_is_reclaimed_without_changing_replay_error():
    """Remove consumed records immediately while preserving the public replay code.

    Args:
        None.

    Returns:
        None.
    """
    store = ConfirmationStore()
    token = store.create_confirmation("wallet", {})
    store.consume_confirmation(token, "wallet", {})
    assert not store._confirmations
    with pytest.raises(ConfirmationError, match="^CONFIRMATION_EXPIRED$"):
        store.consume_confirmation(token, "wallet", {})


def test_issuance_sweeps_expired_records_without_changing_expiry_error(monkeypatch):
    """Reclaim stale disclosures on issuance and still identify expired issued IDs.

    Args:
        monkeypatch: Pytest's scoped clock replacement helper.

    Returns:
        None.
    """
    monkeypatch.setattr("app.confirmations.time.monotonic", lambda: 100.0)
    store = ConfirmationStore()
    expired = [store.create_confirmation("wallet", {}) for _ in range(20)]
    monkeypatch.setattr("app.confirmations.time.monotonic", lambda: 700.0)
    current = store.create_confirmation("wallet", {})
    assert set(store._confirmations) == {current}
    for token in expired:
        with pytest.raises(ConfirmationError, match="^CONFIRMATION_EXPIRED$"):
            store.consume_confirmation(token, "wallet", {})
    store.consume_confirmation(current, "wallet", {})


def test_expired_confirmation_is_reclaimed_on_consumption(monkeypatch):
    """Remove an expired record even when no new disclosures are requested.

    Args:
        monkeypatch: Pytest's scoped clock replacement helper.

    Returns:
        None.
    """
    monkeypatch.setattr("app.confirmations.time.monotonic", lambda: 100.0)
    store = ConfirmationStore()
    token = store.create_confirmation("wallet", {})
    monkeypatch.setattr("app.confirmations.time.monotonic", lambda: 700.0)
    with pytest.raises(ConfirmationError, match="^CONFIRMATION_EXPIRED$"):
        store.consume_confirmation(token, "wallet", {})
    assert not store._confirmations


def test_pending_confirmations_are_bounded_and_consumption_frees_capacity():
    """Reject excess pending disclosures without invalidating already issued tokens.

    Args:
        None.

    Returns:
        None.
    """
    store = ConfirmationStore(max_pending=2)
    first = store.create_confirmation("wallet", {})
    second = store.create_confirmation("wallet", {})
    with pytest.raises(ConfirmationError, match="^CONFIRMATION_LIMIT_REACHED$"):
        store.create_confirmation("wallet", {})
    assert len(store._confirmations) == 2
    store.consume_confirmation(first, "wallet", {})
    third = store.create_confirmation("wallet", {})
    store.consume_confirmation(second, "wallet", {})
    store.consume_confirmation(third, "wallet", {})
    assert not store._confirmations


def test_expiry_frees_pending_capacity(monkeypatch):
    """Prune before enforcing capacity so expired disclosures cannot block issuance.

    Args:
        monkeypatch: Pytest's scoped clock replacement helper.

    Returns:
        None.
    """
    monkeypatch.setattr("app.confirmations.time.monotonic", lambda: 100.0)
    store = ConfirmationStore(max_pending=1)
    store.create_confirmation("wallet", {})
    monkeypatch.setattr("app.confirmations.time.monotonic", lambda: 700.0)
    token = store.create_confirmation("wallet", {})
    assert set(store._confirmations) == {token}


@pytest.mark.parametrize("max_pending", [0, -1])
def test_pending_limit_must_be_positive(max_pending):
    """Reject a store configuration that could never issue a confirmation.

    Args:
        max_pending: An invalid capacity bound.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="positive"):
        ConfirmationStore(max_pending=max_pending)


def test_forged_and_cross_store_tokens_remain_invalid_after_reclamation():
    """Do not confuse invented, altered, or other-process tokens with expired IDs.

    Args:
        None.

    Returns:
        None.
    """
    store = ConfirmationStore()
    token = store.create_confirmation("wallet", {})
    store.consume_confirmation(token, "wallet", {})
    altered = token[:-1] + ("0" if token[-1] != "0" else "1")
    other_token = ConfirmationStore().create_confirmation("wallet", {})
    for invalid in (altered, other_token, "\N{SNOWMAN}", token + ".extra"):
        with pytest.raises(ConfirmationError, match="^INVALID_CONFIRMATION$"):
            store.consume_confirmation(invalid, "wallet", {})
