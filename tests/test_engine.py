from fractions import Fraction
import sqlite3
import random
from copy import deepcopy

import pytest

from everyframe_validator.core.reward_policy import cap_burn, pool_microusd
from everyframe_validator.core.validator import epoch_weight_plan, reserve_run
from everyframe_validator.core.reconcile_weights import observation_matches, is_reveal_event
from conftest import artifact, hot


def plan(a=None, neurons=None, params=None):
    return epoch_weight_plan(a or artifact(), {"a": hot(2), "b": hot(3)}, neurons or [
        {"uid": i, "hotkey": hot(i), "coldkey": hot(i+10), "validator_permit": i==1} for i in range(4)],
        hot(1), params or {"min_allowed_weights": 1,"max_weights_limit":65535}, hot(10),hot(0))


def test_non_owner_validator_same_value_math():
    p = plan()
    assert p["validatorUid"] == 1
    assert p["burnUid"] == 0
    assert p["weights"] == {0:32768, 2:19660, 3:13107}
    assert max(p["quantized"].values()) == 65535  # SDK max-normalization


def test_zero_work_and_oversubscription():
    assert plan(artifact(scores=[]))["weights"] == {0:65535}
    assert cap_burn({2:120000000,3:80000000},Fraction(100000000),0) == {2:39321,3:26214}


def test_job_counts_do_not_control_weights():
    a=artifact()
    a["window"]["scores"][0]["jobs"] = 10000
    assert plan(a)["weights"] == plan()["weights"]


@pytest.mark.parametrize("condition", ["no_permit", "owner_miner", "wrong_owner", "self_miner", "duplicate_uid", "missing_miner"])
def test_identity_constraints(condition):
    ns=[{"uid": i,"hotkey":hot(i),"coldkey":hot(i+10),"validator_permit":i==1} for i in range(4)]
    if condition=="no_permit": ns[1]["validator_permit"]=False
    if condition=="owner_miner": ns[2]["coldkey"]=hot(10)
    if condition=="wrong_owner": ns[0]["coldkey"]=hot(11)
    if condition=="self_miner": ns[1]["hotkey"],ns[2]["hotkey"]=ns[2]["hotkey"],ns[1]["hotkey"]
    if condition=="duplicate_uid": ns[2]["uid"]=3
    if condition=="missing_miner": ns.pop()
    with pytest.raises(ValueError): plan(neurons=ns)


def test_rounding_stable_under_reordering():
    rng=random.Random(117)
    for _ in range(100):
        values={i:rng.randrange(1,10**12) for i in range(1,200)}
        pool=Fraction(rng.randrange(1,10**15))
        result=cap_burn(values,pool,0)
        assert sum(result.values())==65535
        assert result==cap_burn(dict(reversed(list(values.items()))),pool,0)


def test_reveal_requires_event_and_no_pending_commit():
    p=plan()
    record={"plan":p,"netuid":566,"validatorHotkey":hot(1),"commitReveal":True,"policy":"epoch-value-cap-burn-v2"}
    result={"extrinsic_id":"4000-0"}
    args=(record,result,list(p["quantized"].items()),4100,p["identities"])
    ownership={"owner":hot(10),"hotkey":hot(0),"coldkey":hot(10),"uid":0}
    event={"netuid":566,"hotkey":hot(1),"block":4100}
    assert not observation_matches(*args,[],None,ownership)
    assert not observation_matches(*args,[{"hotkey":hot(1),"commit_block":4000}],event,ownership)
    assert observation_matches(*args,[],event,ownership)
    assert not observation_matches(*args,[],event,None)
