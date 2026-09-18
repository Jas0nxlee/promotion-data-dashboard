"""A platform refusal stops that account without inventing successful coverage."""
from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
import comment_monitor as cm
from providers.base import ProviderError

class RatePauseTests(unittest.TestCase):
    def test_rate_refusal_does_not_request_the_next_work_or_mark_scan_complete(self):
        rows=[{'account_key':'bilibili:fixture','platform':'bilibili','platform_label':'B站',
               'account_name':'fixture','content_id':cid,'published_at':'2026-09-01T00:00:00+08:00'}
              for cid in ('BV-first','BV-second')]
        args=SimpleNamespace(platform=['bilibili:fixture'],content_id=None,limit=0,tiered_polling=False,
            no_replies=False,ignore_errors=False,dry_run=False,no_api=False,max_pages=2)
        state={'baseline_done':True,'monitor_started_at':'2026-09-01T00:00:00+08:00'}
        fetch=Mock(side_effect=ProviderError('rate_limited','business code -352'))
        now=datetime(2026,9,17,tzinfo=timezone(timedelta(hours=8)))
        with patch.object(cm,'fetch_root_comments',fetch):
            items,errors,updated=cm.check_comments(SimpleNamespace(call_count=0),rows,args,
                state=state,timeline={'timeline_started_at':state['monitor_started_at'],'events':{}},
                official_identities={},now=now)
        self.assertEqual([],items);self.assertEqual(1,len(errors));self.assertEqual(1,fetch.call_count)
        self.assertFalse(updated['last_scan']['complete'])
        self.assertNotIn('bilibili:BV-second',updated.get('content_poll_at',{}))

if __name__=='__main__':unittest.main()
