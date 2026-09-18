import copy
from contextlib import contextmanager
import sys
from pathlib import Path
import unittest
from urllib.parse import urlencode,urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from providers.base import ProviderError
from providers.zhihu import ZhihuProvider,article_record,ARTICLES,PROFILE,HOST

ACCOUNT={'platform':'zhihu','account_name':'测试作者','platform_uid':'test-author'}
UID='77573d4816253dd34e34869803dd4e29'
CID='2064509416053944616'


def profile(total=1):
    return {'url_token':'test-author','id':UID,'name':'测试作者','articles_count':total,
            'is_active':1775115568,'email':'never-copy@example.invalid'}


def article(cid=CID):
    return {'type':'article','data':{'id':cid,'url_token':cid,'title':'测试文章',
            'created_time':1784997412,'updated_time':1784997412,'excerpt':'文章摘要','thumbnail':''},
            'reaction':{'read_count':120,'vote_up_count':7,'comment_count':2,
                        'collect_count':4,'like_count':3}}


def page(rows=None,total=1,end=True,offset=0):
    rows=[article()] if rows is None else rows
    return {'data':rows,'paging':{'totals':total,'totals_real':total,'is_end':end,
            'next':HOST+ARTICLES+'?'+urlencode({'start':0,'end':0,'limit':10,
                'offset':offset+len(rows),'need_co_creation':1,'sort_type':'created'})}}


class Source:
    def __init__(self,pages=None,profiles=None):
        self.pages=copy.deepcopy(pages if pages is not None else [page()])
        self.profiles=copy.deepcopy(profiles if profiles is not None else [profile(),profile()])
        self.call_count=0
        self.urls=[]
    @contextmanager
    def session(self):
        yield self
    def get_json(self,url,params=None):
        self.call_count+=1
        self.urls.append((url,params))
        if urlsplit(url).path==PROFILE:
            return self.profiles.pop(0)
        assert urlsplit(url).path==ARTICLES
        return self.pages.pop(0)


class ZhihuTests(unittest.TestCase):
    def test_exact_identity_and_distinct_metrics_no_personal_fields_or_invented_author(self):
        source=Source()
        result=ZhihuProvider(ACCOUNT,{'expected_uid':UID},source).collect()
        self.assertTrue(result.complete)
        self.assertEqual('test-author',result.profile['verified_account_id'])
        self.assertIsNone(result.profile['followers'])
        self.assertNotIn('email',result.profile)
        row=result.records[0]
        self.assertEqual(CID,row['article_id'])
        self.assertEqual(7,row['stats']['like'])
        self.assertEqual(3,row['extra_metrics']['like_count'])
        self.assertEqual(120,row['stats']['read'])
        self.assertIsNone(row['stats']['share'])
        self.assertEqual('test-author',row['verified_owner_account_id'])
        self.assertNotIn('source_author_id',row)
        self.assertEqual(3,result.request_count)

    def test_multiple_pages_and_end_count_verified(self):
        source=Source([page(total=2,end=False),page([article('2064509416053944617')],total=2)],
                      [profile(2),profile(2)])
        result=ZhihuProvider(ACCOUNT,{},source).collect()
        self.assertTrue(result.complete)
        self.assertEqual(2,len(result.records))
        self.assertEqual([0,1],[p['offset'] for u,p in source.urls if u.endswith(ARTICLES)])

    def test_partial_discovery_does_not_claim_full_directory(self):
        for args in ({'max_pages':1},{'discovery':True}):
            source=Source([page(total=2,end=False)],[profile(2),profile(2)])
            result=ZhihuProvider(ACCOUNT,{},source).collect(**args)
            self.assertFalse(result.complete)
            self.assertEqual(1,len(result.records))

    def test_empty_directory_requires_zero_total_and_end(self):
        source=Source([page([],0)],[profile(0),profile(0)])
        self.assertEqual([],ZhihuProvider(ACCOUNT,{},source).collect().records)
        with self.assertRaises(ProviderError):
            ZhihuProvider(ACCOUNT,{},Source([page([],1)])).collect()

    def test_identity_mismatch_before_and_after_collect(self):
        for profiles in ([{**profile(),'url_token':'other'}],
                         [{**profile(),'id':'other'}],
                         [profile(),{**profile(),'url_token':'other'}],
                         [{**profile(),'id':''}]):
            with self.subTest(profiles=profiles),self.assertRaises(ProviderError):
                ZhihuProvider(ACCOUNT,{'expected_uid':UID},Source(profiles=profiles)).collect()

    def test_nonarticle_and_explicit_other_author_fail(self):
        for raw in ({**article(),'type':'answer'},
                    {**article(),'data':{**article()['data'],'author':{'url_token':'other'}}}):
            with self.assertRaises(ProviderError):
                ZhihuProvider(ACCOUNT,{},Source([page([raw])])).collect()

    def test_precise_large_ids_reject_floats_and_mismatched_tokens(self):
        for value in (float(CID),True,'',None):
            raw=article();raw['data']['id']=value
            with self.assertRaises(ProviderError):
                ZhihuProvider(ACCOUNT,{},Source([page([raw])])).collect()
        raw=article();raw['data']['url_token']='123'
        with self.assertRaises(ProviderError):
            ZhihuProvider(ACCOUNT,{},Source([page([raw])])).collect()

    def test_missing_metrics_remain_unknown(self):
        raw=article();raw['reaction']={'vote_up_count':None,'like_count':9}
        row=ZhihuProvider(ACCOUNT,{},Source([page([raw])])).collect().records[0]
        self.assertTrue(all(v is None for v in row['stats'].values()))
        self.assertEqual(9,row['extra_metrics']['like_count'])

    def test_repeat_premature_end_and_changing_totals_fail(self):
        cases=[([page(total=2,end=False),page(total=2)],2),
               ([page(total=2)],2),
               ([page(total=2,end=False),page([article('2')],3)],2),
               ([page(total=1,end=False)],1)]
        for pages,total in cases:
            with self.subTest(pages=pages),self.assertRaises(ProviderError):
                ZhihuProvider(ACCOUNT,{},Source(pages,[profile(total),profile(total)])).collect()

    def test_pagination_scope_or_host_change_fails(self):
        urls=['https://evil.invalid'+ARTICLES+'?offset=1',
              HOST+'/api/v4/creators/creations/v2/all?offset=1',
              HOST+ARTICLES+'?'+urlencode({'start':0,'end':0,'limit':10,'offset':0,
                      'need_co_creation':1,'sort_type':'created'})]
        for url in urls:
            payload=page(total=2,end=False);payload['paging']['next']=url
            with self.assertRaises(ProviderError):
                ZhihuProvider(ACCOUNT,{},Source([payload],[profile(2)])).collect()

    def test_blocked_response_never_becomes_empty_success(self):
        with self.assertRaises(ProviderError):
            ZhihuProvider(ACCOUNT,{},Source([{'error':{'code':403,'message':'安全验证'}}])).collect()

    def test_comment_details_are_explicitly_out_of_scope(self):
        provider=ZhihuProvider(ACCOUNT,{},Source())
        for call in (lambda:provider.comments({}),lambda:provider.replies({},'123')):
            with self.assertRaises(ProviderError) as exc:
                call()
            self.assertEqual('unsupported',exc.exception.reason)


if __name__=='__main__':
    unittest.main()
