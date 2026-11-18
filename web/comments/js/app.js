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
let refreshing = false;

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
    ["新增评论", metrics.comments, "上线后已记录", "#0071e3"],
    ["已官方回复", metrics.replied, `${metrics.replies} 条官方回复`, "#1f9d4c"],
    ["未检测到官方回复", metrics.pending, "以最近一次检查为准", "#c76a00"],
    ["官方回复率", metrics.comments ? `${(metrics.rate * 100).toFixed(0)}%` : "—", `已回复 ${metrics.replied} / ${metrics.comments} 条`, "#5e5ce6"],
    ["响应中位数", formatDuration(metrics.median), Number.isFinite(metrics.p90) ? `P90 ${formatDuration(metrics.p90)}` : "暂无有效样本", "#0a66d1"],
    ["今日采集操作", usage.used ?? "—", dailyLimitLabel(usage.limit), "#1d1d1f"],
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
  const used = usage.used == null ? null : Number(usage.used);
  const limit = Number(usage.limit) || 0;
  const ratio = limit > 0 && Number.isFinite(used) ? Math.min(1, used / limit) : null;
  const level = ratio >= .9 ? "error" : ratio >= .7 ? "warn" : "";
  const tasks = Object.entries(usage.by_task || {}).sort((left, right) => right[1] - left[1]);
  $("apiBudget").innerHTML = `
    <div class="budget-number"><strong>${Number.isFinite(used) ? used : "—"}</strong><span>${esc(dailyLimitLabel(usage.limit))}</span></div>
    ${ratio !== null ? `<div class="budget-meter ${level}" style="--usage:${(ratio * 100).toFixed(1)}%"><i></i></div>` : ""}
    <div class="usage-breakdown">${tasks.map(([task, count]) => `
      <div class="usage-row"><span>${esc(TASK_LABEL[task] || task)}</span><strong>${esc(count)}</strong></div>`).join("")
      || `<p class="usage-empty-note">暂无分项采集记录</p>`}</div>`;
}

function renderUsageChart() {
  const container = $("chartUsage");
  const history = (DATA.api_usage?.history || []).slice(-14);
  container.classList.toggle("usage-empty", history.length === 0);
  if (!history.length) {
    usageChart?.dispose();
    usageChart = null;
    container.textContent = "暂无采集操作记录";
    return;
  }
  if (!usageChart) {
    container.textContent = "";
    usageChart = echarts.init(container);
  }
  const dates = history.map((day) => String(day.date || "").slice(5));
  const values = history.map((day) => Number(day.used) || 0);
  const limits = history.map((day) => Number(day.limit) > 0 ? Number(day.limit) : null);
  usageChart.setOption({
    animation: !window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    animationDuration: 250,
    aria: { enabled: true, decal: { show: false } },
    tooltip: { trigger: "axis", confine: true, backgroundColor: "rgba(29,29,31,.94)", borderColor: "#1d1d1f", textStyle: { color: "#fff" } },
    legend: { top: 3, right: 6, icon: "roundRect", itemWidth: 13, itemHeight: 4, textStyle: { color: "#626267", fontSize: 11 } },
    grid: { left: 6, right: 12, top: 40, bottom: 12, containLabel: true },
    xAxis: { type: "category", data: dates, axisLine: { lineStyle: { color: "#d2d2d7" } }, axisLabel: { color: "#6e6e73" } },
    yAxis: { type: "value", minInterval: 1, axisLine: { show: false }, axisLabel: { color: "#6e6e73" }, splitLine: { lineStyle: { color: "#e8e8ed", type: "dashed" } } },
    series: [
      { name: "采集次数", type: "bar", data: values, barMaxWidth: 26, itemStyle: { color: "#0071e3", borderRadius: [3, 3, 0, 0] } },
      { name: "每日上限", type: "line", data: limits, symbol: "none", itemStyle: { color: "#b16000" }, lineStyle: { color: "#b16000", type: "dashed", width: 1.5 } },
    ],
  }, true);
}

