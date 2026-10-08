"""Read finalized chain weights; reconcile the scoped journal without resubmitting."""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
from .chain_scope import scope, add_network, check_database, check_record, read_network
from .submission import recover


def observation_matches(record, result, observed, last_update, identities, pending_commits=None, reveal_event=None, burn_ownership=None):
    expected = {int(uid): int(value) for uid, value in record['plan']['quantized'].items()}
    actual = {int(uid): int(value) for uid, value in observed}
    if len(actual) != len(observed) or actual != expected:
        return False
    included_block = int(result['extrinsic_id'].split('-')[0])
    # Recommitting an identical vector can advance LastUpdate while the OLD
    # vector is still visible. It is not proof that the new timelock revealed.
    if record.get('commitReveal'):
        if pending_commits is None:
            return False
        if any(item['hotkey'] == record['validatorHotkey'] and
               int(item['commit_block']) >= included_block for item in pending_commits):
            return False
        if not reveal_event or reveal_event.get('netuid') != record['netuid'] or reveal_event.get('hotkey') != record['validatorHotkey'] or reveal_event.get('block',0) <= included_block:
            return False
    if last_update < included_block:
        return False
    if record.get('policy') == 'epoch-value-cap-burn-v2':
        plan = record['plan']
        if burn_ownership != {'owner': plan['ownerColdkey'], 'hotkey': plan['burnHotkey'],
                              'coldkey': plan['ownerColdkey'], 'uid': plan['burnUid']}:
            return False
    return all(identities.get(int(uid)) == key for uid, key in record['plan']['identities'].items())


def is_reveal_event(event, netuid, hotkey):
    value = event.get('event', event)
    if value.get('module_id') != 'SubtensorModule' or value.get('event_id') != 'TimelockedWeightsRevealed':
        return False
    attributes = value.get('attributes')
    if isinstance(attributes, dict):
        attributes = list(attributes.values())
    return isinstance(attributes, (list, tuple)) and len(attributes) == 2 and attributes[0] == netuid and attributes[1] == hotkey


async def main(args):
    domain = scope(getattr(args, 'network', 'test'), getattr(args, 'netuid', None))
    if domain['network'] == 'finney':
        check_database(args.journal, domain)
    db = sqlite3.connect(args.journal)
    try:
        db.execute('PRAGMA synchronous=FULL')
        await reconcile_database(args, domain, db)
    finally:
        db.close()


async def reconcile_database(args, domain, db):
    import bittensor as bt
    rows = db.execute("SELECT id,state,record,result FROM weight_runs WHERE state IN ('pending_reveal','finalized','unknown','submitting')").fetchall()
    if not rows:
        print(json.dumps({'pending': 0, 'submittedTransaction': False}))
        return
    async with bt.Client(network=read_network(domain), fallback_endpoints=[], archive_endpoints=[], retry_forever=False) as client:
        if await client._substrate.block_hash(0) != domain['genesis']:
            raise ValueError('unexpected_chain_genesis')
        block_hash = await client._substrate.raw.get_chain_finalised_head()
        block = await client._substrate.raw.get_block_number(block_hash)
        for run_id, state, raw_record, raw_result in rows:
            record, result = json.loads(raw_record), json.loads(raw_result) if raw_result else {}
            netuid = record['netuid']
            if record['network'] != domain['network'] or netuid != domain['netuid']:
                raise ValueError('invalid_journal_scope')
            if domain['network'] == 'finney':
                check_record(record, domain)
            if record['validatorHotkey'] != args.validator_hotkey:
                raise ValueError('journal_validator_mismatch')
            if state in ('unknown', 'submitting'):
                print(json.dumps(await recover(client, db, run_id, record, result, block)), flush=True)
                continue
            uid = record['plan']['validatorUid']
            observed, updates, validator_key = await asyncio.gather(
                client.query(('SubtensorModule', 'Weights'), [netuid, uid], block=block),
                client.query(('SubtensorModule', 'LastUpdate'), [netuid], block=block),
                client.query(('SubtensorModule', 'Keys'), [netuid, uid], block=block))
            if validator_key != args.validator_hotkey or validator_key != record['validatorHotkey']:
                raise ValueError('validator_uid_changed')
            identities = {int(target): await client.query(('SubtensorModule', 'Keys'), [netuid, int(target)], block=block)
                          for target in record['plan']['identities']}
            burn_ownership = None
            if record.get('policy') == 'epoch-value-cap-burn-v2':
                owner, owner_hotkey, graph = await asyncio.gather(
                    client.query(('SubtensorModule', 'SubnetOwner'), [netuid], block=block),
                    client.query(('SubtensorModule', 'SubnetOwnerHotkey'), [netuid], block=block),
                    client.subnets.metagraph(netuid, block=block, commitments=False))
                burn = next((n for n in graph.neurons if n.uid == record['plan']['burnUid']), None)
                if burn and burn.hotkey == owner_hotkey:
                    burn_ownership = {'owner': owner, 'hotkey': owner_hotkey, 'coldkey': burn.coldkey, 'uid': burn.uid}
            # All evidence, including pending commits, must use the same
            # FINALIZED block rather than mixing finalized weights with head state.
            view = await client.at(block)
            commits = await view.read('timelocked_weight_commits', netuid=netuid) if record['commitReveal'] else {}
            pending = [item for items in commits.values() for item in items]
            included_block = int(result['extrinsic_id'].split('-')[0])
            own_pending = any(item['hotkey'] == args.validator_hotkey and item['commit_block'] >= included_block for item in pending)
            if record['commitReveal'] and not result.get('revealEvent'):
                if own_pending:
                    # The requested commit has not yet revealed at this finalized head.
                    result['revealScanBlock'] = block
                else:
                    start = max(included_block, result.get('revealScanBlock', included_block)) + 1
                    # Bounded catch-up; ordinary polling scans only ~5 new blocks.
                    end = min(block, start+19)
                    for at in range(start, end+1):
                        events = await client.query(('System','Events'), [], block=at)
                        if any(is_reveal_event(event, netuid, args.validator_hotkey) for event in (events or [])):
                            result['revealEvent'] = {'block':at,'blockHash':await client._substrate.block_hash(at),
                                                     'netuid':netuid,'hotkey':args.validator_hotkey,
                                                     'event':'TimelockedWeightsRevealed'}
                            break
                        result['revealScanBlock'] = at
                db.execute('UPDATE weight_runs SET result=? WHERE id=?', (json.dumps(result), run_id))
                db.commit()
            matches = observation_matches(record, result, observed or [],
                                          updates[uid] if uid < len(updates or []) else 0, identities, pending, result.get('revealEvent'), burn_ownership)
            report = {'runId': run_id, 'netuid': netuid, 'finalizedBlock': block,
                      'finalizedBlockHash': block_hash, 'weights': observed,
                      'matched': matches, 'submittedTransaction': False, 'revealEvent': result.get('revealEvent'),
                      'includedBlock': included_block}
            if matches:
                new_state = 'revealed' if state == 'pending_reveal' else 'observed'
                result['observation'] = report
                db.execute('UPDATE weight_runs SET state=?,result=? WHERE id=? AND state=?',
                           (new_state, json.dumps(result), run_id, state))
                db.commit()
                report['state'] = new_state
            else:
                report['pendingCommits'] = [{k: str(v) if k == 'reveals_at' else v
                                            for k,v in item.items() if k != 'ciphertext'}
                                           for items in commits.values() for item in items
                                           if item['hotkey'] == args.validator_hotkey]
                report['state'] = state
            print(json.dumps(report), flush=True)
