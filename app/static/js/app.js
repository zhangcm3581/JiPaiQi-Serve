import { api, LiveConnection } from "./api.js";
import { renderWorkbench } from "./components/workbench.js";
import { Dropdown } from "./components/dropdown.js";
import { setupTenants } from "./tenants.js";
import { setupHistory } from "./history.js";
const $ = (s) => document.querySelector(s),
  page = document.body.dataset.page;
const titles = {
  overview: "管理总览",
  live: "实时工作台",
  tenants: "租户管理",
  history: "版本记录",
};
$("#pageCrumb").textContent = titles[page];
$("#today").textContent = new Date().toLocaleDateString("zh-CN", {
  year: "numeric",
  month: "long",
  day: "numeric",
  weekday: "long",
  timeZone: "Asia/Shanghai",
});
document
  .querySelectorAll("nav a")
  .forEach((a) =>
    a.classList.toggle("active", a.getAttribute("href") === location.pathname),
  );
let tenants = [],
  selected = new URLSearchParams(location.search).get("tenant"),
  round = null,
  online = false,
  lastFocus,
  toastTimer,
  live,
  requestSequence = 0,
  lastPushAt = 0;
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").classList.remove("show"), 3500);
}
function showModal(selector) {
  lastFocus = document.activeElement;
  const modal = $(selector);
  modal.classList.add("open");
  setTimeout(
    () => modal.querySelector("input:not(:disabled),button")?.focus(),
    0,
  );
}
function closeModal() {
  document
    .querySelectorAll(".modalback")
    .forEach((m) => m.classList.remove("open"));
  lastFocus?.focus();
}
document.addEventListener("click", (e) => {
  if (
    e.target.closest("[data-close]") ||
    e.target.classList.contains("modalback")
  )
    closeModal();
});
document.addEventListener("keydown", (e) => {
  const m = document.querySelector(".modalback.open");
  if (!m) return;
  if (e.key === "Escape") closeModal();
  if (e.key === "Tab") {
    const list = [...m.querySelectorAll("button,input,textarea")].filter(
        (x) => !x.disabled,
      ),
      first = list[0],
      last = list.at(-1);
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }
});
const sidebar = $("#sidebarToggle");
function collapse(value) {
  document.body.classList.toggle("sidebar-collapsed", value);
  sidebar.setAttribute("aria-expanded", String(!value));
  sidebar.setAttribute("aria-label", value ? "显示菜单栏" : "隐藏菜单栏");
  sidebar.title = value ? "显示菜单栏" : "隐藏菜单栏";
}
sidebar.onclick = () =>
  collapse(!document.body.classList.contains("sidebar-collapsed"));
if (innerWidth <= 740) collapse(true);
const tenantUI = setupTenants({
  getTenants: () => tenants,
  refresh,
  toast,
  showModal,
  closeModal,
});
const history = setupHistory(toast),
  picker = $("#tenantPicker")
    ? new Dropdown($("#tenantPicker"), chooseTenant)
    : null;
