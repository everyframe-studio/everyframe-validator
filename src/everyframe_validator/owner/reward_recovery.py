"""Reviewed missed-reward carry-forward; accounting allocation is not settlement.

The owner's missing submission is NOT evidence that a miner received nothing:
other validators, previously revealed weights and pending reveals must be reviewed.
Only an explicit, finalized, all-validator assessment may open a recovery. Claims
are reserved atomically with a new immutable epoch, using its unused pool only.
"""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3

from ..core.chain_scope import check_database, check_record, scope
from .reward_policy import canonical, digest, load_epoch, pool_microusd, recognition_proofs

POLICY = 'reviewed-missed-reward-carry-v1'
ASSESSMENT_POLICY = 'global-finalized-emission-review-v1'
HASH = re.compile(r'[a-f0-9]{64}')
CHAIN_HASH = re.compile(r'0x[a-f0-9]{64}')
CLAIM_KEYS = {'id', 'sourceEpoch', 'sourceHash', 'minerId', 'amountMicrousd', 'jobs', 'evidenceHash'}


def integer(value, minimum=0):
    return type(value) is int and minimum <= value < 2**63


def tables(db):
    db.execute('''CREATE TABLE IF NOT EXISTS reward_recoveries (
        id TEXT PRIMARY KEY, netuid INTEGER NOT NULL, source_epoch INTEGER NOT NULL,
        miner_id TEXT NOT NULL, record TEXT NOT NULL, hash TEXT NOT NULL,
        UNIQUE(netuid,source_epoch,miner_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS reward_recovery_allocations (
        recovery_id TEXT NOT NULL, netuid INTEGER NOT NULL, epoch INTEGER NOT NULL,
        amount_microusd INTEGER NOT NULL CHECK(amount_microusd > 0),
        PRIMARY KEY(recovery_id,netuid,epoch))''')
    db.execute('''CREATE TABLE IF NOT EXISTS reward_recovery_settlements (
        recovery_id TEXT NOT NULL, netuid INTEGER NOT NULL, epoch INTEGER NOT NULL,
        record TEXT NOT NULL, hash TEXT NOT NULL,
        PRIMARY KEY(recovery_id,netuid,epoch))''')


def exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='reward_recoveries'").fetchone() is not None


def check_evidence(evidence, source):
    ek = {'finalizedBlock', 'finalizedBlockHash', 'observationStartBlock', 'observationEndBlock',
          'allValidatorRewardsReviewed', 'pendingCommitmentsResolved', 'evidenceHash'}
    if (not isinstance(evidence, dict) or set(evidence) != ek
            or any(not integer(evidence[k]) for k in
                   ('finalizedBlock', 'observationStartBlock', 'observationEndBlock'))
            or not 0 <= evidence['observationStartBlock'] < evidence['observationEndBlock']
            or not source['endBlock'] <= evidence['observationEndBlock'] <= evidence['finalizedBlock']
            or evidence['finalizedBlock'] < source['finalizedBlock']
            or evidence['allValidatorRewardsReviewed'] is not True
            or evidence['pendingCommitmentsResolved'] is not True
            or not isinstance(evidence['finalizedBlockHash'], str)
            or not CHAIN_HASH.fullmatch(evidence['finalizedBlockHash'])
            or not isinstance(evidence['evidenceHash'], str)
            or not HASH.fullmatch(evidence['evidenceHash'])):
        raise ValueError('recovery_requires_finalized_global_reward_review')


def assessment_record(assessment, source):
    """Validate a human-reviewed assessment, not infer payment from an owner log."""
    domain = scope(source['network'], source['netuid'])
    keys = {'version', 'policy', *domain, 'sourceEpoch', 'sourceHash', 'minerId',
            'observedRewardMicrousd', 'evidence'}
    if not isinstance(assessment, dict) or set(assessment) != keys:
        raise ValueError('invalid_recovery_assessment')
    check_record(assessment, domain)
    if (assessment['version'] != 1 or type(assessment['version']) is not int
            or assessment['policy'] != ASSESSMENT_POLICY
            or type(assessment['sourceEpoch']) is not int
            or assessment['sourceEpoch'] != source['epoch']
            or assessment['sourceHash'] != digest(source)
            or not isinstance(assessment['minerId'], str)):
        raise ValueError('recovery_source_mismatch')
    # A public projection or an aggregate-only fixture cannot prove the original
    # eligible work, nor be used to recursively recover a prior recovery claim.
    recognition_proofs(source)
    proofs = [p for p in source['window'].get('proofs', []) if p['minerId'] == assessment['minerId']]
    expected = sum(p['feeMicrousd'] for p in proofs)
    received = assessment['observedRewardMicrousd']
    if not integer(expected, 1) or not integer(received) or received >= expected:
        raise ValueError('no_confirmed_reward_shortfall')
    check_evidence(assessment['evidence'], source)
    identifier = digest({**domain, 'sourceEpoch': source['epoch'],
                         'sourceHash': digest(source), 'minerId': assessment['minerId']})
    return {'id': identifier, 'assessment': deepcopy(assessment),
            'expectedMicrousd': expected, 'shortfallMicrousd': expected - received, 'jobs': len(proofs)}


