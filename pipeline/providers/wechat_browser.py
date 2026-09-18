"""Read verified WeChat publication pages without retaining their login token."""
import re
import os
from runtime import RUNTIME
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .base import Collection, ProviderError, identifier, number, timestamp, now
from .browser import BrowserSource

HOST = 'https://mp.weixin.qq.com'
SETTING = '/cgi-bin/settingpage'
PUBLISH = '/cgi-bin/appmsgpublish'


def stable_id(value):
    if isinstance(value, int) and abs(value) > 2 ** 53 - 1:
        raise ProviderError('schema_changed', '网页数值标识超过安全整数范围')
    text = identifier(value)
    if not text.isdigit() or int(text) <= 0:
        raise ProviderError('identity_mismatch', '发表记录缺少稳定的数字标识')
    return text


def public_url(value, biz, mid, idx):
    if not isinstance(value, str):
        raise ProviderError('schema_changed', '公众号内容链接格式改变')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or parsed.netloc != 'mp.weixin.qq.com' or not re.fullmatch(r'/s(?:/[A-Za-z0-9_-]+)?', parsed.path):
        raise ProviderError('identity_mismatch', '发表记录链接不是公众号文章链接')
    query = parse_qs(parsed.query)
    for key, expected in (('__biz', biz), ('mid', mid), ('idx', idx)):
        if key in query and query[key] != [expected]:
            raise ProviderError('identity_mismatch', '文章链接身份与发表记录字段不符')
    # Login tokens and analytics tracking parameters must not leave the session.
    keep = [(k, v) for k in ('__biz', 'mid', 'idx', 'sn', 'chksm') for v in query.get(k, [])]
    return urlunsplit(('https', parsed.netloc, parsed.path, urlencode(keep), ''))


def article_record(raw, group, profile):
    required = {'title', 'cover', 'digest', 'read_num', 'old_like_num', 'like_num', 'share_num'}
    if not required.issubset(raw) or not isinstance(raw.get('title'), str):
        raise ProviderError('schema_changed', '公众号文章元数据或指标字段缺失')
    mid, idx = stable_id(raw.get('appmsgid')), stable_id(raw.get('itemidx'))
    url = public_url(raw.get('content_url'), profile['public_biz'], mid, idx)
    kind = number(raw.get('item_show_type'))
    if kind not in (0, 8) or number(raw.get('share_type')) != kind:
        raise ProviderError('unsupported_content_type', '该公众号消息类型尚未实测支持')
    # UI tooltips explicitly say readers/sharers (people), not page views or
    # sharing actions. Keep those separately until a view-count source exists.
    stats = {'read': None, 'like': None,
             'comment': number(raw.get('total_comment_count_contains_reply')),
             'share': None, 'collect': None}
    return {'article_id': mid + '-' + idx, 'native_content_id': mid + '-' + idx,
            'official_message_id': stable_id(group.get('msgid')),
            'title': raw.get('title', ''), 'summary': raw.get('digest', ''),
            'url': url, 'cover': raw.get('cover', ''),
            'published_at': timestamp((group.get('publish_info') or {}).get('create_time')
                                      if group.get('type') == 10002 else (group.get('sent_info') or {}).get('time')),
            'published_at_source': 'publish_info.create_time' if group.get('type') == 10002 else 'sent_info.time',
            'content_type': '公众号', 'tags': ['普通文章' if kind == 0 else '图片消息'],
            'verified_owner_account_id': profile['verified_account_id'],
            'publication_state': 'published', 'stats': stats,
            'extra_metrics': {'read_users': number(raw.get('read_num')),
                              'like_users': number(raw.get('old_like_num')),
                              'share_users': number(raw.get('share_num')),
                              'recommend_users': number(raw.get('like_num')),
                              'root_comments': number(raw.get('comment_num'))},
            'metric_provenance': {
                'like': {'source': 'wechat_browser', 'missing_reason': 'incompatible_unit'},
                'comment': {'source': 'wechat_browser', 'definition': 'lifetime_visible_comments_including_replies',
                            **({'missing_reason': 'not_returned_in_publication_page'} if stats['comment'] is None else {})},
                'read': {'source': 'wechat_browser', 'missing_reason': 'incompatible_unit'},
                'share': {'source': 'wechat_browser', 'missing_reason': 'incompatible_unit'},
                'collect': {'source': 'wechat_browser', 'missing_reason': 'not_in_publication_page'}},
            'data_source': 'wechat_browser', 'fetched_at': now()}


