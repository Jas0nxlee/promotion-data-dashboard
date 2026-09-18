"""Publication groups, article identity and people/action units stay distinct."""
import copy
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from providers.base import ProviderError
from providers.wechat_browser import WeChatBrowserProvider,normalize_catalog,article_record

PROFILE={'verified_account_id':'gh_test','official_user_id':'gh_test','public_biz':'public-biz','nickname':'测试','followers':5}
ACCOUNT={'platform':'wechat_service','account_name':'测试','platform_uid':'gh_test'}


def item(mid=123,idx=1,kind=0,deleted=False):
    return {'appmsgid':mid,'itemidx':idx,'item_show_type':kind,'share_type':kind,'is_deleted':deleted,
            'content_url':f'https://mp.weixin.qq.com/s?__biz=public-biz&mid={mid}&idx={idx}&token=never-save',
            'title':'测试文章','cover':'','digest':'','read_num':100,'old_like_num':5,
            'like_num':3,'share_num':7,'comment_num':1,'total_comment_count_contains_reply':2}


def group(gid=1,items=None):
    return {'type':9,'msgid':gid,'sent_info':{'time':1779437053},'sent_result':{'msg_status':2},
            'appmsg_info':items if items is not None else [item(100+gid)]}


def page(groups=None,begin=0,total=None):
    groups=[group()] if groups is None else groups
    return {'publish_list':groups,'begin':begin,'count':10,'total_count':len(groups) if total is None else total}


class Source:
    call_count=0
    def __init__(self):
        self.context=Mock()
        self.budget=Mock()
    @contextmanager
    def session(self):yield self