def settlement_record(assessment, original, target):
    domain = scope(target['network'], target['netuid'])
    keys = {'version', 'policy', *domain, 'recoveryId', 'allocationEpoch', 'allocationHash',
            'observedRecoveryMicrousd', 'evidence'}
    if not isinstance(assessment, dict) or set(assessment) != keys:
        raise ValueError('invalid_recovery_settlement')
    check_record(assessment, domain)
    claim = next((c for c in validate_claims(target) if c['id'] == original['id']), None)
    if (type(assessment['version']) is not int or assessment['version'] != 1
            or assessment['policy'] != ASSESSMENT_POLICY or assessment['recoveryId'] != original['id']
            or type(assessment['allocationEpoch']) is not int or assessment['allocationEpoch'] != target['epoch']
            or assessment['allocationHash'] != digest(target) or claim is None
            or not integer(assessment['observedRecoveryMicrousd'])
            or assessment['observedRecoveryMicrousd'] > claim['amountMicrousd']):
        raise ValueError('invalid_recovery_settlement')
    check_evidence(assessment['evidence'], target)
    if assessment['evidence']['observationStartBlock'] < original['assessment']['evidence']['observationEndBlock']:
        raise ValueError('recovery_settlement_overlaps_original_review')
    return deepcopy(assessment)


def settle(epochs, assessment, *, reviewed=False):
    """A reviewed failed/partial recovery reopens only its unpaid remainder.

    A seal, assignment or weight application alone never closes the obligation.
    Pending or uncertain rewards cannot be settled and are not retried blindly.
    """
    if reviewed is not True:
        raise ValueError('explicit_global_reward_review_required')
    domain = scope(assessment.get('network'), assessment.get('netuid'))
    check_database(epochs, domain)
    target = load_epoch(epochs, domain['netuid'], assessment.get('allocationEpoch'))
    with sqlite3.connect(epochs) as db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        available = {r['id']:r for r in records(db, domain['netuid'])}
        original = available.get(assessment.get('recoveryId'))
        if original is None:
            raise ValueError('unapproved_recovery_settlement')
        value = settlement_record(assessment, original, target)
        raw = canonical(value)
        key = (original['id'], domain['netuid'], target['epoch'])
        previous = db.execute('SELECT record FROM reward_recovery_settlements '
                              'WHERE recovery_id=? AND netuid=? AND epoch=?', key).fetchone()
        if previous:
            if previous[0] != raw:
                raise ValueError('immutable_recovery_settlement_conflict')
        else:
            # Reusing a reviewed emission range for two positive allocations is
            # unsafe without a separate, partitioned attribution review.
            for prior_raw, in db.execute('SELECT s.record FROM reward_recovery_settlements s '
                    'JOIN reward_recoveries r ON r.id=s.recovery_id AND r.netuid=s.netuid '
                    'WHERE r.netuid=? AND r.miner_id=?', (domain['netuid'], original['assessment']['minerId'])):
                prior = json.loads(prior_raw); e, old = value['evidence'], prior['evidence']
                overlaps = max(e['observationStartBlock'],old['observationStartBlock']) < min(
                    e['observationEndBlock'],old['observationEndBlock'])
                if overlaps and not (value['observedRecoveryMicrousd']==prior['observedRecoveryMicrousd']==0 and e==old):
                    raise ValueError('overlapping_recovery_reward_observations')
            db.execute('INSERT INTO reward_recovery_settlements VALUES(?,?,?,?,?)', (*key, raw, digest(value)))
    return {'id': original['id'], 'allocationEpoch':target['epoch'],
            'reviewedReceivedMicrousd':value['observedRecoveryMicrousd'],
            'state':'recovery_outcome_reviewed', 'chainTransactions':0}