def normalize_catalog(pages, profile):
    rows, exclusions, group_ids, article_ids = [], [], set(), set()
    excluded_groups = []
    total, expected_begin, complete = None, 0, False
    for payload in pages:
        groups = payload.get('publish_list')
        current_total, begin = number(payload.get('total_count')), number(payload.get('begin'))
        count = number(payload.get('count'))
        if (not isinstance(groups, list) or current_total is None or begin != expected_begin
                or count != 10 or len(groups) != min(count, max(0, current_total - begin))
                or (total is not None and current_total != total)):
            raise ProviderError('incomplete_pagination', '公众号消息组分页游标、条数或总数异常')
        total = current_total
        for group in groups:
            if not isinstance(group, dict):
                raise ProviderError('schema_changed', '公众号消息组格式改变')
            gid = stable_id(group.get('msgid'))
            if gid in group_ids:
                raise ProviderError('incomplete_pagination', '公众号消息组分页重复')
            group_ids.add(gid)
            group_type = group.get('type')
            sent_info, sent_result = group.get('sent_info') or {}, group.get('sent_result') or {}
            published_info = group.get('publish_info') or {}
            standalone = group_type == 10002
            if standalone and (published_info.get('publish_status') != 200
                               or identifier(published_info.get('msgid')) != gid
                               or not timestamp(published_info.get('create_time'))):
                raise ProviderError('schema_changed', f'独立发表消息组 {gid} 的身份、成功状态或发表时间尚未核验')
            if not standalone and not timestamp(sent_info.get('time')):
                raise ProviderError('schema_changed', f'消息组 {gid} 缺少有效发表记录时间')
            status = sent_result.get('msg_status')
            publish_failed = (status == 6 and isinstance(sent_result.get('msg_fail_reason'), str)
                              and '发表失败' in sent_result['msg_fail_reason'])
            send_failed = (status == 5 and isinstance(sent_result.get('refuse_reason'), str)
                           and sent_result['refuse_reason'] == 'SENDFAIL_GETTOUINLIST_FAIL')
            if (group_type == 9 and type(sent_info.get('is_published')) is int and sent_info['is_published'] == 0
                    and (publish_failed or send_failed)):
                # Failed attempts are neither published articles nor deletions.
                # Their draft IDs may recur in a later successful publication.
                excluded_groups.append({'official_message_id': gid, 'reason': 'publication_failed', 'msg_status': status})
                continue
            if (group_type == 9 and status == 1 and type(sent_info.get('is_published')) is int
                    and sent_info['is_published'] == 0 and isinstance(group.get('view'), dict)
                    and group['view'].get('status') == '审核中'):
                excluded_groups.append({'official_message_id': gid, 'reason': 'publication_pending', 'msg_status': 1})
                continue
            unavailable_deleted = (group_type == 9 and status == 8
                and isinstance(group.get('view'), dict) and group['view'].get('status') == '无法查看'
                and isinstance(group.get('appmsg_info'), list) and bool(group['appmsg_info'])
                and all(isinstance(raw, dict) and raw.get('is_deleted') is True for raw in group['appmsg_info']))
            if not standalone and status not in (2, 7) and not unavailable_deleted:
                raise ProviderError('schema_changed', f'消息组 {gid} 的发表状态 {status} 尚未核验')
            if (group_type == 16 and status == 7 and isinstance(group.get('video_info'), dict)
                    and group.get('appmsg_info') == [] and isinstance(group.get('view'), dict)
                    and group['view'].get('status') == '已删除'):
                excluded_groups.append({'official_message_id': gid, 'reason': 'deleted_video_message', 'msg_status': 7})
                continue
            if group_type not in (9, 10002) or not isinstance(group.get('appmsg_info'), list) or not group['appmsg_info']:
                raise ProviderError('unsupported_content_type', '发表目录出现尚未核验的消息类型，停止全量替换')
            for raw in group['appmsg_info']:
                if not isinstance(raw, dict):
                    raise ProviderError('schema_changed', '公众号子文章格式改变')
                mid, idx = stable_id(raw.get('appmsgid')), stable_id(raw.get('itemidx'))
                cid = mid + '-' + idx
                if cid in article_ids:
                    raise ProviderError('incomplete_pagination', '公众号子文章标识重复')
                article_ids.add(cid)
                kind = number(raw.get('item_show_type'))
                if kind != number(raw.get('share_type')) or kind not in (0, 5, 8):
                    raise ProviderError('unsupported_content_type', '公众号内容类型尚未实测支持，不能静默跳过')
                if type(raw.get('is_deleted')) is not bool:
                    raise ProviderError('schema_changed', '发表记录删除标志缺失')
                if raw['is_deleted'] or kind == 5:
                    exclusions.append({'article_id': cid, 'official_message_id': gid,
                                       'item_show_type': kind,
                                       'reason': 'deleted' if raw['is_deleted'] else 'standalone_channels_video'})
                    continue
                row = article_record(raw, group, profile)
                if payload.get('_fetched_at'):
                    row['fetched_at'] = payload['_fetched_at']
                rows.append(row)
        expected_begin += len(groups)
        if expected_begin > total or (not groups and expected_begin < total):
            raise ProviderError('incomplete_pagination', '公众号空页或消息组数超过声明总数')
        complete = expected_begin == total
    if total is None:
        raise ProviderError('incomplete_pagination', '未读取任何发表目录页')
    result_profile = {**profile, 'total': len(rows) if complete else None,
                      'publication_group_total': total, 'publication_groups_covered': len(group_ids),
                      'publication_items_covered': len(article_ids), 'excluded_contents': exclusions,
                      'excluded_publication_groups': excluded_groups}
    return result_profile, rows, complete


