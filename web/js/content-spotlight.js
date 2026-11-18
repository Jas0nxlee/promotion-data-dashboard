/* A read-only, snapshot-backed adaptation of maitian's latest-episode carousel. */
(() => {
  "use strict";

  const MAX_SLIDES = 6;
  const countFormat = new Intl.NumberFormat("zh-CN");
  const dateFormat = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  });

  function node(tag, className, text) {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function webUrl(value) {
    try {
      const url = new URL(value);
      if (["http:", "https:"].includes(url.protocol) && !url.username && !url.password) return url.href;
    } catch { /* Some historical records have no original URL. */ }
    return null;
  }

  function poster(record) {
    const cover = node("div", "spot-poster");
    const date = node("time", "spot-date");
    const isoDay = dateFormat.format(new Date(record.published_at));
    date.dateTime = isoDay;
    const [year, month, day] = isoDay.split("-");
    cover.setAttribute("aria-label", "发布日期 " + isoDay);
    date.append(node("span", "spot-month", year + " / " + month), node("b", "spot-day", day));
    cover.append(date);
    const src = webUrl(record.cover);
    if (src) {
      const image = node("img", "spot-image");
      image.alt = "";
      image.loading = "lazy";
      image.decoding = "async";
      image.referrerPolicy = "no-referrer";
      image.onload = () => cover.classList.add("spot-has-image");
      image.src = src;
      cover.prepend(image);
    }
    return cover;
  }

  function content(kind, record) {
    const slide = node("div", "spot-slide");
    const info = node("div", "spot-info");
    const meta = node("div", "spot-meta");
    const platform = record.platform_label || record.platform;
    if (platform) {
      const badge = node("span", "spot-platform", platform);
      badge.dataset.platform = record.platform;
      meta.append(badge);
    }
    if (record.account_name) meta.append(node("span", "spot-account", record.account_name));
    const heading = node("h3", "spot-title");
    const url = webUrl(record.url);
    const title = record.title || (kind === "video" ? "未命名视频" : "未命名图文");
    if (url) {
      const link = node("a", "spot-original", title);
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = title + "（打开原作）";
      heading.append(link);
    } else {
      heading.textContent = title;
    }
    const metrics = node("dl", "spot-metrics");
    const primary = kind === "video" ? ["play", "播放"] : ["read", "阅读"];
    for (const [key, label] of [primary, ["like", "点赞"], ["comment", "评论"], ["share", "分享"]]) {
      const value = record.stats?.[key];
      const group = node("div", "spot-metric");
      const stat = node("dd", "spot-value", Number.isFinite(value) ? countFormat.format(value) : "—");
      if (!Number.isFinite(value)) stat.setAttribute("aria-label", "暂无数据");
      group.append(node("dt", "spot-label", label), stat);
      metrics.append(group);
    }
    info.append(meta, heading, metrics);
    slide.append(poster(record), info);
    return slide;
  }

  function render(kind, records) {
    const root = document.getElementById("contentSpotlight");
    if (!root) return;
    const slides = records
      .map(record => ({record, time: Date.parse(record.published_at)}))
      .filter(item => Number.isFinite(item.time))
      .sort((a, b) => b.time - a.time)
      .slice(0, MAX_SLIDES);
    const header = node("div", "spot-header");
    header.append(node("h2", "spot-heading", "最新发布"));
    root.replaceChildren(header);
    if (!slides.length) {
      root.append(node("p", "spot-empty", records.length ? "当前内容缺少发布日期" : "当前筛选暂无发布内容"));
      return;
    }
    const region = node("div", "spot-region");
    region.setAttribute("aria-live", "polite");
    region.setAttribute("aria-atomic", "true");
    const counter = node("span", "spot-counter");
    let index = 0;
    function show(offset) {
      index = (index + offset + slides.length) % slides.length;
      counter.textContent = (index + 1) + " / " + slides.length;
      region.replaceChildren(content(kind, slides[index].record));
    }
    if (slides.length > 1) {
      const nav = node("div", "spot-controls");
      nav.append(counter);
      for (const [direction, label, offset] of [["prev", "上一条", -1], ["next", "下一条", 1]]) {
        const button = node("button", "spot-arrow spot-" + direction);
        button.type = "button";
        button.setAttribute("aria-label", label);
        button.addEventListener("click", () => show(offset));
        nav.append(button);
      }
      header.append(nav);
    }
    root.append(region);
    show(0);
  }

  window.ContentSpotlight = {render};
})();
