"""Browser packaging differs between the local Chrome profile and Docker."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from providers.browser import BrowserSource,session_key

ACCOUNT={'platform':'bilibili','account_name':'测试'}
KEY='bilibili:测试'


class BrowserChannelTests(unittest.TestCase):
    def run_session(self,mode,override):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            (directory/session_key(KEY)).mkdir()
            (directory/(session_key(KEY)+'.storage.json')).write_text('{}')
            settings={'channel':'chrome','session_mode':mode}
            original=copy.deepcopy(settings)
            driver=Mock();starter=Mock();starter.start.return_value=driver
            source=BrowserSource(ACCOUNT,settings)
            with patch.dict(os.environ,{},clear=False),patch('providers.browser.SESSIONS',directory),patch('playwright.sync_api.sync_playwright',return_value=starter),patch.object(source,'export_session'):
                if override is None:os.environ.pop('PROMOTION_BROWSER_CHANNEL',None)
                else:os.environ['PROMOTION_BROWSER_CHANNEL']=override
                with source.session():
                    self.assertIsNotNone(source.context)
            self.assertEqual(original,settings)
            self.assertIsNone(source.context)
            driver.stop.assert_called_once()
            return driver

    def test_portable_and_persistent_use_explicit_container_override(self):
        for mode in ('portable','persistent'):
            with self.subTest(mode=mode):
                driver=self.run_session(mode,' chromium ')
                launch=driver.chromium.launch if mode=='portable' else driver.chromium.launch_persistent_context
                self.assertEqual('chromium',launch.call_args.kwargs['channel'])

    def test_unset_or_empty_override_retains_account_browser(self):
        for mode in ('portable','persistent'):
            for override in (None,'','   '):
                with self.subTest(mode=mode,override=override):
                    driver=self.run_session(mode,override)
                    launch=driver.chromium.launch if mode=='portable' else driver.chromium.launch_persistent_context
                    self.assertEqual('chrome',launch.call_args.kwargs['channel'])

    def test_existing_cdp_connection_does_not_launch_another_browser(self):
        with tempfile.TemporaryDirectory() as tmp,patch('providers.browser.SESSIONS',Path(tmp)),patch.dict(os.environ,{'PROMOTION_BROWSER_CHANNEL':'chromium'}):
            driver=Mock();starter=Mock();starter.start.return_value=driver
            context=Mock();driver.chromium.connect_over_cdp.return_value.contexts=[context]
            source=BrowserSource(ACCOUNT,{'channel':'chrome','cdp_url':'http://127.0.0.1:9222'})
            with patch('playwright.sync_api.sync_playwright',return_value=starter):
                with source.session():self.assertIs(context,source.context)
            driver.chromium.launch.assert_not_called()
            driver.chromium.launch_persistent_context.assert_not_called()
            context.close.assert_not_called()
            driver.stop.assert_called_once()


if __name__=='__main__':unittest.main()
