"""Reconstructable, chain-derived miner budgets for Etao (runtime v480+).

Each sample uses block b's observed integer alpha-out rate and b-1's vector:
coinbase runs before block b's extrinsics. Mirror U96F32 share truncation and
integer credit flooring; never round a budget up. If alpha-out itself had
sub-rao precision, its observable integer rate gives a conservative budget.
Historical legacy/fixed-41 reports retain their original policy unchanged.
"""
import asyncio
from fractions import Fraction
import re

CHAIN_SHARE_POLICY = 'chain-miner-emission-v1'
EVIDENCE_SCHEMA = 'everyframe.emission.samples.v1'
WEIGHT_ONE = (1 << 64) - 1
FIXED_ONE = 1 << 32
MAX_SAMPLES = 1000
DECIMAL = re.compile(r'0|[1-9][0-9]{0,19}')


def parse_weights(value):
    if (not isinstance(value, list) or len(value) != 5 or any(
            not isinstance(v, str) or not DECIMAL.fullmatch(v) or int(v) > WEIGHT_ONE
            for v in value)):
        raise ValueError('invalid_emission_weight_vector')
    weights = [int(v) for v in value]
    if (sum(weights) != WEIGHT_ONE or weights[0] or weights[1]
            or weights[2] > WEIGHT_ONE * 33 // 100
            or weights[3] > WEIGHT_ONE // 2
            or weights[4] < WEIGHT_ONE * 33 // 100):
        raise ValueError('unsupported_emission_weight_vector')
    return weights


def credit_rao(alpha_out, weights):
    if type(alpha_out) is not int or not 0 <= alpha_out <= WEIGHT_ONE:
        raise ValueError('invalid_alpha_out_sample')
    # U96F32 division first, then multiplication, then the integer balance cast.
    return alpha_out * (weights[4] * FIXED_ONE // WEIGHT_ONE) // FIXED_ONE


def summarize(evidence, period):
    if (not isinstance(evidence, dict) or set(evidence) != {'schema', 'samples'}
            or evidence['schema'] != EVIDENCE_SCHEMA or type(period) is not int
            or not 2 <= period <= MAX_SAMPLES or not isinstance(evidence['samples'], list)
            or len(evidence['samples']) != period):
        raise ValueError('invalid_emission_evidence')
    total, miner = 0, 0
    for sample in evidence['samples']:
        if not isinstance(sample, list) or len(sample) != 2:
            raise ValueError('invalid_emission_sample')
        rate, raw = sample
        weights = parse_weights(raw)
        miner += credit_rao(rate, weights)
        total += rate
    if total <= 0 or miner <= 0:
        raise ValueError('no_positive_chain_miner_pool')
    return total, miner


def validate_evidence(artifact):
    if artifact.get('network') != 'finney' or artifact.get('minerSharePolicy') != CHAIN_SHARE_POLICY:
        raise ValueError('invalid_chain_emission_policy_domain')
    total, miner = summarize(artifact.get('emissionEvidence'), artifact['blocksPerEpoch'])
    if (Fraction(artifact['emissionsAlpha']) != Fraction(total, 10**9)
            or Fraction(artifact['minerShare']) != Fraction(miner, total)):
        raise ValueError('chain_miner_pool_does_not_match_evidence')
    return Fraction(miner, total)


def expand_history(start, end, history):
    """Expand validated changed-state records, applying the parent vector first."""
    if start - 1 not in history or set(history[start - 1]) != {'rate', 'weights'}:
        raise ValueError('missing_initial_emission_history')
    state = dict(history[start - 1])
    samples = []
    for block in range(start, end):
        parent_weights = state['weights']
        state.update(history.get(block, {}))
        weights = state['weights']
        if not isinstance(weights, list) or any(type(v) is not int for v in weights):
            raise ValueError('chain_emission_vector_unavailable')
        value = [state['rate'], list(map(str, parent_weights))]
        credit_rao(value[0], parse_weights(value[1]))
        samples.append(value)
    evidence = {'schema': EVIDENCE_SCHEMA, 'samples': samples}
    summarize(evidence, end - start)
    return evidence


async def collect_points(client, keys, start, end, first_hash):
    """Bounded safe-RPC path only for an explicitly disabled range RPC."""
    raw = client._substrate.raw
    gate = asyncio.Semaphore(4)

    async def point(block, block_hash=None):
        async with gate:
            block_hash = block_hash or await client._substrate.block_hash(block)
            result = await raw.rpc_request('state_queryStorageAt', [list(keys), block_hash])
        if (not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict)
                or set(result[0]) != {'block', 'changes'} or result[0]['block'] != block_hash
                or not isinstance(result[0]['changes'], list) or len(result[0]['changes']) != 2):
            raise ValueError('incomplete_emission_point_history')
        return block, result[0]

    # Check the oldest required state first. A pruned/unavailable archive must
    # fail before any fan-out, not consume hundreds of failed requests.
    initial = await point(start - 1, first_hash)
    tasks = [asyncio.create_task(point(block)) for block in range(start, end)]
    try:
        points = [initial, *await asyncio.gather(*tasks)]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    known = {group['block']: block for block, group in points}
    if len(known) != end - start + 1:
        raise ValueError('noncanonical_emission_point_history')
    return [group for _, group in points], known


async def collect_history(client, netuid, start, end):
    """Pinned SDK metadata + one bounded standard Substrate storage-history RPC.

    No raw keys or decoders are hardcoded. A range crossing a runtime-codec
    change is deferred. An explicitly unsupported/unsafe-disabled range RPC
    negotiates the bounded safe point-batch API, never on transport/rate errors.
    """
    from bittensor._transport.storage import decode_storage_value
    raw = client._substrate.raw
    first_hash, last_hash = await asyncio.gather(
        client._substrate.block_hash(start - 1), client._substrate.block_hash(end - 1))
    first_codec, codec = await asyncio.gather(
        raw._runtimes.codec_at(first_hash), raw._runtimes.codec_at(last_hash))
    if first_codec is not codec:
        raise ValueError('emission_window_spans_runtime_upgrade')
    names = ('SubnetAlphaOutEmission', 'SubnetEmissionWeights')
    entries = {name: codec.storage_entry('SubtensorModule', name) for name in names}
    keys = {'0x' + codec.storage_key(entries[name], [netuid]).hex(): name for name in names}
    if len(keys) != 2:
        raise ValueError('invalid_emission_storage_metadata')
    from bittensor._transport.errors import SubstrateRequestException
    known = {first_hash: start - 1, last_hash: end - 1}
    try:
        groups = await raw.rpc_request('state_queryStorage', [list(keys), first_hash, last_hash])
    except SubstrateRequestException as exc:
        payload = exc.payload or {}
        error = payload.get('error', payload)
        if not isinstance(error, dict):
            raise
        unsupported = (error.get('code') == -32601 or
                       (error.get('code') == 4003 and
                        error.get('message') == 'RPC call is unsafe to be called externally'))
        if not unsupported:
            raise
        groups, known = await collect_points(client, keys, start, end, first_hash)
    if not isinstance(groups, list) or not 1 <= len(groups) <= end - start + 1:
        raise ValueError('invalid_emission_storage_history')
    history, previous = {}, start - 2
    for group in groups:
        if (not isinstance(group, dict) or set(group) != {'block', 'changes'}
                or not isinstance(group['changes'], list) or not 1 <= len(group['changes']) <= 2):
            raise ValueError('invalid_emission_storage_history')
        block_hash = group['block']
        if block_hash in known:
            block = known[block_hash]
        else:
            header = await raw.rpc_request('chain_getHeader', [block_hash])
            if not isinstance(header, dict) or not isinstance(header.get('number'), str):
                raise ValueError('invalid_emission_history_header')
            block = int(header['number'], 16)
            if await client._substrate.block_hash(block) != block_hash:
                raise ValueError('noncanonical_emission_history_block')
        if not previous < block < end or block < start - 1:
            raise ValueError('unordered_emission_storage_history')
        previous = block
        changes = {}
        for change in group['changes']:
            if not isinstance(change, list) or len(change) != 2:
                raise ValueError('invalid_emission_storage_change')
            key, value = change
            if (not isinstance(key, str) or key not in keys
                    or not isinstance(value, str) or not re.fullmatch(r'0x(?:[a-fA-F0-9]{2})*', value)):
                raise ValueError('invalid_emission_storage_change')
            name = keys[key]
            field = 'rate' if name == names[0] else 'weights'
            if field in changes:
                raise ValueError('duplicate_emission_storage_change')
            changes[field] = decode_storage_value(codec, entries[name], bytes.fromhex(value[2:]))
        history[block] = changes
    return expand_history(start, end, history)


async def collect_evidence(client, netuid, start, end):
    if (type(start) is not int or type(end) is not int or start < 1
            or not 2 <= end - start <= MAX_SAMPLES):
        raise ValueError('invalid_emission_evidence_window')
    raw = getattr(getattr(client, '_substrate', None), 'raw', None)
    if raw is not None and hasattr(raw, '_runtimes') and callable(getattr(raw, 'rpc_request', None)):
        from bittensor._transport.errors import SubstrateRequestException
        from bittensor.result import ChainError
        try:
            return await collect_history(client, netuid, start, end)
        except (SubstrateRequestException, ChainError):
            raise ValueError('chain_emission_history_unavailable') from None
    gate = asyncio.Semaphore(8)

    async def sample(block):
        async with gate:
            rate, raw = await asyncio.gather(
                client.query(('SubtensorModule', 'SubnetAlphaOutEmission'), [netuid], block=block),
                client.query(('SubtensorModule', 'SubnetEmissionWeights'), [netuid], block=block - 1))
        if (not isinstance(raw, list) or len(raw) != 5
                or any(type(v) is not int or not 0 <= v <= WEIGHT_ONE for v in raw)):
            raise ValueError('chain_emission_vector_unavailable')
        value = [rate, [str(v) for v in raw]]
        credit_rao(rate, parse_weights(value[1]))
        return value

    tasks = [asyncio.create_task(sample(block)) for block in range(start, end)]
    try:
        samples = await asyncio.gather(*tasks)
    finally:
        # A single failed/archive read or cancellation must not leave RPC work
        # running behind a closed client or mark partially checked data trusted.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    evidence = {'schema': EVIDENCE_SCHEMA, 'samples': samples}
    summarize(evidence, end - start)
    return evidence


async def verify_chain_evidence(client, artifact):
    if artifact.get('minerSharePolicy') != CHAIN_SHARE_POLICY:
        return
    validate_evidence(artifact)
    expected = await collect_evidence(client, artifact['netuid'], artifact['startBlock'], artifact['endBlock'])
    if expected != artifact['emissionEvidence']:
        raise ValueError('chain_emission_evidence_mismatch')
