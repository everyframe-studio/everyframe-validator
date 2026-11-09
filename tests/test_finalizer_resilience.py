import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from everyframe_validator.owner import finalize_epoch as finalizer


def test_finalizer_retries_transient_timeout_with_fresh_invocation(monkeypatch):
    main = AsyncMock(side_effect=[TimeoutError(), None])
    monkeypatch.setattr(finalizer, 'main', main)
    monkeypatch.setattr(finalizer.asyncio, 'sleep', AsyncMock())
    asyncio.run(finalizer.finalize_with_retry(SimpleNamespace()))
    assert main.await_count == 2


def test_validation_failure_is_never_retried(monkeypatch):
    main = AsyncMock(side_effect=ValueError('sealed epoch chain changed'))
    monkeypatch.setattr(finalizer, 'main', main)
    with pytest.raises(ValueError, match='sealed epoch chain changed'):
        asyncio.run(finalizer.finalize_with_retry(SimpleNamespace()))
    assert main.await_count == 1


def test_finalizer_uses_configured_authenticated_client(monkeypatch, tmp_path):
    import bittensor as bt
    configured = object()
    monkeypatch.setattr(finalizer, 'rpc_options', lambda domain: {'substrate': configured})
    def client(**kwargs):
        assert kwargs['substrate'] is configured
        assert kwargs['fallback_endpoints'] == []
        raise RuntimeError('authenticated_client_selected')
    monkeypatch.setattr(bt, 'Client', client)
    with pytest.raises(RuntimeError, match='authenticated_client_selected'):
        asyncio.run(finalizer.main(SimpleNamespace(network='test', netuid=566,
                                                  epochs=str(tmp_path/'epochs.db'))))
