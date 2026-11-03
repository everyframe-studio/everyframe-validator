"""Normal upgrades adopt the published mainnet treasury without local setup."""
import argparse
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from everyframe_validator import assignment, assignment_publisher
from everyframe_validator.core import prize_pool
from everyframe_validator.core.chain_scope import scope, check_authorization
from everyframe_validator.core.treasury import TREASURY_WALLET_COLDKEY, is_release_treasury
from everyframe_validator.feed import PROFILES
from everyframe_validator.publish import project, sign
from everyframe_validator.state import initialize, atomic_json
from conftest import artifact, hot


@pytest.fixture
def bundled(tmp_path, signing):
    root = tmp_path/'existing-mainnet-state'
    config = {**PROFILES['mainnet'], 'publicKey':signing[1], 'validatorHotkey':hot(1), 'wallet':None}
    initialize(root, config)
    view = {'block':4000, 'params':{'tempo':360}}
    schedule = {'SubnetEpochIndex':20, 'LastEpochBlock':3975, 'previousChainEpochStart':3615,
                'chainEpochStartHash':'0x'+'a'*64}
    report = project(artifact('finney'), {'a':hot(2), 'b':hot(3)})
    base = assignment.project(scope('finney'), view, schedule, report)
    neurons = [{'uid':i, 'hotkey':hot(i), 'coldkey':hot(10+i)} for i in range(4)]
    neurons += [{'uid':156+i, 'hotkey':hot(6+i), 'coldkey':TREASURY_WALLET_COLDKEY} for i in range(2)]
    selection = prize_pool.derive(base,hot(10),neurons,TREASURY_WALLET_COLDKEY)
    selected = assignment.project(scope('finney'),view,schedule,report,contest_pool=selection)
    raw = json.dumps(sign(selected,signing[0])).encode()
    selected = assignment.authenticate(raw, signing[1], scope('finney'), view, schedule)
    client = SimpleNamespace(
        _substrate=SimpleNamespace(block_hash=AsyncMock(return_value=schedule['chainEpochStartHash'])),
        subnets=SimpleNamespace(metagraph=AsyncMock(return_value=SimpleNamespace(
            block=3975,neurons=[SimpleNamespace(**n) for n in neurons]))),
        query=AsyncMock(side_effect=lambda storage,args,block: hot(10) if storage[1]=='SubnetOwner' else [hot(6),hot(7)]),
        execute=AsyncMock())
    return root, selected, client


def test_normal_upgrade_needs_no_treasury_acceptance_and_preserves_state(bundled):
    root, selected, client = bundled
    names = ('config.json','identity.json','journal.db','epochs.db')
    before = {n:(root/n).read_bytes() for n in names}
    asyncio.run(prize_pool.verify(client,selected,root,persist=True))
    assert {n:(root/n).read_bytes() for n in names} == before
    assert not (root/'prize-pool-consent.json').exists()
    assert not (root/'authorization.json').exists()
    assert json.loads((root/'prize-pool-policy.json').read_text())['coldkey']==TREASURY_WALLET_COLDKEY
    client.execute.assert_not_called()
    with pytest.raises(FileNotFoundError):
        check_authorization(root/'authorization.json',scope('finney'),hot(1),1030)
    # A restart continues with the same pinned recipient without any setup command.
    asyncio.run(prize_pool.verify(client,selected,root,persist=True))


@pytest.mark.parametrize('change', [{'network':'test','netuid':566,'genesis':scope('test')['genesis']},
                                  {'netuid':118}, {'netuid':117.0}, {'genesis':'0x'+'b'*64}])
def test_builtin_treasury_is_scoped_to_exact_mainnet_domain(bundled,change):
    root,selected,client=bundled
    selected=deepcopy(selected);selected.update(change)
    selected['contestPool']['seedHash']=prize_pool.seed_hash(selected,TREASURY_WALLET_COLDKEY,selected['contestPool']['candidateHash'])
    assert not is_release_treasury(selected,TREASURY_WALLET_COLDKEY)
    with pytest.raises(ValueError,match='acceptance_required'):
        asyncio.run(prize_pool.verify(client,selected,root))
    client.query.assert_not_called()


def test_changed_treasury_cannot_be_selected_without_existing_explicit_consent(bundled):
    root,selected,client=bundled
    selected=deepcopy(selected);selected['contestPool']['coldkey']=hot(30)
    selected['contestPool']['seedHash']=prize_pool.seed_hash(selected,hot(30),selected['contestPool']['candidateHash'])
    with pytest.raises(ValueError,match='acceptance_required'):
        asyncio.run(prize_pool.verify(client,selected,root))
    client.query.assert_not_called()


def test_builtin_address_still_requires_correct_chain_derived_uid(bundled):
    root,selected,client=bundled
    selected=deepcopy(selected);selected['contestPool']['uid']=99
    with pytest.raises(ValueError,match='does_not_match_chain'):
        asyncio.run(prize_pool.verify(client,selected,root))
    assert not (root/'prize-pool-policy.json').exists()


def test_existing_consent_is_preserved_and_a_conflicting_override_is_not_ignored(bundled):
    root,selected,client=bundled
    value={'policy':prize_pool.POLICY,'coldkey':TREASURY_WALLET_COLDKEY}
    path=root/'prize-pool-consent.json';atomic_json(path,value)
    before=path.read_bytes()
    asyncio.run(prize_pool.verify(client,selected,root))
    assert path.read_bytes()==before
    atomic_json(path,{**value,'coldkey':hot(30)})
    with pytest.raises(ValueError,match='consent_mismatch'):
        asyncio.run(prize_pool.verify(client,selected,root))


def test_corrupt_consent_is_not_treated_as_missing(bundled):
    root,selected,client=bundled
    path=root/'prize-pool-consent.json';atomic_json(path,{'policy':prize_pool.POLICY,'coldkey':TREASURY_WALLET_COLDKEY});path.chmod(0o644)
    with pytest.raises(ValueError,match='private_file_permissions'):
        asyncio.run(prize_pool.verify(client,selected,root))


def test_publisher_uses_bundled_key_only_with_explicit_mainnet_activation_epoch(tmp_path):
    parser=argparse.ArgumentParser();parser.add_argument('--network',default='mainnet')
    assignment_publisher.add_treasury_arguments(parser)
    empty=parser.parse_args([])
    assert assignment_publisher.contest_activation(empty,tmp_path)==(None,False)
    implicit=parser.parse_args(['--treasury-wallet-from-chain-epoch','42'])
    explicit=parser.parse_args(['--treasury-wallet-from-chain-epoch','42','--treasury-wallet-coldkey',TREASURY_WALLET_COLDKEY])
    assert assignment_publisher.contest_activation(implicit,tmp_path)==assignment_publisher.contest_activation(explicit,tmp_path)
    assert assignment_publisher.contest_activation(implicit,tmp_path)[0]['coldkey']==TREASURY_WALLET_COLDKEY
    implicit.network='testnet'
    with pytest.raises(ValueError,match='requires_future_epoch_and_coldkey'):
        assignment_publisher.contest_activation(implicit,tmp_path)
