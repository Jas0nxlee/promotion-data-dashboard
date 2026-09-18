"""Private, operation-local resume cache invalidates only directory structure."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from providers.base import ProviderError
from providers.wechat_publication_cache import PublicationPageCache

IDENTITY={'verified_account_id':'gh_fixture','public_biz':'fixture-biz'}
T1='2026-09-17T08:00:00+08:00'
T2='2026-09-17T09:00:00+08:00'


def group(mid):
    return {'msgid':mid,'type':9,'sent_info':{'time':1700000000,'is_published':0},
            'sent_result':{'msg_status':2},'appmsg_info':[{'appmsgid':mid+100,'itemidx':1,'is_deleted':False,'read_num':3,'like_num':1}]}


def page(begin=0):
    return {'begin':begin,'count':2,'total_count':4,'publish_list':[group(begin+1),group(begin+2)]}


class PublicationPageCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='publication-cache-')
        self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)/'pages'

    def cache(self,head=None,identity=None):
        return PublicationPageCache(self.directory,identity or IDENTITY,head or page())

    def test_fresh_head_missing_page_and_private_permissions(self):
        with patch('providers.wechat_publication_cache.now',return_value=T1):cache=self.cache()
        self.assertEqual(T1,cache.get(0)['_fetched_at'])
        self.assertIsNone(cache.get(2))
        self.assertEqual(0o700,self.directory.stat().st_mode&0o777)
        for path in self.directory.iterdir():self.assertEqual(0o600,path.stat().st_mode&0o777)

    def test_put_is_fresh_and_get_retains_original_timestamp_without_mutating_input(self):
        cache=self.cache();raw=page(2);raw['_fetched_at']='untrusted-clock'
        with patch('providers.wechat_publication_cache.now',return_value=T1):saved=cache.put(raw)
        self.assertEqual('untrusted-clock',raw['_fetched_at'])
        self.assertEqual(T1,saved['_fetched_at'])
        with patch('providers.wechat_publication_cache.now',return_value=T2):retrieved=cache.get(2)
        self.assertEqual(T1,retrieved['_fetched_at'])
        retrieved['publish_list'].clear()
        self.assertEqual(2,len(cache.get(2)['publish_list']))
        envelope=json.loads((self.directory/'page-2.json').read_text())
        self.assertEqual({'identity','data'},set(envelope))
        self.assertEqual(IDENTITY,envelope['identity'])

    def test_metric_changes_preserve_later_pages_but_refresh_current_head(self):
        with patch('providers.wechat_publication_cache.now',return_value=T1):
            cache=self.cache();cache.put(page(2))
        changed=page();changed['publish_list'][0]['appmsg_info'][0].update(read_num=900,like_num=10,comment_num=2)
        changed['publish_list'][0]['sent_status']={'succ':300}
        with patch('providers.wechat_publication_cache.now',return_value=T2):new=self.cache(changed)
        self.assertEqual(900,new.get(0)['publish_list'][0]['appmsg_info'][0]['read_num'])
        self.assertEqual(T2,new.get(0)['_fetched_at'])
        self.assertEqual(T1,new.get(2)['_fetched_at'])

    def test_directory_total_order_status_time_ids_and_deletion_invalidate_cache(self):
        changes=[]
        changed=page();changed['total_count']=6;changes.append(changed)
        changed=page();changed['publish_list'].reverse();changes.append(changed)
        changed=page();changed['publish_list'][0]['sent_result']['msg_status']=7;changes.append(changed)
        changed=page();changed['publish_list'][0]['sent_info']['time']+=1;changes.append(changed)
        changed=page();changed['publish_list'][0]['appmsg_info'][0]['appmsgid']+=1;changes.append(changed)
        changed=page();changed['publish_list'][0]['appmsg_info'][0]['is_deleted']=True;changes.append(changed)
        for changed in changes:
            with self.subTest(changed=changed):
                cache=self.cache();cache.put(page(2))
                new=self.cache(changed)
                self.assertIsNone(new.get(2))
                with self.assertRaises(ProviderError):cache.put(page(2))

    def test_type10002_publish_status_create_time_and_msgid_are_structural(self):
        head=page();head['publish_list'][0]={'type':10002,'publish_info':{
            'publish_status':200,'create_time':1700000000,'msgid':'legacy-1',
            'appmsgex':[{'appmsgid':'123','itemidx':1,'is_deleted':False,'read_num':3}]}}
        for field,value in [('publish_status',201),('create_time',1700000001),('msgid','legacy-2')]:
            cache=self.cache(head);cache.put(page(2))
            changed=copy.deepcopy(head);changed['publish_list'][0]['publish_info'][field]=value
            self.assertIsNone(self.cache(changed).get(2))
        cache=self.cache(head);cache.put(page(2));changed=copy.deepcopy(head)
        changed['publish_list'][0]['publish_info']['appmsgex'][0]['read_num']=99
        self.assertIsNotNone(self.cache(changed).get(2))
        changed['publish_list'][0]['publish_info']['appmsgex'][0]['is_deleted']=True
        self.assertIsNone(self.cache(changed).get(2))

    def test_new_identity_clears_pages_and_old_instance_cannot_read_or_write(self):
        old=self.cache();old.put(page(2))
        new=self.cache(identity={**IDENTITY,'verified_account_id':'gh_other'})
        self.assertIsNone(new.get(2));new.put(page(2))
        with self.assertRaises(ProviderError):old.get(2)
        with self.assertRaises(ProviderError):old.put(page(2))
        changed_biz=self.cache(identity={**IDENTITY,'public_biz':'other-biz'})
        self.assertIsNone(changed_biz.get(2))

    def test_corrupt_json_wrong_identity_begin_total_and_iso_time_fail_closed(self):
        cache=self.cache()
        mutations=[lambda x:x.update(identity={**IDENTITY,'public_biz':'other'}),
                   lambda x:x['data'].update(begin=0),lambda x:x['data'].update(total_count=5),
                   lambda x:x['data'].update(_fetched_at='not-a-time'),
                   lambda x:x['data'].pop('_fetched_at')]
        for mutate in mutations:
            cache.put(page(2));path=self.directory/'page-2.json';saved=json.loads(path.read_text());mutate(saved)
            path.write_text(json.dumps(saved))
            with self.assertRaises(ProviderError):cache.get(2)
        path.write_text('{broken')
        with self.assertRaises(ProviderError):cache.get(2)
        path.write_text('null')
        with self.assertRaises(ProviderError):cache.get(2)
        (self.directory/'manifest.json').write_text('{broken')
        with self.assertRaises(ProviderError):self.cache()

    def test_wrong_page_put_and_credentials_are_rejected(self):
        cache=self.cache()
        for raw in [{**page(2),'total_count':5},{**page(2),'begin':True},{**page(2),'cookies':[]},
                    {**page(2),'publish_list':[{'token':'never-store'}]}]:
            with self.assertRaises(ProviderError):cache.put(raw)
        with self.assertRaises(ProviderError):self.cache(identity={**IDENTITY,'settings':{}})
        with self.assertRaises(ProviderError):PublicationPageCache('https://mp.weixin.qq.com/',IDENTITY,page())


if __name__=='__main__':unittest.main()
