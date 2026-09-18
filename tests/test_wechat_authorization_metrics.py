"""Missing optional publication counts must not invalidate an authorized account."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from providers.base import Collection
from providers.health import record_verification,read_verification
from providers.wechat_browser import article_record
from test_wechat_browser_provider import item,group,PROFILE

class WeChatAuthorizationMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
        self.settings={'provider':'wechat_browser'}
        self.rows=[article_record(item(i+1),group(),PROFILE) for i in range(147)]
        for row in self.rows[:9]:row['stats']['comment']=None

    def verify(self,settings=None,rows=None,key='wechat_service:fixture'):
        return record_verification(key,settings or self.settings,
            Collection(PROFILE,self.rows if rows is None else rows,True),directory=self.path)

    def test_service_and_subscription_keep_missing_comment_counts_unknown(self):
        for platform in ['wechat_service','wechat_subscription']:
            with self.subTest(platform=platform):
                value=self.verify(key=platform+':fixture')
                self.assertTrue(value['metrics_verified'])
                self.assertEqual(138/147,value['metric_coverage']['comment'])
                self.assertEqual(['comment'],value['optional_metric_keys'])
                self.assertNotIn('comment',value['required_metric_keys'])
                self.assertTrue(all(row['stats']['comment'] is None for row in self.rows[:9]))

    def test_missing_required_people_metrics_still_fail(self):
        for metric in ['read_users','like_users','share_users']:
            rows=copy.deepcopy(self.rows)
            for row in rows[:9]:row['extra_metrics'][metric]=None
            self.assertFalse(self.verify(rows=rows)['metrics_verified'])

    def test_explicit_comment_requirement_still_enforced(self):
        value=self.verify(settings={**self.settings,'required_metrics':['comment']})
        self.assertFalse(value['metrics_verified'])
        self.assertEqual([],value['optional_metric_keys'])

    def test_absent_count_has_explicit_provenance_and_is_not_zero(self):
        raw=item();raw.pop('total_comment_count_contains_reply');raw.pop('comment_num')
        row=article_record(raw,group(),PROFILE)
        self.assertIsNone(row['stats']['comment']);self.assertIsNone(row['extra_metrics']['root_comments'])
        self.assertEqual('not_returned_in_publication_page',row['metric_provenance']['comment']['missing_reason'])

    def test_other_platforms_still_require_comment_counts(self):
        result=Collection({'verified_account_id':'fixture'},[{'stats':{'like':2,'comment':None}}],True)
        value=record_verification('douyin:fixture',{'provider':'douyin_creator'},result,directory=self.path)
        self.assertFalse(value['metrics_verified'])
        self.assertIn('comment',value['required_metric_keys'])

if __name__=='__main__':unittest.main()
