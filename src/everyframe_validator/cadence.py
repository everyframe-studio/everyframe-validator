"""Epoch or block-paced weights. Finalized commits may overlap their reveal delay.

An absent (404) accounting epoch may burn only with explicit operator opt-in.
Invalid signatures, conflicting accounting, network errors and uncertain
transactions never become fabricated zero-work accounting or automatic retries.
"""
import asyncio
import hashlib
import json
import re
import sqlite3

from .core.chain_scope import scope, read_network, check_authorization, rpc_options
from .core.reward_policy import digest, seal_epoch
from .core.validator import epoch_weight_plan
from .core.submission import prepare, recover
from .feed import fetch, verify, FeedUnavailable
from .state import read_private, atomic_json, journal_rows


def burn_plan(neurons, hotkey, params, owner, owner_hotkey):
    by_key = {n['hotkey']: n for n in neurons}
    if len(by_key) != len(neurons) or len({n['uid'] for n in neurons}) != len(neurons):
        raise ValueError('duplicate metagraph identity')
    if any(type(n['uid']) is not int or not 0 <= n['uid'] <= 65535 for n in neurons):
        raise ValueError('invalid UID')
    validator, burn = by_key.get(hotkey), by_key.get(owner_hotkey)
    if not validator or validator.get('validator_permit') is not True:
        raise ValueError('validator has no permit')
    if not owner or not burn or burn['coldkey'] != owner:
        raise ValueError('burn target is not the registered subnet owner hotkey')
    if (type(params['min_allowed_weights']) is not int
            or not 0 <= params['min_allowed_weights'] <= 1
            or type(params['max_weights_limit']) is not int
            or params['max_weights_limit'] != 65535):
        raise ValueError('burn weights violate chain constraints')
    uid = burn['uid']
    return {'state': 'ready', 'policy': 'missing-accounting-burn-v1',
            'weights': {uid: 65535}, 'quantized': {uid: 65535},
            'identities': {uid: owner_hotkey}, 'validatorUid': validator['uid'],
            'burnUid': uid, 'burnHotkey': owner_hotkey, 'ownerColdkey': owner,
            'allBurn': True, 'accountingAvailable': False}


