"""Real signatures and filesystem guards; fake RPC, no wallets or submissions."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,NoEncryption

from everyframe_validator import assignment,scheduled
from everyframe_validator.owner import assignment_service as service
from everyframe_validator.core.reward_policy import seal_epoch,digest
from everyframe_validator.feed import FeedUnavailable,verify
from everyframe_validator.core.chain_scope import scope
from everyframe_validator.publish import sign
from conftest import artifact,hot
from test_scheduled import scheduled_case


@pytest.fixture
def publisher(scheduled_case,signing,tmp_path,monkeypatch):
    root,config,view,s=scheduled_case
    seal_epoch(root/'epochs.db',artifact())
    bindings=tmp_path/'bindings.json';bindings.write_text(json.dumps({'a':hot(2),'b':hot(3)}))
    state=tmp_path/'publisher';state.mkdir(mode=0o700)
    key=tmp_path/'accounting.pem'
    key.write_bytes(signing[0].private_bytes(Encoding.PEM,PrivateFormat.PKCS8,NoEncryption()));key.chmod(0o600)
    args=SimpleNamespace(network='testnet',expected_public_key=signing[1],epochs=str(root/'epochs.db'),
        bindings=str(bindings),state_dir=str(state),public_epochs=str(tmp_path/'public-epochs'),
        public_assignments=str(tmp_path/'public-allocations'))
    monkeypatch.setattr(service,'schedule',scheduled.schedule)
    monkeypatch.setattr('everyframe_validator.assignment_publisher.schedule',scheduled.schedule)
    return args,key,view,s


def test_publish_exact_canonical_report_and_assignment_readonly(publisher,signing,chain,monkeypatch):
    import bittensor as bt
    args,key,view,s=publisher
    before=Path(args.epochs).read_bytes(),key.read_bytes()
    monkeypatch.setattr(bt,'Wallet',lambda *a,**kw:pytest.fail('publisher must never load a wallet'))
    first=asyncio.run(service.tick(args,key))
    assert first==asyncio.run(service.tick(args,key))
    assert first['state']=='assignment_feed_ready' and first['accountingEpoch']==10
    raw=(Path(args.public_epochs)/'10.json').read_bytes()
    payload=verify(raw,signing[1],scope('test'),10)
    selected=assignment.authenticate((Path(args.public_assignments)/'20.json').read_bytes(),
        signing[1],scope('test'),view,s)
    assert selected['payloadHash']==digest(payload)
    assert before==(Path(args.epochs).read_bytes(),key.read_bytes())
    assert (Path(args.public_assignments)/'20.json').stat().st_mode&0o777==0o644
    assert first['chainTransactions']==0 and first['walletAccess'] is False
    chain.execute.assert_not_called()


def test_missing_exact_seal_is_not_substituted(publisher,chain):
    args,key,view,s=publisher
    s.update(LastEpochBlock=4336,previousChainEpochStart=3975,SubnetEpochIndex=21)
    with pytest.raises(FeedUnavailable):
        asyncio.run(service.tick(args,key))
    assert not Path(args.public_assignments).exists()
    assert not Path(args.public_epochs).exists()
    chain.execute.assert_not_called()


def test_newer_report_does_not_change_target(publisher):
    args,key,_,_=publisher
    newer=artifact();newer.update(epoch=11,startBlock=3971,endBlock=4332,finalizedBlock=4336)
    seal_epoch(args.epochs,newer)
    result=asyncio.run(service.tick(args,key))
    assert result['accountingEpoch']==10
    assert not (Path(args.public_epochs)/'11.json').exists()


def test_public_conflict_fails_without_overwriting(publisher):
    args,key,_,_=publisher
    dest=Path(args.public_epochs);dest.mkdir();(dest/'10.json').write_text('{"conflict":true}')
    with pytest.raises(ValueError,match='immutable_file_conflict'):
        asyncio.run(service.tick(args,key))
    assert (dest/'10.json').read_text()=='{"conflict":true}'
    assert not Path(args.public_assignments).exists()


def test_epoch_transition_never_exposes_wrong_assignment(publisher,monkeypatch):
    args,key,_,_=publisher
    monkeypatch.setattr(service,'publish_assignment',AsyncMock(return_value={'chainEpoch':21,'accountingEpoch':11}))
    result=asyncio.run(service.tick(args,key))
    assert result['state']=='waiting'
    assert not Path(args.public_assignments).exists()


def test_key_credentials_keep_permissions_and_no_persistent_copy(publisher,tmp_path):
    _,key,_,_=publisher
    key.chmod(0o440);before=key.read_bytes()
    runtime=tmp_path/'runtime';runtime.mkdir(mode=0o700)
    with service.credential_key(key,runtime) as private:
        assert private.read_bytes()==before
        assert private.stat().st_mode&0o777==0o600
    assert list(runtime.iterdir())==[]
    assert key.read_bytes()==before and key.stat().st_mode&0o777==0o440


def test_unsafe_credential_rejected_before_copy(publisher,tmp_path):
    _,key,_,_=publisher
    key.chmod(0o644)
    runtime=tmp_path/'runtime';runtime.mkdir(mode=0o700)
    with pytest.raises(ValueError,match='readonly_credential'):
        with service.credential_key(key,runtime):
            pytest.fail('unsafe credential accepted')
    assert list(runtime.iterdir())==[]


def test_assignment_service_sandbox_has_no_signing_wallet_or_accounting_writes():
    unit=(Path(__file__).parents[1]/'deploy/everyframe-assignment-publisher.service').read_text()
    assert 'ProtectHome=true' in unit and 'NoNewPrivileges=true' in unit
    assert 'ReadOnlyPaths=/var/lib/everyframe-mainnet-publisher' in unit
    assert '--submit' not in unit and 'Wallet' not in unit
