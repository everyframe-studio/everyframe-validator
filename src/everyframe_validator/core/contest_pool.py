"""Explicit non-owner contest funding, never represented as served work or burn."""
import asyncio
import re

from .reward_policy import digest
from ..state import read_private, atomic_json
from . import prize_pool

POLICY = 'contest-pool-remainder-v1'
WEIGHT_POLICY = 'epoch-value-contest-pool-v1'
KEYS = {'policy', 'snapshotBlock', 'snapshotBlockHash', 'ownerColdkey', 'uid', 'hotkey', 'coldkey'}


def identity(value):
    if (not isinstance(value, dict) or set(value) != {'hotkey', 'coldkey'}
            or any(not isinstance(value[k], str) or not re.fullmatch('[1-9A-HJ-NP-Za-km-z]{47,49}', value[k])
                   for k in value)):
        raise ValueError('invalid_contest_pool_identity')
    from bittensor.wallets import is_bittensor_address
    if not all(is_bittensor_address(v) for v in value.values()):
        raise ValueError('invalid_contest_pool_identity')
    return value


def validate(value, assigned=None):
    if isinstance(value, dict) and value.get('policy') == prize_pool.POLICY:
        return prize_pool.validate(value, assigned)
    if (not isinstance(value, dict) or set(value) != KEYS or value['policy'] != POLICY
            or type(value['uid']) is not int or not 0 <= value['uid'] <= 65535
            or type(value['snapshotBlock']) is not int or value['snapshotBlock'] <= 0
            or not isinstance(value['snapshotBlockHash'], str)
            or not re.fullmatch('0x[0-9a-f]{64}', value['snapshotBlockHash'])
            or not isinstance(value['ownerColdkey'], str) or not value['ownerColdkey']):
        raise ValueError('invalid_contest_pool')
    identity({k: value[k] for k in ('hotkey', 'coldkey')})
    if value['coldkey'] == value['ownerColdkey']:
        raise ValueError('contest_pool_must_not_belong_to_owner')
    if assigned is not None and (value['snapshotBlock'] != assigned['chainEpochStart']
                                or value['snapshotBlockHash'] != assigned['chainEpochStartHash']):
        raise ValueError('contest_pool_wrong_snapshot')
    return value


def resolve(neurons, owner, owner_hotkey, value):
    validate(value)
    matches = [n for n in neurons if n['uid'] == value['uid'] or n['hotkey'] == value['hotkey']]
    if (owner != value['ownerColdkey'] or len(matches) != 1
            or any(matches[0][k] != value[k] for k in ('uid', 'hotkey', 'coldkey'))
            or value['hotkey'] == owner_hotkey):
        raise ValueError('contest_pool_identity_changed')
    return matches[0]


async def from_chain(client, assigned, recipient):
    if set(recipient) == {'coldkey'}:
        return await prize_pool.from_chain(client, assigned, recipient)
    identity(recipient)
    block, netuid = assigned['chainEpochStart'], assigned['netuid']
    if await client._substrate.block_hash(block) != assigned['chainEpochStartHash']:
        raise ValueError('contest_pool_wrong_snapshot')
    owner, owner_hotkey, graph = await asyncio.gather(
        client.query(('SubtensorModule', 'SubnetOwner'), [netuid], block=block),
        client.query(('SubtensorModule', 'SubnetOwnerHotkey'), [netuid], block=block),
        client.subnets.metagraph(netuid, block=block, commitments=False))
    if graph.block != block:
        raise ValueError('contest_pool_wrong_snapshot')
    rows = [{'uid': n.uid, 'hotkey': n.hotkey, 'coldkey': n.coldkey} for n in graph.neurons]
    matches = [n for n in rows if n['hotkey'] == recipient['hotkey']]
    if len(matches) != 1 or matches[0]['coldkey'] != recipient['coldkey']:
        raise ValueError('contest_pool_not_registered')
    # Cross-check ownership storage as well as the metagraph at the same block.
    registered_owner = await client.query(('SubtensorModule', 'Owner'), [recipient['hotkey']], block=block)
    if registered_owner != recipient['coldkey']:
        raise ValueError('contest_pool_owner_mapping_inconsistent')
    value = validate({'policy': POLICY, 'snapshotBlock': block,
                      'snapshotBlockHash': assigned['chainEpochStartHash'], 'ownerColdkey': owner,
                      **matches[0]}, assigned)
    resolve(rows, owner, owner_hotkey, value)
    return value


def accept(root, recipient):
    """Explicit local consent. Does not authorize signing or mutate the wallet."""
    identity(recipient)
    value = {'policy': POLICY, **recipient}
    atomic_json(root / 'contest-pool-consent.json', value, immutable=True)
    return {'state': 'contest_pool_accepted', **value, 'submittedTransaction': False}


async def verify(client, assigned, root=None, *, persist=False):
    pin_path = root / 'contest-pool-policy.json' if root else None
    pinned = None
    if pin_path:
        try:
            pinned = read_private(pin_path)
        except FileNotFoundError:
            pass
    if pinned is not None:
        if (not isinstance(pinned, dict) or set(pinned) != {'policy', 'firstChainEpoch', 'hotkey', 'coldkey'}
                or pinned['policy'] != POLICY or type(pinned['firstChainEpoch']) is not int
                or pinned['firstChainEpoch'] < 0):
            raise ValueError('invalid_contest_pool_pin')
        identity({k: pinned[k] for k in ('hotkey', 'coldkey')})
    value = assigned.get('contestPool')
    if isinstance(value, dict) and value.get('policy') == prize_pool.POLICY:
        return
    if value is None:
        if pinned and assigned['chainEpoch'] >= pinned['firstChainEpoch']:
            raise ValueError('contest_pool_policy_downgrade')
        return
    validate(value, assigned)
    recipient = {k: value[k] for k in ('hotkey', 'coldkey')}
    if pinned and any(pinned[k] != recipient[k] for k in recipient):
        raise ValueError('contest_pool_recipient_changed')
    if root:
        try:
            consent = read_private(root / 'contest-pool-consent.json')
        except FileNotFoundError:
            raise ValueError('contest_pool_acceptance_required') from None
        if consent != {'policy': POLICY, **recipient}:
            raise ValueError('contest_pool_consent_mismatch')
    if await from_chain(client, assigned, recipient) != value:
        raise ValueError('contest_pool_does_not_match_chain')
    if pin_path and persist:
        if pinned is None:
            atomic_json(pin_path, {'policy': POLICY, 'firstChainEpoch': assigned['chainEpoch'], **recipient}, immutable=True)
        directory = root / 'verified-contest-pools'
        directory.mkdir(mode=0o700, exist_ok=True)
        atomic_json(directory / (str(assigned['chainEpoch']) + '.json'),
                    {'assignmentHash': digest(assigned), 'contestPool': value}, immutable=True)