class WeChatBrowserTests(unittest.TestCase):
    def test_current_counts_are_not_mislabeled_as_action_counts(self):
        p,rows,complete=normalize_catalog([page()],PROFILE)
        self.assertTrue(complete)
        self.assertEqual(2,rows[0]['stats']['comment'])
        self.assertTrue(all(rows[0]['stats'][k] is None for k in ('read','like','share','collect')))
        self.assertEqual({'read_users':100,'like_users':5,'share_users':7,'recommend_users':3,'root_comments':1},rows[0]['extra_metrics'])
        for k in ('read','like','share'):
            self.assertEqual('incompatible_unit',rows[0]['metric_provenance'][k]['missing_reason'])
        self.assertNotIn('token',rows[0]['url'])

    def test_explicit_itemidx_not_position_identifies_multicontent(self):
        p,rows,complete=normalize_catalog([page([group(items=[item(123,2),item(123,7)])])],PROFILE)
        self.assertEqual(['123-2','123-7'],[r['article_id'] for r in rows])
        self.assertEqual(1,p['publication_group_total'])
        self.assertEqual(2,p['total'])

    def test_images_use_strict_shared_metadata_and_distinct_tag(self):
        _,rows,_=normalize_catalog([page([group(items=[item(kind=8)])])],PROFILE)
        self.assertEqual(['图片消息'],rows[0]['tags'])
        raw=item(kind=8);raw.pop('title')
        with self.assertRaises(ProviderError):normalize_catalog([page([group(items=[raw])])],PROFILE)

    def test_video_and_deleted_items_are_explicit_exclusions(self):
        groups=[group(1,[item(123,1),item(123,2,kind=5),item(123,3,deleted=True)])]
        p,rows,complete=normalize_catalog([page(groups)],PROFILE)
        self.assertTrue(complete)
        self.assertEqual(1,len(rows))
        self.assertEqual(3,p['publication_items_covered'])
        self.assertEqual({'deleted','standalone_channels_video'},{x['reason'] for x in p['excluded_contents']})

    def test_full_pagination_partial_cap_and_empty_catalog(self):
        first=page([group(i) for i in range(1,11)],total=11)
        p,rows,complete=normalize_catalog([first,page([group(11)],begin=10,total=11)],PROFILE)
        self.assertTrue(complete);self.assertEqual(11,len(rows));self.assertEqual(11,p['publication_groups_covered'])
        p,rows,complete=normalize_catalog([first],PROFILE)
        self.assertFalse(complete);self.assertIsNone(p['total'])
        p,rows,complete=normalize_catalog([page([])],PROFILE)
        self.assertTrue(complete);self.assertEqual([],rows)

    def test_failed_unpublished_group_is_audited_without_poisoning_later_publication(self):
        failed=group(1);failed['sent_info']['is_published']=0
        failed['sent_result']={'msg_status':6,'msg_fail_reason':'此内容因违规发表失败，请修改后重新发表'}
        successful=group(2,items=failed['appmsg_info'])
        p,rows,complete=normalize_catalog([page([failed,successful])],PROFILE)
        self.assertTrue(complete);self.assertEqual(1,len(rows))
        self.assertEqual(2,p['publication_groups_covered'])
        self.assertEqual([{'official_message_id':'1','reason':'publication_failed','msg_status':6}],p['excluded_publication_groups'])
        self.assertEqual([],p['excluded_contents'])

    def test_ambiguous_or_contradictory_failure_status_is_not_skipped(self):
        for published,reason in [(None,'发表失败'),(1,'发表失败'),(0,''),(0,'未知状态')]:
            failed=group();failed['sent_info']['is_published']=published
            failed['sent_result']={'msg_status':6,'msg_fail_reason':reason}
            with self.subTest(published=published,reason=reason),self.assertRaises(ProviderError):
                normalize_catalog([page([failed])],PROFILE)

    def test_legacy_send_failure_requires_unpublished_flag_and_explicit_failure_code(self):
        failed=group();failed['sent_info']['is_published']=0
        failed['sent_result']={'msg_status':5,'refuse_reason':'SENDFAIL_GETTOUINLIST_FAIL'}
        profile,rows,complete=normalize_catalog([page([failed])],PROFILE)
        self.assertTrue(complete);self.assertEqual([],rows)
        self.assertEqual(5,profile['excluded_publication_groups'][0]['msg_status'])
        for field,value in [('msg_status',3),('refuse_reason','UNKNOWN'),('refuse_reason','SENDFAIL_UNKNOWN')]:
            wrong=copy.deepcopy(failed);wrong['sent_result'][field]=value
            with self.assertRaises(ProviderError):normalize_catalog([page([wrong])],PROFILE)

    def test_standalone_publication_uses_verified_publish_info_not_missing_send_time(self):
        standalone=group(2652959773,items=[item(2652959773)])
        standalone.update(type=10002,sent_info=None,sent_result={'reject_index_list':[]},
                          publish_info={'msgid':2652959773,'publish_status':200,'create_time':1655973026})
        p,rows,complete=normalize_catalog([page([standalone])],PROFILE)
        self.assertTrue(complete);self.assertEqual('2652959773-1',rows[0]['article_id'])
        self.assertEqual('publish_info.create_time',rows[0]['published_at_source'])
        self.assertTrue(rows[0]['published_at'].startswith('2022-06-23'))
        for key,val in [('msgid',999),('publish_status',100),('create_time',0)]:
            bad=copy.deepcopy(standalone);bad['publish_info'][key]=val
            with self.assertRaises(ProviderError):normalize_catalog([page([bad])],PROFILE)

    def test_explicit_pending_review_is_not_published_or_failed(self):
        pending=group();pending['sent_info']['is_published']=0
        pending.update(sent_result={'msg_status':1},view={'status':'审核中'})
        p,rows,complete=normalize_catalog([page([pending])],PROFILE)
        self.assertTrue(complete);self.assertEqual([],rows)
        self.assertEqual('publication_pending',p['excluded_publication_groups'][0]['reason'])
        for published,label in [(1,'审核中'),(0,'未知状态')]:
            bad=copy.deepcopy(pending);bad['sent_info']['is_published']=published;bad['view']['status']=label
            with self.assertRaises(ProviderError):normalize_catalog([page([bad])],PROFILE)

    def test_unavailable_group_requires_every_article_explicitly_deleted(self):
        unavailable=group(items=[item(deleted=True),item(idx=2,deleted=True)])
        unavailable.update(sent_result={'msg_status':8},view={'status':'无法查看'})
        p,rows,complete=normalize_catalog([page([unavailable])],PROFILE)
        self.assertTrue(complete);self.assertEqual([],rows)
        self.assertEqual(['deleted','deleted'],[r['reason'] for r in p['excluded_contents']])
        for modified in ['visible','unknown-label','empty']:
            bad=copy.deepcopy(unavailable)
            if modified=='visible':bad['appmsg_info'][0]['is_deleted']=False
            if modified=='unknown-label':bad['view']['status']='未知'
            if modified=='empty':bad['appmsg_info']=[]
            with self.assertRaises(ProviderError):normalize_catalog([page([bad])],PROFILE)

    def test_deleted_legacy_video_message_is_not_an_article(self):
        video=group();video.update(type=16,appmsg_info=[],video_info={},sent_result={'msg_status':7},view={'status':'已删除'})
        p,rows,complete=normalize_catalog([page([video])],PROFILE)
        self.assertTrue(complete);self.assertEqual([],rows)
        self.assertEqual('deleted_video_message',p['excluded_publication_groups'][0]['reason'])
        for field,value in [('video_info',None),('appmsg_info',[item()]),('view',{'status':'未知'})]:
            bad=copy.deepcopy(video);bad[field]=value
            with self.assertRaises(ProviderError):normalize_catalog([page([bad])],PROFILE)

    def test_url_and_metadata_disagreement_fail(self):
        for field,val in (('content_url','https://evil.invalid/s'),('content_url','https://mp.weixin.qq.com/s?__biz=other&mid=123&idx=1'),('content_url','https://mp.weixin.qq.com/s?mid=999'),('itemidx',0),('appmsgid',True),('appmsgid',2**54)):
            raw=item();raw[field]=val
            with self.subTest(field=field,val=val),self.assertRaises(ProviderError):
                normalize_catalog([page([group(items=[raw])])],PROFILE)

    def test_short_public_article_link_uses_explicit_stable_metadata(self):
        raw=item();raw['content_url']='https://mp.weixin.qq.com/s/short-public-id'
        self.assertEqual('123-1',article_record(raw,group(),PROFILE)['article_id'])

    def test_unknown_content_types_missing_flags_and_unpublished_groups_fail(self):
        bad=[]
        raw=item(kind=12);bad.append(group(items=[raw]))
        raw=item();raw.pop('is_deleted');bad.append(group(items=[raw]))
        raw=item();raw['share_type']=5;bad.append(group(items=[raw]))
        g=group();g['type']=2;bad.append(g)
        g=group();g['sent_result']['msg_status']=1;bad.append(g)
        for g in bad:
            with self.assertRaises(ProviderError):normalize_catalog([page([g])],PROFILE)

    def test_duplicates_short_nonterminal_page_and_bad_offsets_fail(self):
        cases=[[page([group(1),group(1)])],
               [page([group(items=[item(),item()])])],
               [page([group()],total=2)],
               [page([group()],begin=1,total=2)],
               [page([group(i) for i in range(1,11)],total=11),page([group(11)],begin=10,total=12)]]
        for pages in cases:
            with self.assertRaises(ProviderError):normalize_catalog(pages,PROFILE)

    def test_disabled_comments_remain_unknown(self):
        raw=item();raw.pop('comment_num');raw.pop('total_comment_count_contains_reply')
        _,rows,_=normalize_catalog([page([group(items=[raw])])],PROFILE)
        self.assertIsNone(rows[0]['stats']['comment']);self.assertIsNone(rows[0]['extra_metrics']['root_comments'])

    def test_profile_requires_original_gh_not_nickname(self):
        source=Source();p=WeChatBrowserProvider(ACCOUNT,{},source)
        browser_page=source.context.new_page.return_value
        browser_page.url='https://mp.weixin.qq.com/cgi-bin/home'
        browser_page.locator.return_value.first.get_attribute.return_value='/cgi-bin/settingpage'
        browser_page.evaluate.side_effect=[{'biz':'public-biz','nickname':'测试','followers':'5'},
                                           {'original':'gh_other','nickname':'测试','alias':'test'}]
        with self.assertRaises(ProviderError) as exc:p._profile()
        self.assertEqual('identity_mismatch',exc.exception.reason)
        browser_page.close.assert_called_once()

    def test_collect_checks_identity_after_catalog_and_rejects_comment_details(self):
        p=WeChatBrowserProvider(ACCOUNT,{},Source())
        with patch.object(p,'_profile',side_effect=[PROFILE,{**PROFILE,'public_biz':'other'}]),patch.object(p,'_publication_pages',return_value=[page()]):
            with self.assertRaises(ProviderError):p.collect()
        for call in (lambda:p.comments({}),lambda:p.replies({},'1')):
            with self.assertRaises(ProviderError) as exc:call()
            self.assertEqual('unsupported',exc.exception.reason)


if __name__=='__main__':unittest.main()
