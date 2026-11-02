import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from everyframe_validator import assignment, assignment_publisher, cadence
from everyframe_validator.core import prize_pool as prize, contest_pool
from everyframe_validator.core.chain_scope import scope
from everyframe_validator.core.validator import epoch_weight_plan
from everyframe_validator.publish import sign
from everyframe_validator.state import journal_rows
from conftest import hot
from test_scheduled import scheduled_case, run
from test_burn_selection import rotation, publisher_case
from test_contest_pool import contest


@pytest.fixture
def pool_rotation(contest,monkeypatch,signing):
    import bittensor as bt
    case,_,report,Base=contest
    ns=case[2]['neurons']
    ns.extend([{'uid':i,'hotkey':hot(i),'coldkey':hot(16),'validator_permit':False} for i in (7,8)])
    class Client(Base):
        def __init__(self,**kwargs):
            super().__init__(**kwargs)
            prior=self.query
            async def query(storage,args,block=None):
                if storage[1]=='OwnedHotkeys' and args==[hot(16)]: return [hot(i) for i in (6,7,8)]
                return await prior(storage,args,block=block)
            self.query=query
            async def graph(netuid,block=None,commitments=False):
                return SimpleNamespace(block=block,neurons=[SimpleNamespace(**n) for n in ns])
            self.subnets.metagraph=graph
    monkeypatch.setattr(bt,'Client',Client)
    base=assignment.project(scope('test'),case[2],case[3],report)
    value=asyncio.run(prize.from_chain(Client(),base,{'coldkey':hot(16)}))
    assigned=assignment.project(scope('test'),case[2],case[3],report,contest_pool=value)
    raw=json.dumps(sign(assigned,signing[0])).encode()
    report_raw=json.dumps(sign(report,signing[0])).encode()
    # Reports use the same envelope signature function as assignments.
    monkeypatch.setattr(assignment,'fetch',lambda url,epoch: raw if '/allocations/' in url else report_raw)
    prize.accept(case[0],{'coldkey':hot(16)})
    return case,assigned,report,Client


def test_all_validators_reproduce_rotation_and_all_pool_uids_are_selected(pool_rotation):
    case,assigned,_,_=pool_rotation
    seen=set()
    for epoch in range(100):
        p={**assigned,'chainEpoch':epoch}
        first=prize.derive(p,hot(10),case[2]['neurons'],hot(16))
        assert first==prize.derive(p,hot(10),list(reversed(case[2]['neurons'])),hot(16))
        seen.add(first['uid'])
    assert seen=={6,7,8}
    assert assigned['schema']==assignment.PRIZE_SCHEMA


def test_only_unused_weight_moves_to_selected_uid(pool_rotation):
    case,assigned,report,_=pool_rotation
    args=(report['artifact'],report['bindings'],case[2]['neurons'],hot(1),case[2]['params'],hot(10),hot(0))
    old=epoch_weight_plan(*args)
    new=epoch_weight_plan(*args,contest_pool=assigned['contestPool'])
    uid=assigned['contestPool']['uid']
    assert new['weights'][uid]==old['weights'][0]
    assert all(new['weights'][i]==old['weights'][i] for i in (2,3))
    assert new['contestPoolBudgetUnits']==old['burnBudgetUnits']
    assert sum(new['weights'].values())==65535


def test_signing_restart_keeps_same_choice_and_no_double_pay(pool_rotation,chain):
    case,assigned,_,_=pool_rotation
    assert run(case)['state']=='commit_finalized'
    assert journal_rows(case[0])[0]['record']['plan']['contestPool']==assigned['contestPool']
    assert run(case)['reason']=='chain_epoch_already_committed'
    assert chain.execute.await_count==1


def test_signed_wrong_uid_rejected(pool_rotation,signing):
    case,assigned,_,Client=pool_rotation
    forged=deepcopy(assigned);forged['contestPool']['uid']=99
    checked=assignment.authenticate(json.dumps(sign(forged,signing[0])),signing[1],scope('test'),case[2],case[3])
    with pytest.raises(ValueError,match='does_not_match_chain'): asyncio.run(prize.verify(Client(),checked,case[0]))


def test_candidates_change_next_epoch_without_changing_accepted_coldkey(pool_rotation):
    case,assigned,_,Client=pool_rotation
    asyncio.run(prize.verify(Client(),assigned,case[0],persist=True))
    new={**assigned,'chainEpoch':assigned['chainEpoch']+1,'payloadHash':'b'*64}
    new['contestPool']=prize.derive(new,hot(10),case[2]['neurons'],hot(16))
    asyncio.run(prize.verify(Client(),new,case[0],persist=True))
    assert new['contestPool']['coldkey']==hot(16)


@pytest.mark.parametrize('field,value',[('candidateCount',True),('seedHash','0'*64),('coldkey',hot(10)),('candidateHash','bad')])
def test_invalid_metadata_rejected(pool_rotation,field,value):
    assigned=deepcopy(pool_rotation[1]);assigned['contestPool'][field]=value
    with pytest.raises(ValueError): prize.validate(assigned['contestPool'],assigned)


def test_pin_rejects_downgrade_and_mismatched_coldkey(pool_rotation):
    case,assigned,_,Client=pool_rotation
    asyncio.run(prize.verify(Client(),assigned,case[0],persist=True))
    legacy={k:v for k,v in assigned.items() if k!='contestPool'}
    with pytest.raises(ValueError,match='downgrade'): asyncio.run(prize.verify(Client(),legacy,case[0]))
    new=deepcopy(assigned);new['contestPool']['coldkey']=hot(17)
    new['contestPool']['seedHash']=prize.seed_hash(new,hot(17),new['contestPool']['candidateHash'])
    with pytest.raises(ValueError,match='recipient_changed'): asyncio.run(prize.verify(Client(),new,case[0]))


def test_current_selection_cannot_be_rerolled_when_uid_changes(pool_rotation):
    case,assigned,_,_=pool_rotation
    ns=deepcopy(case[2]['neurons']);uid=assigned['contestPool']['uid']
    ns=[n for n in ns if n['uid']!=uid]
    with pytest.raises(ValueError,match='identity_changed'): contest_pool.resolve(ns,hot(10),hot(0),assigned['contestPool'])


def test_coldkey_only_publisher_activation(publisher_case,pool_rotation):
    args=publisher_case;args.contest_pool_from_chain_epoch=21;args.contest_pool_coldkey=hot(16)
    first=asyncio.run(assignment_publisher.publish(args))
    p=Path(args.output)/'contest-pool-activation.json'
    assert 'hotkey' not in json.loads(p.read_text())
    pool_rotation[0][3]['SubnetEpochIndex']=21
    args.contest_pool_from_chain_epoch=args.contest_pool_coldkey=None
    new=asyncio.run(assignment_publisher.publish(args))
    value=json.loads(Path(new['publicFile']).read_text())['payload']
    assert value['schema']==assignment.PRIZE_SCHEMA
    assert value['contestPool']['candidateCount']==3


def test_reuse_seed_is_consistent_before_and_after_projection(pool_rotation):
    case,_,_,Client=pool_rotation
    case[3].update(LastEpochBlock=3970,previousChainEpochStart=3610,chainEpochStartHash='0x'+'b'*64)
    base=assignment.project(scope('test'),case[2],case[3])
    value=asyncio.run(prize.from_chain(Client(),base,{'coldkey':hot(16)}))
    final=assignment.project(scope('test'),case[2],case[3],contest_pool=value)
    assert final['mode']=='scheduled_reuse_pool'
    assert asyncio.run(prize.from_chain(Client(),final,{'coldkey':hot(16)}))==value
