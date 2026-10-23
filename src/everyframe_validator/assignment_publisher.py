"""Publish an immutable chain-epoch assignment. No chain wallet or transactions."""
import argparse
import asyncio
import json
from pathlib import Path

from . import assignment
from .core.chain_scope import scope, read_network, rpc_options
from .core.reward_policy import digest
from .feed import PROFILES, verify
from .publish import load_signing_key, sign
from .scheduled import schedule
from .state import atomic_json


async def publish(args):
    import bittensor as bt
    profile = PROFILES[args.network]
    domain = scope(profile['network'], profile['netuid'])
    expected = args.expected_public_key or profile['publicKey']
    key = load_signing_key(args.signing_key, expected)
    async with bt.Client(network=read_network(domain), **rpc_options(domain), fallback_endpoints=[],
                         archive_endpoints=[], retry_forever=False) as client:
        if await client._substrate.block_hash(0) != domain['genesis']:
            raise ValueError('unexpected_chain_genesis')
        h = await client._substrate.raw.get_chain_finalised_head()
        block = await client._substrate.raw.get_block_number(h)
        params = await client.subnets.subnet_hyperparameters(domain['netuid'], block=block)
        view = {'block': block, 'params': params}
        s = await schedule(client, domain, block)
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
        envelope = sign(p, key)
        assignment.authenticate(json.dumps(envelope), expected, domain, view, s)
    output = Path(args.output).absolute()
    if output.is_symlink():
        raise ValueError('assignment_output_symlink')
    output.mkdir(parents=True, exist_ok=True)
    target = output / (str(s['SubnetEpochIndex']) + '.json')
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
    a = p.parse_args()
    print(json.dumps(asyncio.run(asyncio.wait_for(publish(a), timeout=180))))


if __name__ == '__main__':
    main()
