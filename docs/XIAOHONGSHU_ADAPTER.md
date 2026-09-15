# 小红书创作后台适配记录

核验日期：2026-09-15。账号：安芯日记。只在 `codex/tikhub-free-collectors` 工作树与 `.runtime/local` 内测试。

## 已验证范围

- 小红书号 `1154342157` 与创作首页 `red_num` 精确匹配，昵称安芯日记，粉丝351。
- 笔记管理“全部”显示62篇；完成6个页面响应，按24位笔记ID去重后62篇，当前均为已发布状态。
- 阅读、点赞、评论、收藏、分享五项累计指标均为62/62可用，不使用首页近7日或近30日汇总替代。
- 导出当前平台会话后，在新的无界面浏览器上下文中再次完成全量采集及图文CLI，结果写入独立测试大屏。
- 本地107项测试通过，包含容器滚动、仅首屏提供总数、置顶重复、错账号阻断、总数变化、指标缺失及临时导航参数不进入快照。

## 网站流程

`/new/home` 自动发出 `/api/galaxy/creator/home/personal_info`；`/new/note-manager` 自动发出 `/api/galaxy/v2/creator/note/user/posted?tab=0&page=0`。

直接重放未签名请求实测返回401/406。采集器监听正常网页生成的签名响应，不保存签名算法或将捕获的请求头写入配置。

笔记管理使用内部 `div.content` 滚动容器。第一页包含置顶笔记和 `tags[].notes_count`，后续页可能为空tags，页码也不保证逐一递增。采集器由页面完成下一页请求，按唯一ID与首屏总数判断覆盖；置顶重复不会增加计数。达到页数上限返回明确的部分结果，总数发生变化或无法覆盖时不会冒充完整。

| 输出 | 页面字段 | 口径 |
| --- | --- | --- |
| read | view_count | 笔记累计观看数 |
| like | likes | 累计点赞数 |
| comment | comments_count | 评论计数，不能代替评论明细 |
| collect | collected_count | 累计收藏数 |
| share | shared_count | 累计分享数 |
| published_at | time | 后台展示的发布时间，精度为分钟 |

`visible_time` 不当作精确发布时间。短期 `xsec_token` 不写入快照、配置或日志；保存的作品URL仅含稳定笔记ID，主站实际访问可能需要登录及页面生成的导航参数。

## 尚未完成的评论接入

创作后台的笔记封面会打开小红书主站详情。主站显示“登录查看全部评论内容”，与创作后台登录态分开；已向用户请求在同一专用浏览器内再次扫码。

在主站完成登录、验证评论及二级回复分页与新旧ID前，适配器明确返回 `setup_required`，不以评论数或匿名可见片段标记评论验证成功，不推进评论提醒状态。该账号目前只有 `content_ready`，不计入完整接入的2/14。

## 本地运行

私有账号配置选择 `provider: xiaohongshu_creator`，要求账号名单中存在已核验的 `provided_id`。运行目录隔离方法见 [本地开发说明](LOCAL_DEVELOPMENT.md)。

```bash
PROMOTION_RUNTIME_DIR="$PWD/.runtime/local" PROMOTION_TEST_MODE=1 \
  .venv/bin/python pipeline/fetch_article_data.py --only 'xiaohongshu:安芯日记' --max-pages 20
```

会话文件仅保存在私有 `.runtime/sessions`，不会进入Git；可移植会话只验证本机恢复，未验证跨机器或跨IP运行。主干提交、生产快照及邮件发送保持原状。
