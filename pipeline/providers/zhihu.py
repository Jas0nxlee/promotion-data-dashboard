"""Read the signed-in creator's article catalog, excluding answers and drafts."""
from urllib.parse import parse_qs, urlsplit

from .base import Collection, ProviderError, identifier, number, timestamp, now
from .browser import BrowserSource

HOST = 'https://www.zhihu.com'
PROFILE = '/api/v4/me'
ARTICLES = '/api/v4/creators/creations/v2/article'


def article_record(raw, profile):
    if raw.get('type') != 'article' or not isinstance(raw.get('data'), dict):
        raise ProviderError('schema_changed', '知乎文章目录混入非文章内容，停止合并')
    data = raw['data']
    cid = identifier(data.get('id'))
    if not cid.isdigit() or identifier(data.get('url_token')) != cid:
        raise ProviderError('identity_mismatch', '知乎文章 ID 与 URL 标识缺失或不一致')
    # The creator catalog omits per-item authors. Do not invent an author from
    # the signed-in account; preserve the verified management scope explicitly.
    author = data.get('author')
    if author is not None:
        if (not isinstance(author, dict)
                or identifier(author.get('url_token')) != profile['verified_account_id']
                or (author.get('id') and identifier(author['id']) != profile['official_user_id'])):
            raise ProviderError('identity_mismatch', '知乎文章作者与已核验账号不一致')
    reaction = raw.get('reaction')
    if not isinstance(reaction, dict):
        raise ProviderError('schema_changed', '知乎文章缺少累计互动指标对象')
    mapping = {'read': 'read_count', 'like': 'vote_up_count',
               'comment': 'comment_count', 'collect': 'collect_count'}
    result = {
        'article_id': cid, 'native_content_id': cid,
        'title': data.get('title', ''), 'summary': data.get('excerpt', ''),
        'url': 'https://zhuanlan.zhihu.com/p/' + cid,
        'cover': data.get('thumbnail') or '', 'published_at': timestamp(data.get('created_time')),
        'updated_at': timestamp(data.get('updated_time')), 'tags': [], 'content_type': 'article',
        'verified_owner_account_id': profile['verified_account_id'],
        'ownership_evidence': 'authenticated_creator_article_catalog',
        'stats': {**{k: number(reaction.get(v)) for k, v in mapping.items()}, 'share': None},
        'extra_metrics': {'like_count': number(reaction.get('like_count'))},
        'data_source': 'zhihu_creator', 'fetched_at': now(),
        'metric_provenance': {k: {'source': 'zhihu_creator', 'definition': v + '_lifetime'}
                              for k, v in mapping.items()},
    }
    if author:
        result.update(source_author=author.get('name', ''), source_author_id=identifier(author.get('id')))
    return result


class ZhihuProvider:
    source = 'zhihu_creator'

    def __init__(self, account, settings, source=None):
        self.account, self.settings = account, settings
        self.browser = source or BrowserSource(account, settings)

    @property
    def call_count(self):
        return self.browser.call_count

    def _get(self, path, params):
        data = self.browser.get_json(HOST + path, params)
        if not isinstance(data, dict) or data.get('error'):
            raise ProviderError('platform_error', '知乎只读接口未成功，暂停本账号采集')
        return data

    def _profile(self):
        # This include query is emitted by the creator page. Only the fields
        # below leave the response; email and account security data are omitted.
        raw = self._get(PROFILE, {'include': 'email,is_active,is_bind_phone'})
        expected = identifier(self.account.get('platform_uid'))
        actual = identifier(raw.get('url_token'))
        uid = identifier(raw.get('id'))
        if (not expected or actual != expected or not uid
                or (self.settings.get('expected_uid') and uid != self.settings['expected_uid'])):
            raise ProviderError('identity_mismatch', '知乎登录账号的 URL token 或内部 ID 与项目绑定不符')
        # is_active is an activation timestamp in the observed response, not
        # a login boolean. The /me URL token and internal ID bind this session.
        profile = {'nickname': raw.get('name', ''), 'verified_account_id': actual,
                   'official_user_id': uid, 'followers': number(raw.get('follower_count')),
                   'total': number(raw.get('articles_count'))}
        if profile['total'] is None:
            raise ProviderError('schema_changed', '知乎账号资料缺少文章总数')
        self.verified_profile = profile
        return profile

    @staticmethod
    def _next_offset(paging, offset, length):
        next_url = paging.get('next')
        if not isinstance(next_url, str):
            raise ProviderError('incomplete_pagination', '知乎下一页地址缺失')
        parsed = urlsplit(next_url)
        query = parse_qs(parsed.query)
        expected = {'start': ['0'], 'end': ['0'], 'limit': ['10'], 'need_co_creation': ['1'],
                    'sort_type': ['created'], 'offset': [str(offset + length)]}
        if (parsed.scheme != 'https' or parsed.netloc != 'www.zhihu.com'
                or parsed.path != ARTICLES or parsed.fragment or query != expected):
            raise ProviderError('incomplete_pagination', '知乎分页地址、游标或查询范围发生变化')
        return offset + length

    def collect(self, max_pages=200, discovery=False):
        rows, seen, offset, total, complete = [], set(), 0, None, False
        with self.browser.session():
            profile = self._profile()
            for _ in range(max(1, 1 if discovery else max_pages)):
                payload = self._get(ARTICLES, {'start': 0, 'end': 0, 'limit': 10, 'offset': offset,
                                             'need_co_creation': 1, 'sort_type': 'created'})
                paging, items = payload.get('paging'), payload.get('data')
                if not isinstance(paging, dict) or not isinstance(items, list):
                    raise ProviderError('schema_changed', '知乎目录缺少文章列表或分页信息')
                current = number(paging.get('totals'))
                if current is None or (total is not None and current != total) or current != profile['total']:
                    raise ProviderError('incomplete_pagination', '知乎目录总数与账号资料不一致或在翻页期间变化')
                total = current
                if type(paging.get('is_end')) is not bool:
                    raise ProviderError('schema_changed', '知乎分页结束标记缺失或格式改变')
                for raw in items:
                    if not isinstance(raw, dict):
                        raise ProviderError('schema_changed', '知乎文章记录不再是对象')
                    row = article_record(raw, profile)
                    if row['article_id'] in seen:
                        raise ProviderError('incomplete_pagination', '知乎文章分页出现重复 ID')
                    rows.append(row)
                    seen.add(row['article_id'])
                if len(rows) > total:
                    raise ProviderError('incomplete_pagination', '知乎文章记录数超过目录总数')
                if paging['is_end']:
                    if len(rows) != total:
                        raise ProviderError('incomplete_pagination', '知乎目录提前结束，未覆盖全部文章')
                    complete = True
                    break
                if not items or len(rows) == total:
                    raise ProviderError('incomplete_pagination', '知乎空页或已到总数仍报告后续内容')
                offset = self._next_offset(paging, offset, len(items))
            after = self._profile()
            if (after['official_user_id'] != profile['official_user_id'] or after['total'] != profile['total']):
                raise ProviderError('identity_mismatch', '知乎账号身份或文章总数在采集期间改变')
        return Collection(profile, rows, complete,
                          '已覆盖创作后台全部文章；回答不计入，赞同与喜欢分列'
                          if complete else '文章目录达到分页上限，保留历史并标记部分覆盖',
                          self.source, self.call_count)

    def comments(self, item, max_pages=200, include_replies=True):
        raise ProviderError('unsupported', '知乎本轮仅采集文章与指标，未接入评论正文')

    def replies(self, item, root_id, max_pages=200):
        raise ProviderError('unsupported', '知乎本轮未接入评论回复明细')