class WeChatBrowserProvider:
    source = 'wechat_browser'

    def __init__(self, account, settings, source=None):
        self.account, self.settings = account, settings
        self.browser = source or BrowserSource(account, settings)
        self.timeout = min(60000, max(1000, int(settings.get('timeout_ms', 25000))))

    @property
    def call_count(self):
        return self.browser.call_count

    def _action(self, name):
        self.browser.budget.consume(self.account['platform'] + ':' + name)
        self.browser.call_count += 1

    def _navigate(self, page, url, path=None):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'mp.weixin.qq.com' or (path and parsed.path != path):
            raise ProviderError('invalid_host', '公众号页面导航地址不属于已核验流程')
        self._action('page')
        page.goto(url, wait_until='domcontentloaded', timeout=self.timeout)
        ready = ('Array.isArray(window.wx?.cgiData?.publish_list)' if path == PUBLISH
                 else 'Boolean(window.cgiData?.original_username)' if path == SETTING
                 else 'Boolean(window.wx?.data?.user_name)')
        try:
            page.wait_for_function(ready, timeout=self.timeout)
        except Exception:
            raise ProviderError('session_expired', '公众号会话需重新登录或人工安全验证') from None
        if urlsplit(page.url).netloc != 'mp.weixin.qq.com':
            raise ProviderError('session_expired', '公众号页面进入外部登录流程')

    def _profile(self):
        page = self.browser.context.new_page()
        try:
            self._navigate(page, HOST + '/')
            home = page.evaluate('''() => ({alias:wx.data.user_name,nickname:wx.data.nick_name,
                biz:wx.data.uin_base64,followers:document.body.innerText.match(/总用户数\\s+([\\d,]+)/)?.[1]})''')
            link = page.locator('a[href*="/cgi-bin/settingpage"]').first.get_attribute('href')
            if not link:
                raise ProviderError('schema_changed', '公众号首页缺少账号设置导航')
            from urllib.parse import urljoin
            self._navigate(page, urljoin(HOST, link), SETTING)
            page.wait_for_function('Boolean(window.cgiData?.original_username)', timeout=self.timeout)
            identity = page.evaluate('''() => ({original:window.cgiData.original_username,
                nickname:window.cgiData.nickname,alias:window.cgiData.alias})''')
            expected = self.account.get('platform_uid')
            if not expected or identity['original'] != expected:
                raise ProviderError('identity_mismatch', '登录公众号原始 ID 与项目绑定不符')
            if self.settings.get('expected_biz') and home['biz'] != self.settings['expected_biz']:
                raise ProviderError('identity_mismatch', '公众号公开 biz 标识与绑定不符')
            if not home.get('biz'):
                raise ProviderError('schema_changed', '公众号首页缺少公开 biz 标识')
            self.verified_profile = {'nickname': identity['nickname'], 'verified_account_id': expected,
                'official_user_id': expected, 'public_biz': home['biz'], 'followers': number(home.get('followers'))}
            return self.verified_profile
        finally:
            page.close()

    def _publication_pages(self, max_pages):
        page = self.browser.context.new_page()
        self._cached_pages_reused = 0
        try:
            self._navigate(page, HOST + '/')
            link = page.locator('a[href*="/cgi-bin/appmsgpublish"]').filter(has_text='发表记录').first.get_attribute('href')
            if not link:
                raise ProviderError('schema_changed', '公众号首页缺少发表记录导航')
            from urllib.parse import urljoin
            self._navigate(page, urljoin(HOST, link), PUBLISH)
            navigation_url = page.url
            def capture():
                return page.evaluate('''() => ({total_count:wx.cgiData.total_count,begin:wx.cgiData.begin,
                    count:wx.cgiData.count,publish_list:wx.cgiData.publish_list})''')
            head = capture()
            cache = None
            if (os.environ.get('PROMOTION_AUTHORIZATION_OPERATION') == '1'
                    and RUNTIME.parent.name == 'authorizations' and re.fullmatch(r'[a-f0-9]{32}', RUNTIME.name)):
                from .wechat_publication_cache import PublicationPageCache
                identity = {key: self.verified_profile[key] for key in ('verified_account_id', 'public_biz')}
                cache = PublicationPageCache(RUNTIME / 'wechat-publication-pages', identity, head)
            begin, loaded_begin = 0, 0
            for index in range(max_pages):
                data = head if index == 0 else cache.get(begin) if cache else None
                reused = index != 0 and data is not None
                if data is None:
                    if loaded_begin + 10 == begin:
                        self._action('next_publication_page')
                        page.get_by_role('link', name='下一页', exact=True).click(timeout=self.timeout)
                        page.wait_for_function('(n) => window.wx?.cgiData?.begin === n', arg=begin, timeout=self.timeout)
                    else:
                        # Resume through the same normal page's observed begin
                        # parameter; do not replay discarded auth headers.
                        parsed = urlsplit(navigation_url)
                        query = parse_qs(parsed.query)
                        query['begin'] = [str(begin)]
                        self._navigate(page, urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                                        urlencode(query, doseq=True), '')), PUBLISH)
                    loaded_begin = begin
                    data = capture()
                if reused:
                    self._cached_pages_reused += 1
                else:
                    data = cache.put(data) if cache else {**data, '_fetched_at': now()}
                yield data
                current, total = number(data.get('begin')), number(data.get('total_count'))
                if current != begin or total is None:
                    raise ProviderError('schema_changed', '公众号分页计数缺失或偏移异常')
                begin += len(data['publish_list'])
                if begin >= total:
                    break
        finally:
            page.close()

    def collect(self, max_pages=200, discovery=False):
        with self.browser.session():
            profile = self._profile()
            profile, records, complete = normalize_catalog(
                self._publication_pages(max(1, 1 if discovery else max_pages)), profile)
            after = self._profile()
            if (after['verified_account_id'] != profile['verified_account_id'] or after['public_biz'] != profile['public_biz']):
                raise ProviderError('identity_mismatch', '公众号身份在采集期间发生变化')
        excluded = profile['excluded_contents']
        note = (f"消息组覆盖 {profile['publication_groups_covered']}/{profile['publication_group_total']}；"
                f"文章与图片消息 {len(records)}；明确排除 {sum(r['reason']=='deleted' for r in excluded)} 条已删除、"
                f"{sum(r['reason']=='standalone_channels_video' for r in excluded)} 条独立视频号内容；"
                f'另有 {len(profile["excluded_publication_groups"])} 个明确排除的消息组（发表失败、审核中或已删除视频消息）未计为图文文章；'
                '阅读与分享人数单列，次数和收藏未知')
        return Collection(profile, records, complete, note, self.source, self.call_count)

    def comments(self, item, max_pages=200, include_replies=True):
        raise ProviderError('unsupported', '公众号后台适配仅获取目录和累计指标，未接入留言正文')

    def replies(self, item, root_id, max_pages=200):
        raise ProviderError('unsupported', '公众号后台适配未接入留言回复明细')
