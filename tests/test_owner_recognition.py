"""Review delays must neither block an epoch nor lose/duplicate eligible work."""
from copy import deepcopy
import json
import sqlite3

from test_owner_reward_policy import Accounting, artifact, policy, score


class Recognition(Accounting):
    def setUp(self):
        super().setUp()
        self.epochs = self.path.with_name('epochs.db')

    def recognized(self, start=100000, end=1000000, at=1000100):
        return policy.recognition_snapshot(self.path, self.epochs, 566, start, end, at)

    def seal(self, window, epoch=10):
        a = artifact(); a['window'] = window
        a.update(epoch=epoch, startBlock=epoch*361, endBlock=(epoch+1)*361,
                 finalizedBlock=(epoch+1)*361+4)
        a['priceSource']['observedAt'] = window['end'] - 1
        policy.seal_epoch(self.epochs, a)
        return a

    def test_pending_review_does_not_block_other_work_and_carries_later(self):
        self.add('ready'); self.add('held', reviewed=False)
        first = self.recognized()
        self.assertEqual(first['scores'], [score('a', 1)])
        self.assertEqual(first['deferred'][0]['reason'], 'pending_review')
        a = self.seal(first)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE payables SET state='approved',approved=1100000 WHERE job_id='held'")
        second = self.recognized(1000000, 2000000, 2000100)
        self.assertEqual(second['scores'], [score('a', 1)])
        self.assertEqual(second['proofs'][0]['jobId'], 'held')
        self.seal(second, 11)
        self.assertEqual(self.recognized(2000000, 3000000, 3000100)['scores'], [])
        self.assertEqual(policy.load_epoch(self.epochs, 566, 10), a)

    def test_old_approved_never_sealed_work_is_recovered(self):
        self.add('stranded', completed=1000)
        self.assertEqual(self.recognized()['scores'], [score('a', 1)])

    def test_approval_exactly_at_close_waits_until_next_epoch(self):
        self.add('boundary')
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE payables SET approved=1000000')
        first = self.recognized()
        self.assertEqual(first['scores'], [])
        self.assertEqual(first['deferred'][0]['reason'], 'review_after_close')
        self.seal(first)
        self.assertEqual(self.recognized(1000000,2000000,2000100)['scores'], [score('a',1)])

    def test_legacy_recognized_proofs_are_not_paid_again(self):
        self.add('legacy')
        self.seal(self.snapshot())
        self.assertEqual(self.recognized()['scores'], [])

    def test_stale_concurrent_snapshot_cannot_claim_job_twice(self):
        self.add('once')
        snap = self.recognized()
        a = self.seal(snap)
        self.assertEqual(policy.seal_epoch(self.epochs, a), policy.digest(a))
        with self.assertRaisesRegex(ValueError, 'already recognized'):
            self.seal(snap, 11)
        self.assertEqual(self.recognized()['scores'], [])

    def test_job_identity_blocks_a_different_attempt(self):
        self.add('same-job'); self.seal(self.recognized())
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE attempts SET id='new-attempt'")
            db.execute("UPDATE jobs SET attempt_id='new-attempt'")
            db.execute("UPDATE payables SET attempt_id='new-attempt'")
        self.assertEqual(self.recognized()['scores'], [])

    def test_corrupt_pending_work_still_blocks(self):
        self.add('held', reviewed=False)
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE payables SET fee_microusd=1')
        with self.assertRaisesRegex(ValueError, 'inconsistent'):
            self.recognized()

    def test_public_projection_is_not_valid_migration_ledger(self):
        self.seal(artifact([score('a', 1)])['window'])
        with self.assertRaisesRegex(ValueError, 'private provenance'):
            self.recognized()

    def test_excluded_work_not_auto_approved(self):
        self.add('test', eligible=0); self.add('disputed')
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE payables SET state='disputed' WHERE job_id='disputed'")
        self.assertEqual(self.recognized()['scores'], [])

    def test_bad_score_or_future_review_cannot_be_sealed(self):
        self.add('job')
        snap = self.recognized()
        bad = deepcopy(snap); bad['scores'][0]['earningsMicrousd'] += 1
        with self.assertRaisesRegex(ValueError, 'private provenance'): self.seal(bad)
        bad = deepcopy(snap); bad['proofs'][0]['approvedAt'] = bad['end']
        with self.assertRaisesRegex(ValueError, 'outside closed'): self.seal(bad)
        self.assertEqual(self.recognized()['scores'], [score('a', 1)])

    def test_tampered_history_blocks_recognition(self):
        self.add('job'); self.seal(self.recognized())
        with sqlite3.connect(self.epochs) as db:
            db.execute("UPDATE reward_epochs SET hash='bad'")
        with self.assertRaisesRegex(ValueError, 'hash mismatch'): self.recognized()
