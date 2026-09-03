/* 视频推广数据大屏 · 渲染逻辑 */
"use strict";

const METRICS = {
  play: "播放", like: "点赞", comment: "评论/回复",
  share: "分享", collect: "收藏", download: "下载",
};
const PLATFORM_COLOR = { douyin: "#ff4d6d", bilibili: "#38bdf8", wechat_channels: "#34d399" };
const PLATFORM_LABEL = { douyin: "抖音", bilibili: "B站", wechat_channels: "视频号" };
const LINE_COLOR = { "望获": "#f5c34d", "芯片": "#a78bfa" };
const STATUS_LABEL = { ok: "完整", partial: "部分", pending: "待配置", stale: "缓存", error: "失败" };
const PERIOD_LABEL = { all: "全部时间", 30: "近 30 天", 90: "近 90 天", 365: "近 1 年" };
const PAGE_SIZE = 15;
const DAY_MS = 86400000;

const state = {
  platform: "all", line: "all", account: "all", period: "all",
  topMetric: "play", acctMetric: "play",
  sortKey: "published_at", sortDir: -1, page: 1,
};
let DATA = null;
const charts = {};

/* ---------- 工具 ---------- */
const $ = (id) => document.getElementById(id);
const num = (value) => (typeof value === "number" && Number.isFinite(value) ? value : null);

function fmt(value) {
  const n = num(value);
  if (n === null) return "-";
  if (Math.abs(n) >= 1e8) return `${(n / 1e8).toFixed(2)}亿`;
  if (Math.abs(n) >= 1e4) return `${(n / 1e4).toFixed(1)}万`;
  return n.toLocaleString("zh-CN");
}

function fmtFull(value) {
  const n = num(value);
  return n === null ? "-" : n.toLocaleString("zh-CN");
}

