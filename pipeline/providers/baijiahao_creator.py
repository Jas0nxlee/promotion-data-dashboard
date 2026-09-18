"""Read published graphic articles from normal creator-page response events."""
from contextlib import contextmanager
import json
import math
import re
import time
from urllib.parse import parse_qs, urlsplit

from .base import Collection, ProviderError, identifier, number, timestamp, now
from .browser import BrowserSource

HOST = 'https://baijiahao.baidu.com'
ACCOUNT_PAGE = HOST + '/builder/rc/settings/accountSet'
CONTENT_PAGE = HOST + '/builder/rc/content?currentPage=1&pageSize=10&type=news&collection=publish&clearBeforeFetch=false'
PROFILE = '/user-ui/cms/settingInfo'
ARTICLES = '/pcui/article/lists'


def decode_payload(body):
    try:
        value = json.loads(body)
    except (ValueError, TypeError):
        raise ProviderError('schema_changed', '百家号后台未返回有效 JSON') from None
    if not isinstance(value, dict) or value.get('errno') != 0 or not isinstance(value.get('data'), dict):
        reason = 'session_expired' if isinstance(value, dict) and value.get('errno') == 10000010 else 'platform_error'
        raise ProviderError(reason, '百家号后台读取未成功，请检查授权会话')
    return value['data']


def article_record(raw, app_id):
    if not isinstance(raw, dict):
        raise ProviderError('schema_changed', '百家号作品记录格式改变')
    cid = identifier(raw.get('article_id'))
    if (not cid.isdigit() or identifier(raw.get('id')) != cid
            or identifier(raw.get('app_id')) != app_id):
        raise ProviderError('identity_mismatch', '百家号文章 ID 或作者 app_id 与绑定不符')
    if raw.get('type') != 'news' or raw.get('status') != 'publish' or raw.get('is_published') != 1:
        raise ProviderError('identity_mismatch', '图文已发布目录混入其他类型或非发布作品')
    parsed = urlsplit(raw.get('url') or '')
    query = parse_qs(parsed.query)
    if parsed.scheme not in ('http', 'https') or parsed.netloc != 'baijiahao.baidu.com' or parsed.path != '/s' or query.get('id') != [cid]:
        raise ProviderError('identity_mismatch', '百家号公开文章 URL 与后台 article_id 不一致')
    cover = ''
    if raw.get('cover_images'):
        try:
            images = json.loads(raw['cover_images'])
        except (TypeError, ValueError):
            raise ProviderError('schema_changed', '百家号封面列表格式改变') from None
        if not isinstance(images, list):
            raise ProviderError('schema_changed', '百家号封面列表格式改变')
        if images and isinstance(images[0], dict):
            cover = images[0].get('src') or ''
    mapping = {'read': 'read_amount', 'like': 'like_amount', 'comment': 'comment_amount',
               'share': 'share_amount', 'collect': 'collection_amount'}
    published = timestamp(raw.get('publish_at'))
    if not published:
        raise ProviderError('schema_changed', '百家号已发布文章缺少有效发表时间')
    return {'article_id': cid, 'native_content_id': cid, 'title': raw.get('title', ''),
            'url': HOST + '/s?id=' + cid, 'cover': cover, 'summary': raw.get('abstract', ''),
            'published_at': published, 'tags': [], 'content_type': '图文',
            'source_author_id': app_id, 'platform_type': 'news', 'platform_status': 'publish',
            'stats': {k: number(raw.get(v)) for k, v in mapping.items()},
            'extra_metrics': {'recommendations': number(raw.get('rec_amount'))},
            'data_source': 'baijiahao_creator', 'fetched_at': now(),
            'metric_provenance': {k: {'source': 'baijiahao_creator',
                                     'definition': v + '_published_content_total',
                                     'scope': 'creator_content_management_no_date_window'}
                                  for k, v in mapping.items()}}


