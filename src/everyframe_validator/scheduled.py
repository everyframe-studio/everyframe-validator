"""One canonical signed accounting assignment per actual chain epoch.

Accounting windows are not chain epochs. Private histories cannot select inputs.
Legacy cursors remain on disk for audit; reviewed shortfalls belong in new owner
accounting, not in per-validator replays of old reports.
Observed weight application is deliberately NOT called an alpha payment.
"""
import asyncio
import hashlib
import json
import re
import sqlite3
from itertools import chain

from . import cadence, assignment
from .core.chain_scope import scope, read_network, rpc_options, check_authorization
from .core.reward_policy import digest, seal_epoch
from .core.validator import epoch_weight_plan
from .core.submission import prepare, recover
from .feed import fetch, verify, FeedUnavailable
from .state import atomic_json, journal_rows, read_private

CADENCE = 'chain-epoch-v3'
MAX_ACCOUNTING_PROBES = 32
ACCOUNTING_SCAN_TIMEOUT_SECONDS = 30


async def reconcile_only(root, config):
    """No wallet, authorization renewal, accounting adoption or submission."""
    import bittensor as bt
    from .application import observe
    domain = scope(config['network'], config['netuid'])
    async with bt.Client(network=read_network(domain), **rpc_options(domain), fallback_endpoints=[],
                         archive_endpoints=[], retry_forever=False) as client:
        view = await cadence.snapshot(client, domain, config)
        with sqlite3.connect(root / 'journal.db') as db:
            db.execute('PRAGMA synchronous=FULL')
            for row in journal_rows(root):
                if row['state'] in ('unknown', 'submitting'):
                    await recover(client, db, row['id'], row['record'], row['result'], view['block'])
        rows = journal_rows(root)
        reports = await observe(client, root, rows, view['block'], view['pending'])
        return {'state': 'blocked' if any(r['state'] in ('unknown', 'submitting') for r in rows)
                else 'reconciled', 'finalizedBlock': view['block'], 'reports': reports,
                'pendingChainCommits': len(view['pending']), 'submittedTransaction': False,
                'note': 'Application evidence is not proof of alpha settlement; historical allocations were not replayed.'}


async def schedule(client, domain, block):
    names = ('SubnetEpochIndex', 'LastEpochBlock', 'LastMechansimStepBlock',
             'PendingEpochAt', 'RevealPeriodEpochs', 'Timestamp')
    values = await asyncio.gather(*(
        client.query(('Timestamp', 'Now'), [], block=block) if name == 'Timestamp'
        else client.query(('SubtensorModule', name), [domain['netuid']], block=block)
        for name in names))
    s = dict(zip(names, values))
    if (any(type(v) is not int or v < 0 for v in values)
            or not 1 <= s['RevealPeriodEpochs'] <= 10
            or not 0 < s['LastEpochBlock'] <= block
            or not 0 < s['LastMechansimStepBlock'] <= block or s['Timestamp'] <= 0):
        raise ValueError('chain_epoch_schedule_unavailable')
    epoch_time = await client.query(('Timestamp', 'Now'), [], block=s['LastEpochBlock'])
    if type(epoch_time) is not int or not 0 < epoch_time <= s['Timestamp']:
        raise ValueError('chain_epoch_schedule_unavailable')
    s['epochStartedAtMs'] = epoch_time
    s['chainEpochStartHash'], s['previousChainEpochStart'] = await asyncio.gather(
        client._substrate.block_hash(s['LastEpochBlock']),
        client.query(('SubtensorModule', 'LastEpochBlock'), [domain['netuid']], block=s['LastEpochBlock'] - 1))
    if (not isinstance(s['chainEpochStartHash'], str)
            or not re.fullmatch(r'0x[a-f0-9]{64}', s['chainEpochStartHash'])
            or type(s['previousChainEpochStart']) is not int
            or not 0 < s['previousChainEpochStart'] < s['LastEpochBlock']):
        raise ValueError('chain_epoch_schedule_unavailable')
    return s


def included(row):
    value = row['result'].get('extrinsic_id', '')
    return int(value.split('-')[0]) if re.fullmatch(r'\d+-\d+', value) else row['record']['block']


