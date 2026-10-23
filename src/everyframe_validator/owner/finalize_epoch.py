"""Seal the newest closed reward accounting window; never signs or sends payments.

Uses finalized chain timestamps, actual per-block alpha-out emissions, the
epoch-close alpha/TAO reserve ratio, and a closed Kraken TAO/USD minute candle.
The trusted coordinator's operator-reviewed earnings are the work-value source.
"""
import argparse
import asyncio
from fractions import Fraction
import json
import os
from pathlib import Path
import time
from urllib.request import Request, urlopen

from .reward_policy import POLICY, MINER_SHARE_TEXT, MINER_SHARE_POLICY, digest, recognition_snapshot, load_epoch, positive, seal_epoch
from ..core.chain_scope import scope, add_network, check_database, read_network
from ..core.emission_policy import CHAIN_SHARE_POLICY, collect_evidence, summarize
from .reward_recovery import add_recoveries

GENESIS = '0x8f9cf856bf558a14440e75569c9e58594757048d7b3a84b5d25f6bd978263105'


def candle_price(payload, end_ms):
    if payload.get('error') or not isinstance(payload.get('result'), dict):
        raise ValueError('USD oracle unavailable')
    candles = payload['result'].get('TAOUSD')
    if not isinstance(candles, list):
        raise ValueError('unexpected USD oracle pair')
    valid = [c for c in candles[:-1] if len(c) == 8 and
             end_ms - 300000 <= (int(c[0]) + 60) * 1000 <= end_ms and int(c[7]) > 0]
    if not valid:
        raise ValueError('no recent closed USD candle at epoch close')
    candle = max(valid, key=lambda c: int(c[0]))
    value = positive(candle[4])
    return {'source': 'kraken:TAOUSD:1m:close', 'observedAt': (int(candle[0]) + 60) * 1000,
            'taoUsd': str(value), 'candleHash': digest(candle)}


def fetch_price(end_ms):
    # Fixed allowlisted public endpoint; no operator/miner-provided URLs or secrets.
    req = Request('https://api.kraken.com/0/public/OHLC?pair=TAOUSD&interval=1',
                  headers={'User-Agent': 'everyframe-subnet-finalizer/2'})
    with urlopen(req, timeout=15) as response:
        raw = response.read(2000001)
    if len(raw) > 2000000:
        raise ValueError('USD oracle response too large')
    return candle_price(json.loads(raw), end_ms)


