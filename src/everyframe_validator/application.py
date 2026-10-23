"""Finalized reveal and first subsequent mechanism-step evidence. No signing.

Binary searches use monotonic queue membership / LastMechansimStepBlock, not a
client-side guess about epoch offsets. Ambiguous evidence is never a payout.
"""
import asyncio
import json
import sqlite3

from .core.reconcile_weights import is_reveal_event


async def first_true(lo, hi, predicate):
    # Caller establishes false at lo and true at hi.
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if await predicate(mid):
            hi = mid
        else:
            lo = mid
    return hi


def vector_matches(record, weights):
    expected = {int(k): v for k, v in record['plan']['quantized'].items()}
    return (isinstance(weights, list) and len(dict(weights)) == len(weights)
            and dict(weights) == expected)


async def inspect(client, row, head, pending):
    record, result = row['record'], row['result']
    netuid, hotkey, uid = record['netuid'], record['validatorHotkey'], record['plan']['validatorUid']
    included = int(result['extrinsic_id'].split('-')[0])
    report = dict(result.get('rewardObservation', {}))
    report.update(finalizedBlock=head, alphaSettlementConfirmed=False)
    if report.get('terminal'):
        return report
    async def query(name, args, block):
        return await client.query(('SubtensorModule', name), args, block=block)
    async def weights(block):
        return await query('Weights', [netuid, uid], block)
    async def identity(block):
        validator = await query('Keys', [netuid, uid], block)
        if validator != hotkey:
            return False
        for target, key in record['plan']['identities'].items():
            if await query('Keys', [netuid, int(target)], block) != key:
                return False
        return True
    if not report.get('revealConfirmed'):
        if record['commitReveal']:
            if any(p['hotkey'] == hotkey and p['commit_block'] == included for p in pending):
                return {**report, 'state': 'pending_reveal'}
            async def absent(block):
                commits = await (await client.at(block)).read('timelocked_weight_commits', netuid=netuid)
                return not any(p['hotkey'] == hotkey and p['commit_block'] == included
                               for items in commits.values() for p in items)
            if await absent(included):
                return {**report, 'state': 'commit_binding_unverified', 'terminal': True}
            reveal = await first_true(included, head, absent)
            events = await client.query(('System', 'Events'), [], block=reveal)
            if sum(is_reveal_event(e, netuid, hotkey) for e in events or []) != 1:
                return {**report, 'state': 'reveal_ambiguous_or_expired', 'terminal': True,
                        'queueRemovedBlock': reveal}
        else:
            reveal = included
        if not vector_matches(record, await weights(reveal)) or not await identity(reveal):
            return {**report, 'state': 'reveal_vector_mismatch', 'terminal': True,
                    'queueRemovedBlock': reveal}
        report.update(revealConfirmed=True, revealBlock=reveal,
                      revealBlockHash=await client._substrate.block_hash(reveal), state='revealed')
    reveal = report['revealBlock']
    # Conservatively require a subsequent step. A reveal exactly on the step
    # block could have occurred before or after the mechanism hook.
    async def stepped(block):
        at = await query('LastMechansimStepBlock', [netuid], block)
        if type(at) is not int or not 0 <= at <= block:
            raise ValueError('mechanism_step_evidence_unavailable')
        return at > reveal
    if not await stepped(head):
        return {**report, 'state': 'awaiting_mechanism_step'}
    step = await first_true(reveal, head, stepped)
    before, after = await asyncio.gather(weights(step - 1), weights(step))
    if not vector_matches(record, before) or not vector_matches(record, after) or not await identity(step):
        return {**report, 'state': 'not_applied_at_next_step', 'terminal': True,
                'mechanismBlock': step, 'applicationConfirmed': False}
    # Emission is a chain observation, not a bank transfer, cash value or an
    # exact attribution to this validator among all other validators.
    emissions = await query('Emission', [netuid], step)
    if not isinstance(emissions, list):
        raise ValueError('mechanism_emission_evidence_unavailable')
    return {**report, 'state': 'application_observed', 'terminal': True,
            'applicationConfirmed': True, 'mechanismBlock': step,
            'mechanismBlockHash': await client._substrate.block_hash(step),
            'emissionRaoByUid': {str(k): emissions[int(k)] for k in record['plan']['identities']
                                 if int(k) < len(emissions)},
            'note': 'Vector verified across a mechanism step; alpha settlement is not inferred.'}


async def observe(client, root, rows, head, pending):
    reports = []
    candidates = [r for r in rows if r['record'].get('cadence') in ('chain-epoch-v2', 'chain-epoch-v3')
                  and r['state'] in ('commit_finalized', 'finalized', 'pending_reveal')
                  and not r['result'].get('rewardObservation', {}).get('terminal')]
    # Prioritize the oldest allocation, bounding each iteration's archive work.
    candidates.sort(key=lambda r: r['record']['block'])
    for row in candidates[:3]:
        report = await inspect(client, row, head, pending)
        result = {**row['result'], 'rewardObservation': report,
                  'revealConfirmed': report.get('revealConfirmed', False),
                  'applicationConfirmed': report.get('applicationConfirmed', False),
                  'alphaSettlementConfirmed': False}
        state = ('application_observed' if report.get('applicationConfirmed') else 'application_needs_review') \
            if report.get('terminal') else row['state']
        with sqlite3.connect(root / 'journal.db') as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('UPDATE weight_runs SET state=?,result=? WHERE id=? AND state=?',
                       (state, json.dumps(result), row['id'], row['state']))
            db.commit()
        reports.append({'runId': row['id'], **report})
    return reports
