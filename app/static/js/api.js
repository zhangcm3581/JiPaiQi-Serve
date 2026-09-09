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
    this.connect();
  }
  connect() {
    if (this.stopped) return;
    this.onStatus(false);
    const socket = (this.socket = new WebSocket(
      `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/admin`,
    ));
    socket.onopen = () => {
      this.delay = 500;
      this.onStatus(true);
      this.subscribe(this.tenant);
      this.ping = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN)
          socket.send(JSON.stringify({ type: "ping" }));
      }, 15000);
    };
    socket.onmessage = (event) => {
      try {
        this.onMessage(JSON.parse(event.data));
      } catch (error) {
        console.error(error);
      }
    };
    socket.onclose = () => {
      clearInterval(this.ping);
      this.onStatus(false);
      if (!this.stopped) {
        this.timer = setTimeout(() => this.connect(), this.delay);
        this.delay = Math.min(10000, this.delay * 2);
      }
    };
    socket.onerror = () => socket.close();
  }
  subscribe(tenant) {
    this.tenant = tenant;
    if (this.socket?.readyState === WebSocket.OPEN)
      this.socket.send(
        JSON.stringify({ type: "subscribe", tenant_id: tenant }),
      );
  }
  close() {
    this.stopped = true;
    clearTimeout(this.timer);
    clearInterval(this.ping);
    this.socket?.close();
  }
}
