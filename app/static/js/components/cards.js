export const ranks = [
  "A",
  "2",
  "3",
  "4",
  "5",
  "6",
  "7",
  "8",
  "9",
  "10",
  "J",
  "Q",
  "K",
];
export const suits = { s: "♠", h: "♥", c: "♣", d: "♦" };
export const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
export const clock = (value) =>
  value == null
    ? "—"
    : new Date(value).toLocaleTimeString("zh-CN", {
        hour12: false,
        timeZone: "Asia/Shanghai",
      });
export const dateTime = (value) =>
  value == null
    ? "—"
    : new Date(value).toLocaleString("zh-CN", {
        hour12: false,
        timeZone: "Asia/Shanghai",
      });
export function renderCard(card) {
  return `<span class="card ${["h", "d"].includes(card.suit) ? "red" : ""}" title="${escapeHtml(suits[card.suit] + card.rank)}"><span class="rank">${escapeHtml(card.rank)}</span><span class="suit">${suits[card.suit] || ""}</span></span>`;
}
export function renderHands(round, live = true) {
  const devices = [...round.devices];
  while (devices.length < 7)
    devices.push({ client_id: null, cards: null, online: false });
  return devices
    .map((device, index) => {
      const received = !!device.cards,
        waiting =
          live &&
          !received &&
          ["waiting", "collecting"].includes(round.state) &&
          round.enabled;
      const label = received ? "已上报" : live ? "等待上报" : "本轮未上报";
      const content = received
        ? device.cards.map(renderCard).join("")
        : `<div class="hand-pending" role="status"><span class="waiting-dots ${waiting ? "animated" : ""}" aria-hidden="true"><i></i><i></i><i></i></span><span>${label}</span></div>`;
      return `<div class="device ${received ? "" : "missing"}"><div class="deviceidentity"><div><div class="devicename">${device.client_id ? "客户端 " + String(index + 1).padStart(2, "0") : "待登记设备"}</div><div class="deviceid" title="${escapeHtml(device.client_id)}">${escapeHtml(device.client_id || "尚未连接")}</div></div></div><div class="cards">${content}</div><div class="device-time ${received ? "" : "wait"}"><b>${received ? "已上报" : "未上报"}</b>${received ? clock(device.received_at_ms) : device.client_id ? (live ? (device.online ? "在线" : "离线") : "—") : "—"}</div></div>`;
    })
    .join("");
}
export function renderResultGrid(result, footer = "等待手牌收齐") {
  const counts = new Map(
    (result || []).map((c) => [c.suit + ":" + c.rank, c.count]),
  );
  const cells = ["s", "s", "h", "h", "c", "c", "d", "d"]
    .map(
      (suit, row) =>
        `<div class="client-cell suit-cell ${["h", "d"].includes(suit) ? "red" : ""}">${suits[suit]}</div>` +
        ranks
          .map((rank) => {
            const filled = (counts.get(suit + ":" + rank) || 0) > row % 2;
            return `<div class="client-cell ${row % 2 ? "alt " : ""}${["h", "d"].includes(suit) ? "red " : ""}${filled ? "filled" : ""}" title="${suits[suit]}${rank} · 第${(row % 2) + 1}张">${filled ? rank : ""}</div>`;
          })
          .join(""),
    )
    .join("");
  const accessible = result
    ? result.map((c) => `${suits[c.suit]}${c.rank} ${c.count}张`).join("，")
    : footer;
  return `<div class="client-grid" role="img" aria-label="${escapeHtml(accessible)}">${cells}</div><div class="client-grid-footer">${escapeHtml(footer)}</div>`;
}
