"""Public article collectors must prove identity and full pagination."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from fetch_article_data import CsdnCollector,ElecfansCollector

CSDN={'platform':'csdn','platform_uid':'ANSILIC','account_name':'国科安芯','business_line':'芯片'}
ELEC={'platform':'elecfans','platform_uid':'6594087','account_name':'安芯','business_line':'芯片',
      'profile_url':'https://bbs.elecfans.com/user/6594087/articles/'}


def profile(username='ANSILIC'):
    return 'window.__INITIAL_STATE__='+json.dumps({'pageData':{'data':{'baseInfo':{
        'userModule':{'username':username,'nickname':'国科安芯','blogUrl':'https://blog.csdn.net/'+username},
        'achievementModule':{'originalCount':10,'fansCount':5}}}}})


def blog(aid):
    return {'articleId':aid,'url':f'https://blog.csdn.net/ANSILIC/article/details/{aid}',
            'title':'文章','viewCount':2,'diggCount':0,'commentCount':0,'collectCount':1}


def csdn_page(rows=None,total=None,code=200):
    rows=[blog(1)] if rows is None else rows
    return {'code':code,'data':{'list':rows,'total':len(rows) if total is None else total}}


class CsdnHttp:
    def __init__(self,pages=None,html=None):
        self.pages=copy.deepcopy(pages or [csdn_page()]);self.html=profile() if html is None else html
    def get(self,*a,**kw):return SimpleNamespace(text=self.html)
    def get_json(self,*a,**kw):return self.pages.pop(0)


def elec_html(ids=(1,),page=1,pages=1,total=None,uid='6594087',foreign_page=False):
    total=len(ids) if total is None else total
    rows=''.join(f'<li><div class="art-list-top"><a href="https://www.elecfans.com/d/{i}.html"><h3>文章</h3></a><div class="time">2026-09-01</div></div><div class="art-detail"><div class="art-follow"><span>0</span></div><div class="art-cate"><span>阅读 10</span><span>评论 0</span></div></div></li>' for i in ids)
    page_uid='999' if foreign_page else uid
    paging=f'<div class="pg"><strong>{page}</strong><span title="共 {pages} 页"></span><a href="user/{page_uid}/articles/{pages}/">末页</a></div>'
    return f'<div class="user-top"><div class="user-name">安芯</div></div><ul class="column-nav"><li class="current"><a href="/user/{uid}/articles/">文章</a><span>{total}</span></li></ul><ul class="article-list">{rows}</ul>{paging}'


class ElecHttp:
    def __init__(self,pages):self.pages=list(pages)
    def get(self,*a,**kw):return SimpleNamespace(text=self.pages.pop(0))


class CsdnPublicTests(unittest.TestCase):
    def test_success_has_verified_identity_and_source(self):
        e,rows=CsdnCollector(CsdnHttp(),2).collect(CSDN)
        self.assertEqual('ok',e['status']);self.assertEqual('ANSILIC',e['verified_account_id'])
        self.assertEqual('csdn_public',e['data_source']);self.assertEqual('ANSILIC',rows[0]['source_author_id'])

    def test_error_json_or_missing_total_never_becomes_empty_success(self):
        for payload in ({'code':403,'data':{}},{'code':200,'data':{}},{'code':200,'data':{'list':[],'total':True}},csdn_page([],5)):
            with self.subTest(payload=payload),self.assertRaises(RuntimeError):
                CsdnCollector(CsdnHttp([payload]),2).collect(CSDN)

    def test_profile_and_article_owner_must_match(self):
        for client in (CsdnHttp(html=profile('other')),CsdnHttp(html='安全验证'),
                       CsdnHttp([csdn_page([{**blog(1),'url':'https://blog.csdn.net/other/article/details/1'}])])):
            with self.assertRaises(RuntimeError):CsdnCollector(client,2).collect(CSDN)

    def test_repeated_empty_or_changed_total_second_page_is_partial(self):
        first=[blog(i) for i in range(1,101)]
        for second in (csdn_page(first,200),csdn_page([],200),csdn_page([blog(101)],201),{'code':403}):
            e,rows=CsdnCollector(CsdnHttp([csdn_page(first,200),second]),2).collect(CSDN)
            self.assertEqual('partial',e['status']);self.assertEqual(100,len(rows));self.assertTrue(e['error'])

    def test_same_page_duplicate_and_missing_id_fail(self):
        for rows in ([blog(1),blog(1)],[{**blog(1),'articleId':True}]):
            with self.assertRaises(RuntimeError):CsdnCollector(CsdnHttp([csdn_page(rows)]),2).collect(CSDN)

    def test_complete_final_short_page_and_zero_are_supported(self):
        first=[blog(i) for i in range(1,101)]
        e,rows=CsdnCollector(CsdnHttp([csdn_page(first,101),csdn_page([blog(101)],101)]),2).collect(CSDN)
        self.assertEqual('ok',e['status']);self.assertEqual(101,len(rows))
        e,rows=CsdnCollector(CsdnHttp([csdn_page([],0)]),2).collect(CSDN)
        self.assertEqual('ok',e['status']);self.assertEqual([],rows)


class ElecfansPublicTests(unittest.TestCase):
    def test_success_has_uid_source_and_actual_zero_metrics(self):
        e,rows=ElecfansCollector(ElecHttp([elec_html()]),2).collect(ELEC)
        self.assertEqual('ok',e['status']);self.assertEqual('6594087',e['verified_account_id'])
        self.assertEqual('elecfans_public',e['data_source']);self.assertEqual(0,rows[0]['stats']['comment'])
        self.assertIsNone(rows[0]['stats']['collect'])

    def test_wrong_account_or_other_account_page_link_fails(self):
        for html in (elec_html(uid='999'),elec_html(foreign_page=True),'安全验证'):
            with self.assertRaises(RuntimeError):ElecfansCollector(ElecHttp([html]),2).collect(ELEC)

    def test_repeated_last_page_or_empty_last_page_is_partial(self):
        first=elec_html((1,),pages=2,total=2)
        for second in (elec_html((1,),page=2,pages=2,total=2),elec_html((),page=2,pages=2,total=2)):
            e,rows=ElecfansCollector(ElecHttp([first,second]),2).collect(ELEC)
            self.assertEqual('partial',e['status']);self.assertEqual(1,len(rows));self.assertTrue(e['error'])

    def test_page_index_total_or_identity_change_is_partial(self):
        first=elec_html((1,),pages=2,total=2)
        for second in (elec_html((2,),page=1,pages=2,total=2),elec_html((2,),page=2,pages=2,total=3),elec_html((2,),page=2,pages=2,total=2,uid='999')):
            e,rows=ElecfansCollector(ElecHttp([first,second]),2).collect(ELEC)
            self.assertEqual('partial',e['status']);self.assertEqual(1,len(rows))

    def test_page_cap_and_missing_records_do_not_claim_complete(self):
        e,rows=ElecfansCollector(ElecHttp([elec_html((1,),pages=2,total=2)]),1).collect(ELEC)
        self.assertEqual('partial',e['status'])
        e,rows=ElecfansCollector(ElecHttp([elec_html((1,),pages=1,total=2)]),2).collect(ELEC)
        self.assertEqual('partial',e['status'])

    def test_complete_pagination_and_empty_proven_account(self):
        e,rows=ElecfansCollector(ElecHttp([elec_html((1,),pages=2,total=2),elec_html((2,),page=2,pages=2,total=2)]),2).collect(ELEC)
        self.assertEqual('ok',e['status']);self.assertEqual(2,len(rows))
        e,rows=ElecfansCollector(ElecHttp([elec_html((),total=0)]),2).collect(ELEC)
        self.assertEqual('ok',e['status']);self.assertEqual([],rows)


if __name__=='__main__':unittest.main()
