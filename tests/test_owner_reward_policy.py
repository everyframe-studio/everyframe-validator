"""Deterministic policy tests; fixtures are not organic work or paid generations."""
import asyncio
from copy import deepcopy
from fractions import Fraction
import importlib.util
import json
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import unittest

from everyframe_validator.owner import reward_policy as policy
from everyframe_validator.owner import finalize_epoch as finalizer
from everyframe_validator.core import reconcile_weights as reconcile
from everyframe_validator.core import validator



def artifact(scores=None):
    return {'version': 2, 'policy': policy.POLICY, 'network': 'test', 'netuid': 566,
            'genesis': finalizer.GENESIS, 'epoch': 10, 'blocksPerEpoch': 361,
            'startBlock': 3610, 'endBlock': 3971, 'finalizedBlock': 3975,
            'endBlockHash': '0x' + 'a' * 64, 'closeBlockHash': '0x' + 'b' * 64,
            # Exactly $100 miner pool: 1000 alpha * 0.41 * (10/41) USD.
            'emissionsAlpha': '1000', 'alphaPriceUsd': '10/41', 'minerShare': '0.41',
            'priceSource': {'source': 'TEST FIXTURE', 'observedAt': 960000},
            'window': {'version': 2, 'policy': policy.POLICY, 'start': 100000,
                       'end': 1000000, 'finalizedAt': 1000100, 'scores': scores or [],
                       'proofs': [], 'excluded': []}}


def score(miner, usd, jobs=1):
    return {'minerId': miner, 'earningsMicrousd': usd * 1000000, 'surchargeMicrousd': 0, 'jobs': jobs}


class Economics(unittest.TestCase):
    def test_under_pool_and_all_burn(self):
        self.assertEqual(policy.pool_microusd(artifact()), 100000000)
        self.assertEqual(policy.cap_burn({2: 30000000, 3: 20000000}, Fraction(100000000), 0),
                         {0: 32768, 2: 19660, 3: 13107})
        self.assertEqual(policy.cap_burn({}, Fraction(100000000), 0), {0: 65535})

    def test_equal_values_not_request_counts(self):
        a = artifact([score('a', 30, jobs=100), score('b', 30, jobs=1)])
        plan = make_plan(a)
        self.assertEqual(plan['weights'][2], plan['weights'][3])
        a['window']['scores'][1]['earningsMicrousd'] *= 2
        plan = make_plan(a)
        self.assertLess(abs(plan['weights'][3] / plan['weights'][2] - 2), .0001)

    def test_over_pool_scales_proportionally(self):
        weights = policy.cap_burn({2: 120000000, 3: 80000000}, Fraction(100000000), 0)
        self.assertEqual(weights, {2: 39321, 3: 26214})
        self.assertEqual(sum(weights.values()), 65535)

    def test_minimum_one_and_large_fleets(self):
        values = {uid: 1 for uid in range(1, 1001)}
        values[1001] = 10**18
        weights = policy.cap_burn(values, Fraction(10**15), 0)
        self.assertEqual(sum(weights.values()), 65535)
        self.assertEqual(weights[1001], 64535)
        self.assertTrue(all(weights[uid] == 1 for uid in range(1, 1001)))
        rng = random.Random(566)
        for _ in range(30):
            values = {uid: rng.randint(1, 10**12) for uid in range(1, 201)}
            pool = Fraction(rng.randint(1, 10**15))
            weights = policy.cap_burn(values, pool, 0)
            self.assertEqual(sum(weights.values()), 65535)
            self.assertEqual(weights, policy.cap_burn(dict(reversed(list(values.items()))), pool, 0))
            self.assertTrue(all(w >= 1 for w in weights.values()))

    def test_invalid_economics_do_not_burn(self):
        for field, value in [('emissionsAlpha', '0'), ('alphaPriceUsd', 'NaN'),
                             ('alphaPriceUsd', '-1'), ('alphaPriceUsd', float('inf')),
                             ('minerShare', '1'), ('network', 'finney')]:
            a = artifact(); a[field] = value
            with self.assertRaises((ValueError, TypeError, ZeroDivisionError)):
                make_plan(a)
        for values in ({2: -1}, {2: True}, {0: 1}):
            with self.assertRaises(ValueError): policy.cap_burn(values, Fraction(100), 0)


NEURONS = [{'uid': 0, 'hotkey': 'owner-hot', 'coldkey': 'owner', 'validator_permit': True},
           {'uid': 2, 'hotkey': 'hot-a', 'coldkey': 'cold-a', 'validator_permit': False},
           {'uid': 3, 'hotkey': 'hot-b', 'coldkey': 'cold-b', 'validator_permit': False}]