class BaijiahaoBrowserSource(BrowserSource):
    """Let the website generate every request; do not cache its auth headers."""
    @contextmanager
    def session(self):
        with super().session():
            self._catalog_ui = None
            self._catalog_current = None
            try:
                yield self
            finally:
                if self._catalog_ui is not None:
                    self._catalog_ui.close()
                self._catalog_ui = None
                self._catalog_current = None

    def profile(self):
        page = self.context.new_page()
        captured, failures = [], []
        timeout = min(60000, max(1000, int(self.settings.get('timeout_ms', 25000))))

        def receive(response):
            parsed = urlsplit(response.url)
            if parsed.netloc != 'baijiahao.baidu.com' or parsed.path != PROFILE:
                return
            self.call_count += 1
            if response.status != 200:
                failures.append(ProviderError('session_expired', '百家号身份页面要求重新授权或安全验证'))
                return
            try:
                data = decode_payload(response.body())
                # Discard private registration/identity fields immediately.
                name = data.get('name')
                captured.append(name)
            except ProviderError as exc:
                failures.append(exc)

        page.on('response', receive)
        try:
            self.budget.consume(self.account['platform'] + ':creator_profile')
            page.goto(ACCOUNT_PAGE, wait_until='domcontentloaded', timeout=timeout)
            until = time.monotonic() + timeout / 1000
            while not captured and not failures and time.monotonic() < until:
                page.wait_for_timeout(100)
            if failures:
                raise failures[0]
            if not captured:
                raise ProviderError('session_expired', '百家号账号设置未加载，请完成正常登录或人工安全验证')
            identity_nodes = page.get_by_text(re.compile(r'百家号ID[：:]\s*\d+'))
            identity_nodes.first.wait_for(state='visible', timeout=timeout)
            texts = identity_nodes.all_text_contents()
            ids = {m.group(1) for text in texts for m in re.finditer(r'百家号ID[：:]\s*(\d+)', text)}
            expected = identifier(self.account.get('platform_uid'))
            if not expected or ids != {expected}:
                raise ProviderError('identity_mismatch', '百家号设置页 app_id 与项目绑定不一致')
            name = captured[-1]
            if not isinstance(name, str) or not name:
                raise ProviderError('session_expired', '百家号正常网页尚未返回账号资料')
            return {'nickname': name, 'verified_account_id': expected,
                    'official_user_id': expected, 'followers': None}
        finally:
            page.close()

    def catalog_page(self, current_page):
        if type(current_page) is not int or current_page < 1:
            raise ProviderError('incomplete_pagination', '百家号请求页码无效')
        if self._catalog_ui is None:
            self._catalog_ui = self.context.new_page()
        page = self._catalog_ui
        timeout = min(60000, max(1000, int(self.settings.get('timeout_ms', 25000))))
        captured, failures = [], []

        def receive(response):
            parsed = urlsplit(response.url)
            query = parse_qs(parsed.query)
            if (parsed.netloc != 'baijiahao.baidu.com' or parsed.path != ARTICLES
                    or query.get('currentPage') != [str(current_page)]
                    or query.get('pageSize') != ['10'] or query.get('type') != ['news']
                    or query.get('collection') != ['publish']):
                return
            self.call_count += 1
            if response.status != 200 or any(query.get(k) for k in ('search', 'startDate', 'endDate')):
                failures.append(ProviderError('page_error', '百家号正常分页响应失败或出现额外筛选条件'))
                return
            try:
                captured.append(decode_payload(response.body()))
            except ProviderError as exc:
                failures.append(exc)

        self.budget.consume(self.account['platform'] + ':creator_catalog_page')
        interval = max(.6, float(self.settings.get('request_interval', .6)))
        time.sleep(max(0, interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        page.on('response', receive)
        try:
            if self._catalog_current == current_page - 1:
                page.locator('li.cheetah-pagination-next[aria-disabled="false"] button').click(timeout=timeout)
            else:
                url = CONTENT_PAGE.replace('currentPage=1&', f'currentPage={current_page}&', 1)
                page.goto(url, wait_until='domcontentloaded', timeout=timeout)
            until = time.monotonic() + timeout / 1000
            while not captured and not failures and time.monotonic() < until:
                page.wait_for_timeout(100)
            if failures:
                raise failures[0]
            if not captured:
                raise ProviderError('session_expired', '未观察到正常图文已发布分页，请检查登录或人工安全验证')
            for title in ('图文', '已发布'):
                if page.get_by_role('tab', name=title, exact=True).get_attribute('aria-selected') != 'true':
                    raise ProviderError('identity_mismatch', '百家号页面离开图文已发布范围')
            self._catalog_current = current_page
            return captured[-1]
        finally:
            page.remove_listener('response', receive)


class BaijiahaoCreatorProvider:
    source = 'baijiahao_creator'

    def __init__(self, account, settings, source=None):
        self.account, self.settings = account, settings
        self.browser = source or BaijiahaoBrowserSource(account, settings)

    @property
    def call_count(self):
        return self.browser.call_count

    def _profile(self):
        profile = self.browser.profile()
        expected = identifier(self.account.get('platform_uid'))
        if profile.get('verified_account_id') != expected:
            raise ProviderError('identity_mismatch', '百家号资料 app_id 与项目配置不符')
        self.verified_profile = profile
        return profile

    def _catalog_page(self, current, expected_total, expected_pages):
        # The creator UI can transiently return errno=0 with an empty 0/0
        # catalog in the middle of a nonempty scan. Re-read only that exact
        # anomaly, through normal UI navigation and the shared request budget.
        # Authentication, HTTP and schema failures still propagate immediately.
        for attempt in range(3):
            data = self.browser.catalog_page(current)
            paging = data.get('page')
            anomalous_empty = (expected_total is not None and expected_total > 0
                and current <= expected_pages and isinstance(paging, dict)
                and number(paging.get('currentPage')) == current
                and number(paging.get('pageSize')) == 10
                and number(paging.get('totalCount')) == 0
                and number(paging.get('totalPage')) == 0 and data.get('list') == [])
            if not anomalous_empty:
                return data, attempt
            if attempt == 2:
                raise ProviderError('incomplete_pagination',
                    f'百家号第 {current} 页连续 3 次返回异常空目录（预期 {expected_total} 条/{expected_pages} 页）；未跳页，原数据保留，请稍后重试')
            time.sleep((2, 5)[attempt])
            self._profile()  # An account switch or expired session must stop retries.

    def collect(self, max_pages=200, discovery=False):
        records, seen, total, total_pages, complete = [], set(), None, None, False
        retried_pages = []
        with self.browser.session():
            profile = self._profile()
            for current in range(1, max(1, 1 if discovery else max_pages) + 1):
                data, retries = self._catalog_page(current, total, total_pages)
                if retries:
                    retried_pages.append({'page': current, 'retries': retries})
                paging, rows = data.get('page'), data.get('list')
                if not isinstance(paging, dict) or not isinstance(rows, list):
                    raise ProviderError('schema_changed', '百家号目录缺少记录或分页结构')
                n, pages = number(paging.get('totalCount')), number(paging.get('totalPage'))
                if (n is None or pages is None or number(paging.get('currentPage')) != current
                        or number(paging.get('pageSize')) != 10 or pages != math.ceil(n / 10)
                        or (total is not None and (total != n or total_pages != pages))):
                    raise ProviderError('incomplete_pagination',
                                        f'百家号分页信息改变：请求页 {current}，返回页 {number(paging.get("currentPage"))}；'
                                        f'总数 {total}→{n}，总页数 {total_pages}→{pages}')
                total, total_pages = n, pages
                expected_rows = min(10, max(0, total - (current - 1) * 10))
                if len(rows) != expected_rows:
                    raise ProviderError('incomplete_pagination', '百家号图文目录提前空页或短页')
                for raw in rows:
                    row = article_record(raw, profile['verified_account_id'])
                    if row['article_id'] in seen:
                        raise ProviderError('incomplete_pagination', '百家号分页出现重复文章 ID')
                    seen.add(row['article_id'])
                    records.append(row)
                if len(records) == total:
                    complete = True
                    break
            after = self._profile()
            if after['verified_account_id'] != profile['verified_account_id']:
                raise ProviderError('identity_mismatch', '百家号登录身份在分页期间变化')
        profile.update(total=total, published_graphic_pages=total_pages, retried_catalog_pages=retried_pages,
                       scope='published_news_only')
        note = (f'已发布图文覆盖 {len(records)}/{total}；排除视频、动态、草稿及非发布内容；'
                '逐篇指标来自作品管理展示总量，未指定7/30天时间窗')
        return Collection(profile, records, complete, note, self.source, self.call_count)

    def comments(self, item, max_pages=200, include_replies=True):
        raise ProviderError('unsupported', '百家号本轮采集文章和评论数量，未接入评论正文')

    def replies(self, item, root_id, max_pages=200):
        raise ProviderError('unsupported', '百家号本轮未接入评论回复明细')
