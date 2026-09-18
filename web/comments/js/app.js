/* 评论互动时间线 · 上线后新增评论与官方回复 */
"use strict";

const PAGE_SIZE = 12;
const DAY_MS = 86400000;
const PLATFORM_LABEL = {
  douyin: "抖音", bilibili: "B站", wechat_channels: "视频号", xiaohongshu: "小红书",
};
const TASK_LABEL = {
  video_data: "视频数据", article_data: "图文数据",
  comment_discovery: "新内容发现", comment_roots: "一级评论",
  comment_replies: "条件官方回复",
};

const state = { platform: "all", account: "all", status: "all", period: "all", page: 1 };
let DATA = null;
let THREADS = [];
let usageChart = null;

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

function safeUrl(value) {
  const url = String(value || "").trim();
  return /^https?:\/\//i.test(url) ? esc(url) : "";
}

function parseTime(value) {
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : null;
}

function formatTime(value) {
  const parsed = parseTime(value);
  if (parsed === null) return "平台时间未提供";
  return new Date(parsed).toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function shortTime(value) {
  const parsed = parseTime(value);
  if (parsed === null) return "--";
  return new Date(parsed).toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "--";
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} 分钟`;
  if (seconds < DAY_MS / 1000) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.round((seconds % 3600) / 60);
    return minutes ? `${hours} 小时 ${minutes} 分` : `${hours} 小时`;
  }
  const days = Math.floor(seconds / 86400);
  const hours = Math.round((seconds % 86400) / 3600);
  return hours ? `${days} 天 ${hours} 小时` : `${days} 天`;
}

function buildThreads(events) {
  const roots = events.filter((event) => event.event_type === "comment");
  const replies = new Map();
  events.filter((event) => event.event_type === "official_reply").forEach((reply) => {
    const key = `${reply.platform}:${reply.parent_comment_id}`;
    if (!replies.has(key)) replies.set(key, []);
    replies.get(key).push(reply);
  });
  return roots.map((comment) => {
    const key = `${comment.platform}:${comment.comment_id}`;
    const officialReplies = (replies.get(key) || []).sort((left, right) =>
      (parseTime(left.platform_created_at) || 0) - (parseTime(right.platform_created_at) || 0));
    return { comment, replies: officialReplies, replied: officialReplies.length > 0 };
  }).sort((left, right) =>
    (parseTime(right.comment.platform_created_at) || 0) - (parseTime(left.comment.platform_created_at) || 0));
}

function withinPeriod(thread) {
  if (state.period === "all") return true;
  const created = parseTime(thread.comment.platform_created_at);
  if (created === null) return false;
  const now = new Date();
  if (state.period === "1") {
    const date = new Date(created);
    return date.getFullYear() === now.getFullYear()
      && date.getMonth() === now.getMonth() && date.getDate() === now.getDate();
  }
  return created >= Date.now() - Number(state.period) * DAY_MS;
}

function filteredThreads() {
  const keyword = $("fSearch").value.trim().toLowerCase();
  return THREADS.filter((thread) => {
    const comment = thread.comment;
    const searchable = [comment.content_title, comment.author, comment.content,
      comment.account_name, ...thread.replies.flatMap((reply) => [reply.author, reply.content])]
      .join(" ").toLowerCase();
    return (state.platform === "all" || comment.platform === state.platform)
      && (state.account === "all" || comment.account_key === state.account)
      && (state.status === "all" || (state.status === "replied") === thread.replied)
      && withinPeriod(thread)
      && (!keyword || searchable.includes(keyword));
  });
}

function threadMetrics(threads) {
  const replied = threads.filter((thread) => thread.replied);
  const responseValues = replied.map((thread) => thread.replies[0]?.response_seconds)
    .filter((value) => Number.isFinite(value));
  const sorted = [...responseValues].sort((left, right) => left - right);
  const median = sorted.length
    ? sorted.length % 2 ? sorted[(sorted.length - 1) / 2]
      : (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2
    : null;
  const p90 = sorted.length ? sorted[Math.max(0, Math.ceil(sorted.length * .9) - 1)] : null;
  return {
    comments: threads.length,
    replies: threads.reduce((total, thread) => total + thread.replies.length, 0),
    replied: replied.length,
    pending: threads.length - replied.length,
    rate: threads.length ? replied.length / threads.length : 0,
    median,
    p90,
  };
}

function dailyLimitLabel(value) {
  if (value == null || value === "" || !Number.isFinite(Number(value))) return "日上限未知";
  return Number(value) > 0 ? `上限 ${Number(value)} 次` : "未设每日上限";
}

function renderKpis(threads) {
  const metrics = threadMetrics(threads);
  const usage = DATA.api_usage || {};
  const cards = [
    ["新增评论", metrics.comments, "上线后已记录", "#38d9f5"],
    ["已官方回复", metrics.replied, `${metrics.replies} 条官方回复`, "#34d399"],
    ["待回复", metrics.pending, "按当前筛选", "#f5c34d"],
    ["官方回复率", `${(metrics.rate * 100).toFixed(0)}%`, `${metrics.replied}/${metrics.comments || 0}`, "#a78bfa"],
    ["响应中位数", formatDuration(metrics.median), `${Number.isFinite(metrics.p90) ? `P90 ${formatDuration(metrics.p90)}` : "暂无有效样本"}`, "#60a5fa"],
    ["今日采集操作", usage.used ?? 0, dailyLimitLabel(usage.limit), "#f87171"],
  ];
  $("kpiStrip").innerHTML = cards.map(([label, value, sub, color]) => `
    <article class="kpi" style="--accent:${color}">
      <div class="kpi-label">${esc(label)}</div>
      <div class="kpi-value">${esc(value)}</div>
      <div class="kpi-sub">${esc(sub)}</div>
    </article>`).join("");
}

function renderBudget() {
  const usage = DATA.api_usage || {};
  const used = Number(usage.used) || 0;
  const limit = Number(usage.limit) || 0;
  const ratio = limit ? Math.min(1, used / limit) : 0;
  const level = ratio >= .9 ? "error" : ratio >= .7 ? "warn" : "";
  const tasks = Object.entries(usage.by_task || {}).sort((left, right) => right[1] - left[1]);
  $("apiBudget").innerHTML = `
    <div class="budget-number"><strong>${used}</strong><span>${esc(dailyLimitLabel(usage.limit))}</span></div>
    ${limit > 0 ? `<div class="budget-meter ${level}" style="--usage:${(ratio * 100).toFixed(1)}%"><i></i></div>` : ""}
    <div class="usage-breakdown">${tasks.map(([task, count]) => `
      <div class="usage-row"><span>${esc(TASK_LABEL[task] || task)}</span><strong>${count}</strong></div>`).join("")
      || `<div class="usage-row"><span>今日尚无 平台采集 调用</span><strong>0</strong></div>`}</div>`;
}

function renderUsageChart() {
  if (!usageChart) usageChart = echarts.init($("chartUsage"));
  const history = (DATA.api_usage?.history || []).slice(-14);
  const dates = history.map((day) => String(day.date || "").slice(5));
  const values = history.map((day) => Number(day.used) || 0);
  const limits = history.map((day) => Number(day.limit) > 0 ? Number(day.limit) : null);
  usageChart.setOption({
    animationDuration: 350,
    aria: { enabled: true, decal: { show: false } },
    tooltip: { trigger: "axis", backgroundColor: "#111c33", borderColor: "#2c4170", textStyle: { color: "#dce6f8" } },
    grid: { left: 46, right: 18, top: 26, bottom: 30 },
    xAxis: { type: "category", data: dates, axisLine: { lineStyle: { color: "#2c4170" } }, axisLabel: { color: "#8296bd" } },
    yAxis: { type: "value", minInterval: 1, axisLine: { show: false }, axisLabel: { color: "#8296bd" }, splitLine: { lineStyle: { color: "rgba(44,65,112,.35)" } } },
    series: [
      { name: "实际调用", type: "bar", data: values, barMaxWidth: 26, itemStyle: { color: "#38d9f5", borderRadius: [3, 3, 0, 0] } },
      { name: "每日上限", type: "line", data: limits, symbol: "none", lineStyle: { color: "#f87171", type: "dashed", width: 1.5 } },
    ],
    graphic: history.length ? [] : [{ type: "text", left: "center", top: "middle", style: { text: "暂无 采集操作记录", fill: "#8296bd" } }],
  }, true);
}

function renderStatus() {
  const scan = DATA.last_scan || {};
  const status = $("timelineStatus");
  const started = DATA.timeline_started_at ? formatTime(DATA.timeline_started_at) : "待首次正式检查";
  if (scan.budget_exhausted) status.className = "timeline-status error";
  else if (scan.complete === false) status.className = "timeline-status warn";
  else status.className = "timeline-status";
  const usageStarted = DATA.api_usage?.tracking_started_at;
  status.innerHTML = `<strong>时间线起点：${esc(started)}</strong><span>·</span>
    <span>上轮检查 ${scan.detail_contents ?? 0} 条内容，一级 ${scan.root_pages ?? 0} 页，条件回复 ${scan.reply_pages ?? 0} 页</span>
    ${usageStarted ? `<span>· 采集记账自 ${esc(shortTime(usageStarted))} 开始</span>` : ""}`;
}

function renderTimeline(threads) {
  const pages = Math.max(1, Math.ceil(threads.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages);
  const pageRows = threads.slice((state.page - 1) * PAGE_SIZE, state.page * PAGE_SIZE);
  $("timelineSummary").textContent = `${threads.length} 条评论 · ${threads.filter((thread) => thread.replied).length} 条已官方回复`;
  $("timelineList").innerHTML = pageRows.map((thread) => {
    const comment = thread.comment;
    const url = safeUrl(comment.content_url);
    return `<article class="thread-card">
      <div class="thread-head">
        <time>${esc(formatTime(comment.platform_created_at))}</time>
        <span class="tag tag-${esc(comment.platform)}">${esc(comment.platform_label || PLATFORM_LABEL[comment.platform] || comment.platform)}</span>
        <span class="tag tag-line">${esc(comment.account_name)}</span>
        ${url ? `<a class="thread-title" href="${url}" target="_blank" rel="noopener" title="${esc(comment.content_title)}">${esc(comment.content_title || "未命名内容")}</a>`
          : `<span class="thread-title">${esc(comment.content_title || "未命名内容")}</span>`}
        <span class="reply-status ${thread.replied ? "replied" : ""}">${thread.replied ? "已官方回复" : "待回复"}</span>
      </div>
      <div class="event-block">
        <div class="event-meta"><strong>${esc(comment.author)}</strong>评论时间<br>${esc(shortTime(comment.platform_created_at))}<br>发现 ${esc(shortTime(comment.first_seen_at))}</div>
        <div class="event-content">${esc(comment.content || "(无文字内容)")}</div>
      </div>
      ${thread.replies.length ? `<div class="official-replies">${thread.replies.map((reply) => `
        <div class="official-reply">
          <strong>${esc(reply.author)}</strong>
          <time>${esc(formatTime(reply.platform_created_at))}</time>
          <span class="response-chip">响应 ${esc(formatDuration(reply.response_seconds))}</span>
          <p>${esc(reply.content || "(无文字内容)")}</p>
        </div>`).join("")}</div>` : `<div class="pending-note">尚未检测到该账号的官方回复</div>`}
    </article>`;
  }).join("") || `<div class="timeline-empty"><strong>当前筛选下暂无互动</strong><span>时间线只记录功能上线后且具有平台时间戳的新评论。</span></div>`;
  $("pager").innerHTML = `<span>共 ${threads.length} 条 · ${state.page}/${pages} 页</span>
    <button id="pgPrev" ${state.page <= 1 ? "disabled" : ""}>上一页</button>
    <button id="pgNext" ${state.page >= pages ? "disabled" : ""}>下一页</button>`;
  $("pgPrev").addEventListener("click", () => { state.page -= 1; render(); });
  $("pgNext").addEventListener("click", () => { state.page += 1; render(); });
}

function csvCell(value) {
  const text = value === null || value === undefined ? "" : String(value).replace(/\r\n?/g, "\n");
  const safe = /^[=+\-@\t]/.test(text) ? `'${text}` : text;
  return `"${safe.replace(/"/g, '""')}"`;
}