function fmtPct(rate) {
  return `${(Math.max(0, Number(rate) || 0) * 100).toFixed(0)}%`;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

function safeUrl(value) {
  const url = String(value || "").trim();
  return /^https?:\/\//i.test(url) ? esc(url) : "";
}

function dayOf(value) { return value ? value.slice(0, 10) : "-"; }
function monthOf(value) { return value ? value.slice(0, 7) : null; }
function platformLabel(platform) { return PLATFORM_LABEL[platform] || platform || "未知"; }
function richText(value) { return String(value ?? "").replace(/[{}|]/g, " "); }

function metricSummary(items, key) {
  const values = items.map((item) => num(item.stats?.[key])).filter((value) => value !== null);
  return {
    value: values.length ? values.reduce((total, value) => total + value, 0) : null,
    available: values.length,
    total: items.length,
    rate: items.length ? values.length / items.length : 0,
  };
}

function withinPeriod(item) {
  if (state.period === "all") return true;
  const published = Date.parse(item.published_at || "");
  return Number.isFinite(published) && published >= Date.now() - Number(state.period) * DAY_MS;
}

function filteredVideos() {
  return DATA.videos.filter((video) =>
    (state.platform === "all" || video.platform === state.platform) &&
    (state.line === "all" || video.business_line === state.line) &&
    (state.account === "all" || video.account_key === state.account) &&
    withinPeriod(video));
}

function visibleAccounts() {
  return DATA.accounts.filter((account) =>
    (state.platform === "all" || account.platform === state.platform) &&
    (state.line === "all" || account.business_line === state.line) &&
    (state.account === "all" || account.account_key === state.account));
}

function uniqueVideos(items) {
  const groups = new Map();
  items.forEach((item) => {
    const identity = item.video_id || item.url || `${item.account_key}:${item.title}:${item.published_at}`;
    const key = `${item.platform}:${identity}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  return [...groups.values()].map((group) => {
    const representative = group.find((item) => item.primary_account_key === item.account_key) || group[0];
    const associated = representative.associated_account_keys || [...new Set(group.map((item) => item.account_key))];
    return { ...representative, _associationCount: associated.length, _associatedAccountKeys: associated };
  });
}

function recentMonths(items, limit = 24) {
  const values = items.map((item) => monthOf(item.published_at)).filter((value) => /^\d{4}-\d{2}$/.test(value));
  if (!values.length) return [];
  const [minYear, minMonth] = [...values].sort()[0].split("-").map(Number);
  const [maxYear, maxMonth] = [...values].sort().at(-1).split("-").map(Number);
  const minimum = new Date(minYear, minMonth - 1, 1);
  const cursor = new Date(maxYear, maxMonth - 1, 1);
  const months = [];
  while (cursor >= minimum && months.length < limit) {
    months.unshift(`${cursor.getFullYear()}-${String(cursor.getMonth() + 1).padStart(2, "0")}`);
    cursor.setMonth(cursor.getMonth() - 1);
  }
  return months;
}

function makeChart(id, option) {
  if (!charts[id]) charts[id] = echarts.init($(id));
  charts[id].setOption({
    animationDuration: 450,
    animationDurationUpdate: 300,
    aria: { enabled: true, decal: { show: false } },
    ...option,
  }, true);
}

const AXIS_STYLE = {
  axisLine: { lineStyle: { color: "#2c4170" } },
  axisLabel: { color: "#8296bd", fontSize: 11 },
  splitLine: { lineStyle: { color: "rgba(44,65,112,.35)" } },
};

function colorWithAlpha(hex, alpha) {
  const value = String(hex || "").replace("#", "");
  if (!/^[0-9a-f]{6}$/i.test(value)) return `rgba(56,217,245,${alpha})`;
  return `rgba(${parseInt(value.slice(0, 2), 16)},${parseInt(value.slice(2, 4), 16)},${parseInt(value.slice(4, 6), 16)},${alpha})`;
}

function accountAxisLabel(rows) {
  const platformStyles = Object.fromEntries(Object.entries(PLATFORM_COLOR).map(([platform, color]) => [
    `platform_${platform}`,
    {
      color,
      backgroundColor: colorWithAlpha(color, .1),
      borderColor: colorWithAlpha(color, .52),
      borderWidth: 1,
      borderRadius: 3,
      padding: [1, 6],
      fontSize: 10,
      fontWeight: 500,
      lineHeight: 18,
      verticalAlign: "middle",
    },
  ]));
  return {
    ...AXIS_STYLE.axisLabel,
    interval: 0,
    formatter: (_value, index) => {
      const account = rows[index]?.account;
      if (!account) return "";
      const platform = account.platform_label || platformLabel(account.platform);
      return `{account|${richText(account.account_name)}}  {platform_${account.platform}|${richText(platform)}}`;
    },
    rich: {
      account: { color: "#dce6f8", fontSize: 12, fontWeight: 600, lineHeight: 20 },
      ...platformStyles,
    },
  };
}

function coverageText(summary) {
  if (!summary.total) return "当前筛选无作品";
  return `有值 ${summary.available}/${summary.total}（${fmtPct(summary.rate)}）`;
}

function ageHours(value) {
  const timestamp = Date.parse(value || "");
  return Number.isFinite(timestamp) ? Math.max(0, (Date.now() - timestamp) / 3600000) : null;
}

function ageText(value) {
  const hours = ageHours(value);
  if (hours === null) return "更新时间未知";
  if (hours < 1) return "1 小时内";
  if (hours < 24) return `${Math.floor(hours)} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

function shortStamp(value) {
  return value ? value.replace("T", " ").slice(0, 16) : "--";
}

/* ---------- 数据质量 ---------- */
function renderHealth() {
  const unique = uniqueVideos(DATA.videos);
  const play = metricSummary(unique, "play");
  const quality = DATA.quality || {};
  const statusCounts = quality.account_status_counts || {};
  const ok = statusCounts.ok ?? DATA.accounts.filter((account) => account.status === "ok").length;
  const issueCount = DATA.accounts.length - ok;
  const associations = quality.association_count ?? DATA.videos.length;
  const uniqueCount = quality.unique_content_count ?? unique.length;
  const shared = quality.shared_content_count ?? Math.max(0, associations - uniqueCount);
  const freshness = ageHours(DATA.updated_at);
  const health = [
    {
      title: "内容粒度",
      text: `${uniqueCount} 条去重作品 · ${associations} 条账号关联${shared ? ` · ${shared} 条共享作品` : ""}`,
      level: shared ? "warn" : "",
    },
    {
      title: "账号采集",
      text: `${ok}/${DATA.accounts.length} 个账号完整${issueCount ? ` · ${issueCount} 个需关注` : ""}`,
      level: issueCount ? "warn" : "",
    },
    {
      title: "播放覆盖",
      text: coverageText(play),
      level: play.rate < .5 ? "warn" : "",
    },
    {
      title: "数据新鲜度",
      text: `${ageText(DATA.updated_at)} · 数据截至 ${dayOf(DATA.data_as_of || quality.date_max)}`,
      level: freshness === null || freshness > 48 ? "error" : freshness > 24 ? "warn" : "",
    },
  ];
  $("dataHealth").innerHTML = health.map((item) =>
    `<div class="health-item ${item.level}"><strong>${item.title}</strong><span title="${esc(item.text)}">${esc(item.text)}</span></div>`).join("");
}

function renderNotice() {
  const issues = DATA.accounts.filter((account) => account.status !== "ok");
  const warnings = (DATA.quality?.warnings || []).filter(Boolean);
  const notice = $("dataNotice");
  if (!issues.length && !warnings.length) {
    notice.hidden = true;
    notice.innerHTML = "";
    return;
  }
  notice.hidden = false;
  notice.innerHTML = `
    <div class="notice-summary"><b>数据提示：</b>
      <span>${issues.length ? `${issues.length} 个账号存在部分覆盖或缓存回退` : "账号采集正常"}</span>
      ${warnings.length ? `<span>${warnings.length} 条质量提醒</span>` : ""}
    </div>
    <details><summary>查看采集与质量说明</summary>
      <ul>
        ${issues.map((account) => `<li>${esc(account.platform_label || platformLabel(account.platform))}「${esc(account.account_name)}」— ${esc(account.error || account.coverage_note || STATUS_LABEL[account.status])}</li>`).join("")}
        ${warnings.map((warning) => `<li>${esc(warning)}</li>`).join("")}
      </ul>
    </details>`;
}

/* ---------- KPI ---------- */
function renderKPI(videos, associations) {
  const accounts = visibleAccounts();
  const covered = accounts.filter((account) => associations.some((video) => video.account_key === account.account_key)).length;
  const cutoff = Date.now() - 30 * DAY_MS;
  const recent = videos.filter((video) => {
    const published = Date.parse(video.published_at || "");
    return Number.isFinite(published) && published >= cutoff;
  }).length;
  const play = metricSummary(videos, "play");
  const like = metricSummary(videos, "like");
  const comment = metricSummary(videos, "comment");
  const share = metricSummary(videos, "share");
  const collect = metricSummary(videos, "collect");
  const tiles = [
    { label: "有数据账号", value: covered, sub: `${accounts.length} 个筛选账号`, accent: "#38d9f5", raw: true },
    { label: "作品总数", value: videos.length,
      sub: associations.length === videos.length ? "按作品去重" : `${associations.length} 条账号关联，已去重`, accent: "#34d399", raw: true },
    { label: "总播放", value: play.value, sub: coverageText(play), accent: "#f5c34d" },
    { label: "总点赞", value: like.value, sub: coverageText(like), accent: "#ff5d7e" },
    { label: "总评论/回复", value: comment.value, sub: coverageText(comment), accent: "#a78bfa" },
    { label: "总分享", value: share.value, sub: coverageText(share), accent: "#34d399" },
    { label: "总收藏", value: collect.value, sub: coverageText(collect), accent: "#38bdf8" },
    { label: "近 30 日发布", value: recent, sub: "按公开发布时间", accent: "#fbbf24", raw: true },
  ];
  $("kpiStrip").innerHTML = tiles.map((tile) => `
    <div class="kpi" style="--accent:${tile.accent}">
      <div class="kpi-label">${tile.label}</div>
      <div class="kpi-value" data-val="${tile.value ?? ""}" data-raw="${tile.raw ? 1 : 0}">${tile.value === null ? "-" : "0"}</div>
      <div class="kpi-sub">${tile.sub}</div>
    </div>`).join("");
  document.querySelectorAll(".kpi-value").forEach((element) => {
    if (element.dataset.val === "") { element.textContent = "-"; return; }
    const target = Number(element.dataset.val);
    const raw = element.dataset.raw === "1";
    const started = performance.now();
    (function tick(now) {
      const progress = Math.min((now - started) / 700, 1);
      const current = Math.round(target * (1 - Math.pow(1 - progress, 3)));
      element.textContent = raw ? current.toLocaleString("zh-CN") : fmt(current);
      if (progress < 1) requestAnimationFrame(tick);
    })(started);
  });
}

/* ---------- 平台作品分布 ---------- */
function renderDist(videos) {
  const counts = {};
  videos.forEach((video) => { counts[video.platform] = (counts[video.platform] || 0) + 1; });
  const rows = Object.entries(counts).map(([platform, value]) => ({ platform, value }))
    .sort((left, right) => left.value - right.value);
  makeChart("chartDist", {
    tooltip: {
      trigger: "axis", axisPointer: { type: "shadow" },
      formatter: (params) => {
        const row = rows[params[0]?.dataIndex];
        const rate = videos.length ? row.value / videos.length : 0;
        return `<b>${esc(platformLabel(row.platform))}</b><br>${fmtFull(row.value)} 条（${fmtPct(rate)}）`;
      },
    },
    grid: { left: 8, right: 44, top: 8, bottom: 8, containLabel: true },
    xAxis: { type: "value", ...AXIS_STYLE, axisLabel: { ...AXIS_STYLE.axisLabel, formatter: (value) => fmt(value) } },
    yAxis: {
      type: "category", data: rows.map((row) => platformLabel(row.platform)), ...AXIS_STYLE,
      axisLabel: { ...AXIS_STYLE.axisLabel, color: "#dce6f8", interval: 0 },
    },
    series: [{
      type: "bar", barMaxWidth: 18,
      data: rows.map((row) => ({
        value: row.value,
        itemStyle: { color: PLATFORM_COLOR[row.platform] || "#8296bd", borderRadius: [0, 6, 6, 0] },
      })),
      label: { show: true, position: "right", color: "#8296bd", formatter: (params) => fmt(params.value) },
    }],
    graphic: rows.length ? [] : [{ type: "text", left: "center", top: "middle", style: { text: "当前筛选无作品", fill: "#8296bd" } }],
  });
}

/* ---------- 月度趋势 ---------- */
function renderTrend(videos) {
  const months = recentMonths(videos, 24);
  const monthSet = new Set(months);
  const platforms = [...new Set(videos.filter((video) => monthSet.has(monthOf(video.published_at)))
    .map((video) => video.platform))];
  const countByMonth = Object.fromEntries(months.map((month) => [month, 0]));
  const values = Object.fromEntries(platforms.map((platform) => [platform,
    Object.fromEntries(months.map((month) => [month, { sum: 0, available: 0 }]))]));
  videos.forEach((video) => {
    const month = monthOf(video.published_at);
    if (!monthSet.has(month)) return;
    countByMonth[month] += 1;
    const play = num(video.stats?.play);
    if (play !== null) {
      values[video.platform][month].sum += play;
      values[video.platform][month].available += 1;
    }
  });
  const availablePlatforms = platforms.filter((platform) =>
    months.some((month) => values[platform][month].available > 0));
  makeChart("chartTrend", {
    tooltip: { trigger: "axis", valueFormatter: (value) => value === null ? "-" : fmtFull(value) },
    legend: { top: 0, type: "scroll", textStyle: { color: "#8296bd", fontSize: 10 }, itemWidth: 9, itemHeight: 9 },
    grid: { left: 8, right: 8, top: 38, bottom: 4, containLabel: true },
    xAxis: { type: "category", data: months, boundaryGap: false, ...AXIS_STYLE },
    yAxis: [
      { type: "value", name: "播放量", nameTextStyle: { color: "#8296bd" }, ...AXIS_STYLE,
        axisLabel: { ...AXIS_STYLE.axisLabel, formatter: (value) => fmt(value) } },
      { type: "value", name: "发布数", nameTextStyle: { color: "#8296bd" }, ...AXIS_STYLE, splitLine: { show: false } },
    ],
    series: [
      ...availablePlatforms.map((platform) => ({
        name: `${platformLabel(platform)}播放`, type: "line", stack: "play", smooth: true,
        symbol: "none", connectNulls: false, areaStyle: { opacity: .2 }, lineStyle: { width: 1.5 },
        itemStyle: { color: PLATFORM_COLOR[platform] || "#8296bd" }, emphasis: { focus: "series" },
        data: months.map((month) => values[platform][month].available ? values[platform][month].sum : null),
      })),
      {
        name: "发布数", type: "line", yAxisIndex: 1, smooth: true,
        symbol: "circle", symbolSize: 5, lineStyle: { width: 2, type: "dashed" },
        itemStyle: { color: "#f5c34d" }, data: months.map((month) => countByMonth[month]),
      },
    ],
    graphic: months.length ? [] : [{ type: "text", left: "center", top: "middle", style: { text: "当前筛选无趋势数据", fill: "#8296bd" } }],
  });
}

/* ---------- 业务线雷达 ---------- */
function renderRadar(videos) {
  const dimensions = ["play", "like", "comment", "share", "collect"];
  const lines = [...new Set(videos.map((video) => video.business_line).filter(Boolean))];
  const raw = {};
  const coverage = {};
  lines.forEach((line) => {
    const subset = videos.filter((video) => video.business_line === line);
    raw[line] = [];
    coverage[line] = [];
    dimensions.forEach((dimension) => {
      const summary = metricSummary(subset, dimension);
      raw[line].push(summary.available ? summary.value / summary.available : 0);
      coverage[line].push(summary);
    });
  });
  const maxima = dimensions.map((_, index) => Math.max(1, ...lines.map((line) => raw[line][index])));
  makeChart("chartRadar", {
    tooltip: {
      formatter: (params) => {
        const line = params.name;
        if (!raw[line]) return "";
        return `<b>${esc(line)}</b><br>` + dimensions.map((dimension, index) => {
          const summary = coverage[line][index];
          const average = summary.available ? fmt(Math.round(raw[line][index])) : "-";
          return `${METRICS[dimension]}均值: ${average}（${summary.available}/${summary.total} 有值）`;
        }).join("<br>");
      },
    },
    legend: { bottom: 0, textStyle: { color: "#8296bd", fontSize: 10 }, itemWidth: 9, itemHeight: 9 },
    radar: {
      indicator: dimensions.map((dimension) => ({ name: METRICS[dimension], max: 100 })),
      center: ["50%", "48%"], radius: "61%", axisName: { color: "#8296bd", fontSize: 10 },
      splitLine: { lineStyle: { color: "rgba(44,65,112,.5)" } },
      splitArea: { areaStyle: { color: ["transparent", "rgba(44,65,112,.12)"] } },
      axisLine: { lineStyle: { color: "#2c4170" } },
    },
    series: [{
      type: "radar",
      data: lines.map((line) => ({
        name: line,
        value: dimensions.map((_, index) => Number((raw[line][index] / maxima[index] * 100).toFixed(1))),
        itemStyle: { color: LINE_COLOR[line] || "#38d9f5" },
        areaStyle: { opacity: .17 }, lineStyle: { width: 2 },
      })),
    }],
  });
}

/* ---------- 账号指标对比 ---------- */
function accountMetric(account, metric, pool) {
  const subset = pool.filter((video) => video.account_key === account.account_key);
  if (metric === "followers") return { value: num(account.followers), hasData: num(account.followers) !== null, available: num(account.followers) !== null ? 1 : 0, total: 1 };
  if (metric === "videos") return { value: subset.length, hasData: true, available: subset.length, total: subset.length };
  const summary = metricSummary(subset, metric);
  return { value: summary.value, hasData: summary.available > 0, available: summary.available, total: summary.total };
}

function renderAcctCompare(associations) {
  const metric = state.acctMetric;
  const rows = visibleAccounts().map((account) => ({ account, ...accountMetric(account, metric, associations) }))
    .sort((left, right) => (left.value || 0) - (right.value || 0));
  const label = metric === "followers" ? "粉丝数" : metric === "videos" ? "作品数" : METRICS[metric];
  makeChart("chartAcct", {
    tooltip: {
      trigger: "axis", axisPointer: { type: "shadow" },
      formatter: (params) => {
        const row = rows[params[0]?.dataIndex];
        if (!row) return "";
        const coverage = ["followers", "videos"].includes(metric) ? "" : `<br>公开值覆盖: ${row.available}/${row.total}`;
        return `<b>${esc(row.account.account_name)}</b>（${esc(platformLabel(row.account.platform))} · ${esc(row.account.business_line)}）<br>` +
          `${label}: ${row.hasData ? fmtFull(row.value) : "-"}${coverage}`;
      },
    },
    grid: { left: 8, right: 52, top: 10, bottom: 4, containLabel: true },
    xAxis: { type: "value", ...AXIS_STYLE, axisLabel: { ...AXIS_STYLE.axisLabel, formatter: (value) => fmt(value) } },
    yAxis: {
      type: "category", data: rows.map((row) => row.account.account_key), ...AXIS_STYLE,
      axisLabel: accountAxisLabel(rows),
    },
    series: [{
      type: "bar", barWidth: 13,
      data: rows.map((row) => ({
        value: row.value || 0,
        itemStyle: {
          opacity: row.hasData ? .9 : .22,
          borderRadius: [0, 6, 6, 0],
          color: PLATFORM_COLOR[row.account.platform] || "#38d9f5",
        },
      })),
      label: {
        show: true, position: "right", color: "#8296bd", fontSize: 10,
        formatter: (params) => rows[params.dataIndex]?.hasData ? fmt(params.value) : "-",
      },
    }],
  });
}

/* ---------- 账号概览 ---------- */
function renderAcctTable(associations) {
  const rows = visibleAccounts().map((account) => {
    const current = associations.filter((video) => video.account_key === account.account_key);
    const lifetime = DATA.videos.filter((video) => video.account_key === account.account_key);
    return {
      account,
      count: current.length,
      total: lifetime.length,
      play: metricSummary(current, "play"),
      like: metricSummary(current, "like"),
      profileUrl: safeUrl(account.profile_url),
    };
  }).sort((left, right) => (right.count > 0 ? 1 : 0) - (left.count > 0 ? 1 : 0) ||
    ((right.play.value || 0) + (right.like.value || 0)) - ((left.play.value || 0) + (left.like.value || 0)));
  $("acctTable").innerHTML = `
    <thead><tr>
      <th>账号</th><th>业务线 / 状态</th><th class="num">粉丝</th>
      <th class="num">当前 / 累计</th><th class="num">播放</th><th class="num">点赞</th>
    </tr></thead>
    <tbody>${rows.map(({ account, count, total, play, like, profileUrl }) => {
      const status = account.status || "error";
      const note = account.error || account.coverage_note || `最近成功 ${shortStamp(account.last_success_at || DATA.updated_at)}`;
      return `
        <tr>
          <td>${profileUrl
            ? `<a class="acct-name acct-profile-link" href="${profileUrl}" target="_blank" rel="noopener noreferrer">${esc(account.account_name)}</a>`
            : `<span class="acct-name">${esc(account.account_name)}</span>`}
            <span class="tag tag-${esc(account.platform)}" style="margin-left:4px">${esc(account.platform_label || platformLabel(account.platform))}</span>
            <div class="coverage-note" title="${esc(note)}">${esc(note)}</div>
          </td>
          <td><span class="tag tag-line">${esc(account.business_line)}</span>
            <div class="coverage-note"><span class="status-pill status-${esc(status)}">${esc(STATUS_LABEL[status] || status)}</span></div>
          </td>
          <td class="num">${fmt(account.followers)}</td>
          <td class="num">${count} / ${num(account.total_videos) ?? total}</td>
          <td class="num" title="${esc(coverageText(play))}">${fmt(play.value)}</td>
          <td class="num" title="${esc(coverageText(like))}">${fmt(like.value)}</td>
        </tr>`;
    }).join("")}</tbody>`;
}

/* ---------- 视频热榜 ---------- */
function renderTopList(videos) {
  const metric = state.topMetric;
  const top = [...videos].filter((video) => num(video.stats?.[metric]) !== null)
    .sort((left, right) => right.stats[metric] - left.stats[metric]).slice(0, 10);
  const maximum = top.length ? top[0].stats[metric] : 1;
  $("topList").innerHTML = top.length ? top.map((video, index) => {
    const url = safeUrl(video.url);
    const shared = video._associationCount > 1 ? `<span class="tag tag-line">共享 ${video._associationCount} 账号</span>` : "";
    return `
      <div class="rank-item">
        <span class="rank-no">${index + 1}</span>
        <div class="rank-main">
          <div class="rank-title">${url ? `<a href="${url}" target="_blank" rel="noopener">${esc(video.title)}</a>` : esc(video.title)}</div>
          <div class="rank-meta">
            <span class="tag tag-${esc(video.platform)}">${esc(video.platform_label || platformLabel(video.platform))}</span>
            <span class="tag tag-line">${esc(video.account_name)}</span>${shared}
            <span class="rank-bar"><i style="width:${Math.max(video.stats[metric] / maximum * 100, 2)}%"></i></span>
          </div>
        </div>
        <span class="rank-val">${fmt(video.stats[metric])}</span>
      </div>`;
  }).join("") : `<div class="muted-val" style="padding:28px 8px;text-align:center">当前筛选条件下无「${METRICS[metric]}」公开数据</div>`;
}

function renderTabs(elementId, stateKey, options) {
  $(elementId).innerHTML = options.map(([value, label]) =>
    `<button data-value="${value}" class="${state[stateKey] === value ? "active" : ""}">${label}</button>`).join("");
  $(elementId).querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => {
      state[stateKey] = button.dataset.value;
      renderTabs(elementId, stateKey, options);
      const associations = filteredVideos();
      if (stateKey === "topMetric") renderTopList(uniqueVideos(associations));
      if (stateKey === "acctMetric") renderAcctCompare(associations);
    });
  });
}

/* ---------- 明细表 ---------- */
const DETAIL_COLS = [
  ["title", "标题"], ["platform", "平台"], ["account", "账号"],
  ["published_at", "发布时间"], ["play", "播放"], ["like", "点赞"],
  ["comment", "评论/回复"], ["share", "分享"], ["collect", "收藏"], ["download", "下载"],
];

function detailValue(video, key) {
  if (key === "title") return video.title || "";
  if (key === "platform") return video.platform_label || platformLabel(video.platform);
  if (key === "account") return video.account_name || "";
  if (key === "published_at") return video.published_at || "";
  if (key in (video.stats || {})) return num(video.stats[key]);
  return null;
}

function compareValues(left, right) {
  if (left === null && right === null) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  if (typeof left === "string" || typeof right === "string") return String(left).localeCompare(String(right), "zh-CN");
  return left > right ? 1 : left < right ? -1 : 0;
}

function renderDetail(videos) {
  const keyword = $("fSearch").value.trim().toLowerCase();
  let rows = videos;
  if (keyword) rows = rows.filter((video) => `${video.title || ""} ${video.account_name || ""}`.toLowerCase().includes(keyword));
  rows = [...rows].sort((left, right) =>
    compareValues(detailValue(left, state.sortKey), detailValue(right, state.sortKey)) * state.sortDir);
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages);
  const pageRows = rows.slice((state.page - 1) * PAGE_SIZE, state.page * PAGE_SIZE);

  $("detailTable").innerHTML = `
    <thead><tr>${DETAIL_COLS.map(([key, label]) => {
      const numeric = ["play", "like", "comment", "share", "collect", "download"].includes(key);
      const arrow = state.sortKey === key ? (state.sortDir === 1 ? "▲" : "▼") : "";
      return `<th data-key="${key}" class="${numeric ? "num" : ""}">${label}<span class="arrow">${arrow}</span></th>`;
    }).join("")}</tr></thead>
    <tbody>${pageRows.map((video) => {
      const url = safeUrl(video.url);
      const shared = video._associationCount > 1 ? `<span class="tag tag-line" style="margin-left:4px">共享 ${video._associationCount}</span>` : "";
      return `
        <tr>
          <td class="cell-title" title="${esc(video.title)}">${url ? `<a href="${url}" target="_blank" rel="noopener">${esc(video.title)}</a>` : esc(video.title)}</td>
          <td><span class="tag tag-${esc(video.platform)}">${esc(video.platform_label || platformLabel(video.platform))}</span></td>
          <td>${esc(video.account_name)}${shared}<span class="tag tag-line" style="margin-left:4px">${esc(video.business_line)}</span></td>
          <td>${dayOf(video.published_at)}</td>
          ${["play", "like", "comment", "share", "collect", "download"].map((key) =>
            `<td class="num ${num(video.stats?.[key]) === null ? "muted-val" : ""}">${fmt(video.stats?.[key])}</td>`).join("")}
        </tr>`;
    }).join("") || `<tr><td colspan="10" class="muted-val" style="text-align:center;padding:24px">无匹配数据</td></tr>`}</tbody>`;

  $("detailTable").querySelectorAll("th").forEach((header) => {
    header.addEventListener("click", () => {
      const key = header.dataset.key;
      if (state.sortKey === key) state.sortDir *= -1;
      else { state.sortKey = key; state.sortDir = key === "title" || key === "platform" || key === "account" ? 1 : -1; }
      renderDetail(videos);
    });
  });

  $("pager").innerHTML = `
    <span>共 ${rows.length} 条 · ${state.page}/${pages} 页</span>
    <button id="pgPrev" ${state.page <= 1 ? "disabled" : ""}>上一页</button>
    <button id="pgNext" ${state.page >= pages ? "disabled" : ""}>下一页</button>`;
  $("pgPrev").addEventListener("click", () => { state.page -= 1; renderDetail(videos); });
  $("pgNext").addEventListener("click", () => { state.page += 1; renderDetail(videos); });
}

