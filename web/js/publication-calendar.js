/* Publication history, adapted from 麦田的运营人大屏's Monday-first calendar. */
(() => {
  "use strict";

  const pad = (n) => String(n).padStart(2, "0");

  // Use the snapshot's recorded calendar date, matching the dashboard detail table.
  function publicationDay(record) {
    const stamp = record.published_at;
    if (typeof stamp !== "string" || !/^\d{4}-\d\d-\d\d(?:$|[T ])/.test(stamp) ||
        !Number.isFinite(Date.parse(stamp))) return "";
    const day = stamp.slice(0, 10);
    const date = new Date(day + "T00:00:00Z");
    return Number.isFinite(date.getTime()) && date.toISOString().startsWith(day) ? day : "";
  }

  function monthStep(month, offset) {
    const date = new Date(month + "-01T00:00:00Z");
    date.setUTCMonth(date.getUTCMonth() + offset);
    return date.toISOString().slice(0, 7);
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function titleLink(record) {
    let url;
    try { url = new URL(record.url); } catch { /* Unlinked snapshot record. */ }
    const linked = url && ["http:", "https:"].includes(url.protocol);
    const node = element(linked ? "a" : "span", "", record.title || "未命名内容");
    if (linked) {
      node.href = url.href;
      node.target = "_blank";
      node.rel = "noopener noreferrer";
    }
    return node;
  }

  let days = new Map();
  let unit = "";
  let month = "";
  let selected = "";
  let earliest = "";
  let latest = "";

  function draw(host) {
    // Keep keyboard focus when the month or day button is redrawn.
    const focusKey = host.contains(document.activeElement) ? document.activeElement.dataset.calendarKey : "";
    const header = element("header", "pubcal-heading");
    header.append(element("h2", "", "发布日历"));
    host.replaceChildren(header);
    if (!latest) {
      host.append(element("p", "pubcal-empty", "当前筛选下没有带发布日期的内容。"));
      return;
    }

    const monthDate = new Date(month + "-01T00:00:00Z");
    const year = monthDate.getUTCFullYear();
    const number = monthDate.getUTCMonth() + 1;
    const dayCount = new Date(Date.UTC(year, number, 0)).getUTCDate();
    const nav = element("div", "pubcal-nav");
    function monthButton(offset, label) {
      const node = element("button", "", offset < 0 ? "‹" : "›");
      node.type = "button";
      node.dataset.calendarKey = offset < 0 ? "previous" : "next";
      node.setAttribute("aria-label", label);
      node.disabled = offset < 0 ? month <= earliest : month >= latest;
      node.onclick = () => {
        month = monthStep(month, offset);
        selected = "";
        draw(host);
      };
      return node;
    }

    const monthLabel = element("strong", "", year + " 年 " + number + " 月");
    monthLabel.setAttribute("aria-live", "polite");
    nav.append(monthButton(-1, "上个月"), monthLabel, monthButton(1, "下个月"));
    header.append(nav);
    const grid = element("div", "pubcal-grid");
    for (const weekday of "一二三四五六日") grid.append(element("span", "pubcal-weekday", weekday));
    const offset = (monthDate.getUTCDay() + 6) % 7;
    for (let i = 0; i < offset; i++) grid.append(element("span", "pubcal-padding"));

    let total = 0;
    for (let day = 1; day <= dayCount; day++) {
      const key = month + "-" + pad(day);
      const count = days.get(key)?.length || 0;
      total += count;
      const cell = element(count ? "button" : "span", "pubcal-day", day);
      if (count) {
        cell.type = "button";
        cell.dataset.calendarKey = key;
        cell.append(element("span", "pubcal-count", count));
        cell.setAttribute("aria-label", number + " 月 " + day + " 日，发布 " + count + " " + unit);
        cell.setAttribute("aria-pressed", String(key === selected));
        cell.onclick = () => {
          selected = key === selected ? "" : key;
          draw(host);
        };
      }
      grid.append(cell);
    }
    host.append(grid);

    const footer = element("div", "pubcal-summary");
    footer.append(element("span", "", "本月发布 " + total + " " + unit));
    if (total) footer.append(element("small", "", "选择日期查看作品"));
    host.append(footer);

    if (selected && days.has(selected)) {
      const detail = element("section", "pubcal-detail");
      const label = number + " 月 " + Number(selected.slice(-2)) + " 日发布";
      const records = days.get(selected);
      detail.setAttribute("aria-label", label);
      detail.append(element("h3", "", label + " · " + records.length + " " + unit));
      const list = element("ul", "");
      for (const record of records) {
        const item = element("li", "");
        item.append(titleLink(record));
        list.append(item);
      }
      detail.append(list);
      host.append(detail);
    }
    if (focusKey) {
      const target = host.querySelector('[data-calendar-key="' + focusKey + '"]:enabled');
      (target || nav.querySelector("button:enabled"))?.focus({ preventScroll: true });
    }
  }

  window.PublicationCalendar = {
    render(kind, records) {
      const host = document.getElementById("publicationCalendar");
      if (!host) return;
      days = new Map();
      earliest = latest = "";
      unit = kind === "article" ? "篇" : "条";
      for (const record of records) {
        const key = publicationDay(record);
        if (!key) continue;
        if (!days.has(key)) days.set(key, []);
        days.get(key).push(record);
        const m = key.slice(0, 7);
        if (!earliest || m < earliest) earliest = m;
        if (m > latest) latest = m;
      }
      if (!month || month < earliest || month > latest) month = latest;
      if (!days.has(selected) || selected.slice(0, 7) !== month) selected = "";
      draw(host);
    }
  };
})();
