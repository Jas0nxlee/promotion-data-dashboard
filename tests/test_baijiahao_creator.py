"""Published graphic article identity, pagination and metric contracts."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from providers.base import ProviderError
from providers.baijiahao_creator import (BaijiahaoCreatorProvider, BaijiahaoBrowserSource,
                                        article_record, decode_payload, HOST, ARTICLES)

APP='1789404703579192'
CID='1876413782762859931'
ACCOUNT={'platform':'baijiahao','platform_uid':APP,'account_name':'测试'}
PROFILE={'nickname':'测试','verified_account_id':APP,'official_user_id':APP,'followers':None}


def article(cid=CID):
    return {'app_id':APP,'article_id':cid,'id':cid,'nid':'9883469821250673726',
            'type':'news','status':'publish','is_published':1,'url':'http://baijiahao.baidu.com/s?id='+cid,
            'title':'测试','abstract':'摘要','publish_at':'2026-09-15 23:54:06',
            'cover_images':'[{"src":"https://example.invalid/cover.jpg"}]',
            'read_amount':120,'like_amount':3,'comment_amount':2,'share_amount':1,'collection_amount':4,'rec_amount':999}


def page(rows=None,total=None,current=1):
    rows=[article()] if rows is None else rows
    total=len(rows) if total is None else total
    return {'list':rows,'page':{'currentPage':current,'pageSize':10,'totalCount':total,'totalPage':(total+9)//10}}


class Source:
    def __init__(self,pages=None,profiles=None):
        self.pages=copy.deepcopy([page()] if pages is None else pages)
        self.profiles=copy.deepcopy([PROFILE,PROFILE] if profiles is None else profiles)
        self.call_count=0
    @contextmanager
    def session(self):yield self
    def profile(self):self.call_count+=1;return self.profiles.pop(0)
    def catalog_page(self,current):self.call_count+=1;return self.pages.pop(0)


class BaijiahaoCreatorTests(unittest.TestCase):
    def test_public_id_exact_and_recommendations_not_read_count(self):
        x=BaijiahaoCreatorProvider(ACCOUNT,{},Source()).collect()
        self.assertTrue(x.complete);self.assertEqual(CID,x.records[0]['article_id'])
        self.assertEqual('https://baijiahao.baidu.com/s?id='+CID,x.records[0]['url'])
        self.assertEqual(120,x.records[0]['stats']['read'])
        self.assertEqual(999,x.records[0]['extra_metrics']['recommendations'])
        self.assertEqual('published_news_only',x.profile['scope'])

    def test_large_json_integer_ids_are_not_floated(self):
        raw=article();raw.update(article_id=int(CID),id=int(CID))
        row=article_record(json.loads(json.dumps(raw)),APP)
        self.assertEqual(CID,row['article_id'])
        raw['article_id']=float(CID)
        with self.assertRaises(ProviderError):article_record(raw,APP)

    def test_other_account_url_or_id_fails(self):
        for field,value in [('app_id','123'),('id','123'),('url','http://baijiahao.baidu.com/s?id=123'),('url','https://evil.invalid/s?id='+CID)]:
            raw=article();raw[field]=value
            with self.subTest(field=field),self.assertRaises(ProviderError):article_record(raw,APP)

    def test_video_draft_unpublished_or_invalid_time_fails(self):
        for field,value in [('type','video'),('status','draft'),('is_published',0),('publish_at','0000-00-00 00:00:00')]:
            raw=article();raw[field]=value
            with self.subTest(field=field),self.assertRaises(ProviderError):article_record(raw,APP)

    def test_missing_metrics_remain_unknown(self):
        raw=article()
        for k in ('read_amount','like_amount','comment_amount','share_amount','collection_amount'):raw.pop(k)
        self.assertTrue(all(v is None for v in article_record(raw,APP)['stats'].values()))

    def test_full_and_partial_pagination(self):
        first=[article(str(int(CID)+i)) for i in range(10)]
        last=article(str(int(CID)+10))
        full=BaijiahaoCreatorProvider(ACCOUNT,{},Source([page(first,11),page([last],11,2)])).collect()
        self.assertTrue(full.complete);self.assertEqual(11,len(full.records))
        part=BaijiahaoCreatorProvider(ACCOUNT,{},Source([page(first,11)])).collect(max_pages=1)
        self.assertFalse(part.complete);self.assertEqual(10,len(part.records))

    def test_repeated_empty_or_changed_total_page_fails(self):
        first=[article(str(int(CID)+i)) for i in range(10)]
        for second in [page([],11,2),page([first[0]],11,2),page([article('123')],12,2),page([article('123')],11,1)]:
            with self.assertRaises(ProviderError):
                BaijiahaoCreatorProvider(ACCOUNT,{},Source([page(first,11),second])).collect()

    @patch('providers.baijiahao_creator.time.sleep')
    def test_transient_zero_total_retries_same_page_without_losing_rows(self, sleep):
        first=[article(str(int(CID)+i)) for i in range(10)]
        source=Source([page(first,11),page([],0,2),page([article(str(int(CID)+10))],11,2)],
                      profiles=[PROFILE,PROFILE,PROFILE])
        source.catalog_page=Mock(wraps=source.catalog_page)
        result=BaijiahaoCreatorProvider(ACCOUNT,{},source).collect()
        self.assertTrue(result.complete);self.assertEqual(11,len(result.records))
        self.assertEqual([1,2,2],[call.args[0] for call in source.catalog_page.call_args_list])
        self.assertEqual([{'page':2,'retries':1}],result.profile['retried_catalog_pages'])
        sleep.assert_called_once_with(2)

    @patch('providers.baijiahao_creator.time.sleep')
    def test_persistent_zero_total_stops_after_two_retries(self, sleep):
        first=[article(str(int(CID)+i)) for i in range(10)]
        source=Source([page(first,11),*[page([],0,2) for _ in range(3)]],
                      profiles=[PROFILE,PROFILE,PROFILE])
        source.catalog_page=Mock(wraps=source.catalog_page)
        with self.assertRaisesRegex(ProviderError,'连续 3 次'):
            BaijiahaoCreatorProvider(ACCOUNT,{},source).collect()
        self.assertEqual([1,2,2,2],[call.args[0] for call in source.catalog_page.call_args_list])
        self.assertEqual([2,5],[call.args[0] for call in sleep.call_args_list])

    @patch('providers.baijiahao_creator.time.sleep')
    def test_retry_rechecks_identity_and_stops_on_expiry(self, sleep):
        first=[article(str(int(CID)+i)) for i in range(10)]
        for error in [ProviderError('session_expired','login'),ProviderError('rate_limited','wait')]:
            source=Source([page(first,11),page([],0,2)])
            source.profile=Mock(side_effect=[PROFILE,error])
            source.catalog_page=Mock(wraps=source.catalog_page)
            with self.assertRaisesRegex(ProviderError,error.reason):
                BaijiahaoCreatorProvider(ACCOUNT,{},source).collect()
            self.assertEqual(2,source.catalog_page.call_count)
        source=Source([page(first,11),page([],0,2)],profiles=[PROFILE,{**PROFILE,'verified_account_id':'other'}])
        with self.assertRaisesRegex(ProviderError,'identity_mismatch'):
            BaijiahaoCreatorProvider(ACCOUNT,{},source).collect()

    def test_page_size_or_page_count_cannot_change_silently(self):
        for key,value in [('pageSize',20),('totalPage',2),('totalCount',True)]:
            payload=page();payload['page'][key]=value
            with self.assertRaises(ProviderError):BaijiahaoCreatorProvider(ACCOUNT,{},Source([payload])).collect()

    def test_identity_checked_before_and_after(self):
        for profiles in [[{**PROFILE,'verified_account_id':'other'}],[PROFILE,{**PROFILE,'verified_account_id':'other'}]]:
            with self.assertRaises(ProviderError):BaijiahaoCreatorProvider(ACCOUNT,{},Source(profiles=profiles)).collect()

    def test_bad_json_auth_failure_and_empty_catalog(self):
        for body in ('<html>登录</html>',json.dumps({'errno':10000010}),json.dumps({'errno':0,'data':[]})):
            with self.assertRaises(ProviderError):decode_payload(body)
        x=BaijiahaoCreatorProvider(ACCOUNT,{},Source([page([],0)])).collect()
        self.assertTrue(x.complete);self.assertEqual([],x.records)

    def test_comment_details_explicitly_unsupported(self):
        p=BaijiahaoCreatorProvider(ACCOUNT,{},Source())
        for call in (lambda:p.comments({}),lambda:p.replies({},'1')):
            with self.assertRaises(ProviderError) as exc:call()
            self.assertEqual('unsupported',exc.exception.reason)

    def test_catalog_uses_normal_page_navigation_then_next_button_not_static_headers(self):
        source=BaijiahaoBrowserSource(ACCOUNT,{'request_interval':.6})
        source.context=Mock();source.budget=Mock();ui=source.context.new_page.return_value
        source._catalog_ui=None;source._catalog_current=None;source.last_request=0
        callback={};seen=[]
        ui.on.side_effect=lambda event,fn:callback.update(fn=fn)
        ui.get_by_role.return_value.get_attribute.return_value='true'
        def deliver(current,category='news',status=200):
            response=Mock()
            response.url=HOST+ARTICLES+'?'+urlencode({'currentPage':current,'pageSize':10,'type':category,'collection':'publish'})
            response.status=status
            response.body.return_value=json.dumps({'errno':0,'data':page(current=current)}).encode()
            callback['fn'](response)
            seen.append(response)
        def navigate(url,**kwargs):
            self.assertIn('/builder/rc/content?',url)
            current=53 if 'currentPage=53&' in url else 52
            self.assertIn(f'currentPage={current}&',url)
            deliver(current,'video')  # Unrelated widgets cannot provide catalog evidence.
            deliver(current)
        ui.goto.side_effect=navigate
        ui.locator.return_value.click.side_effect=lambda **kwargs:deliver(53)
        self.assertEqual(52,source.catalog_page(52)['page']['currentPage'])
        self.assertEqual(53,source.catalog_page(53)['page']['currentPage'])
        self.assertEqual(53,source.catalog_page(53)['page']['currentPage'])
        self.assertEqual(2,ui.goto.call_count)  # Same-page retry reloads; it cannot click Next.
        ui.locator.assert_called_once_with('li.cheetah-pagination-next[aria-disabled="false"] button')
        source.context.request.get.assert_not_called()
        self.assertEqual(3,source.call_count)
        self.assertEqual(3,source.budget.consume.call_count)
        self.assertEqual(3,ui.remove_listener.call_count)
        self.assertFalse(hasattr(source,'_read_headers'))

    def test_normal_page_http_failure_stops_without_direct_request_fallback(self):
        source=BaijiahaoBrowserSource(ACCOUNT,{})
        source.context=Mock();source.budget=Mock();ui=source.context.new_page.return_value
        source._catalog_ui=None;source._catalog_current=None
        callback={};ui.on.side_effect=lambda event,fn:callback.update(fn=fn)
        response=Mock(status=403,url=HOST+ARTICLES+'?currentPage=1&pageSize=10&type=news&collection=publish')
        ui.goto.side_effect=lambda *a,**kw:callback['fn'](response)
        with self.assertRaises(ProviderError):source.catalog_page(1)
        source.context.request.get.assert_not_called()
        response.body.assert_not_called()


if __name__=='__main__':unittest.main()
