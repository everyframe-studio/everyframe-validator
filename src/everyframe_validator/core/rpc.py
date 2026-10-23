"""Explicit RPC selection with optional header auth and no hidden fallback."""
import asyncio
import os
import stat
from urllib.parse import urlsplit

DEFAULT_MAINNET_RPC = 'wss://archive.chain.opentensor.ai:443'


def validate_endpoint(value):
    try:
        url = urlsplit(value)
        valid = (isinstance(value, str) and value == value.strip() and len(value) <= 2048
                 and not any(ord(c) < 33 or ord(c) == 127 for c in value)
                 and url.hostname and not url.username and not url.password
                 and not url.query and not url.fragment and url.port != 0
                 and (url.scheme == 'wss' or (url.scheme == 'ws' and
                      url.hostname in ('localhost', '127.0.0.1', '::1'))))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError('invalid_rpc_endpoint')
    return value


def endpoint(domain):
    override = os.environ.get('EVERYFRAME_RPC_URL', '')
    return validate_endpoint(override) if override else (
        DEFAULT_MAINNET_RPC if domain['network'] == 'finney' else domain['network'])


def api_key():
    path = os.environ.get('EVERYFRAME_RPC_API_KEY_FILE')
    key = os.environ.get('EVERYFRAME_RPC_API_KEY', '')
    if path and key:
        raise ValueError('conflicting_rpc_credentials')
    if path:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError('private_file_permissions')
            key = stream.read(4097).strip()
    if (path and not key) or len(key) > 4096 or any(ord(c) < 33 or ord(c) == 127 for c in key):
        raise ValueError('invalid_rpc_credentials')
    return key


def options(domain):
    key = api_key()
    if not key:
        return {}
    url = endpoint(domain)
    # Never send an account credential to a default public endpoint by accident.
    if not os.environ.get('EVERYFRAME_RPC_URL') or not url.startswith('wss://'):
        raise ValueError('rpc_auth_requires_explicit_wss_endpoint')
    from bittensor._substrate import RpcSubstrate
    from bittensor._transport import SubstrateConnection
    from websockets.asyncio.client import connect

    class AuthenticatedSubstrate(RpcSubstrate):
        def _interface(self, target, fallbacks):
            if target != url or fallbacks:
                raise ValueError('invalid_rpc_destination')

            async def authenticated_connect(target):
                if target != url:
                    raise ValueError('invalid_rpc_destination')
                connection = connect(
                    target, additional_headers={'Authorization': 'Bearer ' + key},
                    max_size=2**32, write_limit=2**16,
                )
                if not callable(getattr(connection, 'process_redirect', None)):
                    raise ValueError('rpc_redirect_policy_unavailable')
                # websockets may otherwise forward custom auth headers on 3xx.
                connection.process_redirect = lambda exc: exc
                return await asyncio.wait_for(connection, timeout=10)
            return SubstrateConnection(url, ss58_format=42, fallback_urls=[],
                                       retry_forever=False, connect_factory=authenticated_connect)

    return {'substrate': AuthenticatedSubstrate(url, fallback_endpoints=[],
                                                archive_endpoints=[], retry_forever=False)}
