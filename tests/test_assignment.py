import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

from everyframe_validator import assignment, scheduled, runner
from everyframe_validator.assignment_publisher import publish
from everyframe_validator.core.chain_scope import scope
from everyframe_validator.core.reward_policy import digest
from everyframe_validator.feed import FeedUnavailable, verify
from everyframe_validator.publish import sign
from everyframe_validator.state import atomic_json, initialize, journal_rows
from conftest import hot
from test_scheduled import scheduled_case, report_for, run, advance


def documents(case, payload, signing, empty=False):
    _,config,view,s = case
    epoch,mode = assignment.target(view,s)
    raw = report_for(payload,epoch,signing,empty)
    p = verify(raw,signing[1],scope(config['network']),epoch) if mode=='signed_epoch' else None
    a = assignment.project(scope(config['network']),view,s,p)
    return raw,json.dumps(sign(a,signing[0])).encode(),a


def fixed_fetch(monkeypatch, case, report, allocation):
    base=assignment.feed_url(case[1]['feed'])
    monkeypatch.setattr(assignment,'fetch',lambda url,e: allocation if url==base else report)


def test_different_validator_histories_produce_identical_weights(scheduled_case, payload, signing, chain, monkeypatch):
    root,config,view,s=scheduled_case
    raw,assigned,_=documents(scheduled_case,payload,signing)
    fixed_fetch(monkeypatch,scheduled_case,raw,assigned)
    second=root.parent/'second-validator'
    initialize(second,{**config,'validatorHotkey':hot(0)})
    for path,epoch in ((root,1),(second,8)):
        atomic_json(path/'reward-schedule.json',{'version':1,'startEpoch':epoch,'adoptedAtBlock':100})
        atomic_json(path/'accounting-scan.json',{'version':1,'nextEpoch':epoch+1})
        with sqlite3.connect(path/'journal.db') as db:
            db.execute('INSERT INTO weight_runs VALUES(?,?,?,?)',('old','application_observed',
                json.dumps({'block':2000,'epoch':epoch,'accountingSource':'signed_epoch'}),'{}'))
    a=run(scheduled_case,submit=False)
    b=asyncio.run(runner.tick(second,submit=False,weight_cadence='chain',weight_interval_blocks=100))
    assert a['epoch']==b['epoch']==10
    assert a['weights']==b['weights']
    assert a['assignmentHash']==b['assignmentHash']
    chain.execute.assert_not_called()


def test_restart_keeps_assignment_and_never_replays(scheduled_case,payload,signing,chain,monkeypatch):
    raw,assigned,a=documents(scheduled_case,payload,signing)
    fixed_fetch(monkeypatch,scheduled_case,raw,assigned)
    result=run(scheduled_case)
    assert result['epoch']==10 and not result['allBurn']
    assert run(scheduled_case)['reason']=='chain_epoch_already_committed'
    assert chain.execute.await_count==1
    assert json.loads((scheduled_case[0]/'assignments/20.json').read_text())=={'assignmentHash':digest(a)}


@pytest.mark.parametrize('change',[
    {'chainEpoch':21},{'chainEpoch':True},{'chainEpochStart':3976},
    {'previousChainEpochStart':3614},{'cutoffBlock':3976},{'accountingEpoch':9},
    {'blocksPerEpoch':360},{'chainEpochStartHash':'0x'+'b'*64},
    {'policy':'unknown'},{'mode':'signed_epoch','payloadHash':None},
    {'netuid':117},{'genesis':'0x'+'b'*64},{'schema':'unknown'},
])
def test_even_signed_assignment_must_match_chain(scheduled_case,payload,signing,change):
    _,_,a=documents(scheduled_case,payload,signing)
    a.update(change)
    with pytest.raises(ValueError):
        assignment.authenticate(json.dumps(sign(a,signing[0])),signing[1],scope('test'),scheduled_case[2],scheduled_case[3])


def test_signature_tamper_rejected(scheduled_case,payload,signing):
    _,raw,_=documents(scheduled_case,payload,signing)
    e=json.loads(raw);e['payload']['accountingEpoch']=9
    with pytest.raises(InvalidSignature):
        assignment.authenticate(json.dumps(e),signing[1],scope('test'),scheduled_case[2],scheduled_case[3])


def test_report_must_match_assigned_hash(scheduled_case,payload,signing,chain,monkeypatch):
    _,assigned,_=documents(scheduled_case,payload,signing)
    other=report_for(payload,10,signing,empty=True)
    fixed_fetch(monkeypatch,scheduled_case,other,assigned)
    with pytest.raises(ValueError,match='assigned_accounting_provenance'):
        run(scheduled_case)
    assert journal_rows(scheduled_case[0])==[]
    chain.execute.assert_not_called()


def test_valid_conflicting_assignment_is_not_adopted(scheduled_case,payload,signing,chain,monkeypatch):
    raw,assigned,_=documents(scheduled_case,payload,signing)
    fixed_fetch(monkeypatch,scheduled_case,raw,assigned)
    run(scheduled_case,submit=False)
    raw,assigned,_=documents(scheduled_case,payload,signing,empty=True)
    fixed_fetch(monkeypatch,scheduled_case,raw,assigned)
    with pytest.raises(ValueError,match='immutable_file_conflict'):
        run(scheduled_case)
    chain.execute.assert_not_called()


