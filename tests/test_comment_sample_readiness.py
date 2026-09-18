import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from providers.base import Collection
from providers.health import record_verification, record_comment_verification, read_verification


class CommentSampleReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="promotion-comment-evidence-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.key = "douyin:fixture"
        self.settings = {"provider": "douyin_creator", "expected_uid": "123"}
        self.result = Collection({"verified_account_id": "handle"},
                                 [{"stats": {"like": 1, "comment": 1}}], complete=True)
        self.comments = [{"comment_id": "111", "parent_comment_id": "", "reply_count": 0}]
        self.stats = {"comments_complete": True, "replies_complete": True, "expected_replies": 0}
        record_verification(self.key, self.settings, self.result, directory=self.directory)

    def record(self, comments=None, stats=None):
        return record_comment_verification(self.key, self.settings,
                                           self.comments if comments is None else comments,
                                           self.directory, stats=self.stats if stats is None else stats)

    def read(self, settings=None):
        return read_verification(self.key, settings or self.settings, self.directory)

    def test_complete_nonempty_zero_reply_sample_has_distinct_ready_mode(self):
        evidence = self.record()
        self.assertTrue(self.read()["ready"])
        self.assertTrue(evidence["comments_verified"])
        self.assertFalse(evidence["replies_verified"])
        self.assertEqual("no_replies_in_complete_sample", evidence["reply_verification_mode"])
        self.assertIn("未执行二级分页", evidence["reply_verification_note"])

    def test_incomplete_unknown_empty_or_nonroot_evidence_cannot_upgrade(self):
        cases = [([], self.stats), (self.comments, {})]
        for field in self.stats:
            stats = self.stats.copy(); stats.pop(field)
            cases.append((self.comments, stats))
        for field in ("comments_complete", "replies_complete"):
            for value in (False, 1, "true", None):
                cases.append((self.comments, {**self.stats, field: value}))
        for expected in (1, None, False, "0", 0.0):
            cases.append((self.comments, {**self.stats, "expected_replies": expected}))
        for field in ("parent_comment_id", "reply_count"):
            comments = copy.deepcopy(self.comments); comments[0].pop(field)
            cases.append((comments, self.stats))
        for count in (None, False, "0", 0.0, 1):
            cases.append(([{**self.comments[0], "reply_count": count}], self.stats))
        cases.append(([{**self.comments[0], "parent_comment_id": None}], self.stats))
        for comments, stats in cases:
            with self.subTest(comments=comments, stats=stats):
                evidence = self.record(comments, stats)
                self.assertEqual("unverified", evidence["reply_verification_mode"])
                self.assertFalse(evidence["replies_verified"])
                self.assertFalse(self.read()["ready"])

    def test_observed_reply_retains_existing_verification_behavior(self):
        evidence = self.record([{"comment_id": "222", "parent_comment_id": "111"}], {})
        self.assertTrue(evidence["replies_verified"])
        self.assertEqual("observed_replies", evidence["reply_verification_mode"])
        self.assertTrue(self.read()["ready"])

    def test_content_refresh_preserves_mode_but_configuration_change_invalidates_it(self):
        evidence = self.record()
        refreshed = record_verification(self.key, self.settings, self.result, directory=self.directory)
        for field in ("reply_verification_mode", "reply_verification_note", "sample_comments_complete",
                      "sample_replies_complete", "sample_expected_replies"):
            self.assertEqual(evidence[field], refreshed[field])
        self.assertTrue(self.read()["ready"])
        changed = {**self.settings, "expected_uid": "456"}
        self.assertFalse(self.read(changed)["ready"])
        record_verification(self.key, changed, self.result, directory=self.directory)
        self.assertFalse(self.read(changed)["ready"])

    def test_reply_sample_does_not_replace_identity_or_full_content_requirements(self):
        self.record()
        partial = Collection({"verified_account_id": "handle"}, self.result.records, complete=False)
        record_verification(self.key, self.settings, partial, directory=self.directory)
        self.assertFalse(self.read()["ready"])

