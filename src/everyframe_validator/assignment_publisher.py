"""Publish an immutable chain-epoch assignment. No chain wallet or transactions."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import stat

from . import assignment
from .core.chain_scope import scope, read_network, rpc_options
from .core.reward_policy import digest
from .feed import PROFILES, strict_json, verify
from .publish import load_signing_key, sign
from .scheduled import schedule
from .state import atomic_json, read_private
from .core import burn_selection
from .core import contest_pool, prize_pool


def add_treasury_arguments(parser):
    """Operator naming aliases retain the existing protocol/config destinations."""
    parser.add_argument('--treasury-wallet-from-chain-epoch', '--contest-pool-from-chain-epoch',
                        dest='contest_pool_from_chain_epoch', type=int,
                        help='Future chain epoch for coordinated treasury wallet activation')
    parser.add_argument('--treasury-wallet-coldkey', '--contest-pool-coldkey',
                        dest='contest_pool_coldkey', help='Non-owner treasury wallet coldkey')
    parser.add_argument('--treasury-wallet-hotkey', '--contest-pool-hotkey',
                        dest='contest_pool_hotkey', help='Legacy fixed-recipient mode only')


def contest_activation(args, output):
    names = ('contest_pool_from_chain_epoch', 'contest_pool_hotkey', 'contest_pool_coldkey')
    values = [getattr(args, k, None) for k in names]
    requested = None
    if any(v is not None for v in values):
        epoch, hotkey, coldkey = values
        if coldkey is None or type(epoch) is not int or epoch < 0:
            raise ValueError('contest_pool_requires_future_epoch_and_coldkey')
        if hotkey:
            contest_pool.identity({'hotkey': hotkey, 'coldkey': coldkey})
            requested = {'policy':assignment.CONTEST_POLICY, 'fromChainEpoch':epoch, 'hotkey':hotkey, 'coldkey':coldkey}
        else:
            prize_pool.identity({'coldkey':coldkey})
            requested = {'policy':assignment.PRIZE_POLICY, 'fromChainEpoch':epoch, 'coldkey':coldkey}
    path = output / 'contest-pool-activation.json'
    try:
        pinned = read_private(path)
    except FileNotFoundError:
        return requested, False
    rotating = isinstance(pinned, dict) and pinned.get('policy') == assignment.PRIZE_POLICY
    if (not isinstance(pinned, dict) or set(pinned) != ({'policy', 'fromChainEpoch', 'coldkey'} if rotating else {'policy', 'fromChainEpoch', 'hotkey', 'coldkey'})
            or pinned['policy'] not in (assignment.CONTEST_POLICY, assignment.PRIZE_POLICY) or type(pinned['fromChainEpoch']) is not int
            or pinned['fromChainEpoch'] < 0):
        raise ValueError('invalid_contest_pool_activation')
    (prize_pool.identity({'coldkey':pinned['coldkey']}) if rotating else contest_pool.identity({k:pinned[k] for k in ('hotkey','coldkey')}))
    if requested is not None and requested != pinned:
        raise ValueError('contest_pool_activation_conflict')
    return pinned, True


async def publish(args):
    output = Path(args.output).absolute()
    if output.is_symlink():
        raise ValueError('assignment_output_symlink')
    output.mkdir(parents=True, exist_ok=True)
    # Serialize both policy adoption and publication, including standalone CLI
    # invocations. The OS releases this lock on crash; never delete it to retry.
    fd = os.open(output / '.publisher.lock',
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('invalid_assignment_publisher_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('assignment_publisher_busy') from None
        return await _publish(args, output)
    finally:
        os.close(fd)


async def _publish(args, output):
    import bittensor as bt
    profile = PROFILES[args.network]
    domain = scope(profile['network'], profile['netuid'])
    expected = args.expected_public_key or profile['publicKey']
    key = load_signing_key(args.signing_key, expected)
    contest, contest_pinned = contest_activation(args, output)
    requested = getattr(args, 'burn_rotation_from_chain_epoch', None)
    if requested is not None and (type(requested) is not int or requested < 0):
        raise ValueError('invalid_burn_rotation_activation_epoch')
    activation_path = output / 'burn-activation.json'
    try:
        pinned = read_private(activation_path)
    except FileNotFoundError:
        pinned = None
    else:
        if (not isinstance(pinned, dict) or set(pinned) != {'policy', 'fromChainEpoch'}
                or pinned['policy'] != assignment.ROTATING_POLICY
                or type(pinned['fromChainEpoch']) is not int or pinned['fromChainEpoch'] < 0):
            raise ValueError('invalid_burn_policy_pin')
        if requested is not None and requested != pinned['fromChainEpoch']:
            raise ValueError('burn_activation_conflict')
        requested = pinned['fromChainEpoch']
    async with bt.Client(network=read_network(domain), **rpc_options(domain), fallback_endpoints=[],
                         archive_endpoints=[], retry_forever=False) as client:
        if await client._substrate.block_hash(0) != domain['genesis']:
            raise ValueError('unexpected_chain_genesis')
        h = await client._substrate.raw.get_chain_finalised_head()
        block = await client._substrate.raw.get_block_number(h)
        params = await client.subnets.subnet_hyperparameters(domain['netuid'], block=block)
        view = {'block': block, 'params': params}
        s = await schedule(client, domain, block)
        if contest and not contest_pinned and contest['fromChainEpoch'] <= s['SubnetEpochIndex']:
            raise ValueError('contest_pool_activation_must_be_future')
        epoch, mode = assignment.target(view, s)
        payload = None
        if mode == 'signed_epoch':
            directory = Path(args.epochs_dir)
            file = directory / (str(epoch) + '.json')
            if directory.is_symlink() or file.is_symlink():
                raise ValueError('assignment_report_symlink')
            with file.open('rb') as f:
                raw = f.read(2_000_001)
            payload = verify(raw, expected, domain, epoch)
            a = payload['artifact']
            if (a['endBlockHash'] != await client._substrate.block_hash(a['endBlock'])
                    or a['closeBlockHash'] != await client._substrate.block_hash(a['endBlock'] - 1)):
                raise ValueError('assignment_accounting_chain_provenance_mismatch')
            await assignment.verify_emissions(client, a, getattr(args, 'verification_root', None), persist=True)
        p = assignment.project(domain, view, s, payload)
        if contest and not contest_pinned:
            await contest_pool.from_chain(client, p, {k: contest[k] for k in ('hotkey', 'coldkey') if k in contest})
        activation = requested
        if contest and s['SubnetEpochIndex'] >= contest['fromChainEpoch']:
            recipient = {k: contest[k] for k in ('hotkey', 'coldkey') if k in contest}
            selection = await contest_pool.from_chain(client, p, recipient)
            p = assignment.project(domain, view, s, payload, contest_pool=selection)
        elif activation is not None:
            if type(activation) is not int or activation < 0:
                raise ValueError('invalid_burn_rotation_activation_epoch')
            if s['SubnetEpochIndex'] >= activation:
                selection = await burn_selection.from_chain(client, p)
                p = assignment.project(domain, view, s, payload, burn_selection=selection)
        envelope = sign(p, key)
        assignment.authenticate(json.dumps(envelope), expected, domain, view, s)
    target = output / (str(s['SubnetEpochIndex']) + '.json')
    # Refuse a conflicting existing assignment before pinning new policy. Pin
    # before exposing a new assignment so a crash cannot silently revert v2.
    if target.is_symlink():
        raise ValueError('immutable_file_conflict')
    try:
        with target.open('rb') as f:
            previous = strict_json(f.read(2_000_001))
    except FileNotFoundError:
        pass
    else:
        if previous != envelope:
            raise ValueError('immutable_file_conflict')
    if requested is not None and pinned is None:
        atomic_json(activation_path, {'policy': assignment.ROTATING_POLICY, 'fromChainEpoch': requested}, immutable=True)
    if contest and not contest_pinned:
        atomic_json(output / 'contest-pool-activation.json', contest, immutable=True)
    atomic_json(target, envelope, immutable=True)
    target.chmod(0o644)
    return {'state': 'assignment_published_locally', 'chainEpoch': s['SubnetEpochIndex'],
            'accountingEpoch': epoch, 'mode': mode, 'assignmentHash': digest(p),
            'publicFile': str(target), 'chainTransactions': 0, 'walletAccess': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--network', choices=PROFILES, default='mainnet')
    for name in ('epochs-dir', 'output', 'signing-key'):
        p.add_argument('--'+name, required=True)
    p.add_argument('--expected-public-key')
    add_treasury_arguments(p)
    p.add_argument('--burn-rotation-from-chain-epoch', type=int,
                   help='Opt in to v2 deterministic owner-burn assignments from a future chain epoch, after all validators upgrade')
    a = p.parse_args()
    print(json.dumps(asyncio.run(asyncio.wait_for(publish(a), timeout=180))))


if __name__ == '__main__':
    main()