/* ---------- 筛选器 ---------- */
function renderFilterSummary(associations, videos) {
  const accounts = visibleAccounts().length;
  const shared = Math.max(0, associations.length - videos.length);
  $("filterSummary").textContent = `${PERIOD_LABEL[state.period]} · ${accounts} 个账号 · ${videos.length} 条作品${shared ? `（去重 ${shared} 条关联）` : ""}`;
}

function initFilters() {
  const platforms = [...new Set(DATA.accounts.map((account) => account.platform))];
  const lines = [...new Set(DATA.accounts.map((account) => account.business_line))];
  $("fPlatform").innerHTML = `<option value="all">全部平台</option>` + platforms.map((platform) =>
    `<option value="${esc(platform)}">${esc(platformLabel(platform))}</option>`).join("");
  $("fLine").innerHTML = `<option value="all">全部业务线</option>` + lines.map((line) =>
    `<option value="${esc(line)}">${esc(line)}</option>`).join("");

  const rebuildAccounts = () => {
    const accounts = DATA.accounts.filter((account) =>
      (state.platform === "all" || account.platform === state.platform) &&
      (state.line === "all" || account.business_line === state.line));
    $("fAccount").innerHTML = `<option value="all">全部账号</option>` + accounts.map((account) =>
      `<option value="${esc(account.account_key)}">${esc(account.platform_label || platformLabel(account.platform))} · ${esc(account.account_name)}</option>`).join("");
    $("fAccount").value = state.account;
  };

  $("fPlatform").addEventListener("change", (event) => {
    state.platform = event.target.value; state.account = "all"; state.page = 1;
    rebuildAccounts(); refresh();
  });
  $("fLine").addEventListener("change", (event) => {
    state.line = event.target.value; state.account = "all"; state.page = 1;
    rebuildAccounts(); refresh();
  });
  $("fAccount").addEventListener("change", (event) => {
    state.account = event.target.value; state.page = 1; refresh();
  });
  $("fPeriod").addEventListener("change", (event) => {
    state.period = event.target.value; state.page = 1; refresh();
  });
  $("resetFilters").addEventListener("click", () => {
    state.platform = "all"; state.line = "all"; state.account = "all"; state.period = "all"; state.page = 1;
    $("fPlatform").value = "all"; $("fLine").value = "all"; $("fPeriod").value = "all";
    $("fSearch").value = "";
    rebuildAccounts(); refresh();
  });
  let debounce;
  $("fSearch").addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => { state.page = 1; renderDetail(uniqueVideos(filteredVideos())); }, 180);
  });
}