def approve(epochs, assessment, *, reviewed=False):
    """Record explicit operator consent. Caller verifies the chain anchor first.

    This is an owner-reviewed accounting assertion, NOT independent proof of USD
    settlement. No receipt, payable, historical report or chain weight is changed.
    """
    if reviewed is not True:
        raise ValueError('explicit_global_reward_review_required')
    domain = scope(assessment.get('network'), assessment.get('netuid'))
    check_database(epochs, domain)
    source = load_epoch(epochs, domain['netuid'], assessment.get('sourceEpoch'))
    record = assessment_record(assessment, source)
    raw = canonical(record)
    with sqlite3.connect(epochs) as db:
        db.execute('PRAGMA synchronous=FULL')
        tables(db)
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT id,record FROM reward_recoveries WHERE netuid=? AND source_epoch=? AND miner_id=?',
                              (domain['netuid'], source['epoch'], assessment['minerId'])).fetchone()
        if previous:
            if previous != (record['id'], raw):
                raise ValueError('immutable_recovery_assessment_conflict')
        else:
            # Do not allow the same on-chain observation interval to be attributed
            # to several source reports for a miner. Review them as one range or
            # partition evidence into non-overlapping intervals before approval.
            for prior_raw, in db.execute('SELECT record FROM reward_recoveries WHERE netuid=? AND miner_id=?',
                                         (domain['netuid'], assessment['minerId'])):
                prior = json.loads(prior_raw)['assessment']['evidence']
                current = assessment['evidence']
                overlaps = max(prior['observationStartBlock'], current['observationStartBlock']) < min(
                        prior['observationEndBlock'], current['observationEndBlock'])
                # A single reviewed zero-emission range can establish several
                # missed reports. Nonzero emissions need disjoint attribution.
                same_zero_review = (assessment['observedRewardMicrousd'] == 0
                    and json.loads(prior_raw)['assessment']['observedRewardMicrousd'] == 0
                    and current == prior)
                if overlaps and not same_zero_review:
                    raise ValueError('overlapping_recovery_reward_observations')
            db.execute('INSERT INTO reward_recoveries VALUES(?,?,?,?,?,?)',
                       (record['id'], domain['netuid'], source['epoch'], assessment['minerId'], raw, digest(record)))
    return {'id': record['id'], 'state': 'reviewed_shortfall', 'shortfallMicrousd': record['shortfallMicrousd'],
            'chainTransactions': 0, 'paymentConfirmed': False}


