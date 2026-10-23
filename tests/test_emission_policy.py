"""Dynamic miner-pool accounting: real math/signatures, synthetic chain only."""
import asyncio
from copy import deepcopy
from fractions import Fraction
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from everyframe_validator.core import emission_policy as emission
from everyframe_validator.core import reward_policy as reader
from everyframe_validator.owner import reward_policy as writer
from everyframe_validator.owner import finalize_epoch as finalizer
from everyframe_validator import assignment
from everyframe_validator.feed import verify, validate_payload
from everyframe_validator.publish import project, sign
from everyframe_validator.core.chain_scope import scope
from everyframe_validator.core.validator import epoch_weight_plan
from conftest import artifact, hot


def vector(owner=33, validator=34):
    owner_parts=emission.WEIGHT_ONE*owner//100
    validator_parts=emission.WEIGHT_ONE*validator//100
    return [0,0,owner_parts,validator_parts,emission.WEIGHT_ONE-owner_parts-validator_parts]


def new_artifact(period=2, samples=None):
    a=artifact('finney')
    evidence={'schema':emission.EVIDENCE_SCHEMA,'samples':samples or
              [[1_000_000_000,[str(v) for v in vector()]] for _ in range(period)]}
    total,miner=emission.summarize(evidence,period)
    a.update(blocksPerEpoch=period,startBlock=10*period,endBlock=11*period,
             finalizedBlock=11*period+4,emissionsAlpha=str(Fraction(total,10**9)),
             minerShare=str(Fraction(miner,total)),minerSharePolicy=emission.CHAIN_SHARE_POLICY,
             emissionEvidence=evidence,alphaPriceUsd='1')
    return a


def chain(weights=None, rates=None):
    async def query(key,args,block):
        if key==('SubtensorModule','SubnetEmissionWeights'):
            return (weights or {}).get(block,vector())
        if key==('SubtensorModule','SubnetAlphaOutEmission'):
            return (rates or {}).get(block,1_000_000_000)
        return {('Timestamp','Now'):block*12000,
                ('SubtensorModule','SubnetTAO'):1_000_000_000,
                ('SubtensorModule','SubnetAlphaIn'):2_000_000_000,
                ('SubtensorModule','SubnetOwnerCut'):11796,
                ('SubtensorModule','RecycleOrBurn'):'Recycle'}[key]
    return SimpleNamespace(query=AsyncMock(side_effect=query),
        subnets=SimpleNamespace(subnet_hyperparameters=AsyncMock(return_value={'tempo':1,'owner_cut_enabled':True})),
        _substrate=SimpleNamespace(block_hash=AsyncMock(side_effect=lambda b:scope('finney')['genesis'] if b==0 else '0x'+'a'*64),
          raw=SimpleNamespace(get_chain_finalised_head=AsyncMock(return_value='head'),get_block_number=AsyncMock(return_value=22))))


def test_33_percent_uses_total_not_33_percent_of_41():
    a=new_artifact()
    assert Fraction(a['minerShare'])==Fraction(329_999_999,1_000_000_000)
    assert reader.pool_microusd(a)==writer.pool_microusd(a)==Fraction(659_999_998,1000)
    assert float(Fraction(a['minerShare']))==pytest.approx(.33)
    assert float(Fraction(a['minerShare']))!=pytest.approx(.33*.41)


def test_chain_rounding_never_rounds_pool_up():
    weights=vector()
    assert emission.credit_rao(1_000_000_000,weights)==329_999_999
    assert emission.credit_rao(0,weights)==0
    assert emission.credit_rao(1,weights)==0
    assert emission.credit_rao(1_000_000_000,weights)<=Fraction(1_000_000_000*weights[4],emission.WEIGHT_ONE)


def test_signed_evidence_roundtrip_and_privacy(signing):
    a=new_artifact(361)
    payload=project(a,{'a':hot(2),'b':hot(3)})
    raw=json.dumps(sign(payload,signing[0])).encode()
    assert len(raw)<100_000
    checked=verify(raw,signing[1],scope('finney'),10)
    assert checked['artifact']['emissionEvidence']==a['emissionEvidence']
    assert b'PRIVATE' not in raw
    assert reader.pool_microusd(checked['artifact'])==writer.pool_microusd(a)


