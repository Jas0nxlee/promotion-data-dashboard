"""Bilibili authorization evidence must match the complete visible inbox scan."""
from contextlib import contextmanager
import copy
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from providers.base import ProviderError
from providers.bilibili_creator import BilibiliCreatorProvider
from providers.browser import BrowserSource

ACCOUNT={'platform':'bilibili','account_name':'fixture','platform_uid':'123'}
PROFILE={'isLogin':True,'mid':123,'uname':'fixture'}


def comment(cid,root=0,reply_count=0):
    return {'rpid':cid,'root':root,'parent':root,'bvid':'BVfixture','member':{'mid':123},
            'content':{'message':'fixture'},'rcount':reply_count}


class Browser:
    def __init__(self,pages):
        self.pages=copy.deepcopy(pages);self.call_count=0;self.requests=[]
    @contextmanager
    def session(self):yield self
    def get_json(self,url,params=None,**kwargs):
        self.call_count+=1
        self.requests.append((url,params,kwargs))
        data=PROFILE if urlsplit(url).path=='/x/web-interface/nav' else self.pages.pop(0)
        return {'code':0,'data':data}


def page(rows,total):return {'list':rows,'page':{'total':total}}


class BilibiliCommentCoverageTests(unittest.TestCase):
    item={'content_id':'BVfixture'}

    def provider(self,pages):
        return BilibiliCreatorProvider(ACCOUNT,{},Browser(pages))

    def test_full_mixed_catalog_proves_comments_and_replies(self):
        provider=self.provider([page([comment(10,reply_count=2)],3),
                                page([comment(11,10),comment(12,10)],3)])
        rows,stats=provider.comments(self.item,max_pages=2,include_replies=True)
        self.assertEqual(3,len(rows))
        self.assertIs(stats['comments_complete'],True)
        self.assertIs(stats['replies_complete'],True)
        self.assertEqual(2,stats['expected_replies'])
        self.assertEqual(2,stats['root_pages']);self.assertEqual(0,stats['reply_pages'])
        replies,used=provider.replies(self.item,'10',max_pages=2)
        self.assertEqual(2,len(replies));self.assertEqual(0,used)
        self.assertEqual([{}, {"min_interval": 2.0}, {"min_interval": 2.0}],
                         [request[2] for request in provider.browser.requests])

    def test_identity_and_catalog_keep_default_pacing(self):
        provider=self.provider([])
        provider._profile()
        self.assertEqual({},provider.browser.requests[0][2])
        provider.browser.pages=[{"mid":123,"follower":5},
                                {"page":{"count":0},"arc_audits":[]}]
        provider.collect(max_pages=1)
        self.assertEqual([{}, {}, {}],
                         [request[2] for request in provider.browser.requests[1:]])

    def test_root_only_output_cannot_certify_delivered_replies(self):
        provider=self.provider([page([comment(10,reply_count=1),comment(11,10)],2)])
        roots,stats=provider.comments(self.item,include_replies=False)
        self.assertEqual(1,len(roots))
        self.assertTrue(stats['comments_complete']);self.assertFalse(stats['replies_complete'])
        self.assertEqual(1,stats['expected_replies'])
        rows,complete=provider.comments(self.item,include_replies=True)
        self.assertEqual(2,len(rows));self.assertTrue(complete['replies_complete'])
        self.assertEqual(2,provider.call_count)

    def test_zero_replies_are_proven_only_with_full_output(self):
        provider=self.provider([page([comment(10)],1)])
        rows,stats=provider.comments(self.item,include_replies=True)
        self.assertEqual(0,stats['expected_replies']);self.assertTrue(stats['replies_complete'])
        self.assertEqual(0,rows[0]['reply_count'])
        _,root_only=provider.comments(self.item,include_replies=False)
        self.assertFalse(root_only['replies_complete'])

    def test_empty_visible_catalog_has_complete_zero_reply_counts(self):
        provider=self.provider([{'page':{'total':0}}])
        rows,stats=provider.comments(self.item)
        self.assertEqual([],rows)
        self.assertTrue(stats['comments_complete']);self.assertTrue(stats['replies_complete'])
        self.assertEqual(0,stats['expected_replies'])

    def test_page_cap_never_caches_partial_completion(self):
        provider=self.provider([page([comment(10,reply_count=1)],2)])
        with self.assertRaisesRegex(ProviderError,'incomplete_pagination'):
            provider.comments(self.item,max_pages=1)
        self.assertEqual({},provider._comment_cache)

    def test_total_change_or_overcount_cannot_claim_complete(self):
        cases=[[page([comment(10)],2),page([comment(11)],1)],
               [page([comment(10)],2),page([comment(11)],3)],
               [page([comment(10)],0)],
               [page([comment(10),comment(11)],1)]]
        for pages in cases:
            provider=self.provider(pages)
            with self.subTest(pages=pages),self.assertRaisesRegex(ProviderError,'incomplete_pagination'):
                provider.comments(self.item,max_pages=2)
            self.assertEqual({},provider._comment_cache)

    def test_repeated_or_empty_incomplete_page_never_claims_full(self):
        for second in (page([comment(10)],2),page([],2)):
            provider=self.provider([page([comment(10)],2),second])
            with self.assertRaisesRegex(ProviderError,'incomplete_pagination'):
                provider.comments(self.item,max_pages=2)
            self.assertEqual({},provider._comment_cache)

    def test_creator_display_cap_is_not_full_coverage(self):
        provider=self.provider([page([],50000)])
        with self.assertRaisesRegex(ProviderError,'coverage_limited'):provider.comments(self.item)
        self.assertEqual({},provider._comment_cache)

    def test_observed_business_code_352_stops_comment_read_without_cache(self):
        provider=self.provider([])
        provider.browser.get_json=lambda url, params=None, **kw: (
            {'code':0,'data':PROFILE} if url.endswith('/x/web-interface/nav')
            else {'code':-352,'message':'-352'})
        with self.assertRaises(ProviderError) as caught:
            provider.comments(self.item)
        self.assertEqual('rate_limited',caught.exception.reason)
        self.assertEqual({},provider._comment_cache)

    def test_nonzero_business_code_still_fails_without_caching(self):
        provider=self.provider([])
        with patch.object(provider.browser, 'get_json', side_effect=[
                {'code':0,'data':PROFILE}, {'code':12345,'data':{}}]):
            with self.assertRaisesRegex(ProviderError,'platform_error'):
                provider.comments(self.item)
        self.assertEqual({},provider._comment_cache)


