"""Finalized-epoch value scoring and cap/burn economics (policy version 2).

Independent implementation of SayGM's public cap-and-burn policy:
https://github.com/taostat/gm-validator/blob/main/validator/src/gm_validator/alpha_economics.py
Money stays in exact integer/rational arithmetic until SDK quantization.
"""
import hashlib
import json
from fractions import Fraction
from pathlib import Path
import sqlite3
from ..core.chain_scope import scope, check_record, bind_database

POLICY = 'epoch-value-cap-burn-v2'
MAX_WEIGHT = 65535
MINER_SHARE_POLICY = 'fixed-41-percent-v1'
MINER_SHARE_TEXT = '0.41'
MINER_SHARE = Fraction(41, 100)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def positive(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError('money must be an exact decimal string or integer')
    result = Fraction(value)
    if result <= 0:
        raise ValueError('price and emissions must be positive')
    return result


def cap_burn(values, pool_microusd, burn_uid):
    """u16 budget sums to 65535; positive tiny earners receive at least 1.

    If minimum-one rounding exceeds the budget, shave the highest allocations,
    breaking ties by true work value then UID. Zero work is explicitly all-burn.
    """
    pool = positive(pool_microusd) if not isinstance(pool_microusd, Fraction) else pool_microusd
    if pool <= 0 or burn_uid in values:
        raise ValueError('invalid pool or burn/miner collision')
    if len(values) > MAX_WEIGHT or any(type(v) is not int or v <= 0 for v in values.values()):
        raise ValueError('invalid approved work values')
    denominator = max(pool, sum(values.values()))
    weights = {uid: max(1, int(Fraction(value * MAX_WEIGHT, 1) / denominator))
               for uid, value in sorted(values.items())}
    excess = sum(weights.values()) - MAX_WEIGHT
    # At most one unit of rounding per positive miner. A heap keeps large
    # fleets bounded without repeatedly sorting the entire vector.
    if excess > 0:
        import heapq
        heap = [(-weight, values[uid], uid) for uid, weight in weights.items()]
        heapq.heapify(heap)
        for _ in range(excess):
            neg, value, uid = heapq.heappop(heap)
            if -neg <= 1:
                raise ValueError('weight floor cannot fit')
            weights[uid] -= 1
            heapq.heappush(heap, (neg + 1, value, uid))
    remainder = MAX_WEIGHT - sum(weights.values())
    if remainder:
        weights[burn_uid] = remainder
    return dict(sorted(weights.items()))


def epoch_snapshot(database, start, end, finalized_at, *, recognized=None):
    """Read one consistent coordinator snapshot; missing reviews are not zero.

    Fees are immutable server-priced earnings captured at order creation and
    copied into operator-approved payables, never a miner-reported provider bill.
    There is no separate surcharge in the current EveryFrame accounting schema.
    """
    if any(type(v) is not int for v in (start, end, finalized_at)) or not 0 <= start < end <= finalized_at:
        raise ValueError('invalid finalized accounting interval')
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reward_snapshot_meta'").fetchone():
            captures = db.execute('SELECT captured_ms FROM reward_snapshot_meta').fetchall()
            if len(captures) != 1 or not end <= captures[0][0] <= finalized_at:
                raise ValueError('accounting snapshot does not cover the closed epoch')
        # Recognition mode includes older, newly reviewed work. The immutable
        # epoch journal, not a moving timestamp cursor, determines what was used.
        rows = db.execute('''SELECT a.id attempt_id,a.job_id,a.miner_id,a.completed,a.receipt,a.output_hash,
            j.state job_state,j.attempt_id current_attempt,j.fee_microusd job_fee,j.reward_eligible,
            p.state payable_state,p.approved,p.fee_microusd fee,p.miner_id payable_miner,
            p.job_id payable_job,m.enabled,r.hash artifact_hash
            FROM attempts a LEFT JOIN jobs j ON j.id=a.job_id
            LEFT JOIN payables p ON p.attempt_id=a.id LEFT JOIN miners m ON m.id=a.miner_id
            LEFT JOIN artifacts r ON r.job_id=a.job_id
            WHERE a.state='accepted' AND a.completed>=? AND a.completed<? ORDER BY a.id''',
            (0 if recognized is not None else start, end)).fetchall()
    scores, proofs, excluded, deferred = {}, [], [], []
    for row in rows:
        r = dict(row)
        if recognized is not None and (r['job_id'] in recognized['jobs'] or r['attempt_id'] in recognized['attempts']):
            continue
        if (r['job_state'] != 'succeeded' or r['current_attempt'] != r['attempt_id'] or
                r['payable_job'] != r['job_id'] or r['payable_miner'] != r['miner_id'] or
                not r['receipt'] or not r['output_hash'] or r['output_hash'] != r['artifact_hash'] or
                type(r['fee']) is not int or r['fee'] <= 0 or r['fee'] != r['job_fee'] or
                r['enabled'] not in (0, 1) or r['reward_eligible'] not in (0, 1)):
            raise ValueError('inconsistent accepted-work accounting')
        if recognized is not None and r['payable_state'] == 'review' and r['approved'] is None:
            deferred.append({'jobId': r['job_id'], 'attemptId': r['attempt_id'], 'reason': 'pending_review'})
            continue
        if r['payable_state'] not in ('approved', 'disputed') or type(r['approved']) is not int or not r['completed'] <= r['approved'] <= finalized_at:
            raise ValueError('epoch accounting is not reviewed/finalized')
        if recognized is not None and r['approved'] >= end:
            deferred.append({'jobId': r['job_id'], 'attemptId': r['attempt_id'], 'reason': 'review_after_close'})
            continue
        proof = {'jobId': r['job_id'], 'attemptId': r['attempt_id'], 'minerId': r['miner_id'],
                 'completed': r['completed'], 'approvedAt': r['approved'], 'feeMicrousd': r['fee'],
                 'outputHash': r['output_hash'], 'receiptHash': hashlib.sha256(r['receipt'].encode()).hexdigest(),
                 'payableState': r['payable_state'], 'rewardEligible': bool(r['reward_eligible']),
                 'enabled': bool(r['enabled'])}
        if r['payable_state'] != 'approved' or not r['reward_eligible'] or not r['enabled']:
            excluded.append(proof)
            continue
        bucket = scores.setdefault(r['miner_id'], {'minerId': r['miner_id'], 'earningsMicrousd': 0,
                                                   'surchargeMicrousd': 0, 'jobs': 0})
        bucket['earningsMicrousd'] += r['fee']
        bucket['jobs'] += 1
        proofs.append(proof)
    result = {'version': 2, 'policy': POLICY, 'start': start, 'end': end, 'finalizedAt': finalized_at,
              'scores': [scores[key] for key in sorted(scores)], 'proofs': proofs, 'excluded': excluded}
    if recognized is not None:
        result.update(recognitionPolicy='reviewed-unassigned-v1', deferred=deferred)
    return result


def recognition_proofs(artifact):
    """Private provenance must fully explain the scores before migration/claiming.

    Public feed projections deliberately lack proofs and cannot serve as a
    publisher's recognition journal. Never infer that their jobs were unpaid.
    """
    scores, jobs, attempts = {}, set(), set()
    w = artifact['window']
    for p in w.get('proofs', []):
        if (not isinstance(p.get('jobId'), str) or not p['jobId'] or
                not isinstance(p.get('attemptId'), str) or not p['attemptId'] or
                p['jobId'] in jobs or p['attemptId'] in attempts or
                p.get('payableState') != 'approved' or p.get('rewardEligible') is not True or
                p.get('enabled') is not True or type(p.get('feeMicrousd')) is not int or p['feeMicrousd'] <= 0):
            raise ValueError('invalid recognition provenance')
        if w.get('recognitionPolicy') == 'reviewed-unassigned-v1' and not (
                type(p.get('completed')) is int and type(p.get('approvedAt')) is int and
                0 <= p['completed'] <= p['approvedAt'] < w['end']):
            raise ValueError('recognition review is outside closed window')
        jobs.add(p['jobId']); attempts.add(p['attemptId'])
        s = scores.setdefault(p['minerId'], {'minerId': p['minerId'], 'earningsMicrousd': 0,
                                            'surchargeMicrousd': 0, 'jobs': 0})
        s['earningsMicrousd'] += p['feeMicrousd']; s['jobs'] += 1
    # A recovery is a separately reviewed financial adjustment, not a second
    # recognition of an original job/attempt. Its approval and reservation are
    # checked against the durable recovery ledger by seal_epoch.
    from .reward_recovery import validate_claims
    for claim in validate_claims(artifact):
        s = scores.setdefault(claim['minerId'], {'minerId': claim['minerId'], 'earningsMicrousd': 0,
                                               'surchargeMicrousd': 0, 'jobs': 0})
        s['earningsMicrousd'] += claim['amountMicrousd']
        s['jobs'] += claim['jobs']
    if [scores[k] for k in sorted(scores)] != w['scores']:
        raise ValueError('recognition scores lack complete private provenance')
    return {'jobs': jobs, 'attempts': attempts}


def recognized_work(db, netuid):
    result = {'jobs': set(), 'attempts': set()}
    for raw, hashed in db.execute('SELECT artifact,hash FROM reward_epochs WHERE netuid=? ORDER BY epoch', (netuid,)):
        a = json.loads(raw)
        if digest(a) != hashed:
            raise ValueError('epoch artifact hash mismatch')
        validate_artifact(a)
        used = recognition_proofs(a)
        for key in result:
            if result[key] & used[key]:
                raise ValueError('work recognized in multiple historical epochs')
            result[key].update(used[key])
    return result


def recognition_snapshot(database, epochs, netuid, start, end, finalized_at):
    # Existing seals are the durable deduplication ledger, including pre-upgrade
    # epochs. Reading is non-mutating; seal_epoch rechecks under a write lock.
    if Path(epochs).exists():
        with sqlite3.connect(Path(epochs).resolve().as_uri() + '?mode=ro', uri=True) as db:
            used = recognized_work(db, netuid)
    else:
        used = {'jobs': set(), 'attempts': set()}
    return epoch_snapshot(database, start, end, finalized_at, recognized=used)


def seal_epoch(path, artifact):
    """Atomic immutable local artifact publication; no overwrite or re-finalization."""
    validate_artifact(artifact)
    raw = canonical(artifact)
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA synchronous=FULL')
        bind_database(db, scope(artifact['network'], artifact['netuid']), legacy_test=True)
        db.execute('''CREATE TABLE IF NOT EXISTS reward_epochs
            (netuid INTEGER NOT NULL,epoch INTEGER NOT NULL,artifact TEXT NOT NULL,hash TEXT NOT NULL,
             PRIMARY KEY(netuid,epoch))''')
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT artifact FROM reward_epochs WHERE netuid=? AND epoch=?',
                              (artifact['netuid'], artifact['epoch'])).fetchone()
        if previous:
            if previous[0] != raw:
                raise ValueError('epoch already sealed; refusing revised accounting')
            return digest(artifact)
        if artifact['window'].get('recognitionPolicy') == 'reviewed-unassigned-v1':
            used = recognized_work(db, artifact['netuid'])
            incoming = recognition_proofs(artifact)
            if any(used[key] & incoming[key] for key in used):
                raise ValueError('work already recognized; rebuild accounting snapshot')
        from .reward_recovery import reserve_claims, validate_claims
        if validate_claims(artifact):
            # Validate the complete private explanation even for a manually
            # prepared recovery report. Claim and epoch commit in one transaction.
            recognition_proofs(artifact)
            reserve_claims(db, artifact)
        db.execute('INSERT INTO reward_epochs VALUES(?,?,?,?)',
                   (artifact['netuid'], artifact['epoch'], raw, digest(artifact)))
    return digest(artifact)