@pytest.mark.parametrize('change',[
    lambda a:a.update(minerShare='0.41'),
    lambda a:a.update(emissionsAlpha='3'),
    lambda a:a.pop('emissionEvidence'),
    lambda a:a['emissionEvidence'].update(schema='unknown'),
    lambda a:a['emissionEvidence'].update(private='not-allowed'),
    lambda a:a['emissionEvidence']['samples'].pop(),
    lambda a:a['emissionEvidence']['samples'][0].__setitem__(0,True),
    lambda a:a['emissionEvidence']['samples'][0].__setitem__(0,-1),
    lambda a:a['emissionEvidence']['samples'][0].__setitem__(0,1<<64),
    lambda a:a['emissionEvidence']['samples'][0][1].__setitem__(4,'0'),
    lambda a:a['emissionEvidence']['samples'][0][1].__setitem__(0,'01'),
    lambda a:a['emissionEvidence']['samples'][0][1].__setitem__(0,0),
    lambda a:a['emissionEvidence']['samples'][0][1].__setitem__(0,'18446744073709551616'),
    lambda a:a['emissionEvidence']['samples'][0].__setitem__(1,[str(v) for v in vector(34,34)]),
    lambda a:a['emissionEvidence']['samples'][0].__setitem__(1,[str(v) for v in vector(18,51)]),
])
def test_bad_or_incomplete_evidence_is_not_a_valid_pool(change):
    a=new_artifact();change(a)
    for policy in (reader,writer):
        with pytest.raises((ValueError,TypeError,KeyError)):
            policy.validate_artifact(a)


def test_unknown_policy_or_evidence_under_legacy_is_rejected():
    for policy in (None,'unknown','fixed-41-percent-v1'):
        a=new_artifact()
        if policy is None:a.pop('minerSharePolicy')
        else:a['minerSharePolicy']=policy
        with pytest.raises(ValueError):reader.validate_artifact(a)
        with pytest.raises(ValueError):project(a,{'a':hot(2),'b':hot(3)})


def test_signature_does_not_replace_chain_verification():
    a=new_artifact()
    client=chain(rates={20:2_000_000_000})
    with pytest.raises(ValueError,match='chain_emission_evidence_mismatch'):
        asyncio.run(emission.verify_chain_evidence(client,a))


def test_full_window_and_parent_state_are_used():
    old,new=vector(18,41),vector()
    client=chain(weights={19:old,20:new},rates={20:1_000_000_000,21:2_000_000_000})
    a=asyncio.run(finalizer.collect_chain(client,117,'finney'))
    assert a['minerSharePolicy']==emission.CHAIN_SHARE_POLICY
    assert a['emissionEvidence']['samples']==[[1_000_000_000,list(map(str,old))],
                                             [2_000_000_000,list(map(str,new))]]
    miner=emission.credit_rao(1_000_000_000,old)+emission.credit_rao(2_000_000_000,new)
    assert Fraction(a['minerShare'])==Fraction(miner,3_000_000_000)
    assert a['emissionsAlpha']=='3'
    # A scalar close-of-window percentage would give the wrong budget.
    assert Fraction(a['minerShare'])!=Fraction(emission.credit_rao(3_000_000_000,new),3_000_000_000)
    requested=[(call.args[0],call.kwargs['block']) for call in client.query.await_args_list]
    assert (('SubtensorModule','SubnetEmissionWeights'),19) in requested
    assert (('SubtensorModule','SubnetEmissionWeights'),20) in requested
    assert (('SubtensorModule','SubnetEmissionWeights'),21) not in requested


def test_reader_cache_is_hash_bound_and_doctor_does_not_write(tmp_path):
    a=new_artifact();client=chain()
    asyncio.run(assignment.verify_emissions(client,a,tmp_path))
    assert not (tmp_path/'verified-emissions').exists()
    asyncio.run(assignment.verify_emissions(client,a,tmp_path,persist=True))
    assert (tmp_path/'verified-emissions/10.json').exists()
    client.query.reset_mock()
    asyncio.run(assignment.verify_emissions(client,a,tmp_path))
    client.query.assert_not_called()
    changed=deepcopy(a);changed['window']['scores'][0]['earningsMicrousd']+=1
    with pytest.raises(ValueError,match='immutable_emission_evidence_conflict'):
        asyncio.run(assignment.verify_emissions(client,changed,tmp_path))


def test_rpc_failure_never_creates_verified_cache(tmp_path):
    client=chain();client.query.side_effect=TimeoutError('fixture')
    with pytest.raises(TimeoutError):
        asyncio.run(assignment.verify_emissions(client,new_artifact(),tmp_path,persist=True))
    assert not (tmp_path/'verified-emissions').exists()


def test_existing_signed_policies_remain_immutable(tmp_path):
    old=artifact('finney')
    fixed=deepcopy(old);fixed.update(minerShare='0.41',minerSharePolicy='fixed-41-percent-v1',
                                  epoch=11,startBlock=3971,endBlock=4332,finalizedBlock=4336)
    path=tmp_path/'epochs.db'
    reader.seal_epoch(path,old);reader.seal_epoch(path,fixed)
    before=reader.load_epoch(path,117,10),reader.load_epoch(path,117,11)
    reader.validate_artifact(new_artifact())
    assert before==(reader.load_epoch(path,117,10),reader.load_epoch(path,117,11))
    with pytest.raises(ValueError,match='already sealed'):
        reader.seal_epoch(path,new_artifact(361))