def records(db, netuid):
    if not exists(db):
        for raw, in db.execute('SELECT artifact FROM reward_epochs WHERE netuid=?',(netuid,)):
            if validate_claims(json.loads(raw)):
                raise ValueError('recovery_assessment_journal_missing')
        return []
    # The immutable epoch seals are the other half of the reservation ledger.
    # A deleted allocation row must not silently make already assigned money due.
    sealed = {}
    for epoch, raw, hashed in db.execute(
            'SELECT epoch,artifact,hash FROM reward_epochs WHERE netuid=? ORDER BY epoch', (netuid,)):
        artifact = json.loads(raw)
        if digest(artifact) != hashed:
            raise ValueError('epoch_artifact_hash_mismatch')
        for c in validate_claims(artifact):
            sealed.setdefault(c['id'], []).append((epoch, c['amountMicrousd']))
    out = []
    for identifier, raw, hashed in db.execute(
            'SELECT id,record,hash FROM reward_recoveries WHERE netuid=? ORDER BY source_epoch,id', (netuid,)):
        r = json.loads(raw)
        if digest(r) != hashed or r.get('id') != identifier:
            raise ValueError('recovery_journal_hash_mismatch')
        source_epoch = r['assessment']['sourceEpoch']
        row = db.execute('SELECT artifact,hash FROM reward_epochs WHERE netuid=? AND epoch=?',
                         (netuid, source_epoch)).fetchone()
        if not row or digest(json.loads(row[0])) != row[1]:
            raise ValueError('recovery_source_hash_mismatch')
        if assessment_record(r['assessment'], json.loads(row[0])) != r:
            raise ValueError('invalid_recovery_journal_record')
        allocations = db.execute('SELECT epoch,amount_microusd FROM reward_recovery_allocations '
                                 'WHERE recovery_id=? AND netuid=? ORDER BY epoch', (identifier, netuid)).fetchall()
        if allocations != sealed.pop(identifier, []):
            raise ValueError('recovery_allocation_journal_mismatch')
        allocated, received, in_flight = 0, 0, 0
        for epoch, amount in allocations:
            row = db.execute('SELECT artifact,hash FROM reward_epochs WHERE netuid=? AND epoch=?',
                             (netuid, epoch)).fetchone()
            if not integer(amount, 1) or not row or digest(json.loads(row[0])) != row[1]:
                raise ValueError('recovery_allocation_journal_mismatch')
            claims = validate_claims(json.loads(row[0]))
            if sum(c['amountMicrousd'] for c in claims if c['id'] == identifier) != amount:
                raise ValueError('recovery_allocation_journal_mismatch')
            a = r['assessment']
            expected = {'id':identifier,'sourceEpoch':a['sourceEpoch'],'sourceHash':a['sourceHash'],
                        'minerId':a['minerId'],'amountMicrousd':amount,'jobs':r['jobs'],
                        'evidenceHash':a['evidence']['evidenceHash']}
            if next(c for c in claims if c['id']==identifier) != expected:
                raise ValueError('recovery_allocation_journal_mismatch')
            allocated += amount
            settlement = db.execute('SELECT record,hash FROM reward_recovery_settlements '
                                    'WHERE recovery_id=? AND netuid=? AND epoch=?', (identifier,netuid,epoch)).fetchone()
            if settlement:
                value = json.loads(settlement[0])
                if digest(value) != settlement[1] or settlement_record(value,r,json.loads(row[0])) != value:
                    raise ValueError('recovery_settlement_journal_mismatch')
                received += value['observedRecoveryMicrousd']
            else:
                in_flight += amount
        if not 0 <= received + in_flight <= r['shortfallMicrousd']:
            raise ValueError('recovery_overallocated')
        out.append({**r, 'assignedMicrousd': allocated,
                    'reviewedReceivedMicrousd': received, 'inFlightMicrousd': in_flight,
                    'outstandingMicrousd': r['shortfallMicrousd'] - received,
                    'unassignedMicrousd': r['shortfallMicrousd'] - received - in_flight})
    if sealed:
        raise ValueError('recovery_assessment_journal_missing')
    return out


def validate_claims(artifact):
    w = artifact['window']
    if 'recoveryPolicy' not in w and 'recoveries' not in w:
        return []
    if w.get('recoveryPolicy') != POLICY or not isinstance(w.get('recoveries'), list) or len(w['recoveries']) > 4096:
        raise ValueError('invalid_recovery_window')
    seen = set()
    for c in w['recoveries']:
        if (not isinstance(c, dict) or set(c) != CLAIM_KEYS
                or any(not isinstance(c[k], str) or not HASH.fullmatch(c[k])
                       for k in ('id', 'sourceHash', 'evidenceHash'))
                or not integer(c['sourceEpoch']) or c['sourceEpoch'] >= artifact['epoch']
                or not isinstance(c['minerId'], str) or not c['minerId']
                or not integer(c['amountMicrousd'], 1) or not integer(c['jobs'], 1)
                or c['id'] in seen):
            raise ValueError('invalid_recovery_claim')
        seen.add(c['id'])
    return w['recoveries']


def add_recoveries(artifact, epochs, eligible_miners=None):
    """Pure snapshot: current work first, confirmed old shortfalls in unused pool.

    Each reserved dollar is accounting value, not proof of future alpha received.
    If the pool is exhausted, the remainder stays in the durable ledger. Existing
    signed reports and their prices/emissions are never rewritten or summed in.
    """
    result = deepcopy(artifact)
    w = result['window']
    if 'recoveryPolicy' in w or 'recoveries' in w:
        raise ValueError('recovery_already_planned')
    base = sum(s['earningsMicrousd'] + s['surchargeMicrousd'] for s in w['scores'])
    budget = max(0, int(pool_microusd(artifact)) - base)
    w.update(recoveryPolicy=POLICY, recoveries=[])
    if not Path(epochs).exists():
        return result
    check_database(epochs, scope(artifact['network'], artifact['netuid']))
    with sqlite3.connect(Path(epochs).resolve().as_uri() + '?mode=ro', uri=True) as db:
        available = records(db, artifact['netuid'])
    scores = {s['minerId']: dict(s) for s in w['scores']}
    for r in available:
        a = r['assessment']
        amount = min(r['unassignedMicrousd'], budget)
        if (not amount or a['sourceEpoch'] >= artifact['epoch']
                or a['evidence']['finalizedBlock'] > artifact['finalizedBlock']
                or (eligible_miners is not None and a['minerId'] not in eligible_miners)):
            continue
        if len(w['recoveries']) >= 4096:
            break
        if a['minerId'] not in scores and len(scores) >= 4096:
            continue
        existing = scores.get(a['minerId'], {}).get('earningsMicrousd', 0)
        amount = min(amount, 2**63 - 1 - existing)
        if amount <= 0:
            continue
        c = {'id': r['id'], 'sourceEpoch': a['sourceEpoch'], 'sourceHash': a['sourceHash'],
             'minerId': a['minerId'], 'amountMicrousd': amount, 'jobs': r['jobs'],
             'evidenceHash': a['evidence']['evidenceHash']}
        w['recoveries'].append(c)
        s = scores.setdefault(a['minerId'], {'minerId': a['minerId'], 'earningsMicrousd': 0,
                                           'surchargeMicrousd': 0, 'jobs': 0})
        s['earningsMicrousd'] += amount
        s['jobs'] += r['jobs']
        budget -= amount
    w['scores'] = [scores[k] for k in sorted(scores)]
    return result


