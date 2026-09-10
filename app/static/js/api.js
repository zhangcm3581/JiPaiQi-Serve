export async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const data = await response.json();
  if (!response.ok) {
    const message =
      data.message ||
      (Array.isArray(data.detail)
        ? data.detail.map((x) => x.msg).join("；")
        : data.detail) ||
      "请求失败";
    throw new Error(message);
  }
  return data;
}

export class LiveConnection {
  constructor(onMessage, onStatus) {
    this.onMessage = onMessage;
    this.onStatus = onStatus;
    this.tenant = null;
    this.delay = 500;
    this.stopped = false;
    this.offline = () => this.retire(this.socket);
    window.addEventListener("offline", this.offline);
    this.connect();
  }
  connect() {
    if (this.stopped || this.socket) return;
    this.onStatus(false);
    const socket = (this.socket = new WebSocket(
      `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/admin`,
    ));
    let lastSeen = performance.now(), lastPing = lastSeen;
    // A lost connection may remain OPEN and never deliver onclose.
    this.ping = setInterval(() => {
      const now = performance.now();
      if (now - lastSeen >= 35000) { this.retire(socket); return; }
      if (socket.readyState === WebSocket.OPEN && now - lastPing >= 15000) {
        lastPing = now;
        socket.send(JSON.stringify({ type: "ping" }));
      }
    }, 5000);
    socket.onopen = () => {
      if (this.socket !== socket) return;
      lastSeen = performance.now();
      this.delay = 500;
      this.onStatus(true);
      this.subscribe(this.tenant);
    };
    socket.onmessage = (event) => {
      if (this.socket !== socket) return;
      try {
        const message = JSON.parse(event.data);
        lastSeen = performance.now();
        this.onMessage(message);
      } catch (error) { console.error(error); }
    };
    socket.onclose = socket.onerror = () => this.retire(socket);
  }
  retire(socket) {
    if (!socket || this.socket !== socket) return;
    this.socket = null;
    socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
    clearInterval(this.ping);
    this.onStatus(false);
    socket.close();
    if (!this.stopped) {
      this.timer = setTimeout(() => this.connect(), this.delay);
      this.delay = Math.min(10000, this.delay * 2);
    }
  }
  subscribe(tenant) {
    this.tenant = tenant;
    if (this.socket?.readyState === WebSocket.OPEN)
      this.socket.send(JSON.stringify({ type: "subscribe", tenant_id: tenant }));
  }
  close() {
    this.stopped = true;
    clearTimeout(this.timer);
    window.removeEventListener("offline", this.offline);
    this.retire(this.socket);
  }
}