def test_independent_validators_use_identical_pool_and_weights():
    a=new_artifact(361)
    neurons=[{'uid':i,'hotkey':hot(i),'coldkey':hot(i+10),'validator_permit':i in (0,1)} for i in range(4)]
    params={'min_allowed_weights':1,'max_weights_limit':65535}
    plans=[epoch_weight_plan(a,{'a':hot(2),'b':hot(3)},neurons,hot(uid),params,hot(10),hot(0)) for uid in (0,1)]
    assert plans[0]['quantized']==plans[1]['quantized']
    assert plans[0]['totalWorkMicrousd']==50_000_000
    assert Fraction(plans[0]['poolMicrousd'])==Fraction(361*329_999_999,1000)


def test_legacy_finalizer_mode_is_explicitly_available_for_staged_rollout():
    a=asyncio.run(finalizer.collect_chain(chain(),117,'finney',chain_emissions=False))
    assert a['minerSharePolicy']=='fixed-41-percent-v1'
    assert a['minerShare']=='0.41'
    assert 'emissionEvidence' not in a


def history_client(monkeypatch):
    from bittensor._transport import storage
    class Codec:
        def storage_entry(self,module,name):return name
        def storage_key(self,entry,args):return b'r' if entry=='SubnetAlphaOutEmission' else b'w'
    codec=Codec()
    encode=lambda value:'0x'+json.dumps(value).encode().hex()
    groups=[{'block':'hash19','changes':[['0x72',encode(1_000_000_000)],['0x77',encode(vector(18,41))]]},
            {'block':'hash20','changes':[['0x72',encode(2_000_000_000)],['0x77',encode(vector())]]}]
    async def request(method,params):
        if method=='state_queryStorage':return groups
        if method=='chain_getHeader':return {'number':'0x14'}
        raise AssertionError(method)
    monkeypatch.setattr(storage,'decode_storage_value',lambda codec,entry,raw:json.loads(raw))
    client=SimpleNamespace(query=AsyncMock(side_effect=AssertionError('must not issue per-block RPCs')),
        _substrate=SimpleNamespace(block_hash=AsyncMock(side_effect=lambda b:'hash'+str(b)),
            raw=SimpleNamespace(_runtimes=SimpleNamespace(codec_at=AsyncMock(return_value=codec)),
                                rpc_request=AsyncMock(side_effect=request))))
    return client,groups


def test_bounded_history_matches_per_block_reads_with_intrablock_changes(monkeypatch):
    client,groups=history_client(monkeypatch)
    actual=asyncio.run(emission.collect_evidence(client,117,20,22))
    assert actual['samples']==[[2_000_000_000,list(map(str,vector(18,41)))],
                               [2_000_000_000,list(map(str,vector()))]]
    client.query.assert_not_called()
    requests=client._substrate.raw.rpc_request.await_args_list
    assert [r.args[0] for r in requests]==['state_queryStorage','chain_getHeader']
    assert requests[0].args[1]==[['0x72','0x77'],'hash19','hash21']


@pytest.mark.parametrize('change',[
    lambda groups:groups[0]['changes'].pop(),
    lambda groups:groups[0]['changes'].append(groups[0]['changes'][0]),
    lambda groups:groups[0]['changes'][0].__setitem__(0,'unknown'),
    lambda groups:groups[0]['changes'][0].__setitem__(1,None),
    lambda groups:groups.reverse(),
    lambda groups:groups[1].__setitem__('block','noncanonical'),
    lambda groups:groups[1].update(extra='not-allowed'),
])
def test_incomplete_or_noncanonical_bulk_history_fails_closed(monkeypatch,change):
    client,groups=history_client(monkeypatch);change(groups)
    with pytest.raises(ValueError):asyncio.run(emission.collect_evidence(client,117,20,22))
    client.query.assert_not_called()


def test_bulk_transport_error_does_not_trigger_hundreds_of_fallbacks(monkeypatch):
    client,_=history_client(monkeypatch)
    client._substrate.raw.rpc_request.side_effect=TimeoutError('fixture')
    with pytest.raises(TimeoutError):asyncio.run(emission.collect_evidence(client,117,20,22))
    client.query.assert_not_called()


def test_bulk_codec_transition_is_deferred(monkeypatch):
    client,_=history_client(monkeypatch)
    client._substrate.raw._runtimes.codec_at.side_effect=[object(),object()]
    with pytest.raises(ValueError,match='runtime_upgrade'):
        asyncio.run(emission.collect_evidence(client,117,20,22))
    client._substrate.raw.rpc_request.assert_not_called()


def test_chain_policy_cannot_be_smuggled_into_testnet():
    a=artifact('test')
    a['minerSharePolicy']=emission.CHAIN_SHARE_POLICY
    for policy in (reader,writer):
        with pytest.raises(ValueError):policy.validate_artifact(a)