function renderStats(s) {
  if (!$("#stats")) return;
  $("#tenantTotal").innerHTML = s.tenant_count + "<span>个</span>";
  $("#enabledTotal").textContent =
    `${s.enabled_count} 个已启用 · ${s.tenant_count - s.enabled_count} 个已停用`;
  $("#onlineTotal").innerHTML =
    s.online_count + "<span>/ " + s.registered_count + "</span>";
  $("#offlineTotal").textContent =
    s.registered_count - s.online_count + " 个已登记客户端离线";
  $("#completedTotal").innerHTML = s.completed_today + "<span>轮</span>";
  $("#calculationTotal").innerHTML =
    s.average_calculation_ms == null
      ? "—"
      : s.average_calculation_ms.toFixed(1) + "<span>ms</span>";
}
function updateTenants(items) {
  tenants = items;
  tenantUI.render(tenants);
  history?.updateTenants(tenants);
  if (picker) {
    if (!tenants.some((t) => t.tenant_id === selected))
      chooseTenant(tenants[0]?.tenant_id || null);
    picker.update(
      tenants.map((t) => ({
        value: t.tenant_id,
        label: "租户 " + t.tenant_id,
        note: t.note,
      })),
      selected,
    );
    $("#navCount").textContent =
      tenants.find((t) => t.tenant_id === selected)?.received_count || 0;
  } else {
    $("#navCount").textContent = tenants.reduce(
      (sum, t) => sum + t.received_count,
      0,
    );
  }
}
function renderRound(data) {
  if (!$("#workbench") || data.tenant_id !== selected) return;
  round = data;
  $("#workbench").innerHTML = renderWorkbench(data);
  $("#closeRound").disabled = !data.enabled;
  $("#navCount").textContent = data.received_count;
  $("#liveStatus").textContent = online
    ? "实时连接已建立"
    : "连接中断 · 显示最后同步数据";
}
async function chooseTenant(id) {
  selected = id;
  round = null;
  lastPushAt = 0;
  const seq = ++requestSequence;
  picker?.update(
    tenants.map((t) => ({
      value: t.tenant_id,
      label: "租户 " + t.tenant_id,
      note: t.note,
    })),
    id,
  );
  live?.subscribe(id);
  if (!id) {
    $("#workbench").innerHTML =
      '<div class="workbench-empty"><h2>先创建一个租户</h2><p>客户端连接后，手牌会实时显示在这里。</p><button class="btn primary" data-create>创建租户</button></div>';
    $("#closeRound").disabled = true;
    return;
  }
  $("#workbench").innerHTML =
    '<div class="loading">正在同步租户 ' + id + "…</div>";
  const started = performance.now();
  try {
    const data = await api(
      "/api/tenants/" + encodeURIComponent(id) + "/rounds/current",
    );
    if (seq === requestSequence && lastPushAt <= started) renderRound(data);
  } catch (error) {
    if (seq === requestSequence) {
      $("#workbench").textContent = error.message;
      toast(error.message);
    }
  }
}
async function refresh() {
  try {
    const [items, summary] = await Promise.all([
      api("/api/tenants"),
      api("/api/summary"),
    ]);
    updateTenants(items);
    renderStats(summary);
    if (!online && selected && picker) await chooseTenant(selected);
  } catch (error) {
    toast(error.message);
  }
}
if ($("#closeRound"))
  $("#closeRound").onclick = async () => {
    if (!round) return;
    const expected = round;
    $("#closeRound").disabled = true;
    try {
      await api(
        `/api/tenants/${encodeURIComponent(expected.tenant_id)}/close`,
        {
          method: "POST",
          body: JSON.stringify({ round_version: expected.round_version }),
        },
      );
      toast("本轮已关闭，正在等待下一局");
      await refresh();
    } catch (error) {
      toast(error.message);
      $("#closeRound").disabled = false;
    }
  };
await refresh();
await history?.load();
live = new LiveConnection(
  (message) => {
    if (message.type === "admin.snapshot") {
      updateTenants(message.payload.tenants);
      renderStats(message.payload.summary);
      history?.invalidate();
    }
    if (message.type === "admin.round" && message.tenant_id === selected) {
      lastPushAt = performance.now();
      renderRound(message.payload);
    }
    if (message.type === "error") toast(message.payload.message);
  },
  (connected) => {
    online = connected;
    $("#connectionLabel").textContent = connected
      ? "实时连接已建立"
      : "连接中断 · 正在重连";
    $("#connection").classList.toggle("offline", !connected);
    if ($("#liveStatus"))
      $("#liveStatus").textContent = connected
        ? "实时连接已建立"
        : "正在重连 · 数据可能已过期";
  },
);
live.subscribe(selected);
window.addEventListener("beforeunload", () => live.close());