async def collect_chain(client, netuid, network='test', *, chain_emissions=True):
    domain = scope(network, netuid)
    if await client._substrate.block_hash(0) != domain['genesis']:
        raise ValueError('unexpected chain genesis')
    head_hash = await client._substrate.raw.get_chain_finalised_head()
    head = await client._substrate.raw.get_block_number(head_hash)
    params = await client.subnets.subnet_hyperparameters(netuid, block=head)
    period = params['tempo'] + 1
    # Bounded RPC work; production pilot uses tempo 360, not arbitrary historical scans.
    if not 2 <= period <= 1000:
        raise ValueError('epoch period outside supported finalizer bound')
    epoch = head // period - 1
    # These are fixed UTC-chain accounting windows, not subnet-offset Yuma
    # distribution epochs. Validator and finalizer use the same window ID.
    if epoch < 0:
        raise ValueError('no closed epoch')
    start, end = epoch * period, (epoch + 1) * period
    start_ms, end_ms, reserve_tao, reserve_alpha, old_params, end_hash, close_hash = await asyncio.gather(
        client.query(('Timestamp', 'Now'), [], block=start),
        client.query(('Timestamp', 'Now'), [], block=end),
        client.query(('SubtensorModule', 'SubnetTAO'), [netuid], block=end - 1),
        client.query(('SubtensorModule', 'SubnetAlphaIn'), [netuid], block=end - 1),
        client.subnets.subnet_hyperparameters(netuid, block=start),
        client._substrate.block_hash(end), client._substrate.block_hash(end - 1))
    if old_params['tempo'] + 1 != period:
        raise ValueError('tempo changed during epoch')
    if any(type(v) is not int or v <= 0 for v in (start_ms, end_ms, reserve_tao, reserve_alpha)):
        raise ValueError('missing chain time or alpha liquidity')
    gate = asyncio.Semaphore(8)

    async def emission(block):
        async with gate:
            value = await client.query(('SubtensorModule', 'SubnetAlphaOutEmission'), [netuid], block=block)
        if type(value) is not int or value < 0:
            raise ValueError('invalid chain emission schema')
        return value

    # Sum observed issuance rates over the whole epoch, not a made-up fixed
    # emission allowance or one current rate projected over historical blocks.
    evidence = None
    if network == 'finney' and chain_emissions:
        evidence = await collect_evidence(client, netuid, start, end)
        emissions = [s[0] for s in evidence['samples']]
    else:
        emissions = await asyncio.gather(*(emission(block) for block in range(start, end)))
    total = sum(emissions)
    if total <= 0:
        raise ValueError('no positive alpha emission pool; defer rather than invent a USD pool')
    economics = {'minerShare': MINER_SHARE_TEXT}
    if network == 'finney':
        cut, old_cut, disposition = await asyncio.gather(
            client.query(('SubtensorModule', 'SubnetOwnerCut'), [], block=end-1),
            client.query(('SubtensorModule', 'SubnetOwnerCut'), [], block=start),
            client.query(('SubtensorModule', 'RecycleOrBurn'), [netuid], block=end-1))
        if (type(cut) is not int or not 0 <= cut < 65535
                or (evidence is None and (old_cut != cut
                    or old_params.get('owner_cut_enabled') != params.get('owner_cut_enabled')))
                or type(params.get('owner_cut_enabled')) is not bool
                or disposition not in ('Recycle', 'Burn')):
            raise ValueError('unsupported or changing mainnet emission policy')
        economics = {'minerShare': MINER_SHARE_TEXT, 'minerSharePolicy': MINER_SHARE_POLICY,
                     'ownerCutU16': cut, 'ownerCutEnabled': params['owner_cut_enabled'],
                     'unusedAllocation': disposition, 'accountingWindow': 'fixed-tempo-plus-one-v1'}
        if evidence is not None:
            total_rao, miner_rao = summarize(evidence, period)
            economics.update(minerShare=str(Fraction(miner_rao, total_rao)),
                             minerSharePolicy=CHAIN_SHARE_POLICY, emissionEvidence=evidence,
                             ownerCutEnabled=int(evidence['samples'][-1][1][2]) > 0)
    return {'version': 2, 'policy': POLICY, **domain,
            'epoch': epoch, 'blocksPerEpoch': period, 'startBlock': start, 'endBlock': end,
            'endBlockHash': end_hash, 'closeBlockHash': close_hash, 'finalizedBlock': head,
            'emissionsAlpha': str(Fraction(total, 10**9)), **economics,
            'emissionSource': 'sum:SubnetAlphaOutEmission:[startBlock,endBlock)',
            'emissionSamplesHash': digest(emissions), 'alphaTao': str(Fraction(reserve_tao, reserve_alpha)),
            'startMs': start_ms, 'endMs': end_ms}