def test_bulk_archive_errors_have_safe_actionable_diagnostics(monkeypatch):
    from bittensor._transport.errors import SubstrateRequestException
    from everyframe_validator.diagnostics import diagnostic
    client,_=history_client(monkeypatch)
    client._substrate.raw.rpc_request.side_effect=SubstrateRequestException('untrusted-private-provider-detail')
    with pytest.raises(ValueError,match='^chain_emission_history_unavailable$') as failed:
        asyncio.run(emission.collect_evidence(client,117,20,22))
    result=diagnostic(failed.value)
    assert result['code']=='emission_history_unavailable'
    assert 'state_queryStorage' in result['message']
    assert 'untrusted-private' not in json.dumps(result)
    client.query.assert_not_called()


@pytest.mark.parametrize('error',[
    {'code':-32601,'message':'Method not found'},
    {'code':4003,'message':'RPC call is unsafe to be called externally'},
])
def test_explicit_capability_rejection_uses_safe_point_batches(monkeypatch,error):
    from bittensor._transport.errors import SubstrateRequestException
    client,groups=history_client(monkeypatch)
    encode=lambda value:'0x'+json.dumps(value).encode().hex()
    async def request(method,params):
        if method=='state_queryStorage':raise SubstrateRequestException({'error':error})
        assert method=='state_queryStorageAt'
        block=int(params[1].removeprefix('hash'))
        rate=1_000_000_000 if block==19 else 2_000_000_000
        weights=vector(18,41) if block==19 else vector()
        return [{'block':params[1],'changes':[['0x72',encode(rate)],['0x77',encode(weights)]]}]
    client._substrate.raw.rpc_request.side_effect=request
    evidence=asyncio.run(emission.collect_evidence(client,117,20,22))
    assert evidence['samples']==[[2_000_000_000,list(map(str,vector(18,41)))],
                                  [2_000_000_000,list(map(str,vector()))]]
    calls=client._substrate.raw.rpc_request.await_args_list
    assert len([c for c in calls if c.args[0]=='state_queryStorage'])==1
    assert len([c for c in calls if c.args[0]=='state_queryStorageAt'])==3
    client.query.assert_not_called()


def test_pruned_initial_point_fails_before_fanout(monkeypatch):
    from bittensor._transport.errors import SubstrateRequestException,StateDiscardedError
    client,_=history_client(monkeypatch)
    async def request(method,params):
        if method=='state_queryStorage':raise SubstrateRequestException({'error':{'code':-32601}})
        raise StateDiscardedError('hash19')
    client._substrate.raw.rpc_request.side_effect=request
    with pytest.raises(ValueError,match='chain_emission_history_unavailable'):
        asyncio.run(emission.collect_evidence(client,117,20,22))
    assert client._substrate.raw.rpc_request.await_count==2
    client.query.assert_not_called()


def test_rate_limit_never_falls_back_to_point_requests(monkeypatch):
    from bittensor._transport.errors import SubstrateRequestException
    client,_=history_client(monkeypatch)
    client._substrate.raw.rpc_request.side_effect=SubstrateRequestException({'error':{'code':429,'message':'rate limit'}})
    with pytest.raises(ValueError,match='chain_emission_history_unavailable'):
        asyncio.run(emission.collect_evidence(client,117,20,22))
    assert client._substrate.raw.rpc_request.await_count==1


def test_doctor_can_probe_new_policy_before_activation(profile,chain,payload,signing,monkeypatch):
    from everyframe_validator import scheduled
    import everyframe_validator.doctor as module
    root,config=profile
    s={'SubnetEpochIndex':20,'LastEpochBlock':3975,'previousChainEpochStart':3615,'chainEpochStartHash':'0x'+'a'*64}
    monkeypatch.setattr(scheduled,'schedule',AsyncMock(return_value=s))
    selected=assignment.project(scope('test'),{'block':4000,'params':chain.params},s,payload)
    monkeypatch.setattr(module,'fetch',lambda url,e:json.dumps(sign(
        selected if url==assignment.feed_url(config['feed']) else payload,signing[0])).encode())
    probe=AsyncMock(return_value=new_artifact(361)['emissionEvidence'])
    monkeypatch.setattr(emission,'collect_evidence',probe)
    before={p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
    result=asyncio.run(module.doctor(root,check_emissions=True))
    assert result['ok'] and not result['submittedTransaction']
    check=next(c for c in result['checks'] if c['name']=='emissionHistory')
    assert check['ok'] and check['readOnlyProbe']
    assert Fraction(check['effectiveMinerShare'])==Fraction(329999999,1000000000)
    assert before=={p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert not (root/'verified-emissions').exists()
    chain.execute.assert_not_called()
