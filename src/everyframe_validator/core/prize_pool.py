"""Report-derived rotation across UIDs registered to one prize-pool coldkey."""
import asyncio
import re

from .burn_selection import owner_candidates, selection_index
from .reward_policy import digest
from ..state import atomic_json, read_private
from .treasury import is_release_treasury

POLICY = 'report-prize-pool-v1'
KEYS = {'policy', 'snapshotBlock', 'snapshotBlockHash', 'ownerColdkey', 'coldkey',
        'candidateHash', 'candidateCount', 'seedHash', 'uid', 'hotkey'}


def candidates(neurons, coldkey):
    try:
        return owner_candidates(neurons, coldkey)
    except ValueError as exc:
        if str(exc) == 'no_registered_owner_burn_candidates':
            raise ValueError('no_registered_prize_pool_uids') from None
        raise


def identity(value):
    from bittensor.wallets import is_bittensor_address
    if (not isinstance(value, dict) or set(value) != {'coldkey'}
            or not isinstance(value['coldkey'], str) or not is_bittensor_address(value['coldkey'])):
        raise ValueError('invalid_prize_pool_coldkey')
    return value


def seed_hash(assigned, coldkey, candidate_hash):
    return digest({'policy': POLICY, 'network': assigned['network'], 'netuid': assigned['netuid'],
                   'genesis': assigned['genesis'], 'chainEpoch': assigned['chainEpoch'],
                   'chainEpochStart': assigned['chainEpochStart'],
                   'chainEpochStartHash': assigned['chainEpochStartHash'],
                   'accountingEpoch': assigned['accountingEpoch'],
                   'mode': 'scheduled_reuse_pool' if assigned['mode'] == 'scheduled_reuse_burn' else assigned['mode'],
                   'payloadHash': assigned['payloadHash'], 'coldkey': coldkey, 'candidateHash': candidate_hash})


def validate(value, assigned=None):
    if (not isinstance(value, dict) or set(value) != KEYS or value['policy'] != POLICY
            or type(value['uid']) is not int or not 0 <= value['uid'] <= 65535
            or type(value['snapshotBlock']) is not int or value['snapshotBlock'] <= 0
            or type(value['candidateCount']) is not int or not 1 <= value['candidateCount'] <= 4096
            or not isinstance(value['snapshotBlockHash'], str)
            or not re.fullmatch('0x[a-f0-9]{64}', value['snapshotBlockHash'])
            or any(not isinstance(value[k], str) or not re.fullmatch('[a-f0-9]{64}', value[k])
                   for k in ('candidateHash', 'seedHash'))):
        raise ValueError('invalid_prize_pool_selection')
    identity({'coldkey': value['coldkey']})
    from bittensor.wallets import is_bittensor_address
    if not is_bittensor_address(value['hotkey']) or not is_bittensor_address(value['ownerColdkey']):
        raise ValueError('invalid_prize_pool_selection')
    if value['coldkey'] == value['ownerColdkey']:
        raise ValueError('contest_pool_must_not_belong_to_owner')
    if assigned and (value['snapshotBlock'] != assigned['chainEpochStart']
                     or value['snapshotBlockHash'] != assigned['chainEpochStartHash']
                     or value['seedHash'] != seed_hash(assigned, value['coldkey'], value['candidateHash'])):
        raise ValueError('prize_pool_wrong_snapshot_or_seed')
    return value


def derive(assigned, owner, neurons, coldkey):
    identity({'coldkey': coldkey})
    if owner == coldkey:
        raise ValueError('contest_pool_must_not_belong_to_owner')
    eligible = candidates(neurons, coldkey)
    hashed = digest(eligible)
    seed = seed_hash(assigned, coldkey, hashed)
    return validate({'policy': POLICY, 'snapshotBlock': assigned['chainEpochStart'],
                     'snapshotBlockHash': assigned['chainEpochStartHash'], 'ownerColdkey': owner,
                     'coldkey': coldkey, 'candidateHash': hashed, 'candidateCount': len(eligible),
                     'seedHash': seed, **eligible[selection_index(seed, len(eligible))]}, assigned)