async def main(args):
    import bittensor as bt
    os.umask(0o077)
    domain = scope(getattr(args, 'network', 'test'), args.netuid)
    if domain['network'] == 'finney':
        check_database(args.database, domain)
        if Path(args.epochs).exists():
            check_database(args.epochs, domain)
    async with bt.Client(network=read_network(domain), fallback_endpoints=[], archive_endpoints=[], retry_forever=False) as client:
        if Path(args.epochs).exists():
            if await client._substrate.block_hash(0) != domain['genesis']:
                raise ValueError('unexpected chain genesis')
            head_hash = await client._substrate.raw.get_chain_finalised_head()
            head = await client._substrate.raw.get_block_number(head_hash)
            params = await client.subnets.subnet_hyperparameters(args.netuid, block=head)
            target = head // (params['tempo'] + 1) - 1
            try:
                previous = load_epoch(args.epochs, args.netuid, target)
            except ValueError as error:
                if str(error) != 'newest closed epoch is not finalized':
                    raise
            else:
                if (previous['genesis'] != domain['genesis'] or previous['blocksPerEpoch'] != params['tempo'] + 1
                        or previous['endBlockHash'] != await client._substrate.block_hash(previous['endBlock'])):
                    raise ValueError('sealed epoch chain changed')
                print(json.dumps({'state': 'already_finalized', 'epoch': target, 'hash': digest(previous)}))
                return
        # Deliberate rollout gate: readers must be upgraded before the owner
        # emits a format that v0.5.0 correctly rejects as unknown.
        if getattr(args, 'chain_emission_policy', False):
            artifact = await collect_chain(client, args.netuid, domain['network'])
        else:
            artifact = await collect_chain(client, args.netuid, domain['network'], chain_emissions=False)
        if Path(args.epochs).exists():
            # Idempotent tick: never rebuild a previously published epoch using
            # new approvals or a later oracle response.
            try:
                previous = load_epoch(args.epochs, args.netuid, artifact['epoch'])
            except ValueError as error:
                if str(error) != 'newest closed epoch is not finalized':
                    raise
            else:
                if previous['endBlockHash'] != artifact['endBlockHash']:
                    raise ValueError('sealed epoch chain changed')
                print(json.dumps({'state': 'already_finalized', 'epoch': artifact['epoch'], 'hash': digest(previous)}))
                return
        price = await asyncio.to_thread(fetch_price, artifact['endMs'])
        artifact['priceSource'] = price
        artifact['alphaPriceUsd'] = str(positive(artifact.pop('alphaTao')) * positive(price['taoUsd']))
        artifact['window'] = recognition_snapshot(args.database, args.epochs, args.netuid,
            artifact.pop('startMs'), artifact.pop('endMs'), int(time.time() * 1000))
        # Explicit mapping enables recovery for registered, enabled, non-owner
        # miners only. Unregistered/disabled recipients remain owed, but must not
        # make every validator reject unrelated new work in this report.
        eligible = set()
        bindings_path = getattr(args, 'recovery_bindings', None)
        if bindings_path:
            import sqlite3
            from everyframe_validator.feed import strict_json
            bindings = strict_json(Path(bindings_path).read_bytes())
            if not isinstance(bindings, dict) or any(not isinstance(v, str) for v in bindings.values()):
                raise ValueError('invalid_recovery_bindings')
            graph = await client.subnets.metagraph(args.netuid, block=artifact['finalizedBlock'], commitments=False)
            owner = await client.query(('SubtensorModule', 'SubnetOwner'), [args.netuid], block=artifact['finalizedBlock'])
            owner_hotkey = await client.query(('SubtensorModule', 'SubnetOwnerHotkey'), [args.netuid], block=artifact['finalizedBlock'])
            registered = {n.hotkey for n in graph.neurons if n.coldkey != owner and n.hotkey != owner_hotkey}
            with sqlite3.connect(Path(args.database).resolve().as_uri()+'?mode=ro',uri=True) as db:
                enabled = {r[0] for r in db.execute('SELECT id FROM miners WHERE enabled=1')}
            eligible = {miner for miner, hotkey in bindings.items() if miner in enabled and hotkey in registered}
        artifact = add_recoveries(artifact, args.epochs, eligible_miners=eligible)
        # Publish only the newest closed epoch. Its work value includes eligible
        # approvals not recognized in an earlier seal, not a replay of old weights.
        latest_hash = await client._substrate.raw.get_chain_finalised_head()
        latest = await client._substrate.raw.get_block_number(latest_hash)
        if latest // artifact['blocksPerEpoch'] - 1 != artifact['epoch']:
            raise ValueError('epoch advanced while finalizing; retry newest epoch')
    hashed = seal_epoch(args.epochs, artifact)
    print(json.dumps({'state': 'finalized', 'epoch': artifact['epoch'], 'hash': hashed,
                      'miners': len(artifact['window']['scores']),
                      'recoveryAssignedMicrousd': sum(c['amountMicrousd'] for c in artifact['window']['recoveries']),
                      'paymentConfirmed': False, 'chainTransactions': 0}))


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    add_network(parser)
    parser.add_argument('--database', required=True, help='Trusted coordinator SQLite database')
    parser.add_argument('--epochs', required=True, help='Local immutable epoch artifact SQLite database')
    parser.add_argument('--recovery-bindings', help='Reviewed miner ID to hotkey map; enables approved recovery allocations for eligible current recipients')
    parser.add_argument('--chain-emission-policy', action='store_true',
                        help='Opt in to chain-derived miner budgets only after all validators accept chain-miner-emission-v1')
    parser.add_argument('--netuid', type=int, required=True)
    args = parser.parse_args()
    if not 0 < args.netuid < 4096:
        parser.error('invalid netuid')
    asyncio.run(asyncio.wait_for(main(args), timeout=180))


if __name__ == '__main__':
    cli()
