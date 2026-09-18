"""Route-only browser regression for the Channels video-management count."""
from pathlib import Path
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from providers.base import ProviderError
from providers.wechat_channels import WeChatChannelsProvider,HOME,POSTS,POST_PATH

ACCOUNT={'platform':'wechat_channels','account_name':'fixture','platform_uid':'sphFixture'}
SETTINGS={'expected_sph':'sphFixture','expected_finder_id':'v2_fixture@finder'}


class BoundedLocator:
    """Use real browser locators, shortening only negative test deadlines."""
    def __init__(self,locator):self.locator=locator
    @property
    def first(self):return BoundedLocator(self.locator.first)
    def wait_for(self,**kwargs):
        kwargs['timeout']=min(kwargs.get('timeout',800),800)
        return self.locator.wait_for(**kwargs)
    def click(self,**kwargs):
        kwargs['timeout']=min(kwargs.get('timeout',800),800)
        return self.locator.click(**kwargs)
    def __getattr__(self,name):return getattr(self.locator,name)


class BoundedPage:
    def __init__(self,page):self.page=page
    def get_by_text(self,*args,**kwargs):return BoundedLocator(self.page.get_by_text(*args,**kwargs))
    def get_by_role(self,*args,**kwargs):return BoundedLocator(self.page.get_by_role(*args,**kwargs))
    def expect_response(self,predicate,**kwargs):
        kwargs['timeout']=min(kwargs.get('timeout',800),800)
        return self.page.expect_response(predicate,**kwargs)
    def __getattr__(self,name):return getattr(self.page,name)


class ChannelsCatalogNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.driver=sync_playwright().start()
        cls.browser=cls.driver.chromium.launch(channel='chrome',headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.driver.stop()

    def setUp(self):
        self.context=self.browser.new_context(service_workers='block')
        self.addCleanup(self.context.close)
        self.page=self.context.new_page()
        self.requests=[]
        self.documents=[]
        self.unexpected=[]
        self.source=SimpleNamespace(budget=Mock())
        self.provider=WeChatChannelsProvider(ACCOUNT,SETTINGS,self.source)

    def setup_route(self,*,total=23,rendered=23,emit_response=True,userpage_type=11,label='视频',error_code=0,render_delay=250):
        config=json.dumps({'postPath':POST_PATH,'posts':POSTS,'total':total,'rendered':rendered,
                           'emitResponse':emit_response,'userpageType':userpage_type,
                           'label':label,'delay':render_delay},ensure_ascii=False)
        html='''<!doctype html><html><meta charset="utf-8"><body>
<a id="manage" href="javascript:;">内容管理</a><div>内容管理</div>
<a id="videos" href="javascript:;" hidden>视频</a>
<wujie-app id="content"></wujie-app>
<script>
const fixture=CONFIG;
const shadow=document.getElementById('content').attachShadow({mode:'open'});
window.headerHistory=[];
function renderCount(value) {
 const label=fixture.label+'('+value+')';
 // Match the actual Wujie label text, including its surrounding whitespace.
 shadow.innerHTML='<div role="tab" style="white-space: pre">\\n    '+fixture.label+' ('+value+')\\n  </div>';
 window.headerHistory.push(label);
}
document.getElementById('manage').onclick=()=>{document.getElementById('videos').hidden=false;};
document.getElementById('videos').onclick=()=>{
 history.pushState({},'',fixture.posts);
 renderCount(0);
 if(!fixture.emitResponse) {renderCount(fixture.rendered);return;}
 fetch(fixture.postPath,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({userpageType:fixture.userpageType,pageSize:20,currentPage:1,stickyOrder:true})})
 .then(r=>r.json()).then(()=>setTimeout(()=>renderCount(fixture.rendered),fixture.delay));
};
</script></body></html>'''.replace('CONFIG',config)
        def route(request):
            parsed=urlsplit(request.request.url)
            if request.request.is_navigation_request():
                self.documents.append(request.request.url)
                if request.request.url==POSTS:
                    # The actual regression: a reloaded deep link returns Home.
                    request.fulfill(status=302,headers={'Location':HOME})
                elif request.request.url==HOME:
                    request.fulfill(status=200,content_type='text/html',body=html)
                else:
                    self.unexpected.append(request.request.url);request.abort()
            elif parsed.path==POST_PATH:
                self.requests.append(request.request.post_data_json)
                request.fulfill(status=201,content_type='application/json',body=json.dumps({
                    'errCode':error_code,'data':{'totalCount':total,'list':[],'continueFlag':False}}))
            else:
                self.unexpected.append(request.request.url);request.abort()
        self.context.route('**/*',route)
        self.page.goto(HOME,wait_until='domcontentloaded')

    def read_total(self):return self.provider._video_catalog_total(BoundedPage(self.page))

    def test_zero_placeholder_waits_for_normal_response_and_delayed_shadow_dom_count(self):
        self.setup_route()
        self.assertEqual(23,self.read_total())
        self.assertEqual(['视频(0)','视频(23)'],self.page.evaluate('window.headerHistory'))
        self.assertEqual([HOME],self.documents)
        self.assertEqual([{'userpageType':11,'pageSize':20,'currentPage':1,'stickyOrder':True}],self.requests)
        self.assertEqual([],self.unexpected)

    def test_header_alone_without_normal_list_response_is_not_coverage_evidence(self):
        self.setup_route(emit_response=False)
        with self.assertRaises(ProviderError):self.read_total()
        self.assertEqual('视频(23)',self.page.evaluate('window.headerHistory.at(-1)'))
        self.assertEqual([],self.requests)
        self.assertEqual([HOME],self.documents)

    def test_wrong_request_scope_cannot_be_accepted_even_with_matching_header(self):
        self.setup_route(userpage_type=99,render_delay=0)
        with self.assertRaisesRegex(ProviderError,'schema_changed'):self.read_total()
        self.assertEqual(99,self.requests[0]['userpageType'])

    def test_header_total_mismatch_fails_after_deadline(self):
        self.setup_route(total=23,rendered=7,render_delay=0)
        # Patch only this module's clock binding; do not affect Playwright's clock.
        clock=SimpleNamespace(monotonic=Mock(side_effect=[0,21]))
        with patch('providers.wechat_channels.time',clock),self.assertRaisesRegex(ProviderError,'incomplete_pagination'):
            self.read_total()
        self.assertEqual(1,len(self.requests))

    def test_image_header_cannot_prove_video_scope(self):
        self.setup_route(label='图文',render_delay=0)
        with self.assertRaisesRegex(ProviderError,'schema_changed'):self.read_total()

    def test_unsuccessful_normal_response_never_certifies_header(self):
        self.setup_route(error_code=401,render_delay=0)
        with self.assertRaisesRegex(ProviderError,'schema_changed'):self.read_total()

    def test_true_empty_video_catalog_requires_a_normal_success_response(self):
        self.setup_route(total=0,rendered=0,render_delay=0)
        self.assertEqual(0,self.read_total())
        self.assertEqual(1,len(self.requests))
        self.assertEqual([HOME],self.documents)


if __name__=='__main__':unittest.main()
