"""Network-scoped reward weights; mainnet requires bounded signing authorization."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import sys

from .reward_policy import POLICY, cap_burn, digest, load_epoch, pool_microusd
from .chain_scope import scope, add_network, bind_database, check_database, check_record, check_authorization, read_network
from .submission import prepare

def snapshot(database, end):
    """Legacy count-based diagnostic fixture only; NOT the production reward path."""
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        rows = db.execute("""SELECT a.miner_id,count(*) FROM attempts a
          JOIN jobs j ON j.id=a.job_id JOIN payables p ON p.attempt_id=a.id
          JOIN miners m ON m.id=a.miner_id
          WHERE a.state='accepted' AND j.reward_eligible=1 AND p.state='approved' AND m.enabled=1
          AND a.completed>=? AND a.completed<? GROUP BY a.miner_id ORDER BY a.miner_id""",
          (end-86400000, end)).fetchall()
    return {'version': 1, 'start': end-86400000, 'end': end,
            'scores': [{'minerId': miner, 'score': score} for miner, score in rows]}

def pilot_snapshot(database, end, job_ids, netuid):
    """Explicit synthetic-work test on subnet 566 only; never changes paid eligibility."""
    if netuid != 566 or not 1 <= len(job_ids) <= 3 or len(set(job_ids)) != len(job_ids):
        raise ValueError('invalid pilot scope')
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    scores, proofs = {}, []
    with sqlite3.connect(uri, uri=True) as db:
        for job_id in job_ids:
            row = db.execute('''SELECT a.miner_id,a.output_hash,a.receipt,a.completed,
                j.state,j.reward_eligible,p.state,r.hash
                FROM jobs j JOIN attempts a ON a.id=j.attempt_id
                JOIN payables p ON p.attempt_id=a.id JOIN artifacts r ON r.job_id=j.id
                WHERE j.id=? AND a.state='accepted' ''', (job_id,)).fetchone()
            if (not row or row[4] != 'succeeded' or row[5] != 0 or row[6] != 'review'
                    or row[1] != row[7] or not row[2] or not end-86400000 <= row[3] < end):
                raise ValueError('pilot job is not accepted, synthetic, review-held and recent')
            scores[row[0]] = scores.get(row[0], 0) + 1
            proofs.append({'jobId': job_id, 'outputHash': row[1],
                           'receiptHash': hashlib.sha256(row[2].encode()).hexdigest()})
    return {'version': 1, 'start': end-86400000, 'end': end, 'testOnly': True,
            'organicDemand': False, 'proofs': proofs,
            'scores': [{'minerId': miner, 'score': score} for miner, score in sorted(scores.items())]}

def validate_miner_ownership(scores, bindings, neurons, subnet_owner, owner_hotkey):
    """Owner-associated hotkeys cannot receive mining incentives, even with valid work."""
    if not subnet_owner or not owner_hotkey:
        raise ValueError('subnet ownership unavailable')
    by_hotkey = {n['hotkey']: n for n in neurons}
    for row in scores:
        hotkey = bindings.get(row['minerId'])
        miner = by_hotkey.get(hotkey)
        if not miner or not miner.get('coldkey'):
            raise ValueError('miner coldkey ownership unavailable')
        if miner['coldkey'] == subnet_owner or hotkey == owner_hotkey:
            raise ValueError('owner-associated miner cannot receive mining incentives')


def weight_plan(scores, bindings, neurons, validator_hotkey, params):
    """Legacy count-based synthetic pilot only. Organic rewards use epoch_weight_plan."""
    by_hotkey = {n['hotkey']: n for n in neurons}
    if len(by_hotkey) != len(neurons):
        raise ValueError('duplicate metagraph hotkey')
    validator = by_hotkey.get(validator_hotkey)
    if not validator or validator.get('validator_permit') is not True:
        raise ValueError('validator has no permit')
    if len(set(bindings.values())) != len(bindings):
        raise ValueError('duplicate operator hotkey binding')
    weights, identities = {}, {}
    for row in scores:
        if type(row['score']) is not int or row['score'] <= 0:
            raise ValueError('invalid work score')
        hotkey = bindings.get(row['minerId'])
        if hotkey not in by_hotkey or hotkey == validator_hotkey:
            raise ValueError('missing, unregistered or self hotkey')
        uid = by_hotkey[hotkey]['uid']
        if type(uid) is not int or uid < 0 or uid in weights:
            raise ValueError('invalid or duplicated UID')
        weights[uid] = row['score']
        identities[uid] = hotkey
    if not weights:
        return {'state': 'no_work', 'weights': {}, 'identities': {}}
    minimum, maximum = params['min_allowed_weights'], params['max_weights_limit']
    if type(minimum) is not int or type(maximum) is not int or not (0 <= maximum <= 65535):
        raise ValueError('unsupported chain constraint schema')
    if len(weights) < minimum or maximum == 0:
        raise ValueError('insufficient eligible miners; never pad with fabricated work')
    # Refuse a vector that requires changing the serving-based distribution.
    # Apply SDK quantization before checking the actual on-chain max/sum constraint.
    from bittensor.intents import normalize
    uids, quantized = normalize(list(weights), list(weights.values()))
    if len(uids) < minimum or len(uids) != len(weights) or max(quantized) / sum(quantized) > maximum / 65535 + 1e-12:
        raise ValueError('weight distribution violates chain constraints')
    return {'state': 'ready', 'weights': weights, 'identities': identities,
            'quantized': dict(zip(uids, quantized)), 'validatorUid': validator['uid']}


def epoch_weight_plan(artifact, bindings, neurons, validator_hotkey, params, owner, owner_hotkey):
    """Value-proportional cap/burn, with an owner-verified burn identity exception."""
    pool = pool_microusd(artifact)
    by_hotkey = {n['hotkey']: n for n in neurons}
    if len(by_hotkey) != len(neurons) or len({n['uid'] for n in neurons}) != len(neurons):
        raise ValueError('duplicate metagraph identity')
    if any(type(n['uid']) is not int or not 0 <= n['uid'] <= 65535 for n in neurons):
        raise ValueError('invalid UID')
    validator = by_hotkey.get(validator_hotkey)
    if not validator or validator.get('validator_permit') is not True:
        raise ValueError('validator has no permit')
    burn = by_hotkey.get(owner_hotkey)
    if not owner or not burn or burn.get('coldkey') != owner:
        raise ValueError('burn target is not the registered subnet owner hotkey')
    if len(set(bindings.values())) != len(bindings):
        raise ValueError('duplicate operator hotkey binding')
    scores = artifact['window']['scores']
    validate_miner_ownership(scores, bindings, neurons, owner, owner_hotkey)
    values, identities = {}, {}
    for row in scores:
        hotkey = bindings.get(row['minerId'])
        if hotkey == validator_hotkey:
            raise ValueError('validator cannot earn serving weights')
        uid = by_hotkey[hotkey]['uid']
        value = row['earningsMicrousd'] + row['surchargeMicrousd']
        if value:
            values[uid] = value
            identities[uid] = hotkey
    weights = cap_burn(values, pool, burn['uid'])
    if burn['uid'] in weights:
        identities[burn['uid']] = owner_hotkey
    from bittensor.intents import normalize
    uids, quantized = normalize(list(weights), list(weights.values()))
    minimum, maximum = params['min_allowed_weights'], params['max_weights_limit']
    if (type(minimum) is not int or minimum < 0 or type(maximum) is not int or not 0 < maximum <= 65535
            or len(uids) < minimum or len(uids) != len(weights)
            or max(quantized) * 65535 > maximum * sum(quantized)):
        raise ValueError('cap/burn weights violate chain constraints; never invent work or change chain settings')
    return {'state': 'ready', 'policy': POLICY, 'weights': weights, 'quantized': dict(zip(uids, quantized)),
            'identities': identities, 'validatorUid': validator['uid'], 'burnUid': burn['uid'],
            'burnHotkey': owner_hotkey, 'ownerColdkey': owner, 'allBurn': not values,
            'totalWorkMicrousd': sum(values.values()), 'poolMicrousd': str(pool),
            'burnBudgetUnits': weights.get(burn['uid'], 0), 'allocationBudgetUnits': 65535}


def reserve_run(journal, run_id, record, authorization=None):
    """Durable epoch deduplication is separate from block-dependent record hashes."""
    journal.execute('BEGIN IMMEDIATE')
    try:
        if authorization:
            used = sum(json.loads(raw).get('authorizationId') == authorization['id']
                       for (raw,) in journal.execute("SELECT record FROM weight_runs WHERE state!='not_submitted'"))
            if used >= authorization['maxSubmissions']:
                raise ValueError('mainnet authorization submission budget exhausted')
        if journal.execute("SELECT 1 FROM weight_runs WHERE state IN ('submitting','unknown','pending_reveal','finalized')").fetchone():
            raise ValueError('unresolved prior weight submission; reconcile before proceeding')
        if record.get('policy') == POLICY:
            for (raw,) in journal.execute("SELECT record FROM weight_runs WHERE state!='not_submitted'"):
                prior = json.loads(raw)
                if (prior.get('network') == record['network'] and prior.get('netuid') == record['netuid']
                        and prior.get('validatorHotkey') == record['validatorHotkey']
                        and prior.get('policy') == POLICY
                        and (prior['epoch'] >= record['epoch'] or prior['openEpoch'] >= record['openEpoch'])):
                    raise ValueError('epoch already submitted or stale; do not replay historical rewards')
        journal.execute('INSERT INTO weight_runs VALUES(?,?,?,NULL)', (run_id, 'submitting', json.dumps(record)))
        journal.commit()
    except Exception:
        journal.rollback()
        raise

async def main(args):
    import bittensor as bt
    domain = scope(getattr(args, 'network', 'test'), args.netuid)
    authorization = check_authorization(getattr(args, 'mainnet_authorization', None), domain,
        args.validator_hotkey, args.version_key) if args.submit else None
    end = int(time.time() * 1000)
    pilot_jobs = getattr(args, 'test_job_id', [])
    if pilot_jobs and domain['network'] != 'test':
        raise ValueError('synthetic rewards are forbidden on mainnet')
    if not pilot_jobs and not getattr(args, 'epochs', None):
        raise ValueError('--epochs finalized artifact database is required; count-based organic scoring was retired')
    if domain['network'] == 'finney':
        check_database(args.epochs, domain)
    if not pilot_jobs and args.version_key < 2:
        raise ValueError('epoch-value policy requires scoring version 2 or newer')
    data = pilot_snapshot(args.database, end, pilot_jobs, args.netuid) if pilot_jobs else None
    bindings = json.loads(Path(args.bindings).read_text())
    if not isinstance(bindings, dict):
        raise ValueError('invalid bindings')
    async with bt.Client(network=read_network(domain), fallback_endpoints=[], archive_endpoints=[], retry_forever=False,
                         policy=bt.Policy(max_fee_tao='0.02', allowed_netuids=[args.netuid])) as client:
        if await client._substrate.block_hash(0) != domain['genesis']:
            raise ValueError('unexpected chain genesis')
        finalized_hash = await client._substrate.raw.get_chain_finalised_head()
        finalized_block = await client._substrate.raw.get_block_number(finalized_hash)
        graph = await client.subnets.metagraph(args.netuid, block=finalized_block, commitments=False)
        if graph is None:
            raise ValueError('subnet not found on selected chain')
        params = await client.subnets.subnet_hyperparameters(args.netuid, block=graph.block)
        if args.version_key < params['weights_version']:
            raise ValueError('outdated scoring version; even a dry-run must use a compatible version')
        owner, owner_hotkey = await asyncio.gather(
            client.query(('SubtensorModule','SubnetOwner'), [args.netuid], block=graph.block),
            client.query(('SubtensorModule','SubnetOwnerHotkey'), [args.netuid], block=graph.block))
        neurons = [{'uid': n.uid, 'hotkey': n.hotkey, 'coldkey':n.coldkey, 'validator_permit': n.validator_permit} for n in graph.neurons]
        artifact = None
        if pilot_jobs:
            validate_miner_ownership(data['scores'], bindings, neurons, owner, owner_hotkey)
            plan = weight_plan(data['scores'], bindings, neurons, args.validator_hotkey, params)
        else:
            period = params['tempo'] + 1
            epoch = finalized_block // period - 1
            artifact = load_epoch(args.epochs, args.netuid, epoch)
            check_record(artifact, domain)
            if artifact['finalizedBlock'] > finalized_block:
                raise ValueError('artifact claims a future finalized block')
            if artifact['blocksPerEpoch'] != period or artifact['genesis'] != await client._substrate.block_hash(0):
                raise ValueError('epoch chain/tempo mismatch')
            if (artifact['endBlockHash'] != await client._substrate.block_hash(artifact['endBlock']) or
                    artifact['closeBlockHash'] != await client._substrate.block_hash(artifact['endBlock'] - 1)):
                raise ValueError('epoch chain provenance mismatch')
            data = artifact['window']
            plan = epoch_weight_plan(artifact, bindings, neurons, args.validator_hotkey, params, owner, owner_hotkey)
        record = {**domain, 'block': graph.block, 'window': data,
                  'validatorHotkey': args.validator_hotkey,
                  'plan': plan, 'versionKey': args.version_key,
                  'commitReveal': params['commit_reveal_weights_enabled']}
        if artifact:
            record.update(policy=POLICY, epoch=artifact['epoch'], openEpoch=finalized_block // period,
                          epochHash=digest(artifact), economics=artifact)
        if authorization:
            record['authorizationId'] = authorization['id']
        print(json.dumps(record, sort_keys=True))
        if not args.submit or plan['state'] != 'ready':
            return
        if artifact:
            updates = await client.query(('SubtensorModule', 'LastUpdate'), [args.netuid], block=graph.block)
            rate_limit = params['weights_rate_limit']
            uid = plan['validatorUid']
            if type(rate_limit) is not int or rate_limit < 0 or not isinstance(updates, list) or uid >= len(updates):
                raise ValueError('weight cooldown state unavailable')
            if graph.block - updates[uid] < rate_limit:
                print(json.dumps({'state': 'deferred', 'reason': 'chain_weight_rate_limit', 'epoch': artifact['epoch']}))
                return
        if not args.wallet:
            raise ValueError('--wallet is required for explicit submission')
        wallet = bt.Wallet(args.wallet, args.wallet_hotkey, **({'path': args.wallet_path} if args.wallet_path else {}))
        if wallet.hotkeypub.ss58_address != args.validator_hotkey:
            raise ValueError('wallet does not match intended validator')
        password = None
        if args.wallet_password_file:
            secret_path = Path(args.wallet_password_file)
            if secret_path.stat().st_mode & 0o077:
                raise ValueError('password file must be owner-only')
            # Password stays in-process; never passed on command line or printed.
            password = json.loads(secret_path.read_text())['hotkey']
        signer = wallet.get_hotkey(password)
        del password
        os.umask(0o077)
        journal = sqlite3.connect(args.journal)
        journal.execute('PRAGMA synchronous=FULL')
        bind_database(journal, domain, legacy_test=True)
        journal.execute('''CREATE TABLE IF NOT EXISTS weight_runs
            (id TEXT PRIMARY KEY, state TEXT NOT NULL, record TEXT NOT NULL, result TEXT)''')
        run_id = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        # Any prior uncertain broadcast or pending reveal requires operator reconciliation.
        # Never automatically rebroadcast after a network exception/restart.
        reserve_run(journal, run_id, record, authorization)
        broadcast_started = False
        try:
            # Recheck identity immediately before signing. No cached UID bindings.
            latest = await client.subnets.metagraph(args.netuid, commitments=False)
            current_owner, current_owner_hotkey = await asyncio.gather(
                client.query(('SubtensorModule','SubnetOwner'), [args.netuid], block=latest.block),
                client.query(('SubtensorModule','SubnetOwnerHotkey'), [args.netuid], block=latest.block))
            validate_miner_ownership(data['scores'], bindings,
                [{'hotkey':n.hotkey,'coldkey':n.coldkey} for n in latest.neurons], current_owner, current_owner_hotkey)
            if artifact:
                latest_params = await client.subnets.subnet_hyperparameters(args.netuid, block=latest.block)
                if latest_params['tempo'] + 1 != period or latest.block // period != record['openEpoch']:
                    raise ValueError('epoch changed before signing; refuse stale submission')
                if current_owner != owner or current_owner_hotkey != owner_hotkey:
                    raise ValueError('burn ownership changed before signing')
                fresh = epoch_weight_plan(artifact, bindings,
                    [{'uid':n.uid,'hotkey':n.hotkey,'coldkey':n.coldkey,'validator_permit':n.validator_permit} for n in latest.neurons],
                    args.validator_hotkey, latest_params, current_owner, current_owner_hotkey)
                if fresh != plan or args.version_key < latest_params['weights_version'] or latest_params['commit_reveal_weights_enabled'] != record['commitReveal']:
                    raise ValueError('reward plan or chain policy changed before signing')
            live = {n.uid: n.hotkey for n in latest.neurons}
            if any(live.get(uid) != hotkey for uid, hotkey in plan['identities'].items()):
                raise ValueError('UID ownership changed')
            if authorization:
                check_authorization(args.mainnet_authorization, domain, args.validator_hotkey, args.version_key)
            intent = bt.SetWeights(netuid=args.netuid, weights=plan['weights'], version_key=args.version_key)
            # Start recovery at the finalized snapshot, not a reorg-prone head.
            signed, broadcast = await prepare(client, intent, signer, journal, run_id, record['block'])
            if authorization:
                check_authorization(args.mainnet_authorization, domain, args.validator_hotkey, args.version_key)
            broadcast_started = True
            # Exactly the policy-checked bytes whose hash is durable above.
            # This pinned SDK transport does not re-sign or retry the broadcast.
            result = await client._substrate.submit_signed(signed, signer, wait_for_inclusion=True,
                                                           wait_for_finalization=True)
            if not result.success or not result.block_hash or not result.extrinsic_id:
                raise ValueError('submission not confirmed')
            state = 'pending_reveal' if record['commitReveal'] else 'finalized'
            journal.execute('UPDATE weight_runs SET state=?,result=? WHERE id=?',
                (state, json.dumps({**result.to_dict(), 'broadcast': broadcast}, default=str), run_id))
            journal.commit()
            print(json.dumps({'runId': run_id, 'state': state, 'blockHash': result.block_hash,
                              'result': result.to_dict()}))
        except BaseException:
            # A failed pre-sign identity/epoch check is provably not a broadcast.
            # Anything after entering submit_signed remains uncertain; never auto-retry.
            journal.execute('UPDATE weight_runs SET state=? WHERE id=?',
                            ('unknown' if broadcast_started else 'not_submitted', run_id))
            journal.commit()
            raise
        finally:
            journal.close()