def reserve_claims(db, artifact):
    """Caller holds the epoch seal's BEGIN IMMEDIATE transaction."""
    claims = validate_claims(artifact)
    if not claims:
        return
    available = {r['id']: r for r in records(db, artifact['netuid'])}
    total = sum(c['amountMicrousd'] for c in claims)
    scored = sum(s['earningsMicrousd'] + s['surchargeMicrousd'] for s in artifact['window']['scores'])
    if total > max(0, int(pool_microusd(artifact)) - (scored - total)):
        raise ValueError('recovery_exceeds_unused_epoch_pool')
    for c in claims:
        r = available.get(c['id'])
        if r is None:
            raise ValueError('unapproved_recovery_claim')
        a = r['assessment']
        expected = {'id': r['id'], 'sourceEpoch': a['sourceEpoch'], 'sourceHash': a['sourceHash'],
                    'minerId': a['minerId'], 'amountMicrousd': c['amountMicrousd'],
                    'jobs': r['jobs'], 'evidenceHash': a['evidence']['evidenceHash']}
        if (c != expected or c['amountMicrousd'] > r['unassignedMicrousd']
                or a['evidence']['finalizedBlock'] > artifact['finalizedBlock']):
            raise ValueError('recovery_claim_changed_or_already_assigned')
        db.execute('INSERT INTO reward_recovery_allocations VALUES(?,?,?,?)',
                   (c['id'], artifact['netuid'], artifact['epoch'], c['amountMicrousd']))


def status(epochs, domain):
    check_database(epochs, domain)
    with sqlite3.connect(Path(epochs).resolve().as_uri() + '?mode=ro', uri=True) as db:
        values = records(db, domain['netuid'])
    return {'recoveries': [{'id': r['id'], 'minerId': r['assessment']['minerId'],
                            'sourceEpoch': r['assessment']['sourceEpoch'],
                            'shortfallMicrousd': r['shortfallMicrousd'],
                            'assignedMicrousd': r['assignedMicrousd'],
                            'reviewedReceivedMicrousd': r['reviewedReceivedMicrousd'],
                            'inFlightMicrousd': r['inFlightMicrousd'],
                            'outstandingMicrousd': r['outstandingMicrousd'],
                            'unassignedMicrousd': r['unassignedMicrousd']} for r in values],
            'totals': {k: sum(r[k] for r in values) for k in
                       ('shortfallMicrousd', 'assignedMicrousd', 'unassignedMicrousd',
                        'reviewedReceivedMicrousd', 'inFlightMicrousd', 'outstandingMicrousd')},
            'paymentConfirmed': False, 'chainTransactions': 0}