async def from_chain(client, assigned, recipient):
    identity(recipient)
    coldkey = recipient['coldkey']; block = assigned['chainEpochStart']; netuid = assigned['netuid']
    if await client._substrate.block_hash(block) != assigned['chainEpochStartHash']:
        raise ValueError('prize_pool_wrong_snapshot_or_seed')
    owner, graph, owned = await asyncio.gather(
        client.query(('SubtensorModule', 'SubnetOwner'), [netuid], block=block),
        client.subnets.metagraph(netuid, block=block, commitments=False),
        client.query(('SubtensorModule', 'OwnedHotkeys'), [coldkey], block=block))
    if graph.block != block:
        raise ValueError('prize_pool_wrong_snapshot_or_seed')
    neurons = [{'uid':n.uid, 'hotkey':n.hotkey, 'coldkey':n.coldkey} for n in graph.neurons]
    eligible = candidates(neurons, coldkey)
    if (not isinstance(owned, list) or any(not isinstance(k, str) for k in owned)
            or len(owned) != len(set(owned)) or any(n['hotkey'] not in owned for n in eligible)
            or any(n['hotkey'] in owned and n['coldkey'] != coldkey for n in neurons)):
        raise ValueError('prize_pool_owner_mapping_inconsistent')
    return derive(assigned, owner, neurons, coldkey)


def accept(root, recipient):
    identity(recipient)
    value = {'policy': POLICY, **recipient}
    atomic_json(root / 'prize-pool-consent.json', value, immutable=True)
    return {'state': 'prize_pool_coldkey_accepted', **value, 'submittedTransaction': False}


async def verify(client, assigned, root=None, *, persist=False):
    pinned = None
    path = root / 'prize-pool-policy.json' if root else None
    if path:
        try: pinned = read_private(path)
        except FileNotFoundError: pass
    if pinned is not None:
        if (not isinstance(pinned, dict) or set(pinned) != {'policy', 'firstChainEpoch', 'coldkey'}
                or pinned['policy'] != POLICY or type(pinned['firstChainEpoch']) is not int
                or pinned['firstChainEpoch'] < 0):
            raise ValueError('invalid_prize_pool_pin')
        identity({'coldkey': pinned['coldkey']})
    value = assigned.get('contestPool', {})
    if value.get('policy') != POLICY:
        if pinned and assigned['chainEpoch'] >= pinned['firstChainEpoch']:
            raise ValueError('contest_pool_policy_downgrade')
        return
    validate(value, assigned)
    recipient = {'coldkey': value['coldkey']}
    if pinned and pinned['coldkey'] != value['coldkey']:
        raise ValueError('contest_pool_recipient_changed')
    if root:
        try:
            consent = read_private(root / 'prize-pool-consent.json')
        except FileNotFoundError:
            if not is_release_treasury(assigned, recipient['coldkey']):
                raise ValueError('contest_pool_acceptance_required') from None
        else:
            # Keep existing operator overrides and consent files authoritative.
            # Only an absent file uses the release-pinned mainnet default.
            if consent != {'policy': POLICY, **recipient}:
                raise ValueError('contest_pool_consent_mismatch')
    if await from_chain(client, assigned, recipient) != value:
        raise ValueError('contest_pool_does_not_match_chain')
    if path and persist:
        if pinned is None:
            atomic_json(path, {'policy': POLICY, 'firstChainEpoch': assigned['chainEpoch'], **recipient}, immutable=True)
        directory = root / 'verified-prize-pools'; directory.mkdir(mode=0o700, exist_ok=True)
        atomic_json(directory / (str(assigned['chainEpoch'])+'.json'),
                    {'assignmentHash':digest(assigned), 'contestPool':value}, immutable=True)