def gate(view, s, rows):
    """Bound signing to an early, stable slot; legacy commits also occupy slots."""
    first, now, tempo = s['LastEpochBlock'], view['block'], view['params']['tempo']
    if s['PendingEpochAt']:
        return 'pending_epoch_transition'
    if any(r['state'] != 'not_submitted' and
           (included(r) >= first or r['record'].get('chainEpoch') == s['SubnetEpochIndex'])
           for r in rows):
        return 'chain_epoch_already_committed'
    if any(p['commit_block'] >= first for p in view['pending']):
        return 'chain_epoch_already_committed'
    # Never build near a boundary. The 64-block transaction mortality also
    # stays within this epoch for supported tempos. Slow preparation is checked
    # again immediately before broadcast.
    if tempo < 200 or view['params']['weights_rate_limit'] >= tempo:
        return 'unsupported_epoch_submission_window'
    if not first + 5 <= now <= first + min(90, tempo // 3):
        return 'waiting_for_chain_epoch_window'
    if len(view['pending']) >= 10:
        return 'pending_commit_limit'
    return None


def accounting_cursor(root, view, rows, persist):
    path = root / 'reward-schedule.json'
    try:
        state = read_private(path)
    except FileNotFoundError:
        # Explicit cutover: no replay of old submitted or skipped accounting.
        prior = [r['record']['epoch'] for r in rows if r['state'] != 'not_submitted']
        state = {'version': 1, 'startEpoch': max([view['epoch'], *[e + 1 for e in prior]]),
                 'adoptedAtBlock': view['block']}
        if persist:
            atomic_json(path, state, immutable=True)
    if (state.get('version') != 1 or type(state.get('startEpoch')) is not int
            or state['startEpoch'] < 0 or type(state.get('adoptedAtBlock')) is not int
            or not 0 <= state['adoptedAtBlock'] <= view['block']):
        raise ValueError('invalid_reward_schedule_cursor')
    represented = {r['record']['epoch'] for r in rows if r['state'] != 'not_submitted'
                   and r['record'].get('accountingSource') == 'signed_epoch'}
    epoch = state['startEpoch']
    while epoch in represented:
        epoch += 1
    return epoch


async def available_accounting(client, root, config, domain, view, rows, first, persist):
    """Find signed work across sparse publication, never consume a 404.

    The submission journal alone consumes paid reports. Missing reports remain
    eligible on every later scan. A bounded rotating scan prevents many holes
    from starving newer reports; invalid signatures/transport failures stop it.
    Authenticated zero-work reports need no dedicated chain slot ahead of work.
    """
    path = root / 'accounting-scan.json'
    try:
        scan = read_private(path)
    except FileNotFoundError:
        scan = {'version': 1, 'nextEpoch': first}
    if (scan.get('version') != 1 or type(scan.get('nextEpoch')) is not int
            or scan['nextEpoch'] < 0):
        raise ValueError('invalid_accounting_scan_cursor')
    last = view['epoch']
    start = scan['nextEpoch'] if first <= scan['nextEpoch'] <= last else first
    represented = {r['record']['epoch'] for r in rows if r['state'] != 'not_submitted'
                   and r['record'].get('accountingSource') == 'signed_epoch'}
    missing, empty, probes = [], [], 0
    first_empty = None

    def checkpoint(next_epoch):
        if persist:
            atomic_json(path, {'version': 1, 'nextEpoch': next_epoch})

    for epoch in chain(range(start, last + 1), range(first, start)):
        if epoch in represented:
            continue
        if probes >= MAX_ACCOUNTING_PROBES:
            checkpoint(epoch)
            return None, first, {'missingAccountingEpochs': missing,
                                'emptyAccountingEpochs': empty, 'accountingScanComplete': False}
        probes += 1
        try:
            raw = await asyncio.to_thread(fetch, config['feed'], epoch)
        except FeedUnavailable:
            missing.append(epoch)
            continue
        payload = verify(raw, config['publicKey'], domain, epoch)
        artifact = payload['artifact']
        if (artifact['finalizedBlock'] > view['block']
                or artifact['blocksPerEpoch'] != view['params']['tempo'] + 1
                or artifact['endBlockHash'] != await client._substrate.block_hash(artifact['endBlock'])
                or artifact['closeBlockHash'] != await client._substrate.block_hash(artifact['endBlock'] - 1)):
            raise ValueError('epoch chain provenance mismatch')
        cache = root / 'accepted'
        cache.mkdir(mode=0o700, exist_ok=True)
        atomic_json(cache / (str(epoch) + '.json'), {'payloadHash': digest(payload)}, immutable=True)
        seal_epoch(root / 'epochs.db', artifact)
        if any(r['earningsMicrousd'] + r['surchargeMicrousd'] > 0 for r in artifact['window']['scores']):
            checkpoint(first)
            return payload, epoch, {'missingAccountingEpochs': missing,
                                    'emptyAccountingEpochs': empty, 'accountingScanComplete': True}
        empty.append(epoch)
        if first_empty is None:
            first_empty = (payload, epoch)
    checkpoint(first)
    payload, epoch = first_empty or (None, first)
    return payload, epoch, {'missingAccountingEpochs': missing,
                            'emptyAccountingEpochs': empty, 'accountingScanComplete': True}


def reserve(db, run_id, record, consent):
    """Reuse the transaction journal and its consent cap, atomically deduplicate."""
    db.execute('BEGIN IMMEDIATE')
    try:
        rows = [(state, json.loads(raw), json.loads(result) if result else {})
                for state, raw, result in db.execute('SELECT state,record,result FROM weight_runs')]
        if any(state in ('submitting', 'unknown') for state, _, _ in rows):
            raise ValueError('unresolved prior weight submission')
        if sum(state != 'not_submitted' and r.get('authorizationId') == consent['id']
               for state, r, _ in rows) >= consent['maxSubmissions']:
            raise ValueError('mainnet authorization submission budget exhausted')
        for state, prior, result in rows:
            if state == 'not_submitted':
                continue
            at = included({'record': prior, 'result': result})
            if at >= record['chainEpochStart'] or prior.get('chainEpoch') == record['chainEpoch']:
                raise ValueError('chain epoch already committed')
            if record['block'] < at + record['intervalBlocks']:
                raise ValueError('block cadence already submitted')
            if (record['accountingSource'] == 'signed_epoch'
                    and prior.get('accountingSource', 'signed_epoch') == 'signed_epoch'
                    and prior['epoch'] == record['epoch']):
                raise ValueError('accounting epoch already submitted')
        db.execute('INSERT INTO weight_runs VALUES(?,?,?,NULL)',
                   (run_id, 'submitting', json.dumps(record)))
        db.commit()
    except BaseException:
        db.rollback()
        raise


def check_round(plan, view, s, rows):
    if not view['params']['commit_reveal_weights_enabled']:
        return {}
    round_ = getattr(plan, 'extras', {}).get('reveal_round')
    if type(round_) is not int or round_ <= 0:
        raise ValueError('timelock_schedule_unavailable')
    from bittensor.timelock import reveal_time
    at = int(reveal_time(round_).timestamp() * 1000)
    if at <= s['Timestamp']:
        raise ValueError('timelock_schedule_not_future')
    prior = [p['reveal_round'] for p in view['pending']]
    prior += [r['result'].get('broadcast', {}).get('revealRound', 0)
              for r in rows if r['state'] != 'not_submitted']
    if prior and round_ <= max(prior):
        raise ValueError('timelock_reveal_window_collision')
    # Same-window rounds can differ by seconds. Require at least half an epoch's
    # observed wall time between rounds as well as a distinct chain epoch.
    if prior and max(prior):
        previous = int(reveal_time(max(prior)).timestamp() * 1000)
        elapsed = s['Timestamp'] - s['epochStartedAtMs']
        blocks = view['block'] - s['LastEpochBlock']
        if elapsed <= 0 or blocks < 5:
            raise ValueError('timelock_schedule_unavailable')
        minimum_gap = max(60_000, elapsed * view['params']['tempo'] // (2 * blocks))
        if at - previous < minimum_gap:
            raise ValueError('timelock_reveal_window_collision')
    return {'revealRound': round_, 'revealAtMs': at}


async def tick(root, config, submit, interval=100, missing='burn', grace=25):
    import bittensor as bt
    if type(interval) is not int or not 100 <= interval <= 10000 or missing not in ('wait', 'burn'):
        raise ValueError('invalid chain cadence configuration')
    if type(grace) is not int or not 0 <= grace <= 1000:
        raise ValueError('invalid accounting publication grace')
    domain = scope(config['network'], config['netuid'])
    consent = check_authorization(root / 'authorization.json', domain,
                                  config['validatorHotkey'], config['versionKey']) if submit else None
    async with bt.Client(network=read_network(domain), **rpc_options(domain), fallback_endpoints=[],
                         archive_endpoints=[], retry_forever=False,
                         policy=bt.Policy(max_fee_tao='0', allowed_netuids=[domain['netuid']])) as client:
        view = await cadence.snapshot(client, domain, config)
        s = await schedule(client, domain, view['block'])
        rows = journal_rows(root)
        with sqlite3.connect(root / 'journal.db') as db:
            db.execute('PRAGMA synchronous=FULL')
            for row in rows:
                if row['state'] in ('unknown', 'submitting'):
                    await recover(client, db, row['id'], row['record'], row['result'], view['block'])
        rows = journal_rows(root)
        common = {'finalizedBlock': view['block'], 'chainEpoch': s['SubnetEpochIndex'],
                  'chainEpochStart': s['LastEpochBlock'], 'pendingChainCommits': len(view['pending']),
                  'cadence': CADENCE, 'submittedTransaction': False}
        from .application import observe
        try:
            common['rewardObservations'] = await asyncio.wait_for(
                observe(client, root, rows, view['block'], view['pending']), timeout=45)
        except Exception:
            # Archive unavailability must be visible, never called settlement.
            common['observationError'] = 'reward_application_evidence_unavailable'
        if any(r['state'] in ('unknown', 'submitting') for r in rows):
            return {**common, 'state': 'blocked', 'reason': 'uncertain_submission'}
        # Recovery of an uncertain broadcast still takes precedence. Historical
        # cursors stay intact for audit, but NEVER select current scoring inputs.
        epoch, source = assignment.target(view, s)
        common['nextAccountingEpoch'] = epoch
        common['assignmentPolicy'] = assignment.POLICY
        if submit:
            from .wallet import checked_wallet
            checked_wallet(config)
        reason = gate(view, s, rows)
        next_block = cadence.due_at(rows, view, interval)
        if reason == 'unsupported_epoch_submission_window' or (submit and (reason or view['block'] < next_block)):
            next_window = s['LastEpochBlock'] + (5 if view['block'] < s['LastEpochBlock'] + 5
                                                else view['params']['tempo'] + 5)
            return {**common, 'state': 'blocked' if reason == 'unsupported_epoch_submission_window' else 'waiting',
                    'reason': reason or 'block_cadence', 'nextEligibleBlock': max(next_block, next_window)}
        if not submit:
            common['signingWindowOpen'] = reason is None and view['block'] >= next_block
            common['signingWindowReason'] = reason or ('block_cadence' if view['block'] < next_block else None)
        load_task = asyncio.create_task(assignment.load(client, root, config, domain, view, s))
        try:
            payload, epoch, selected = await asyncio.wait_for(load_task, timeout=ACCOUNTING_SCAN_TIMEOUT_SECONDS)
        except FeedUnavailable:
            # No local fallback: a CDN 404 or publisher outage is not a signed
            # declaration of zero work. All operators wait for the SAME inputs.
            return {**common, 'state': 'waiting', 'reason': 'canonical_assignment_or_report_not_published'}
        except asyncio.TimeoutError:
            if not load_task.cancelled():
                raise
            return {**common, 'state': 'waiting', 'reason': 'assignment_fetch_timeout'}
        source = selected['mode']
        common['assignmentHash'] = digest(selected)
        if source == 'signed_epoch' and any(
                r['state'] != 'not_submitted' and r['record'].get('accountingSource', 'signed_epoch') == 'signed_epoch'
                and r['record']['epoch'] == epoch for r in rows):
            return {**common, 'state': 'waiting', 'reason': 'assigned_report_already_submitted',
                    'note': 'Preserve the historical allocation; do not replay it or select a different report.'}
        def make_plan(v):
            args = (v['neurons'], config['validatorHotkey'], v['params'], v['owner'], v['ownerHotkey'])
            return epoch_weight_plan(payload['artifact'], payload['bindings'], *args) if payload else cadence.burn_plan(*args)
        plan = make_plan(view)
        fresh = await cadence.snapshot(client, domain, config)
        fresh_s = await schedule(client, domain, fresh['block'])
        if (fresh_s['SubnetEpochIndex'] != s['SubnetEpochIndex'] or fresh_s['LastEpochBlock'] != s['LastEpochBlock']
                or fresh_s['RevealPeriodEpochs'] != s['RevealPeriodEpochs']
                or fresh['params'] != view['params'] or (submit and gate(fresh, fresh_s, rows))
                or make_plan(fresh) != plan or (submit and fresh['block'] < cadence.due_at(rows, fresh, interval))
                or assignment.project(domain,fresh,fresh_s,payload) != selected):
            return {**common, 'state': 'deferred', 'reason': 'chain_changed_before_signing'}
        record = {**domain, 'block': fresh['block'], 'validatorHotkey': config['validatorHotkey'],
                  'plan': plan, 'versionKey': config['versionKey'], 'epoch': epoch, 'openEpoch': view['epoch'] + 1,
                  'policy': plan['policy'], 'cadence': CADENCE, 'chainEpoch': s['SubnetEpochIndex'],
                  'chainEpochStart': s['LastEpochBlock'], 'revealPeriodEpochs': s['RevealPeriodEpochs'],
                  'intervalBlocks': max(interval, fresh['params']['weights_rate_limit']),
                  'commitReveal': fresh['params']['commit_reveal_weights_enabled'],
                  'accountingSource': source, 'payloadHash': digest(payload) if payload else None,
                  'assignmentHash': digest(selected), 'assignmentPolicy': assignment.POLICY}
        result = {**common, 'state': 'dry_run', 'epoch': epoch, 'weights': plan['quantized'],
                  'allBurn': plan['allBurn'], 'accountingSource': source}
        if not submit:
            return result
        from .wallet import checked_wallet, check_signer
        wallet = checked_wallet(config)
        password = read_private(config['walletPasswordFile'])['hotkey'] if config.get('walletPasswordFile') else None
        signer = check_signer(wallet.get_hotkey(password), config['validatorHotkey'])
        del password
        record['authorizationId'] = consent['id']
        run_id = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        with sqlite3.connect(root / 'journal.db') as db:
            db.execute('PRAGMA synchronous=FULL')
            reserve(db, run_id, record, consent)
            broadcast_started = False
            try:
                check_authorization(root / 'authorization.json', domain, config['validatorHotkey'], config['versionKey'])
                intent = bt.SetWeights(netuid=domain['netuid'], weights=plan['weights'], version_key=config['versionKey'])
                signed, broadcast = await prepare(client, intent, signer, db, run_id, record['block'],
                    validate_plan=lambda prepared: check_round(prepared, fresh, fresh_s, rows))
                final = await cadence.snapshot(client, domain, config)
                final_s = await schedule(client, domain, final['block'])
                if (final_s['SubnetEpochIndex'] != s['SubnetEpochIndex']
                        or final_s['LastEpochBlock'] != s['LastEpochBlock']
                        or final_s['RevealPeriodEpochs'] != s['RevealPeriodEpochs']
                        or final['params'] != fresh['params'] or gate(final, final_s, rows)
                        or assignment.project(domain,final,final_s,payload) != selected
                        or make_plan(final) != plan or final['lastUpdate'] != fresh['lastUpdate']
                        or final['pending'] != fresh['pending'] or final['block'] - record['block'] > 15):
                    raise ValueError('chain_changed_before_broadcast')
                check_authorization(root / 'authorization.json', domain, config['validatorHotkey'], config['versionKey'])
                broadcast_started = True
                outcome = await client._substrate.submit_signed(signed, signer, wait_for_inclusion=True, wait_for_finalization=True)
                if not outcome.success or not outcome.block_hash or not re.fullmatch(r'\d+-\d+', outcome.extrinsic_id or ''):
                    raise ValueError('submission not confirmed')
                state = 'commit_finalized' if record['commitReveal'] else 'finalized'
                db.execute('UPDATE weight_runs SET state=?,result=? WHERE id=?',
                           (state, json.dumps({**outcome.to_dict(), 'broadcast': broadcast,
                                              'revealConfirmed': False, 'applicationConfirmed': False,
                                              'alphaSettlementConfirmed': False}, default=str), run_id))
                db.commit()
                return {**result, 'state': state, 'submittedTransaction': True, 'runId': run_id,
                        'includedBlock': int(outcome.extrinsic_id.split('-')[0]),
                        'extrinsicId': outcome.extrinsic_id, 'revealAtMs': broadcast.get('revealAtMs'),
                        'note': 'One allocation per chain epoch. Finalized is not revealed, applied or paid.'}
            except BaseException:
                db.execute('UPDATE weight_runs SET state=? WHERE id=?',
                           ('unknown' if broadcast_started else 'not_submitted', run_id))
                db.commit()
                raise