PARAMS = {'min_allowed_weights': 1, 'max_weights_limit': 65535}


def make_plan(a, neurons=None, params=None, bindings=None):
    return validator.epoch_weight_plan(a, bindings or {'a': 'hot-a', 'b': 'hot-b'},
                                      neurons or NEURONS, 'owner-hot', params or PARAMS, 'owner', 'owner-hot')


class IdentityAndJournal(unittest.TestCase):
    def test_burn_only_is_ready_and_reconciles_sdk_vector(self):
        plan = make_plan(artifact())
        self.assertEqual(plan['state'], 'ready')
        self.assertTrue(plan['allBurn'])
        self.assertEqual(plan['quantized'], {0: 65535})
        plan = make_plan(artifact([score('a', 30), score('b', 20)]))
        self.assertEqual(sum(plan['weights'].values()), 65535)
        from bittensor.intents import normalize
        uids, values = normalize(list(plan['weights']), list(plan['weights'].values()))
        self.assertEqual(plan['quantized'], dict(zip(uids, values)))
        self.assertEqual(max(plan['quantized'].values()), 65535)

    def test_burn_owner_must_match_and_no_serving_reward_to_owner(self):
        neurons = deepcopy(NEURONS); neurons[0]['coldkey'] = 'impostor'
        with self.assertRaises(ValueError): make_plan(artifact(), neurons=neurons)
        with self.assertRaises(ValueError): make_plan(artifact([score('a', 10)]), bindings={'a': 'owner-hot'})
        neurons = deepcopy(NEURONS); neurons[1]['coldkey'] = 'owner'
        with self.assertRaises(ValueError): make_plan(artifact([score('a', 10)]), neurons=neurons)

    def test_registration_permit_constraints_and_rebound_uid(self):
        neurons = deepcopy(NEURONS); neurons[0]['validator_permit'] = False
        with self.assertRaises(ValueError): make_plan(artifact(), neurons=neurons)
        with self.assertRaises(ValueError): make_plan(artifact([score('a', 10)]), bindings={'a': 'gone'})
        with self.assertRaises(ValueError): make_plan(artifact(), params={**PARAMS, 'min_allowed_weights': 2})
        with self.assertRaises(ValueError): make_plan(artifact(), params={**PARAMS, 'max_weights_limit': 100})
        neurons = deepcopy(NEURONS); neurons[0]['uid'] = 7
        self.assertEqual(make_plan(artifact(), neurons=neurons)['weights'], {7: 65535})

    def test_epoch_submission_is_deduplicated_across_restart(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE weight_runs(id TEXT PRIMARY KEY,state TEXT,record TEXT,result TEXT)')
            record = {'policy': policy.POLICY, 'network': 'test', 'netuid': 566,
                      'validatorHotkey': 'v', 'epoch': 10, 'openEpoch': 11}
            validator.reserve_run(db, 'first', record)
            db.execute("UPDATE weight_runs SET state='revealed'"); db.commit()
            for changed in ({}, {'block': 999}, {'epoch': 9}, {'epoch': 11}):
                with self.assertRaises(ValueError): validator.reserve_run(db, 'second', {**record, **changed})
            validator.reserve_run(db, 'next', {**record, 'epoch': 11, 'openEpoch': 12})
            with self.assertRaises(ValueError): validator.reserve_run(db, 'pending', {**record, 'epoch': 12, 'openEpoch': 13})
            db.execute("UPDATE weight_runs SET state='not_submitted' WHERE id='next'");db.commit()
            validator.reserve_run(db, 'safe-preflight-retry', {**record, 'epoch': 11, 'openEpoch': 12})

    def test_burn_reconciliation_requires_current_owner_identity(self):
        plan = make_plan(artifact())
        record = {'policy': policy.POLICY, 'plan': plan, 'commitReveal': False}
        result = {'extrinsic_id': '100-0001'}
        args = (record, result, [(0, 65535)], 101, {0: 'owner-hot'})
        self.assertFalse(reconcile.observation_matches(*args))
        ownership = {'owner': 'owner', 'hotkey': 'owner-hot', 'coldkey': 'owner', 'uid': 0}
        self.assertTrue(reconcile.observation_matches(*args, burn_ownership=ownership))
        self.assertFalse(reconcile.observation_matches(*args, burn_ownership={**ownership, 'coldkey': 'changed'}))


class Accounting(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'coordinator.db'
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE miners(id TEXT PRIMARY KEY,enabled INTEGER);
                CREATE TABLE jobs(id TEXT PRIMARY KEY,attempt_id TEXT,state TEXT,fee_microusd INTEGER,reward_eligible INTEGER);
                CREATE TABLE attempts(id TEXT PRIMARY KEY,job_id TEXT,miner_id TEXT,state TEXT,completed INTEGER,receipt TEXT,output_hash TEXT);
                CREATE TABLE payables(attempt_id TEXT PRIMARY KEY,job_id TEXT,miner_id TEXT,state TEXT,approved INTEGER,fee_microusd INTEGER);
                CREATE TABLE artifacts(job_id TEXT PRIMARY KEY,hash TEXT);
                INSERT INTO miners VALUES('a',1),('b',1);''')

    def add(self, job, miner='a', fee=1000000, completed=500000, reviewed=True, eligible=1):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)', (job, job, 'succeeded', fee, eligible))
            db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?)', (job, job, miner, 'accepted', completed, 'signed-receipt', 'hash'))
            db.execute('INSERT INTO payables VALUES(?,?,?,?,?,?)', (job, job, miner, 'approved' if reviewed else 'review', completed+1 if reviewed else None, fee))
            db.execute('INSERT INTO artifacts VALUES(?,?)', (job, 'hash'))

    def snapshot(self):
        return policy.epoch_snapshot(self.path, 100000, 1000000, 1000100)

    def test_value_aggregation_half_open_boundaries_and_readonly(self):
        self.add('a1', fee=2000000, completed=100000)
        self.add('a2', fee=3000000)
        self.add('b1', miner='b', fee=5000000)
        self.add('before', completed=99999)
        self.add('after', completed=1000000)
        before = self.path.read_bytes()
        snap = self.snapshot()
        self.assertEqual(snap['scores'], [score('a', 5, 2), score('b', 5)])
        self.assertEqual(self.path.read_bytes(), before)

    def test_reviews_missing_and_corrupt_accounting_never_mean_zero_work(self):
        self.add('review', reviewed=False, eligible=0)
        with self.assertRaisesRegex(ValueError, 'not reviewed'): self.snapshot()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE payables SET state='approved',approved=500001")
        self.assertEqual(self.snapshot()['scores'], [])  # Explicit nonorganic decision.
        with sqlite3.connect(self.path) as db: db.execute('UPDATE payables SET fee_microusd=90000000')
        with self.assertRaisesRegex(ValueError, 'inconsistent'): self.snapshot()

    def test_zero_nonorganic_paused_and_disputed_work(self):
        self.assertEqual(self.snapshot()['scores'], [])
        self.add('free', eligible=0)
        self.add('paused', miner='b')
        self.add('disputed')
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE miners SET enabled=0 WHERE id='b'")
            db.execute("UPDATE payables SET state='disputed' WHERE job_id='disputed'")
        self.assertEqual(self.snapshot()['scores'], [])
        self.assertEqual(len(self.snapshot()['excluded']), 3)

    def test_epoch_seal_immutable_missing_artifact_and_tampering(self):
        a = artifact(); a['window'] = self.snapshot()
        path = Path(self.directory.name) / 'epochs.db'
        hashed = policy.seal_epoch(path, a)
        self.assertEqual(policy.seal_epoch(path, a), hashed)
        self.assertEqual(policy.load_epoch(path, 566, 10), a)
        with self.assertRaises(ValueError): policy.load_epoch(path, 566, 11)
        b = deepcopy(a); b['window']['finalizedAt'] += 1
        with self.assertRaises(ValueError): policy.seal_epoch(path, b)
        with sqlite3.connect(path) as db: db.execute("UPDATE reward_epochs SET hash='wrong'")
        with self.assertRaises(ValueError): policy.load_epoch(path, 566, 10)

    def test_closed_epoch_and_fresh_price_required(self):
        for field, value in [('epoch', 11), ('finalizedBlock', 3900), ('blocksPerEpoch', 360)]:
            a = artifact(); a[field] = value
            with self.assertRaises(ValueError): policy.validate_artifact(a)
        for at in (1, 1000001):
            a = artifact(); a['priceSource']['observedAt'] = at
            with self.assertRaises(ValueError): policy.validate_artifact(a)


class Oracle(unittest.TestCase):
    def test_only_closed_fresh_candles_with_trades(self):
        data = {'error': [], 'result': {'TAOUSD': [[900, '1','1','1','200','1','1',2],
                                                  [960, '1','1','1','999','1','1',2]]}}
        result = finalizer.candle_price(data, 1000000)
        self.assertEqual(result['taoUsd'], '200')
        self.assertEqual(result['observedAt'], 960000)
        for payload, end in (({'error':['bad']}, 1000000), (data, 2000000), (data, 950000)):
            with self.assertRaises(ValueError): finalizer.candle_price(payload, end)


if __name__ == '__main__':
    unittest.main()
