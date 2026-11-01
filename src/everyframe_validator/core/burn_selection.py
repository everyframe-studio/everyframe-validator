"""Deterministic owner-only burn selection, bound to one finalized chain slot.

Public data makes this reproducible, not secret. It can disrupt stale-vector
copying but cannot exclude validators that reconstruct current public reports.
"""
import asyncio
import hashlib
import re

from .reward_policy import digest

POLICY = 'report-owner-burn-v1'
KEYS = {'policy', 'snapshotBlock', 'snapshotBlockHash', 'ownerColdkey',
        'candidateHash', 'candidateCount', 'seedHash', 'uid', 'hotkey'}


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def validate(selection, assigned=None):
    if (not isinstance(selection, dict) or set(selection) != KEYS
            or selection['policy'] != POLICY
            or type(selection['snapshotBlock']) is not int or selection['snapshotBlock'] <= 0
            or not isinstance(selection['snapshotBlockHash'], str)
            or re.fullmatch('0x[a-f0-9]{64}', selection['snapshotBlockHash']) is None
            or type(selection['candidateCount']) is not int
            or not 1 <= selection['candidateCount'] <= 4096
            or type(selection['uid']) is not int or not 0 <= selection['uid'] <= 65535
            or any(not isinstance(selection[k], str) or not 1 <= len(selection[k]) <= 128
                   for k in ('ownerColdkey', 'hotkey'))
            or any(not _hash(selection[k]) for k in ('candidateHash', 'seedHash'))):
        raise ValueError('invalid_burn_selection')
    if assigned is not None:
        if (selection['snapshotBlock'] != assigned['chainEpochStart']
                or selection['snapshotBlockHash'] != assigned['chainEpochStartHash']):
            raise ValueError('burn_selection_wrong_snapshot')
        if selection['seedHash'] != seed_hash(assigned, selection['candidateHash']):
            raise ValueError('burn_selection_wrong_seed')
    return selection


def owner_candidates(neurons, owner):
    if not isinstance(owner, str) or not owner or not isinstance(neurons, list):
        raise ValueError('invalid_burn_owner_snapshot')
    if len(neurons) > 4096:
        raise ValueError('invalid_burn_owner_snapshot')
    uids, keys, candidates = set(), set(), []
    for n in neurons:
        if (not isinstance(n, dict) or type(n.get('uid')) is not int
                or not 0 <= n['uid'] <= 65535
                or not isinstance(n.get('hotkey'), str) or not n['hotkey']
                or not isinstance(n.get('coldkey'), str) or not n['coldkey']
                or n['uid'] in uids or n['hotkey'] in keys):
            raise ValueError('invalid_burn_owner_snapshot')
        uids.add(n['uid']); keys.add(n['hotkey'])
        if n['coldkey'] == owner:
            candidates.append({'uid': n['uid'], 'hotkey': n['hotkey']})
    if not candidates:
        raise ValueError('no_registered_owner_burn_candidates')
    return sorted(candidates, key=lambda n: (n['uid'], n['hotkey']))


def seed_hash(assigned, candidate_hash):
    # No local clock, validator identity, latest metagraph, private key or RNG.
    # The mode is the explicit input marker when a reuse slot has no report.
    return digest({'policy': POLICY, 'network': assigned['network'],
                   'netuid': assigned['netuid'], 'genesis': assigned['genesis'],
                   'chainEpoch': assigned['chainEpoch'],
                   'chainEpochStart': assigned['chainEpochStart'],
                   'chainEpochStartHash': assigned['chainEpochStartHash'],
                   'accountingEpoch': assigned['accountingEpoch'],
                   'mode': assigned['mode'], 'payloadHash': assigned['payloadHash'],
                   'candidateHash': candidate_hash})


def selection_index(seed, count):
    """Unbiased hash-to-index with deterministic rejection expansion."""
    if not _hash(seed) or type(count) is not int or not 1 <= count <= 4096:
        raise ValueError('invalid_burn_selection_seed')
    ceiling = 2**256 - 2**256 % count
    value, counter = int(seed, 16), 0
    while value >= ceiling:
        counter += 1
        value = int.from_bytes(hashlib.sha256(bytes.fromhex(seed) + counter.to_bytes(8, 'big')).digest(), 'big')
    return value % count


def derive(assigned, owner, neurons):
    candidates = owner_candidates(neurons, owner)
    candidate_hash = digest(candidates)
    seed = seed_hash(assigned, candidate_hash)
    chosen = candidates[selection_index(seed, len(candidates))]
    return validate({'policy': POLICY, 'snapshotBlock': assigned['chainEpochStart'],
                     'snapshotBlockHash': assigned['chainEpochStartHash'],
                     'ownerColdkey': owner, 'candidateHash': candidate_hash,
                     'candidateCount': len(candidates), 'seedHash': seed, **chosen}, assigned)


async def from_chain(client, assigned):
    block, netuid = assigned['chainEpochStart'], assigned['netuid']
    if await client._substrate.block_hash(block) != assigned['chainEpochStartHash']:
        raise ValueError('burn_selection_wrong_snapshot')
    owner, graph = await asyncio.gather(
        client.query(('SubtensorModule', 'SubnetOwner'), [netuid], block=block),
        client.subnets.metagraph(netuid, block=block, commitments=False))
    if graph.block != block:
        raise ValueError('invalid_burn_owner_snapshot')
    neurons = [{'uid': n.uid, 'hotkey': n.hotkey, 'coldkey': n.coldkey} for n in graph.neurons]
    candidates = owner_candidates(neurons, owner)
    owned = await client.query(('SubtensorModule', 'OwnedHotkeys'), [owner], block=block)
    if (not isinstance(owned, list) or any(not isinstance(k, str) for k in owned)
            or len(set(owned)) != len(owned)
            or any(n['hotkey'] not in owned for n in candidates)
            or any(n['hotkey'] in owned and n['coldkey'] != owner for n in neurons)):
        raise ValueError('burn_owner_mapping_inconsistent')
    return derive(assigned, owner, neurons)


def resolve(neurons, owner, owner_hotkey, selection=None):
    """Use a previously verified selection; never reroll from current candidates."""
    if selection is None:
        burn = next((n for n in neurons if n['hotkey'] == owner_hotkey), None)
        if not owner or not burn or burn.get('coldkey') != owner:
            raise ValueError('burn target is not the registered subnet owner hotkey')
        return burn
    validate(selection)
    burn = next((n for n in neurons if n['uid'] == selection['uid']), None)
    if (owner != selection['ownerColdkey'] or not burn
            or burn['hotkey'] != selection['hotkey'] or burn.get('coldkey') != owner):
        raise ValueError('burn_selection_ownership_changed')
    return burn
