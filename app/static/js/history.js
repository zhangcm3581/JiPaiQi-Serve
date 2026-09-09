import { api } from "./api.js";
import { Dropdown } from "./components/dropdown.js";
import { renderWorkbench, reasonLabels } from "./components/workbench.js";
import { escapeHtml as esc, dateTime } from "./components/cards.js";
export function setupHistory(toast) {
  const rows = document.querySelector("#historyRows");
  if (!rows) return null;
  let tenant = "",
    reason = "",
    offset = 0,
    total = 0,
    items = [],
    expanded = null,
    sequence = 0;
  const details = new Map();
  const tenantPicker = new Dropdown(
    document.querySelector("#historyTenantPicker"),
    (value) => {
      tenant = value;
      offset = 0;
      expanded = null;
      load();
    },
  );
  const statusPicker = new Dropdown(
    document.querySelector("#historyStatusPicker"),
    (value) => {
      reason = value;
      offset = 0;
      expanded = null;
      load();
    },
  );
  const statusOptions = [
    { value: "", label: "全部状态" },
    ...Object.entries(reasonLabels).map(([value, label]) => ({ value, label })),
  ];
  statusPicker.update(statusOptions, "");
  function render() {
    rows.innerHTML = items.length
      ? items
          .map((h) => {
            let key = h.tenant_id + ":" + h.round_version;
            return (
              `<tr class="history-row" data-history-key="${esc(key)}"><td><strong>${h.round_version}</strong></td><td>${esc(h.tenant_id)}</td><td>${h.received_count} / 7 <span class="muted">· ${h.received_count * 13} 张</span></td><td>${h.has_result ? '<span class="green">剩余 13 张</span>' : '<span class="muted">数据不完整</span>'}</td><td>${dateTime(h.created_at_ms)}</td><td><span class="badge ${h.close_reason === "timeout" ? "wait" : "gray"}">${reasonLabels[h.close_reason] || esc(h.close_reason)}</span></td><td style="text-align:right"><button class="textbtn history-toggle" aria-expanded="${expanded === key}" aria-label="${expanded === key ? "收起" : "展开"}租户${esc(h.tenant_id)}版本${h.round_version}">${expanded === key ? "收起" : "展开"} <svg><use href="#arrow"/></svg></button></td></tr>` +
              (expanded === key
                ? `<tr class="history-expand"><td colspan="7">${details.has(key) ? renderWorkbench(details.get(key), false) : '<div class="loading">正在读取完整手牌…</div>'}</td></tr>`
                : "")
            );
          })
          .join("")
      : '<tr><td colspan="7" class="empty">暂无已关闭的版本记录</td></tr>';
    document.querySelector("#historyCount").textContent =
      `共 ${total} 条 · 第 ${Math.floor(offset / 20) + 1} 页`;
    document.querySelector("#previousPage").disabled = offset === 0;
    document.querySelector("#nextPage").disabled = offset + 20 >= total;
  }
  async function load() {
    const current = ++sequence;
    try {
      const query = new URLSearchParams({
        limit: "20",
        offset: String(offset),
      });
      if (tenant) query.set("tenant_id", tenant);
      if (reason) query.set("reason", reason);
      const data = await api("/api/rounds?" + query);
      if (current !== sequence) return;
      items = data.items;
      total = data.total;
      render();
    } catch (error) {
      toast(error.message);
    }
  }
  rows.addEventListener("click", async (e) => {
    const row = e.target.closest("[data-history-key]");
    if (!row) return;
    const key = row.dataset.historyKey;
    expanded = expanded === key ? null : key;
    render();
    if (expanded && !details.has(key)) {
      const item = items.find(
        (x) => x.tenant_id + ":" + x.round_version === key,
      );
      try {
        const detail = await api(
          `/api/tenants/${encodeURIComponent(item.tenant_id)}/rounds/${item.round_version}`,
        );
        details.set(key, detail);
        if (expanded === key) render();
      } catch (error) {
        toast(error.message);
        if (expanded === key) {
          expanded = null;
          render();
        }
      }
    }
  });
  document.querySelector("#previousPage").onclick = () => {
    offset = Math.max(0, offset - 20);
    expanded = null;
    load();
  };
  document.querySelector("#nextPage").onclick = () => {
    offset += 20;
    expanded = null;
    load();
  };
  let refreshTimer;
  return {
    load,
    updateTenants(tenants) {
      tenantPicker.update(
        [
          { value: "", label: "全部租户" },
          ...tenants.map((t) => ({
            value: t.tenant_id,
            label: "租户 " + t.tenant_id,
            note: t.note,
          })),
        ],
        tenant,
      );
    },
    invalidate() {
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(load, 180);
    },
  };
}