/* ---------- 主流程 ---------- */
function refresh() {
  const associations = filteredVideos();
  const videos = uniqueVideos(associations);
  renderFilterSummary(associations, videos);
  renderKPI(videos, associations);
  renderDist(videos);
  renderTrend(videos);
  renderRadar(videos);
  renderAcctCompare(associations);
  renderAcctTable(associations);
  renderTopList(videos);
  renderDetail(videos);
  requestAnimationFrame(() => Object.values(charts).forEach((chart) => chart.resize()));
}

function loadScript(path) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = `${path}?_=${Date.now()}`;
    script.onload = resolve;
    script.onerror = () => reject(new Error(`数据脚本加载失败: ${path}`));
    document.head.appendChild(script);
  });
}

async function loadDashboardData() {
  if (location.protocol !== "file:") {
    const response = await fetch(`data/dashboard_data.json?_=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`数据文件加载失败: ${response.status}`);
    return response.json();
  }
  if (!window.__DASHBOARD_DATA__) await loadScript("data/dashboard_data.js");
  if (!window.__DASHBOARD_DATA__) throw new Error("本地数据脚本未提供 __DASHBOARD_DATA__");
  return window.__DASHBOARD_DATA__;
}

async function main() {
  DATA = await loadDashboardData();
  DATA.accounts = Array.isArray(DATA.accounts) ? DATA.accounts : [];
  DATA.videos = Array.isArray(DATA.videos) ? DATA.videos : [];

  const platformCount = new Set(DATA.accounts.map((account) => account.platform)).size;
  $("subtitle").textContent = `${DATA.accounts.length} 个账号 · ${platformCount} 个平台`;
  const badge = $("sourceBadge");
  const freshness = ageHours(DATA.updated_at);
  if (DATA.source === "mock") {
    badge.textContent = "演示数据";
    badge.classList.add("mock");
  } else if (freshness === null || freshness > 48) {
    badge.textContent = "数据较旧";
    badge.classList.add("stale");
  } else {
    badge.textContent = "实时数据";
    if (DATA.accounts.some((account) => account.status !== "ok")) badge.classList.add("partial");
  }
  $("updatedAt").textContent = shortStamp(DATA.updated_at);
  const updateClock = () => { $("clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false }); };
  updateClock();
  setInterval(updateClock, 1000);

  renderHealth();
  renderNotice();
  initFilters();
  renderTabs("topTabs", "topMetric", [
    ["play", "播放"], ["like", "点赞"], ["comment", "评论"], ["share", "分享"], ["collect", "收藏"],
  ]);
  renderTabs("acctTabs", "acctMetric", [
    ["play", "播放"], ["like", "点赞"], ["share", "分享"], ["collect", "收藏"], ["videos", "作品"], ["followers", "粉丝"],
  ]);
  refresh();
  window.addEventListener("resize", () => Object.values(charts).forEach((chart) => chart.resize()));
}

main().catch((error) => {
  document.body.innerHTML = `
    <div style="padding:60px;text-align:center;color:#8296bd">
      <h2 style="color:#dce6f8;margin-bottom:12px">数据加载失败</h2>
      <p>${esc(error.message)}</p>
      <p style="margin-top:8px">请先运行 <code>python3 pipeline/fetch_data.py</code> 生成数据，并用本地服务打开页面。</p>
    </div>`;
});
