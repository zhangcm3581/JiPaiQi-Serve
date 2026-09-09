import { api } from "./api.js";
import { escapeHtml as esc } from "./components/cards.js";
export function setupTenants({
  getTenants,
  refresh,
  toast,
  showModal,
  closeModal,
}) {
  let editing = null,
    original = null,
    deviceTenant = null;
  const form = document.querySelector("#tenantForm"),
    idInput = document.querySelector("#tenantId");
  function open(id = null) {
    editing = id;
    const t = getTenants().find((t) => t.tenant_id === id);
    original = t ? { ...t } : null;
    document.querySelector("#modalTitle").textContent = id
      ? "编辑租户"
      : "创建租户";
    idInput.value = t?.tenant_id || "";
    idInput.disabled = !!id;
    document.querySelector("#tenantMemo").value = t?.note || "";
    document.querySelector("#tenantVersion").value = t?.round_version ?? 1;
    document.querySelector("#tenantTimeout").value = t?.timeout_seconds ?? 180;
    document.querySelector("#tenantEnabled").checked = t ? !!t.enabled : true;
    document.querySelector("#tenantEnabled").disabled = !id;
    document.querySelector("#formError").textContent = "";
    showModal("#tenantModal");
  }
  form.onsubmit = async (e) => {
    e.preventDefault();
    const button = document.querySelector("#tenantSave");
    button.disabled = true;
    const data = {
      note: document.querySelector("#tenantMemo").value.trim(),
      round_version: Number(document.querySelector("#tenantVersion").value),
      timeout_seconds: Number(document.querySelector("#tenantTimeout").value),
    };
    try {
      if (editing) {
        data.enabled = document.querySelector("#tenantEnabled").checked;
        // A round may advance while this form is open. Only send edited settings.
        const changes = Object.fromEntries(
          Object.entries(data).filter(
            ([key, value]) => value !== original[key],
          ),
        );
        await api("/api/tenants/" + encodeURIComponent(editing), {
          method: "PATCH",
          body: JSON.stringify(changes),
        });
      } else {
        data.tenant_id = idInput.value.trim();
        await api("/api/tenants", {
          method: "POST",
          body: JSON.stringify(data),
        });
      }
      closeModal();
      await refresh();
      toast("租户信息已保存");
    } catch (error) {
      document.querySelector("#formError").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  async function showDevices(id) {
    deviceTenant = id;
    document.querySelector("#deviceTitle").textContent =
      "租户 " + id + " · 客户端";
    showModal("#deviceModal");
    const target = document.querySelector("#managedDevices");
    target.innerHTML = '<div class="loading">正在读取…</div>';
    try {
      const round = await api(
        "/api/tenants/" + encodeURIComponent(id) + "/rounds/current",
      );
      if (deviceTenant !== id) return;
      target.innerHTML = round.devices.length
        ? round.devices
            .map(
              (d) =>
                `<div class="managed-device"><div>${esc(d.client_id)}<div class="sub">${d.online ? "在线" : "离线"} · 绑定版本 ${d.bound_round_version ?? "—"}</div></div><button class="btn tiny" data-remove-client="${esc(d.client_id)}" ${d.online || round.state !== "waiting" ? "disabled" : ""}>移除登记</button></div>`,
            )
            .join("")
        : '<div class="empty">尚无设备，客户端连接后自动登记。</div>';
    } catch (error) {
      target.textContent = error.message;
    }
  }
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.hasAttribute("data-create")) open();
    if (b.dataset.edit) open(b.dataset.edit);
    if (b.dataset.devices) showDevices(b.dataset.devices);
    if (b.dataset.removeClient) {
      b.disabled = true;
      try {
        await api(
          `/api/tenants/${encodeURIComponent(deviceTenant)}/clients/${encodeURIComponent(b.dataset.removeClient)}`,
          { method: "DELETE" },
        );
        await showDevices(deviceTenant);
        await refresh();
        toast("设备登记已移除");
      } catch (error) {
        toast(error.message);
        b.disabled = false;
      }
    }
  });
  const search = document.querySelector("#search");
  if (search) search.oninput = () => render(getTenants());
  function render(tenants) {
    const rows = document.querySelector("#tenantRows");
    if (!rows) return;
    const query = search.value.trim().toLowerCase(),
      items = tenants.filter((t) =>
        (t.tenant_id + " " + t.note).toLowerCase().includes(query),
      );
    rows.innerHTML = items.length
      ? items
          .map(
            (t) =>
              `<tr><td><div class="tenantcell"><span class="tenanticon">${esc(t.tenant_id.slice(-2))}</span><strong>${esc(t.tenant_id)}</strong></div></td><td class="tenantnote" title="${esc(t.note)}">${esc(t.note || "暂无备注")}</td><td><span class="version">${t.round_version}</span></td><td><button class="textbtn" data-devices="${esc(t.tenant_id)}">${t.online_count} / ${t.registered_count}</button></td><td><span class="badge ${t.enabled ? "" : "gray"}">${t.enabled ? "已启用" : "已停用"}</span></td><td style="text-align:right"><a class="textbtn" href="/live?tenant=${encodeURIComponent(t.tenant_id)}">查看</a><button class="textbtn" data-edit="${esc(t.tenant_id)}">编辑租户</button></td></tr>`,
          )
          .join("")
      : '<tr><td colspan="6" class="empty">没有匹配的租户。点击“创建租户”开始。</td></tr>';
    document.querySelector("#rowCount").textContent =
      "共 " + items.length + " 条";
    document.querySelector("#tenantCountLabel").textContent =
      "全部租户 · " + tenants.length;
  }
  return { render, open };
}
