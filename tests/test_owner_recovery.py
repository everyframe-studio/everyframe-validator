"""Synthetic accounting only: no provider calls, wallets, weights or payments."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from test_owner_reward_policy import artifact, policy, score
from everyframe_validator.owner import reward_recovery as recovery


def report(epoch, value=0, miner='a'):
    a = artifact([score(miner, value)] if value else [])
    a.update(epoch=epoch, startBlock=epoch*361, endBlock=(epoch+1)*361,
             finalizedBlock=(epoch+1)*361+20)
    w = a['window']
    w.update(start=epoch*100000, end=(epoch+1)*100000, finalizedAt=(epoch+1)*100000+100,
             recognitionPolicy='reviewed-unassigned-v1')
    a['priceSource']['observedAt'] = w['end']-1
    w['proofs'] = ([{'jobId': 'job-'+str(epoch), 'attemptId': 'attempt-'+str(epoch),
        'minerId': miner, 'completed': w['end']-100, 'approvedAt': w['end']-50,
        'feeMicrousd': value*1000000, 'payableState': 'approved',
        'rewardEligible': True, 'enabled': True}] if value else [])
    return a


class Recoveries(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.epochs = Path(self.directory.name)/'epochs.db'
        self.source = report(10, 30)
        policy.seal_epoch(self.epochs, self.source)
        self.assessment = {
            'version': 1, 'policy': recovery.ASSESSMENT_POLICY,
            **{k:self.source[k] for k in ('network','netuid','genesis')},
            'sourceEpoch': 10, 'sourceHash': policy.digest(self.source), 'minerId': 'a',
            'observedRewardMicrousd': 0,
            'evidence': {'finalizedBlock': 4000, 'finalizedBlockHash': '0x'+'c'*64,
                         'observationStartBlock': 0, 'observationEndBlock': 4000,
                         'allValidatorRewardsReviewed': True, 'pendingCommitmentsResolved': True,
                         'evidenceHash': 'd'*64}}

    def approve(self, assessment=None):
        return recovery.approve(self.epochs, assessment or self.assessment, reviewed=True)

    def plan(self, epoch=11, current=0):
        return recovery.add_recoveries(report(epoch, current), self.epochs)

    def totals(self):
        from everyframe_validator.core.chain_scope import scope
        return recovery.status(self.epochs, scope('test'))['totals']

    def test_skipped_source_added_to_new_epoch_not_rewritten(self):
        before = self.source.copy()
        self.approve()
        target = self.plan(current=20)
        self.assertEqual(target['window']['scores'], [score('a',50,2)])
        self.assertEqual(target['window']['recoveries'][0]['sourceEpoch'], 10)
        policy.seal_epoch(self.epochs, target)
        self.assertEqual(policy.load_epoch(self.epochs,566,10), before)
        totals=self.totals()
        self.assertEqual(totals['shortfallMicrousd'],30000000)
        self.assertEqual(totals['assignedMicrousd'],30000000)
        self.assertEqual(totals['unassignedMicrousd'],0)
        self.assertEqual(totals['outstandingMicrousd'],30000000)  # Not marked paid at seal.
        self.assertEqual(totals['inFlightMicrousd'],30000000)
        self.assertEqual(self.plan(12)['window']['scores'], [])

    def test_other_validator_paid_part_is_deducted(self):
        self.assessment['observedRewardMicrousd'] = 12000000
        self.approve()
        target = self.plan()
        self.assertEqual(target['window']['scores'], [score('a',18)])
        self.assertEqual(target['window']['scores'][0]['surchargeMicrousd'], 0)

    def test_partial_pool_carries_remaining_not_prorated_away(self):
        self.approve()
        first = self.plan(current=90)
        self.assertEqual(first['window']['recoveries'][0]['amountMicrousd'],10000000)
        policy.seal_epoch(self.epochs, first)
        self.assertEqual(self.totals()['unassignedMicrousd'],20000000)
        second = self.plan(12)
        self.assertEqual(second['window']['scores'],[score('a',20)])
        policy.seal_epoch(self.epochs,second)
        self.assertEqual(self.totals()['unassignedMicrousd'],0)

    def test_current_work_exhausting_pool_does_not_consume_recovery(self):
        self.approve()
        for value in (100,120):
            target = self.plan(current=value)
            self.assertEqual(target['window']['recoveries'],[])
            self.assertEqual(self.totals()['unassignedMicrousd'],30000000)

    def test_snapshot_is_read_only_and_requires_review(self):
        before = self.epochs.read_bytes()
        self.assertEqual(self.plan()['window']['scores'],[])
        self.assertEqual(self.epochs.read_bytes(),before)
        with self.assertRaisesRegex(ValueError,'explicit_global'):
            recovery.approve(self.epochs,self.assessment)
        self.assertEqual(self.epochs.read_bytes(),before)
        self.approve()
        before = self.epochs.read_bytes()
        self.plan()
        self.assertEqual(self.epochs.read_bytes(),before)

    def test_owner_audit_never_invents_a_shortfall_or_opens_debt(self):
        from everyframe_validator.core.chain_scope import bind_database,scope
        journal=self.epochs.with_name('owner.db')
        with sqlite3.connect(journal) as db:
            bind_database(db,scope('test'))
            db.execute('CREATE TABLE weight_runs(state TEXT,record TEXT,result TEXT)')
        before=self.epochs.read_bytes(),journal.read_bytes()
        result=recovery.audit_owner(self.epochs,journal,scope('test'),10)
        self.assertEqual(result['ownerUnsubmittedReportCount'],1)
        self.assertIsNone(result['confirmedShortfallMicrousd'])
        self.assertEqual(result['reports'][0]['originalEarningsMicrousd'],30000000)
        self.assertTrue(result['reports'][0]['requiresGlobalRewardReview'])
        self.assertEqual((self.epochs.read_bytes(),journal.read_bytes()),before)
        self.assertEqual(self.totals()['shortfallMicrousd'],0)

    def test_idempotent_assessment_and_seal_across_restarts(self):
        self.assertEqual(self.approve(),self.approve())
        target = self.plan()
        hashed = policy.seal_epoch(self.epochs,target)
        self.assertEqual(policy.seal_epoch(self.epochs,target),hashed)
        self.assertEqual(self.totals()['assignedMicrousd'],30000000)

    def test_stale_concurrent_claim_rolls_back_epoch_and_reservation(self):
        self.approve()
        first,stale = self.plan(),self.plan(12)
        policy.seal_epoch(self.epochs,first)
        with self.assertRaisesRegex(ValueError,'already_assigned'):
            policy.seal_epoch(self.epochs,stale)
        with self.assertRaisesRegex(ValueError,'not finalized'):
            policy.load_epoch(self.epochs,566,12)
        self.assertEqual(self.totals()['assignedMicrousd'],30000000)

    def test_failed_seal_rolls_back_reserved_money(self):
        self.approve()
        target = self.plan()
        with sqlite3.connect(self.epochs) as db:
            db.execute("CREATE TRIGGER reject_seal BEFORE INSERT ON reward_epochs BEGIN SELECT RAISE(FAIL,'fixture failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'fixture failure'):
            policy.seal_epoch(self.epochs,target)
        self.assertEqual(self.totals()['assignedMicrousd'],0)

    def test_missing_owner_weights_cannot_approve_or_mean_zero_reward(self):
        for field in ('allValidatorRewardsReviewed','pendingCommitmentsResolved'):
            a = deepcopy(self.assessment); a['evidence'][field] = False
            with self.assertRaisesRegex(ValueError,'global_reward_review'):
                self.approve(a)
        self.assertEqual(self.totals()['shortfallMicrousd'],0)

    def test_fully_paid_unknown_negative_and_boolean_rewards_rejected(self):
        for received in (None,-1,True,30000000,40000000):
            a = deepcopy(self.assessment); a['observedRewardMicrousd'] = received
            with self.assertRaisesRegex(ValueError,'shortfall'):
                self.approve(a)

    def test_changed_assessment_conflicts_without_overwriting(self):
        self.approve()
        a = deepcopy(self.assessment); a['observedRewardMicrousd'] = 1000000
        with self.assertRaisesRegex(ValueError,'assessment_conflict'):
            self.approve(a)
        self.assertEqual(self.totals()['shortfallMicrousd'],30000000)

    def test_unapproved_or_tampered_claim_cannot_be_sealed(self):
        self.approve()
        for field,value in [('id','e'*64),('amountMicrousd',31000000),('sourceHash','e'*64),('minerId','b'),('jobs',2)]:
            target = self.plan()
            target['window']['recoveries'][0][field] = value
            with self.assertRaises(ValueError):
                policy.seal_epoch(self.epochs,target)
        self.assertEqual(self.totals()['assignedMicrousd'],0)

    def test_public_projection_no_private_recovery_or_job_data(self):
        from everyframe_validator.publish import project,sign
        from everyframe_validator.feed import verify
        from everyframe_validator.core.chain_scope import scope
        from bittensor.keyfiles import Keypair
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
        import base64
        self.approve(); target = self.plan(current=20)
        key = Ed25519PrivateKey.generate()
        encoded = base64.b64encode(key.public_key().public_bytes(Encoding.DER,PublicFormat.SubjectPublicKeyInfo)).decode()
        hotkey = Keypair(public_key=bytes([2])*32,ss58_format=42).ss58_address
        payload = project(target,{'a':hotkey})
        raw = json.dumps(sign(payload,key)).encode()
        checked = verify(raw,encoded,scope('test'),11)
        self.assertEqual(checked['artifact']['window']['scores'],[score('a',50,2)])
        for private in (b'proofs',b'recoveries',b'evidenceHash',b'observedRewardMicrousd'):
            self.assertNotIn(private,raw)
        self.assertEqual(checked['sourceHash'],policy.digest(target))

    def test_same_evidence_cannot_credit_received_rewards_twice(self):
        self.assessment['observedRewardMicrousd'] = 1000000
        self.approve()
        second = report(11,30); policy.seal_epoch(self.epochs,second)
        a = deepcopy(self.assessment)
        a.update(sourceEpoch=11,sourceHash=policy.digest(second))
        a['evidence'].update(observationEndBlock=4500,finalizedBlock=4500)
        with self.assertRaisesRegex(ValueError,'overlapping'):
            self.approve(a)

    def test_deleted_allocation_row_cannot_reopen_already_assigned_amount(self):
        self.approve(); policy.seal_epoch(self.epochs,self.plan())
        with sqlite3.connect(self.epochs) as db:
            db.execute('DELETE FROM reward_recovery_allocations')
        with self.assertRaisesRegex(ValueError,'journal_mismatch'):
            self.plan(12)

    def test_journal_tampering_blocks_finalization(self):
        self.approve()
        with sqlite3.connect(self.epochs) as db:
            db.execute("UPDATE reward_recoveries SET hash='bad'")
        with self.assertRaisesRegex(ValueError,'hash_mismatch'):
            self.plan()

    def test_future_evidence_is_not_assigned_early(self):
        self.assessment['evidence'].update(finalizedBlock=10000,observationEndBlock=10000)
        self.approve()
        self.assertEqual(self.plan()['window']['recoveries'],[])
        self.assertEqual(self.totals()['unassignedMicrousd'],30000000)

    def test_ineligible_miner_debt_is_not_lost_or_added_to_weights(self):
        self.approve()
        target=recovery.add_recoveries(report(11,20,miner='b'),self.epochs,eligible_miners={'b'})
        self.assertEqual(target['window']['scores'],[score('b',20)])
        self.assertEqual(target['window']['recoveries'],[])
        policy.seal_epoch(self.epochs,target)
        self.assertEqual(self.totals()['unassignedMicrousd'],30000000)
        self.assertEqual(recovery.add_recoveries(report(12),self.epochs,eligible_miners={'a'})['window']['scores'],[score('a',30)])

    def test_chain_anchor_checked_before_approval_without_wallet(self):
        import bittensor as bt
        from types import SimpleNamespace
        class Client:
            def __init__(self,**kw):
                self._substrate = SimpleNamespace(
                    block_hash=AsyncMock(side_effect=lambda block: self_source['genesis'] if block==0 else '0x'+'c'*64),
                    raw=SimpleNamespace(get_chain_finalised_head=AsyncMock(return_value='head'),
                        get_block_number=AsyncMock(return_value=5000)))
            async def __aenter__(self): return self
            async def __aexit__(self,*args): return False
        self_source = self.source
        with patch.object(bt,'Client',Client),patch.object(bt,'Wallet',side_effect=AssertionError('no wallet')):
            result = asyncio.run(recovery.checked_approval(self.epochs,self.assessment,True))
        self.assertEqual(result['shortfallMicrousd'],30000000)
        self.assertFalse(result['paymentConfirmed'])

    def test_recovery_is_not_a_new_job_recognition_or_recursive_debt(self):
        self.approve(); target = self.plan()
        policy.seal_epoch(self.epochs,target)
        with sqlite3.connect(self.epochs) as db:
            used = policy.recognized_work(db,566)
        self.assertEqual(used['jobs'],{'job-10'})
        a = deepcopy(self.assessment)
        a.update(sourceEpoch=11,sourceHash=policy.digest(target))
        a['evidence'].update(observationEndBlock=4500,finalizedBlock=4500)
        with self.assertRaisesRegex(ValueError,'shortfall'):
            self.approve(a)

    def finalize_case(self,enabled):
        import bittensor as bt
        from types import SimpleNamespace
        from test_owner_reward_policy import finalizer
        database=self.epochs.with_name('coordinator.db')
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE miners(id TEXT,enabled INTEGER)')
            db.execute("INSERT INTO miners VALUES('a',1)")
        binding=self.epochs.with_name('bindings.json');binding.write_text('{"a":"hot-a"}')
        target=report(11,20);window=target.pop('window')
        target.pop('priceSource');target.pop('alphaPriceUsd')
        target.update(alphaTao='10/41',startMs=window['start'],endMs=window['end'])
        class Client:
            def __init__(self,**kw):
                self._substrate=SimpleNamespace(
                    block_hash=AsyncMock(side_effect=lambda b:self_source['genesis'] if b==0 else '0x'+'a'*64),
                    raw=SimpleNamespace(get_chain_finalised_head=AsyncMock(return_value='head'),
                        get_block_number=AsyncMock(return_value=4355)))
                self.subnets=SimpleNamespace(subnet_hyperparameters=AsyncMock(return_value={'tempo':360}),
                    metagraph=AsyncMock(return_value=SimpleNamespace(neurons=[
                        SimpleNamespace(hotkey='hot-a',coldkey='cold-a')])))
                self.query=AsyncMock(side_effect=lambda key,*a,**kw:'owner' if key[1]=='SubnetOwner' else 'owner-hot')
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
        self_source=self.source
        args=SimpleNamespace(network='test',netuid=566,database=str(database),epochs=str(self.epochs),
                             recovery_bindings=str(binding) if enabled else None)
        with patch.object(bt,'Client',Client),patch.object(bt,'Wallet',side_effect=AssertionError('no wallet')),\
             patch.object(finalizer,'collect_chain',AsyncMock(return_value=target)),\
             patch.object(finalizer,'recognition_snapshot',return_value=window),\
             patch.object(finalizer,'fetch_price',return_value={
                'source':'TEST FIXTURE','observedAt':window['end']-1,'taoUsd':'1'}):
            asyncio.run(finalizer.main(args))
        return policy.load_epoch(self.epochs,566,11)

    def test_actual_finalizer_includes_reviewed_recovery_with_eligible_mapping(self):
        self.approve()
        self.assertEqual(self.finalize_case(True)['window']['scores'],[score('a',50,2)])
        self.assertEqual(self.totals()['inFlightMicrousd'],30000000)
        self.assertEqual(self.totals()['reviewedReceivedMicrousd'],0)

    def test_actual_finalizer_leaves_debt_pending_without_recipient_mapping(self):
        self.approve()
        self.assertEqual(self.finalize_case(False)['window']['scores'],[score('a',20)])
        self.assertEqual(self.totals()['unassignedMicrousd'],30000000)

    def settlement(self,target,received=0):
        return {'version':1,'policy':recovery.ASSESSMENT_POLICY,
            **{k:target[k] for k in ('network','netuid','genesis')},
            'recoveryId':target['window']['recoveries'][0]['id'],
            'allocationEpoch':target['epoch'],'allocationHash':policy.digest(target),
            'observedRecoveryMicrousd':received,
            'evidence':{'finalizedBlock':target['finalizedBlock']+10,'finalizedBlockHash':'0x'+'e'*64,
                'observationStartBlock':4000 if target['epoch']==11 else (target['epoch'])*361+30,
                'observationEndBlock':target['finalizedBlock']+10,
                'allValidatorRewardsReviewed':True,'pendingCommitmentsResolved':True,
                'evidenceHash':policy.digest({'allocation':target['epoch'],'received':received})}}

    def test_missed_recovery_itself_stays_owed_and_can_carry_again(self):
        self.approve(); first=self.plan();policy.seal_epoch(self.epochs,first)
        self.assertEqual(self.plan(12)['window']['scores'],[])  # Pending, not retryable.
        outcome=self.settlement(first)
        recovery.settle(self.epochs,outcome,reviewed=True)
        self.assertEqual(self.totals()['unassignedMicrousd'],30000000)
        self.assertEqual(self.totals()['outstandingMicrousd'],30000000)
        second=self.plan(12);policy.seal_epoch(self.epochs,second)
        self.assertEqual(second['window']['scores'],[score('a',30)])
        self.assertEqual(self.totals()['assignedMicrousd'],60000000)  # Attempts, not payment.
        self.assertEqual(self.totals()['inFlightMicrousd'],30000000)

    def test_partial_recovery_received_only_closes_received_amount(self):
        self.approve();first=self.plan();policy.seal_epoch(self.epochs,first)
        outcome=self.settlement(first,10000000)
        recovery.settle(self.epochs,outcome,reviewed=True)
        self.assertEqual(self.totals()['reviewedReceivedMicrousd'],10000000)
        self.assertEqual(self.totals()['outstandingMicrousd'],20000000)
        second=self.plan(12);policy.seal_epoch(self.epochs,second)
        recovery.settle(self.epochs,self.settlement(second,20000000),reviewed=True)
        self.assertEqual(self.totals()['outstandingMicrousd'],0)
        self.assertEqual(self.plan(13)['window']['scores'],[])

    def test_settlement_needs_finalized_global_review_not_just_owner_failure(self):
        self.approve();target=self.plan();policy.seal_epoch(self.epochs,target)
        for field in ('allValidatorRewardsReviewed','pendingCommitmentsResolved'):
            outcome=self.settlement(target);outcome['evidence'][field]=False
            with self.assertRaisesRegex(ValueError,'global_reward_review'):
                recovery.settle(self.epochs,outcome,reviewed=True)
        self.assertEqual(self.totals()['inFlightMicrousd'],30000000)

    def test_settlement_is_immutable_idempotent_and_bounded(self):
        self.approve();target=self.plan();policy.seal_epoch(self.epochs,target)
        outcome=self.settlement(target,10000000)
        with self.assertRaisesRegex(ValueError,'explicit_global'):
            recovery.settle(self.epochs,outcome)
        self.assertEqual(recovery.settle(self.epochs,outcome,reviewed=True),recovery.settle(self.epochs,outcome,reviewed=True))
        bad=deepcopy(outcome);bad['observedRecoveryMicrousd']=20000000
        with self.assertRaisesRegex(ValueError,'settlement_conflict'):
            recovery.settle(self.epochs,bad,reviewed=True)
        bad['observedRecoveryMicrousd']=31000000
        with self.assertRaisesRegex(ValueError,'invalid_recovery_settlement'):
            recovery.settle(self.epochs,bad,reviewed=True)

    def test_settlement_journal_corruption_blocks_new_allocations(self):
        self.approve();target=self.plan();policy.seal_epoch(self.epochs,target)
        recovery.settle(self.epochs,self.settlement(target),reviewed=True)
        with sqlite3.connect(self.epochs) as db:
            db.execute("UPDATE reward_recovery_settlements SET hash='bad'")
        with self.assertRaisesRegex(ValueError,'settlement_journal_mismatch'):
            self.plan(12)


if __name__ == '__main__':
    unittest.main()