def audit_owner(epochs, journal, domain, first=None):
    """Read-only triage. Never call an owner-unsubmitted report unpaid globally."""
    check_database(epochs, domain)
    check_database(journal, domain)
    with sqlite3.connect(Path(journal).resolve().as_uri()+'?mode=ro',uri=True) as db:
        submissions = [(state,json.loads(raw),json.loads(result or '{}'))
                       for state,raw,result in db.execute('SELECT state,record,result FROM weight_runs')]
    with sqlite3.connect(Path(epochs).resolve().as_uri()+'?mode=ro',uri=True) as db:
        latest = db.execute('SELECT max(epoch) FROM reward_epochs WHERE netuid=?',(domain['netuid'],)).fetchone()[0]
        first = max(0,(latest or 0)-32) if first is None else first
        if not integer(first):
            raise ValueError('invalid_recovery_audit_range')
        rows = db.execute('SELECT artifact,hash FROM reward_epochs WHERE netuid=? AND epoch>=? '
                          'ORDER BY epoch LIMIT 1001',(domain['netuid'],first)).fetchall()
    if len(rows)>1000:
        raise ValueError('recovery_audit_range_too_large')
    reports=[]
    for raw,hashed in rows:
        a=json.loads(raw)
        if digest(a)!=hashed:
            raise ValueError('epoch_artifact_hash_mismatch')
        recognition_proofs(a)
        value=sum(p['feeMicrousd'] for p in a['window'].get('proofs',[]))
        if not value:
            continue
        own=[s for s,r,result in submissions if r.get('epoch')==a['epoch']
             and r.get('accountingSource','signed_epoch')=='signed_epoch' and s!='not_submitted']
        reports.append({'sourceEpoch':a['epoch'],'sourceHash':hashed,'originalEarningsMicrousd':value,
                        'ownerSubmissionStates':own,'ownerHasNoSubmission':not own,
                        'requiresGlobalRewardReview':True,'confirmedShortfallMicrousd':None})
    return {'reports':reports,'sourceReportCount':len(reports),
            'ownerUnsubmittedReportCount':sum(r['ownerHasNoSubmission'] for r in reports),
            'confirmedShortfallMicrousd':None,'chainTransactions':0,'ledgerMutations':0,
            'note':'Owner journal absence/application is not global payment evidence. Review other validators and pending reveals before approving a shortfall.'}


async def checked_approval(epochs, assessment, reviewed, settlement=False):
    """Read-only chain anchor check; never loads a wallet or submits a transaction."""
    import bittensor as bt
    domain = scope(assessment.get('network'), assessment.get('netuid'))
    check_database(epochs, domain)
    if settlement:
        source = load_epoch(epochs, domain['netuid'], assessment.get('allocationEpoch'))
        with sqlite3.connect(Path(epochs).resolve().as_uri()+'?mode=ro',uri=True) as db:
            original = next((r for r in records(db,domain['netuid']) if r['id']==assessment.get('recoveryId')),None)
        if original is None:
            raise ValueError('unapproved_recovery_settlement')
        settlement_record(assessment,original,source)
    else:
        source = load_epoch(epochs, domain['netuid'], assessment.get('sourceEpoch'))
        assessment_record(assessment, source)
    async with bt.Client(network=domain['network'], fallback_endpoints=[], archive_endpoints=[], retry_forever=False) as client:
        if await client._substrate.block_hash(0) != domain['genesis']:
            raise ValueError('unexpected_chain_genesis')
        head = await client._substrate.raw.get_chain_finalised_head()
        finalized = await client._substrate.raw.get_block_number(head)
        e = assessment['evidence']
        if (e['finalizedBlock'] > finalized
                or await client._substrate.block_hash(e['finalizedBlock']) != e['finalizedBlockHash']):
            raise ValueError('recovery_evidence_not_finalized_on_chain')
    return (settle if settlement else approve)(epochs, assessment, reviewed=reviewed)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('status', 'audit-owner', 'approve', 'settle'))
    p.add_argument('--epochs', required=True)
    p.add_argument('--network', choices=('finney', 'test'), default='finney')
    p.add_argument('--assessment', help='Private reviewed assessment JSON; not an owner submission log')
    p.add_argument('--journal', help='Existing owner submission journal; read-only audit input')
    p.add_argument('--from-epoch',type=int,help='Audit from this accounting epoch (default latest minus 32; maximum 1000 reports)')
    p.add_argument('--confirm-global-reward-review', action='store_true')
    a = p.parse_args()
    if a.command == 'status':
        result = status(a.epochs, scope(a.network))
    elif a.command == 'audit-owner':
        if not a.journal:
            p.error('audit-owner requires --journal')
        result=audit_owner(a.epochs,a.journal,scope(a.network),a.from_epoch)
    else:
        if not a.assessment or not a.confirm_global_reward_review:
            p.error('approval requires --assessment and --confirm-global-reward-review')
        from everyframe_validator.feed import strict_json
        assessment = strict_json(Path(a.assessment).read_bytes())
        check_record(assessment, scope(a.network))
        result = asyncio.run(asyncio.wait_for(
            checked_approval(a.epochs, assessment, True, settlement=a.command=='settle'), timeout=60))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
