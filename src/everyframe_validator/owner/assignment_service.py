"""Read-only accounting source -> authenticated public chain-epoch assignments.

This publisher never loads a Bittensor wallet, submits weights, approves debt or
changes historical accounting. A separate validator cutover is still required.
"""
import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from types import SimpleNamespace

from .. import assignment
from ..assignment_publisher import publish as publish_assignment
from ..core.chain_scope import scope, read_network, rpc_options, check_database
from ..core.reward_policy import digest
from ..feed import PROFILES, verify, strict_json, FeedUnavailable
from ..publish import publish as publish_epoch
from ..scheduled import schedule
from ..state import atomic_json, private_directory


@contextmanager
def credential_key(source, runtime):
    """Use a systemd credential without relaxing the original key's permissions."""
    runtime = private_directory(runtime)
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        info = os.fstat(f.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or stat.S_IMODE(info.st_mode) not in (0o400, 0o440)):
            raise ValueError('systemd_readonly_credential_required')
        raw = f.read(16385)
        if not raw or len(raw) > 16384:
            raise ValueError('invalid_accounting_credential')
    with tempfile.TemporaryDirectory(prefix='accounting-key-',dir=runtime) as directory:
        path = Path(directory)/'key.pem'
        fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:
            f.write(raw)
        del raw
        yield path


def expose(value, directory, identifier):
    directory = Path(directory).absolute()
    if directory.is_symlink():
        raise ValueError('public_directory_symlink')
    directory.mkdir(mode=0o755,parents=True,exist_ok=True)
    path = directory/(str(identifier)+'.json')
    atomic_json(path,value,immutable=True)
    path.chmod(0o644)
    return path


async def tick(args, key):
    import bittensor as bt
    profile = PROFILES[args.network]
    domain = scope(profile['network'],profile['netuid'])
    expected = args.expected_public_key or profile['publicKey']
    check_database(args.epochs,domain)
    root = private_directory(args.state_dir)
    # Read actual finalized timing. Never infer the accounting target from the
    # previous publisher status, local state or wall-clock time.
    async with bt.Client(network=read_network(domain),**rpc_options(domain),fallback_endpoints=[],
                         archive_endpoints=[],retry_forever=False) as client:
        if await client._substrate.block_hash(0) != domain['genesis']:
            raise ValueError('unexpected_chain_genesis')
        h = await client._substrate.raw.get_chain_finalised_head()
        block = await client._substrate.raw.get_block_number(h)
        params = await client.subnets.subnet_hyperparameters(domain['netuid'],block=block)
        view = {'block':block,'params':params}
        s = await schedule(client,domain,block)
    epoch,mode = assignment.target(view,s)
    if mode == 'signed_epoch':
        # The newest published report may have advanced during this chain slot.
        # Re-expose the exact already-sealed target, never re-finalize or revise it.
        try:
            published = publish_epoch(SimpleNamespace(network=args.network,epochs=args.epochs,
                epoch=epoch,bindings=args.bindings,signing_key=str(key),expected_public_key=expected,
                output=str(root/'epochs')))
        except ValueError as exc:
            if str(exc) == 'newest closed epoch is not finalized':
                raise FeedUnavailable('assigned_accounting_not_sealed') from None
            raise
        raw = Path(published['publicFile']).read_bytes()
        verify(raw,expected,domain,epoch)
        expose(strict_json(raw),args.public_epochs,epoch)
    result = await publish_assignment(SimpleNamespace(network=args.network,expected_public_key=expected,
        signing_key=str(key),epochs_dir=str(root/'epochs'),output=str(root/'assignments')))
    if result['chainEpoch'] != s['SubnetEpochIndex'] or result['accountingEpoch'] != epoch:
        return {'state':'waiting','reason':'chain_epoch_advanced_during_publication',
                'chainTransactions':0,'walletAccess':False}
    raw = Path(result['publicFile']).read_bytes()
    selected = assignment.authenticate(raw,expected,domain,view,s)
    if digest(selected) != result['assignmentHash']:
        raise ValueError('assignment_publication_hash_mismatch')
    expose(strict_json(raw),args.public_assignments,result['chainEpoch'])
    return {'state':'assignment_feed_ready',**domain,'chainEpoch':result['chainEpoch'],
            'accountingEpoch':epoch,'mode':mode,'assignmentHash':result['assignmentHash'],
            'chainTransactions':0,'walletAccess':False,'historicalAccountingChanged':False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--network',choices=PROFILES,default='mainnet')
    for name in ('epochs','bindings','state-dir','public-epochs','public-assignments'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--expected-public-key')
    args = p.parse_args()
    os.umask(0o077)
    root = private_directory(args.state_dir)
    fd = os.open(root/'publisher.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('invalid_publisher_lock')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        credentials = os.environ.get('CREDENTIALS_DIRECTORY')
        runtime = os.environ.get('RUNTIME_DIRECTORY')
        if not credentials or not runtime:
            raise ValueError('systemd_accounting_credential_and_private_runtime_required')
        with credential_key(Path(credentials)/'accounting-signing-key',runtime) as key:
            result = asyncio.run(asyncio.wait_for(tick(args,key),timeout=120))
    except BlockingIOError:
        print(json.dumps({'state':'busy','chainTransactions':0,'walletAccess':False}))
        return
    except FeedUnavailable:
        result = {'state':'waiting','reason':'assigned_accounting_not_sealed',
                  'chainTransactions':0,'walletAccess':False}
    except Exception as exc:
        result = {'state':'failed','code':'assignment_publication_failed','errorType':type(exc).__name__,
                  'chainTransactions':0,'walletAccess':False}
    finally:
        os.close(fd)
    result['atMs'] = int(time.time()*1000)
    atomic_json(root/'status.json',result)
    print(json.dumps(result),flush=True)
    if result['state']=='failed':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