async def snapshot(client, domain, config):
    if await client._substrate.block_hash(0) != domain['genesis']:
        raise ValueError('wrong_chain')
    block_hash = await client._substrate.raw.get_chain_finalised_head()
    block = await client._substrate.raw.get_block_number(block_hash)
    params = await client.subnets.subnet_hyperparameters(domain['netuid'], block=block)
    if (type(params['tempo']) is not int or not 1 <= params['tempo'] <= 999
            or type(params['weights_rate_limit']) is not int or params['weights_rate_limit'] < 0
            or config['versionKey'] < params['weights_version']):
        raise ValueError('unsupported_chain_policy')
    graph = await client.subnets.metagraph(domain['netuid'], block=block, commitments=False)
    neurons = [{'uid': n.uid, 'hotkey': n.hotkey, 'coldkey': n.coldkey,
                'validator_permit': n.validator_permit} for n in graph.neurons]
    owner, owner_hotkey, updates = await asyncio.gather(
        client.query(('SubtensorModule', 'SubnetOwner'), [domain['netuid']], block=block),
        client.query(('SubtensorModule', 'SubnetOwnerHotkey'), [domain['netuid']], block=block),
        client.query(('SubtensorModule', 'LastUpdate'), [domain['netuid']], block=block))
    validator = next((n for n in neurons if n['hotkey'] == config['validatorHotkey']), None)
    if not validator or validator['validator_permit'] is not True:
        raise ValueError('validator has no permit')
    uid = validator['uid']
    if not isinstance(updates, list) or not 0 <= uid < len(updates) or type(updates[uid]) is not int:
        raise ValueError('weight cooldown state unavailable')
    pending = []
    if params['commit_reveal_weights_enabled']:
        commits = await (await client.at(block)).read('timelocked_weight_commits', netuid=domain['netuid'])
        pending = [item for items in commits.values() for item in items
                   if item['hotkey'] == config['validatorHotkey']]
    return dict(block=block, blockHash=block_hash, params=params, neurons=neurons,
                owner=owner, ownerHotkey=owner_hotkey, lastUpdate=updates[uid], pending=pending,
                epoch=block // (params['tempo'] + 1) - 1)


def due_at(rows, view, interval):
    last = view['lastUpdate']
    for row in rows:
        if row['state'] == 'not_submitted':
            continue
        result = row['result']
        included = result.get('extrinsic_id')
        if included and re.fullmatch(r'\d+-\d+', included):
            last = max(last, int(included.split('-')[0]))
        else:
            last = max(last, row['record']['block'])
    return last + max(interval, view['params']['weights_rate_limit'])


def reserve(db, run_id, record, consent):
    db.execute('BEGIN IMMEDIATE')
    try:
        rows = [(state, json.loads(raw), json.loads(result) if result else {})
                for state, raw, result in db.execute('SELECT state,record,result FROM weight_runs')]
        if any(state in ('submitting', 'unknown') for state, _, _ in rows):
            raise ValueError('unresolved prior weight submission; reconcile before proceeding')
        used = sum(state != 'not_submitted' and r.get('authorizationId') == consent['id']
                   for state, r, _ in rows)
        if used >= consent['maxSubmissions']:
            raise ValueError('mainnet authorization submission budget exhausted')
        for state, prior, result in rows:
            if state == 'not_submitted':
                continue
            if (record.get('cadence') == 'accounting-epoch-v1' and prior.get('epoch') == record['epoch']
                    and (prior.get('accountingSource', 'signed_epoch') == 'signed_epoch'
                         or record['accountingSource'] == 'missing_epoch_burn')):
                raise ValueError('accounting epoch already submitted')
            included = result.get('extrinsic_id', '')
            last = max(prior['block'], int(included.split('-')[0])
                       if re.fullmatch(r'\d+-\d+', included) else prior['block'])
            if record['block'] < last + record['intervalBlocks']:
                raise ValueError('block cadence already submitted')
        db.execute('INSERT INTO weight_runs VALUES(?,?,?,NULL)',
                   (run_id, 'submitting', json.dumps(record)))
        db.commit()
    except BaseException:
        db.rollback()
        raise


async def tick(root, config, submit, interval, missing, weight_cadence='block', accounting_grace_blocks=25):
    import bittensor as bt
    if type(interval) is not int or not 100 <= interval <= 10000 or missing not in ('wait', 'burn'):
        raise ValueError('invalid block cadence configuration')
    if (weight_cadence not in ('epoch', 'block') or type(accounting_grace_blocks) is not int
            or not 0 <= accounting_grace_blocks <= 1000):
        raise ValueError('invalid epoch cadence configuration')
    domain = scope(config['network'], config['netuid'])
    consent = None
    if submit:
        read_private(root / 'authorization.json')
        consent = check_authorization(root / 'authorization.json', domain,
                                      config['validatorHotkey'], config['versionKey'])
    async with bt.Client(network=read_network(domain), **rpc_options(domain),
                         fallback_endpoints=[], archive_endpoints=[], retry_forever=False,
                         policy=bt.Policy(max_fee_tao='0', allowed_netuids=[domain['netuid']])) as client:
        view = await snapshot(client, domain, config)
        rows = journal_rows(root)
        unresolved = [r for r in rows if r['state'] in ('unknown', 'submitting')]
        if unresolved:
            with sqlite3.connect(root / 'journal.db') as db:
                db.execute('PRAGMA synchronous=FULL')
                for row in unresolved:
                    await recover(client, db, row['id'], row['record'], row['result'], view['block'])
            rows = journal_rows(root)
            if any(r['state'] in ('unknown', 'submitting') for r in rows):
                return {'state': 'blocked', 'reason': 'uncertain_submission', 'submittedTransaction': False}
        next_block = due_at(rows, view, interval)
        common = {'finalizedBlock': view['block'], 'nextEligibleBlock': next_block,
                  'intervalBlocks': max(interval, view['params']['weights_rate_limit']),
                  'pendingChainCommits': len(view['pending']), 'submittedTransaction': False}
        if submit and view['block'] < next_block:
            return {**common, 'state': 'waiting', 'reason': 'block_cadence'}
        if submit and len(view['pending']) >= 10:
            return {**common, 'state': 'waiting', 'reason': 'pending_commit_limit'}
        epoch, payload = view['epoch'], None
        if epoch < 0:
            return {**common, 'state': 'waiting', 'reason': 'no_closed_epoch'}
        period = view['params']['tempo'] + 1
        prior = {r['record'].get('accountingSource', 'signed_epoch') for r in rows
                 if r['state'] != 'not_submitted' and r['record'].get('epoch') == epoch}
        epoch_wait = {**common, 'state': 'waiting', 'reason': 'epoch_cadence', 'epoch': epoch,
                      'nextEligibleBlock': max(next_block, (epoch + 2) * period)}
        if submit and weight_cadence == 'epoch' and 'signed_epoch' in prior:
            return {**epoch_wait, 'accountingSource': 'signed_epoch'}
        try:
            raw = await asyncio.to_thread(fetch, config['feed'], epoch)
        except FeedUnavailable:
            if missing != 'burn':
                return {**common, 'state': 'waiting', 'reason': 'newest_epoch_not_published', 'epoch': epoch}
            if weight_cadence == 'epoch':
                if submit and 'missing_epoch_burn' in prior:
                    # Still fetch on every poll: a later signed report may replace
                    # this safety fallback once, subject to the chain cooldown.
                    return {**epoch_wait, 'accountingSource': 'missing_epoch_burn'}
                grace_end = (epoch + 1) * period + min(accounting_grace_blocks, period - 1)
                if view['block'] < grace_end:
                    return {**common, 'state': 'waiting', 'reason': 'accounting_publication_grace',
                            'epoch': epoch, 'nextEligibleBlock': max(next_block, grace_end)}
        else:
            payload = verify(raw, config['publicKey'], domain, epoch)
        if payload:
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
        def make_plan(v):
            args = (v['neurons'], config['validatorHotkey'], v['params'], v['owner'], v['ownerHotkey'])
            return epoch_weight_plan(payload['artifact'], payload['bindings'], *args) if payload else burn_plan(*args)
        plan = make_plan(view)
        # Revalidate finalized scope, identity, policy and spacing immediately before signing.
        fresh = await snapshot(client, domain, config)
        if (fresh['epoch'] != epoch or fresh['params'] != view['params'] or make_plan(fresh) != plan):
            return {**common, 'state': 'deferred', 'reason': 'chain_changed_before_signing'}
        if submit and (fresh['block'] < due_at(rows, fresh, interval) or len(fresh['pending']) >= 10):
            return {**common, 'state': 'deferred', 'reason': 'chain_weight_rate_limit'}
        record = {**domain, 'block': fresh['block'], 'validatorHotkey': config['validatorHotkey'],
                  'plan': plan, 'versionKey': config['versionKey'], 'epoch': epoch, 'openEpoch': epoch + 1,
                  'policy': plan['policy'], 'cadence': 'accounting-epoch-v1' if weight_cadence == 'epoch' else 'block-paced-v1',
                  'intervalBlocks': max(interval, fresh['params']['weights_rate_limit']),
                  'commitReveal': fresh['params']['commit_reveal_weights_enabled'],
                  'accountingSource': 'signed_epoch' if payload else 'missing_epoch_burn',
                  'payloadHash': digest(payload) if payload else None}
        result = {**common, 'state': 'dry_run', 'epoch': epoch, 'weights': plan['quantized'],
                  'allBurn': plan['allBurn'], 'accountingSource': record['accountingSource']}
        if not submit:
            return result
        if not config.get('wallet'):
            raise ValueError('wallet_not_configured')
        wallet = bt.Wallet(config['wallet'], config.get('walletHotkey', 'default'),
                           **({'path': config['walletPath']} if config.get('walletPath') else {}))
        if wallet.hotkeypub.ss58_address != config['validatorHotkey']:
            raise ValueError('wallet does not match intended validator')
        password = read_private(config['walletPasswordFile'])['hotkey'] if config.get('walletPasswordFile') else None
        signer = wallet.get_hotkey(password)
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
                signed, broadcast = await prepare(client, intent, signer, db, run_id, record['block'])
                check_authorization(root / 'authorization.json', domain, config['validatorHotkey'], config['versionKey'])
                broadcast_started = True
                outcome = await client._substrate.submit_signed(signed, signer, wait_for_inclusion=True, wait_for_finalization=True)
                if not outcome.success or not outcome.block_hash or not re.fullmatch(r'\d+-\d+', outcome.extrinsic_id or ''):
                    raise ValueError('submission not confirmed')
                included = int(outcome.extrinsic_id.split('-')[0])
                state = 'commit_finalized' if record['commitReveal'] else 'finalized'
                db.execute('UPDATE weight_runs SET state=?,result=? WHERE id=?',
                           (state, json.dumps({**outcome.to_dict(), 'broadcast': broadcast,
                                              'revealConfirmed': False}, default=str), run_id))
                db.commit()
                return {**result, 'state': state, 'submittedTransaction': True, 'runId': run_id,
                        'includedBlock': included, 'extrinsicId': outcome.extrinsic_id,
                        'nextEligibleBlock': included + record['intervalBlocks'],
                        'note': 'Finalized submission is not proof of reveal. Later finalized commits may queue while awaiting reveal.'}
            except BaseException:
                db.execute('UPDATE weight_runs SET state=? WHERE id=?',
                           ('unknown' if broadcast_started else 'not_submitted', run_id))
                db.commit()
                raise
