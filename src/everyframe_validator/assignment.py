"""One authenticated accounting assignment per actual chain epoch.

The target is derived from finalized epoch-start blocks, never a validator's
private backlog or request arrival time. HTTP absence is not zero work. The same
accounting window sometimes spans two chain slots (tempo vs tempo+1); the second
slot has an explicit deterministic burn rather than paying that report twice.
"""
import asyncio
import base64
from urllib.parse import urljoin

from .core.chain_scope import check_record
from .core.reward_policy import canonical, digest, seal_epoch
from .feed import fetch, strict_json, public_key, exact_keys, verify, validate_url
from .state import atomic_json

SCHEMA = 'everyframe.validator.assignment.v1'
POLICY = 'epoch-start-accounting-v1'
KEYS = {'schema', 'policy', 'network', 'netuid', 'genesis', 'chainEpoch', 'chainEpochStart',
        'chainEpochStartHash', 'previousChainEpochStart', 'cutoffBlock', 'accountingEpoch',
        'blocksPerEpoch', 'mode', 'payloadHash'}


def feed_url(base):
    validate_url(base)
    value = urljoin(base, '../allocations/')
    validate_url(value)
    return value


def target(view, s):
    period = view['params']['tempo'] + 1
    start, previous = s['LastEpochBlock'], s['previousChainEpochStart']
    if (type(period) is not int or not 2 <= period <= 1000
            or type(start) is not int or type(previous) is not int or not 0 < previous < start
            or type(s['SubnetEpochIndex']) is not int or s['SubnetEpochIndex'] < 1):
        raise ValueError('invalid_assignment_chain_schedule')
    epoch = start // period - 1
    if epoch < 0:
        raise ValueError('no_closed_assignment_epoch')
    return epoch, ('scheduled_reuse_burn' if previous // period - 1 == epoch else 'signed_epoch')


def project(domain, view, s, payload=None):
    epoch, mode = target(view, s)
    if mode == 'signed_epoch':
        if payload is None or payload['artifact']['epoch'] != epoch:
            raise ValueError('assignment_requires_exact_accounting_report')
        check_record(payload['artifact'], domain)
        if (payload['artifact']['blocksPerEpoch'] != view['params']['tempo'] + 1
                or payload['artifact']['finalizedBlock'] > view['block']):
            raise ValueError('assignment_accounting_not_finalized')
    elif payload is not None:
        raise ValueError('reuse_burn_cannot_reallocate_report')
    return {'schema': SCHEMA, 'policy': POLICY, **domain, 'chainEpoch': s['SubnetEpochIndex'],
            'chainEpochStart': s['LastEpochBlock'], 'chainEpochStartHash': s['chainEpochStartHash'],
            'previousChainEpochStart': s['previousChainEpochStart'], 'cutoffBlock': s['LastEpochBlock'],
            'accountingEpoch': epoch, 'blocksPerEpoch': view['params']['tempo'] + 1,
            'mode': mode, 'payloadHash': digest(payload) if payload is not None else None}


def authenticate(raw, key, domain, view, s):
    envelope = strict_json(raw)
    exact_keys(envelope, {'payload', 'signature'})
    p = envelope['payload']
    exact_keys(p, KEYS)
    public_key(key).verify(base64.b64decode(envelope['signature'], validate=True),
                           canonical(p).encode())
    check_record(p, domain)
    epoch, mode = target(view, s)
    expected = {'schema': SCHEMA, 'policy': POLICY, **domain, 'chainEpoch': s['SubnetEpochIndex'],
                'chainEpochStart': s['LastEpochBlock'], 'chainEpochStartHash': s['chainEpochStartHash'],
                'previousChainEpochStart': s['previousChainEpochStart'], 'cutoffBlock': s['LastEpochBlock'],
                'accountingEpoch': epoch, 'blocksPerEpoch': view['params']['tempo'] + 1, 'mode': mode}
    if (any(p[k] != v or type(p[k]) is not type(v) for k,v in expected.items())
            or (mode == 'scheduled_reuse_burn' and p['payloadHash'] is not None)
            or (mode == 'signed_epoch' and (not isinstance(p['payloadHash'], str)
                or len(p['payloadHash']) != 64 or any(c not in '0123456789abcdef' for c in p['payloadHash'])))):
        raise ValueError('assignment_does_not_match_finalized_chain_epoch')
    return p


async def verify_report(client, raw, key, domain, view, assignment):
    epoch = assignment['accountingEpoch']
    payload = verify(raw, key, domain, epoch)
    a = payload['artifact']
    if (digest(payload) != assignment['payloadHash'] or a['finalizedBlock'] > view['block']
            or a['blocksPerEpoch'] != view['params']['tempo'] + 1
            or a['endBlockHash'] != await client._substrate.block_hash(a['endBlock'])
            or a['closeBlockHash'] != await client._substrate.block_hash(a['endBlock'] - 1)):
        raise ValueError('assigned_accounting_provenance_mismatch')
    return payload


async def load(client, root, config, domain, view, s):
    raw = await asyncio.to_thread(fetch, feed_url(config['feed']), s['SubnetEpochIndex'])
    assignment = authenticate(raw, config['publicKey'], domain, view, s)
    accepted = root / 'assignments'
    accepted.mkdir(mode=0o700, exist_ok=True)
    atomic_json(accepted / (str(s['SubnetEpochIndex']) + '.json'),
                {'assignmentHash': digest(assignment)}, immutable=True)
    epoch = assignment['accountingEpoch']
    if assignment['mode'] == 'scheduled_reuse_burn':
        return None, epoch, assignment
    raw = await asyncio.to_thread(fetch, config['feed'], epoch)
    payload = await verify_report(client,raw,config['publicKey'],domain,view,assignment)
    accepted = root / 'accepted'
    accepted.mkdir(mode=0o700, exist_ok=True)
    atomic_json(accepted / (str(epoch) + '.json'), {'payloadHash': digest(payload)}, immutable=True)
    seal_epoch(root / 'epochs.db', payload['artifact'])
    return payload, epoch, assignment