class BilibiliNativePacingTests(unittest.TestCase):
    def test_comment_floor_inherits_last_account_request_and_respects_higher_setting(self):
        for configured, minimum, expected_wait in ((0.6, 2.0, 1.5),
                                                   (2.75, 2.0, 2.25),
                                                   (0.6, 0.2, 0.1)):
            with self.subTest(configured=configured, minimum=minimum):
                source=BrowserSource(ACCOUNT, {'request_interval':configured})
                source.last_request=100.0
                dispose=Mock()
                response=SimpleNamespace(status=200, body=lambda:b'{}', dispose=dispose)
                request=Mock(return_value=response)
                source.context=SimpleNamespace(request=SimpleNamespace(get=request))
                source.budget=SimpleNamespace(consume=lambda *args, **kwargs:None)
                with patch('providers.browser.time.monotonic', return_value=100.5), \
                     patch('providers.browser.time.sleep') as sleep:
                    self.assertEqual({},source.get_json(
                        'https://api.bilibili.com/x/v2/reply/up/fulllist',
                        min_interval=minimum))
                sleep.assert_called_once()
                self.assertAlmostEqual(expected_wait,sleep.call_args.args[0])
                self.assertEqual(100.5,source.last_request)
                self.assertEqual(1,source.call_count)
                dispose.assert_called_once()


if __name__=='__main__':unittest.main()
