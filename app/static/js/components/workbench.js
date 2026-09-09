import {
  renderHands,
  renderResultGrid,
  escapeHtml as esc,
  clock,
} from "./cards.js";
export const reasonLabels = {
  game_end: "正常结束",
  timeout: "超时关闭",
  manual: "手动关闭",
  version_changed: "版本调整",
};
export function renderWorkbench(round, live = true, connected = true) {
  const ready = !!round.result,
    n = round.received_count;
  const label =
    !round.enabled && live
      ? "已停用"
      : {
          waiting: "等待新局",
          collecting: "收集中",
          ready: "结果已锁定",
          closed: "已关闭",
        }[round.state];
  const pending = round.missing_client_ids.length
    ? `等待 ${round.missing_client_ids[0]}${round.missing_client_ids.length > 1 ? " 等设备" : ""}上报`
    : round.unregistered_count
      ? `还有 ${round.unregistered_count} 个设备未登记`
      : "等待新局与手牌上报";
  const status = ready
    ? "13 张结果已生成"
    : live
      ? pending
      : reasonLabels[round.close_reason] || "已关闭";
  const footer = ready
    ? "本轮结果已锁定"
    : live
      ? round.enabled
        ? "等待手牌收齐"
        : "租户已停用"
      : "本轮已关闭 · 数据未收齐";
  const onlineCount = round.devices.filter((device) => device.online).length;
  const offlineCount = round.devices.length - onlineCount;
  const connectionSummary = live
    ? `<div class="client-connections" role="status">${
        connected
          ? `<span class="connection-online">在线 <b>${onlineCount}</b> / 7</span><span class="connection-offline">离线 <b>${offlineCount}</b></span><span>待登记 <b>${round.unregistered_count}</b></span>`
          : '<span>管理页面连接中断 · 客户端状态待同步</span>'
      }</div>`
    : "";
  return `<div class="main-grid"><div class="panel"><div class="panelhead"><div class="roundtitle"><h3>租户 ${esc(round.tenant_id)}</h3><span class="version">版本 ${round.round_version}</span><span class="badge ${ready ? "" : round.state === "collecting" ? "wait" : "gray"}">${label}</span><span class="round-status" title="${esc(status)}">${esc(status)}</span></div><span class="sub" id="deadline" style="margin:0">${live ? (round.deadline_at_ms ? "截止 " + clock(round.deadline_at_ms) : "尚未开始计时") : clock(round.created_at_ms) + " 开始"}</span></div>${connectionSummary}<div class="progressblock"><div class="progresslabels"><b>已收到 ${n} / 7 份手牌</b><span>${ready ? "全部收齐 · 结果已锁定" : live ? "每份须含 13 张完整手牌" : reasonLabels[round.close_reason] || "已关闭"}</span></div><div class="progress"><div style="width:${(n / 7) * 100}%"></div></div></div><div class="device-list">${renderHands(round, live, connected)}</div><div class="panelfoot"><span>每份 13 张 · 已接收 ${n * 13} 张</span><span>${live ? "花色 s / h / c / d" : "历史快照 · 只读"}</span></div></div><div class="rightcol"><div class="panel"><div class="panelhead"><h3>本轮计算结果</h3><span class="badge ${ready ? "" : "gray"}">${ready ? "已锁定" : live ? "等待收齐" : "数据不完整"}</span></div><div class="result-body"><div class="resultsummary"><strong>${ready ? "13" : "—"}</strong><span>张剩余手牌</span><span class="result-order">列顺序 A → K</span></div>${renderResultGrid(round.result, footer)}<div class="resultnote">${ready ? '<span class="green">7 份数据校验通过</span><br>本轮结果只读，等待下一轮。' : live ? `<b>还差 ${7 - n} 份手牌</b><br>收齐后自动计算剩余 13 张。` : `本轮仅收到 ${n} 份手牌，未生成确定结果。`}</div></div></div></div></div>`;
}