def load_epoch(path, netuid, epoch):
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        row = db.execute('SELECT artifact,hash FROM reward_epochs WHERE netuid=? AND epoch=?', (netuid, epoch)).fetchone()
    if not row:
        raise ValueError('newest closed epoch is not finalized')
    artifact = json.loads(row[0])
    if digest(artifact) != row[1]:
        raise ValueError('epoch artifact hash mismatch')
    validate_artifact(artifact)
    if artifact['netuid'] != netuid or artifact['epoch'] != epoch:
        raise ValueError('epoch artifact scope mismatch')
    return artifact


def validate_artifact(a):
    if a.get('policy') != POLICY or a.get('version') != 2 or a.get('network') not in ('test', 'finney'):
        raise ValueError('unsupported reward artifact')
    check_record(a, scope(a['network'], a.get('netuid')))
    for key in ('netuid', 'epoch', 'blocksPerEpoch', 'startBlock', 'endBlock', 'finalizedBlock'):
        if type(a.get(key)) is not int or a[key] < 0:
            raise ValueError('invalid epoch metadata')
    period = a['blocksPerEpoch']
    if period < 2 or a['startBlock'] != a['epoch'] * period or a['endBlock'] != (a['epoch'] + 1) * period or a['finalizedBlock'] < a['endBlock']:
        raise ValueError('epoch is not closed and finalized')
    for key in ('endBlockHash', 'closeBlockHash', 'genesis'):
        if not isinstance(a.get(key), str) or len(a[key]) != 66 or not a[key].startswith('0x'):
            raise ValueError('missing chain provenance')
    positive(a['alphaPriceUsd'])
    positive(a['emissionsAlpha'])
    if a['network'] == 'test':
        if a.get('minerShare') != '0.41':
            raise ValueError('unexpected miner emission share')
    else:
        cut = a.get('ownerCutU16')
        if (type(cut) is not int or not 0 <= cut < 65535 or type(a.get('ownerCutEnabled')) is not bool
                or a.get('unusedAllocation') not in ('Recycle', 'Burn')
                or a.get('accountingWindow') != 'fixed-tempo-plus-one-v1'):
            raise ValueError('missing mainnet emission policy evidence')
        # Preserve legacy signed accounting; new reports explicitly pin 41%.
        share_policy = a.get('minerSharePolicy')
        if 'minerSharePolicy' in a and share_policy != MINER_SHARE_POLICY:
            raise ValueError('unsupported miner share policy')
        expected = (MINER_SHARE if share_policy == MINER_SHARE_POLICY else
                    Fraction(65535-(cut if a['ownerCutEnabled'] else 0), 2*65535))
        if positive(a['minerShare']) != expected:
            raise ValueError('mainnet miner share does not match accounting policy')
    window = a['window']
    if window['policy'] != POLICY or window['version'] != 2 or not 0 <= window['start'] < window['end'] <= window['finalizedAt']:
        raise ValueError('invalid accounting window')
    if not isinstance(a.get('priceSource'), dict) or not a['priceSource'].get('source'):
        raise ValueError('missing price provenance')
    if not window['end'] - 300000 <= a['priceSource'].get('observedAt', 0) <= window['end']:
        raise ValueError('stale or future USD price')
    ids = set()
    for row in window['scores']:
        if not isinstance(row['minerId'], str) or not row['minerId'] or row['minerId'] in ids:
            raise ValueError('invalid/duplicate scoring identity')
        ids.add(row['minerId'])
        if any(type(row[key]) is not int or row[key] < 0 for key in ('earningsMicrousd', 'surchargeMicrousd', 'jobs')):
            raise ValueError('invalid work value')


def pool_microusd(artifact):
    validate_artifact(artifact)
    return positive(artifact['emissionsAlpha']) * positive(artifact['minerShare']) * positive(artifact['alphaPriceUsd']) * 1000000
