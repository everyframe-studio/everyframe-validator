import asyncio
import os

import pytest

from everyframe_validator.cli import parser
from everyframe_validator.core import rpc
from everyframe_validator.core.chain_scope import read_network, rpc_options, scope


@pytest.fixture(autouse=True)
def clear_rpc(monkeypatch):
    for name in ('EVERYFRAME_RPC_URL', 'EVERYFRAME_RPC_API_KEY', 'EVERYFRAME_RPC_API_KEY_FILE'):
        monkeypatch.delenv(name, raising=False)


def test_finney_default_and_release_defaults():
    assert read_network(scope('finney', 117)) == 'finney'
    assert read_network(scope('test', 566)) == 'test'
    args = parser().parse_args(['run', '--state-dir', '/state'])
    assert (args.interval, args.weight_interval_blocks, args.missing_accounting) == (15, 100, 'burn')


@pytest.mark.parametrize('url', ['https://example.org', 'ws://example.org',
                               'wss://user:password@example.org', 'wss://example.org?key=secret',
                               'wss://example.org#fragment', 'wss://example.org\n', 'wss://example.org:0'])
def test_invalid_endpoints_do_not_echo_credentials(url):
    with pytest.raises(ValueError, match='^invalid_rpc_endpoint$'):
        rpc.validate_endpoint(url)


@pytest.mark.parametrize('url', ['wss://rpc.example.org/path', 'ws://localhost:9944', 'ws://127.0.0.1:9944', 'ws://[::1]:9944'])
def test_explicit_endpoint_keeps_network_scope(monkeypatch, url):
    monkeypatch.setenv('EVERYFRAME_RPC_URL', url)
    assert read_network(scope('finney', 117)) == url
    assert scope('finney', 117)['netuid'] == 117


def test_auth_requires_explicit_tls_endpoint(monkeypatch):
    monkeypatch.setenv('EVERYFRAME_RPC_API_KEY', 'fixture-key')
    with pytest.raises(ValueError, match='rpc_auth_requires_explicit_wss_endpoint'):
        rpc_options(scope('finney', 117))


def test_private_key_file(tmp_path, monkeypatch):
    file = tmp_path / 'rpc-key'
    file.write_text('fixture-key\n')
    file.chmod(0o600)
    monkeypatch.setenv('EVERYFRAME_RPC_API_KEY_FILE', str(file))
    assert rpc.api_key() == 'fixture-key'
    file.chmod(0o644)
    with pytest.raises(ValueError, match='private_file_permissions'):
        rpc.api_key()
    file.chmod(0o600)
    link = tmp_path / 'link'
    link.symlink_to(file)
    monkeypatch.setenv('EVERYFRAME_RPC_API_KEY_FILE', str(link))
    with pytest.raises(OSError): rpc.api_key()


def test_key_never_enters_url_and_redirects_disabled(monkeypatch):
    import websockets.asyncio.client
    seen = []
    class Connection:
        def __init__(self, url, **kwargs):
            seen.append((url, kwargs, self))
        def process_redirect(self, exc):
            return 'wss://different-host.example'
        def __await__(self):
            async def result(): return self
            return result().__await__()
    monkeypatch.setattr(websockets.asyncio.client, 'connect', Connection)
    monkeypatch.setenv('EVERYFRAME_RPC_URL', 'wss://rpc.example.org')
    monkeypatch.setenv('EVERYFRAME_RPC_API_KEY', 'fixture-secret')
    substrate = rpc_options(scope('finney', 117))['substrate']
    interface = substrate._interface('wss://rpc.example.org', [])
    asyncio.run(interface._session._connect('wss://rpc.example.org'))
    url, kwargs, connection = seen[0]
    assert url == 'wss://rpc.example.org'
    assert kwargs['additional_headers'] == {'Authorization': 'Bearer fixture-secret'}
    error = RuntimeError('redirect')
    assert connection.process_redirect(error) is error
    with pytest.raises(ValueError, match='invalid_rpc_destination'):
        asyncio.run(interface._session._connect('wss://different-host.example'))
    assert len(seen) == 1


def test_conflicting_or_empty_credentials(tmp_path, monkeypatch):
    file = tmp_path / 'rpc-key'
    file.write_text('')
    file.chmod(0o600)
    monkeypatch.setenv('EVERYFRAME_RPC_API_KEY_FILE', str(file))
    with pytest.raises(ValueError, match='invalid_rpc_credentials'): rpc.api_key()
    monkeypatch.setenv('EVERYFRAME_RPC_API_KEY', 'fixture-key')
    with pytest.raises(ValueError, match='conflicting_rpc_credentials'): rpc.api_key()