function renderStatus() {
  const scan = DATA.last_scan || {};
  const status = $("timelineStatus");
  const started = DATA.timeline_started_at ? formatTime(DATA.timeline_started_at) : "待首次正式检查";
  if (scan.budget_exhausted) status.className = "timeline-status error";
  else if (!DATA.timeline_started_at || scan.complete === false) status.className = "timeline-status warn";
  else status.className = "timeline-status";
  const scanWarning = scan.budget_exhausted ? "采集已达上限" : scan.complete === false ? "上轮检查未完成" : "";
  const usageStarted = DATA.api_usage?.tracking_started_at;
  status.innerHTML = `<strong>时间线起点 · ${esc(started)}</strong>
    ${scanWarning ? `<span>${scanWarning}</span>` : ""}
    ${scan.detail_contents != null ? `<span>上轮检查 ${esc(scan.detail_contents)} 条内容，一级 ${esc(scan.root_pages ?? 0)} 页，条件回复 ${esc(scan.reply_pages ?? 0)} 页</span>` : ""}
    ${usageStarted ? `<span>采集记账自 ${esc(shortTime(usageStarted))} 开始</span>` : ""}`;
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
        <span class="reply-status ${thread.replied ? "replied" : ""}">${thread.replied ? "已官方回复" : "尚未检测到官方回复"}</span>
      </div>
      ${url ? `<a class="thread-title" href="${url}" target="_blank" rel="noopener" title="${esc(comment.content_title)}">${esc(comment.content_title || "未命名内容")}</a>`
        : `<span class="thread-title">${esc(comment.content_title || "未命名内容")}</span>`}
      <div class="event-block">
        <div class="event-meta"><strong>${esc(comment.author || "评论者")}</strong><span>首次发现 ${esc(shortTime(comment.first_seen_at))}</span></div>
        <div class="event-content">${esc(comment.content || "(无文字内容)")}</div>
      </div>
      ${thread.replies.length ? `<div class="official-replies">${thread.replies.map((reply) => `
        <div class="official-reply">
          <div class="reply-meta"><strong>官方回复 · ${esc(reply.author || comment.account_name)}</strong>
            <time>${esc(formatTime(reply.platform_created_at))}</time>
            <span class="response-chip">响应 ${esc(formatDuration(reply.response_seconds))}</span></div>
          <p>${esc(reply.content || "(无文字内容)")}</p>
        </div>`).join("")}</div>` : `<div class="pending-note">最近检查 ${esc(formatTime(comment.last_seen_at))}</div>`}
    </article>`;
  }).join("") || `<div class="timeline-empty"><strong>${THREADS.length ? "当前筛选下暂无互动" : "暂无评论记录"}</strong>
    <span>${THREADS.length ? "可调整筛选条件或搜索关键词。" : "首次监测后，这里会显示带有平台时间戳的新评论及官方回复。"}</span></div>`;
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
      thread.replied ? "已官方回复" : "尚未检测到官方回复", reply?.author || "", reply?.content || "",
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
  const originalLabel = [...button.childNodes];
  button.setAttribute("aria-busy", "true");
  button.textContent = `已导出 ${rows.length} 行`;
  button.disabled = true;
  setTimeout(() => {
    button.replaceChildren(...originalLabel);
    button.removeAttribute("aria-busy");
    button.disabled = !filteredThreads().length;
  }, 1500);
}

function populateFilters(bindEvents = true) {
  const platforms = [...new Set(THREADS.map((thread) => thread.comment.platform))];
  if (!platforms.includes(state.platform)) state.platform = "all";
  $("fPlatform").innerHTML = `<option value="all">全部平台</option>` + platforms.map((platform) =>
    `<option value="${esc(platform)}">${esc(PLATFORM_LABEL[platform] || platform)}</option>`).join("");
  $("fPlatform").value = state.platform;

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
  rebuildAccounts();
  if (!bindEvents) return;

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
  const exportButton = $("exportData");
  exportButton.disabled = !threads.length || exportButton.hasAttribute("aria-busy");
  $("filterSummary").textContent = `${threads.length} 条评论 · ${metrics.replied} 条已回复 · ${metrics.pending} 条尚未检测到官方回复`;
  renderKpis(threads);
  renderTimeline(threads);
  requestAnimationFrame(() => usageChart?.resize());
}

async function loadData() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(`data/comment_timeline.json?_=${Date.now()}`, { cache: "no-store", signal: controller.signal });
    if (response.status === 404) {
      if (DATA) throw new Error("时间线暂时不可用");
      return { events: [], stats: {}, api_usage: {}, last_scan: {}, provenance: {} };
    }
    if (!response.ok) throw new Error(`时间线数据加载失败: ${response.status}`);
    const data = await response.json();
    if (!data || !Array.isArray(data.events)) throw new Error("时间线数据格式错误");
    return data;
  } finally { clearTimeout(timeout); }
}

async function refreshData(initial = false) {
  if (refreshing) return;
  refreshing = true;
  try {
    DATA = await loadData();
    THREADS = buildThreads(DATA.events);
    $("updatedAt").textContent = DATA.updated_at ? formatTime(DATA.updated_at) : "待初始化";
    const badge = $("sourceBadge");
    badge.textContent = DATA.timeline_started_at ? "上线后数据" : "待首次检查";
    badge.classList.toggle("partial", !DATA.timeline_started_at);
    renderStatus();
    renderBudget();
    renderUsageChart();
    populateFilters(initial);
    render();
    $("refreshStatus").classList.remove("sync-error");
    $("refreshStatus").textContent = "页面每 60 秒自动同步 · 评论与回复状态以各条记录的最近检查为准";
  } catch (error) {
    if (initial) throw error;
    $("refreshStatus").classList.add("sync-error");
    $("refreshStatus").textContent = "自动同步失败，当前保留上次数据；将在下次自动重试。";
  } finally { refreshing = false; }
}

async function main() {
  await refreshData(true);
  const updateClock = () => { $("clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false }); };
  updateClock();
  setInterval(updateClock, 1000);
  setInterval(() => refreshData(), 60000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshData(); });
  window.addEventListener("resize", () => usageChart?.resize());
}

main().catch((error) => {
  $("timelineStatus").className = "timeline-status error";
  $("timelineStatus").textContent = `时间线加载失败 · ${error.message}`;
  $("sourceBadge").textContent = "加载失败";
  $("refreshStatus").innerHTML = '<a href="">重新加载</a>';
});