def test_assignment_absent_waits_without_local_burn_or_old_report(scheduled_case,chain,monkeypatch):
    monkeypatch.setattr(assignment,'fetch',lambda *a: (_ for _ in ()).throw(FeedUnavailable()))
    assert run(scheduled_case,missing='burn')['reason']=='canonical_assignment_or_report_not_published'
    chain.execute.assert_not_called()
    assert not (scheduled_case[0]/'assignments').exists()


def test_assigned_report_absent_waits_without_consuming_it(scheduled_case,payload,signing,chain,monkeypatch):
    raw,assigned,_=documents(scheduled_case,payload,signing)
    base=assignment.feed_url(scheduled_case[1]['feed'])
    def fetch(url,e):
        if url==base:return assigned
        raise FeedUnavailable()
    monkeypatch.setattr(assignment,'fetch',fetch)
    assert run(scheduled_case)['state']=='waiting'
    assert journal_rows(scheduled_case[0])==[]
    fixed_fetch(monkeypatch,scheduled_case,raw,assigned)
    assert run(scheduled_case)['state']=='commit_finalized'


def test_repeated_accounting_window_has_shared_signed_burn_slot(scheduled_case,payload,signing,chain,monkeypatch):
    scheduled_case[3].update(LastEpochBlock=3970,previousChainEpochStart=3610)
    assert assignment.target(scheduled_case[2],scheduled_case[3])==(9,'scheduled_reuse_burn')
    result=run(scheduled_case)
    assert result['accountingSource']=='scheduled_reuse_burn'
    assert result['weights']=={0:65535}
    assert result['allBurn'] is True
    assert not (scheduled_case[0]/'accepted/9.json').exists()
    assert journal_rows(scheduled_case[0])[0]['record']['payloadHash'] is None


def test_recovery_values_are_calculated_by_both_validators(scheduled_case,payload,signing,monkeypatch):
    # The public aggregate includes a reviewed $12 correction in earnings, not a
    # fabricated larger emission pool or a local validator-specific adjustment.
    corrected=deepcopy(payload)
    corrected['artifact']['window']['scores'][0]['earningsMicrousd']+=12_000_000
    corrected['artifact']['window']['scores'][0]['jobs']+=1
    raw,assigned,_=documents(scheduled_case,corrected,signing)
    fixed_fetch(monkeypatch,scheduled_case,raw,assigned)
    first=run(scheduled_case,submit=False)
    second=scheduled_case[0].parent/'fresh'
    initialize(second,{**scheduled_case[1],'validatorHotkey':hot(0)})
    other=asyncio.run(runner.tick(second,weight_cadence='chain',weight_interval_blocks=100))
    assert first['weights']==other['weights']
    assert first['epoch']==other['epoch']==10


def test_publisher_roundtrip_idempotence_and_no_wallet(scheduled_case,payload,signing,chain,tmp_path,monkeypatch):
    import bittensor as bt
    raw,_,_=documents(scheduled_case,payload,signing)
    epochs=tmp_path/'public-epochs';epochs.mkdir();(epochs/'10.json').write_bytes(raw)
    key=tmp_path/'key.pem'
    key.write_bytes(signing[0].private_bytes(Encoding.PEM,PrivateFormat.PKCS8,NoEncryption()))
    key.chmod(0o600)
    a=SimpleNamespace(network='testnet',expected_public_key=signing[1],signing_key=str(key),
                      epochs_dir=str(epochs),output=str(tmp_path/'allocations'))
    monkeypatch.setattr('everyframe_validator.assignment_publisher.schedule',scheduled.schedule)
    monkeypatch.setattr(bt,'Wallet',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('no wallet')))
    first=asyncio.run(publish(a))
    assert first==asyncio.run(publish(a))
    checked=assignment.authenticate(Path(first['publicFile']).read_bytes(),signing[1],scope('test'),scheduled_case[2],scheduled_case[3])
    assert checked['accountingEpoch']==10
    assert first['chainTransactions']==0 and first['walletAccess'] is False
    assert Path(first['publicFile']).stat().st_mode&0o777==0o644
    chain.execute.assert_not_called()


def test_report_target_is_stable_across_poll_time(scheduled_case):
    view,s=scheduled_case[2:]
    first=assignment.target(view,s)
    view['block']+=40;view['epoch']+=1
    assert assignment.target(view,s)==first


def test_assignment_url_keeps_same_origin():
    assert assignment.feed_url('https://subnet.everyframe.studio/mainnet/v1/validator/epochs/')==\
        'https://subnet.everyframe.studio/mainnet/v1/validator/allocations/'


def test_dry_canary_checks_current_assignment_outside_signing_window(scheduled_case,chain):
    scheduled_case[2]['block']=4200
    result=run(scheduled_case,submit=False)
    assert result['state']=='dry_run'
    assert result['signingWindowOpen'] is False
    assert result['signingWindowReason']=='waiting_for_chain_epoch_window'
    assert result['submittedTransaction'] is False
    chain.execute.assert_not_called()
    chain.preview.assert_not_called()
