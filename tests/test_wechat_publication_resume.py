"""Authorization retries reuse validated pages and navigate only to missing offsets."""
import copy
from contextlib import contextmanager
from pathlib import Path
import sys,tempfile,unittest
from types import SimpleNamespace
from urllib.parse import parse_qs,urlsplit
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from providers.wechat_browser import WeChatBrowserProvider,HOST,PUBLISH
from test_wechat_browser_provider import ACCOUNT,PROFILE,group,page as payload

class Page:
    def __init__(self,source):self.source=source;self.url=HOST+'/';self.begin=0
    def goto(self,url,**kwargs):
        self.url=url
        if urlsplit(url).path==PUBLISH:
            self.begin=int(parse_qs(urlsplit(url).query).get('begin',['0'])[0]);self.source.visits.append(('goto',self.begin))
    def locator(self,*a):return SimpleNamespace(filter=lambda **k:SimpleNamespace(first=SimpleNamespace(get_attribute=lambda _:PUBLISH+'?begin=0&count=10')))
    def wait_for_function(self,*a,**kw):
        if 'arg' in kw:assert kw['arg']==self.begin
    def evaluate(self,*a):return copy.deepcopy(self.source.pages[self.begin])
    def get_by_role(self,*a,**kw):
        def click(**kwargs):self.begin+=10;self.source.visits.append(('next',self.begin))
        return SimpleNamespace(click=click)
    def close(self):pass

class Source:
    def __init__(self):
        self.pages={n:payload([group(i+1) for i in range(n,min(n+10,25))],begin=n,total=25) for n in (0,10,20)}
        self.visits=[];self.budget=Mock();self.call_count=0
        self.context=SimpleNamespace(new_page=lambda:Page(self))
    @contextmanager
    def session(self):yield self

class WeChatPublicationResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.runtime=Path(self.tmp.name)/'authorizations'/('a'*32)
        for p in [patch('providers.wechat_browser.RUNTIME',self.runtime),patch.dict('os.environ',{'PROMOTION_AUTHORIZATION_OPERATION':'1'})]:
            p.start();self.addCleanup(p.stop)
        self.source=Source();self.provider=WeChatBrowserProvider(ACCOUNT,{},self.source);self.provider.verified_profile=PROFILE
    def prime(self):
        it=self.provider._publication_pages(5);rows=[next(it),next(it)];it.close();return rows
    def test_resume_keeps_page_timestamp_and_skips_prior_navigation(self):
        first=self.prime();self.source.visits.clear()
        result=list(self.provider._publication_pages(5))
        self.assertEqual([0,10,20],[p['begin'] for p in result])
        self.assertEqual([('goto',0),('goto',20)],self.source.visits)
        self.assertEqual(first[1]['_fetched_at'],result[1]['_fetched_at'])
        self.assertEqual(1,self.provider._cached_pages_reused)
    def test_changed_catalog_reloads_instead_of_reusing_stale_tail(self):
        self.prime();self.source.visits.clear();self.source.pages[0]['publish_list'][0]['msgid']=999
        result=list(self.provider._publication_pages(5))
        self.assertEqual([('goto',0),('next',10),('next',20)],self.source.visits)
        self.assertEqual(0,self.provider._cached_pages_reused)
    def test_normal_collection_does_not_use_authorization_cache(self):
        self.prime();self.source.visits.clear()
        with patch.dict('os.environ',{'PROMOTION_AUTHORIZATION_OPERATION':'0'}):list(self.provider._publication_pages(5))
        self.assertEqual([('goto',0),('next',10),('next',20)],self.source.visits)
    def test_page_cap_still_counts_reused_pages(self):
        self.prime();self.source.visits.clear();rows=list(self.provider._publication_pages(1))
        self.assertEqual(1,len(rows));self.assertEqual([('goto',0)],self.source.visits)

if __name__=='__main__':unittest.main()