function exportData() {
  const threads = filteredThreads();
  if (!threads.length) return;
  const header = ["平台", "业务线", "账号", "作品", "评论人", "评论内容", "评论时间",
    "首次发现时间", "状态", "官方回复人", "官方回复内容", "官方回复时间", "响应耗时(秒)", "链接"];
  const rows = threads.flatMap((thread) => {
    const replies = thread.replies.length ? thread.replies : [null];
    return replies.map((reply) => [
      thread.comment.platform_label, thread.comment.business_line, thread.comment.account_name,
      thread.comment.content_title, thread.comment.author, thread.comment.content,
      thread.comment.platform_created_at, thread.comment.first_seen_at,
      thread.replied ? "已官方回复" : "待回复", reply?.author || "", reply?.content || "",
      reply?.platform_created_at || "", reply?.response_seconds ?? "", thread.comment.content_url || "",
    ]);
  });
  const csv = [header, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob(["\ufeff", csv], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  const now = new Date();
  const date = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  link.href = url;
  link.download = `评论官方回复时间线_${date}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
  const button = $("exportData");
  button.textContent = `已导出 ${rows.length} 行`;
  button.disabled = true;
  setTimeout(() => { button.textContent = "导出数据"; button.disabled = false; }, 1500);
}

function populateFilters() {
  const platforms = [...new Set(THREADS.map((thread) => thread.comment.platform))];
  $("fPlatform").innerHTML = `<option value="all">全部平台</option>` + platforms.map((platform) =>
    `<option value="${esc(platform)}">${esc(PLATFORM_LABEL[platform] || platform)}</option>`).join("");

  const rebuildAccounts = () => {
    const accounts = new Map();
    THREADS.forEach(({ comment }) => {
      if (state.platform === "all" || comment.platform === state.platform) {
        accounts.set(comment.account_key, comment.account_name);
      }
    });
    $("fAccount").innerHTML = `<option value="all">全部账号</option>` + [...accounts].map(([key, name]) =>
      `<option value="${esc(key)}">${esc(name)}</option>`).join("");
    if (!accounts.has(state.account)) state.account = "all";
    $("fAccount").value = state.account;
  };

  $("fPlatform").addEventListener("change", (event) => {
    state.platform = event.target.value; state.account = "all"; state.page = 1;
    rebuildAccounts(); render();
  });
  $("fAccount").addEventListener("change", (event) => { state.account = event.target.value; state.page = 1; render(); });
  $("fStatus").addEventListener("change", (event) => { state.status = event.target.value; state.page = 1; render(); });
  $("fPeriod").addEventListener("change", (event) => { state.period = event.target.value; state.page = 1; render(); });
  let debounce;
  $("fSearch").addEventListener("input", () => {
    clearTimeout(debounce); debounce = setTimeout(() => { state.page = 1; render(); }, 160);
  });
  $("resetFilters").addEventListener("click", () => {
    Object.assign(state, { platform: "all", account: "all", status: "all", period: "all", page: 1 });
    ["fPlatform", "fStatus", "fPeriod"].forEach((id) => { $(id).value = "all"; });
    $("fSearch").value = "";
    rebuildAccounts(); render();
  });
  $("exportData").addEventListener("click", exportData);
  rebuildAccounts();
}

function render() {
  const threads = filteredThreads();
  const metrics = threadMetrics(threads);
  $("filterSummary").textContent = `${threads.length} 条评论 · ${metrics.replied} 条已回复 · ${metrics.pending} 条待回复`;
  renderKpis(threads);
  renderTimeline(threads);
  requestAnimationFrame(() => usageChart?.resize());
}

async function loadData() {
  const response = await fetch(`data/comment_timeline.json?_=${Date.now()}`, { cache: "no-store" });
  if (response.status === 404) {
    return { events: [], stats: {}, api_usage: {}, last_scan: {}, provenance: {} };
  }
  if (!response.ok) throw new Error(`时间线数据加载失败: ${response.status}`);
  return response.json();
}

async function main() {
  DATA = await loadData();
  DATA.events = Array.isArray(DATA.events) ? DATA.events : [];
  THREADS = buildThreads(DATA.events);
  $("updatedAt").textContent = DATA.updated_at ? formatTime(DATA.updated_at) : "待初始化";
  const badge = $("sourceBadge");
  badge.textContent = DATA.timeline_started_at ? "上线后数据" : "待首次检查";
  if (!DATA.timeline_started_at) badge.classList.add("partial");
  const updateClock = () => { $("clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false }); };
  updateClock();
  setInterval(updateClock, 1000);
  renderStatus();
  renderBudget();
  renderUsageChart();
  populateFilters();
  render();
  window.addEventListener("resize", () => usageChart?.resize());
}

main().catch((error) => {
  document.body.innerHTML = `<div style="padding:60px;text-align:center;color:#8296bd">
    <h2 style="color:#dce6f8;margin-bottom:12px">时间线加载失败</h2><p>${esc(error.message)}</p></div>`;
});
