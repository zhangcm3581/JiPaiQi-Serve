# 客户端接入示例

先在管理页面创建租户 `100001`。下列三个业务请求按顺序发送到 `/ws/client?tenant_id=100001&client_id=device_001`，每一步等待对应 `reply_to` 的 ack。实际版本必须取自当前 `round.state`，示例假设为1。

## 1. 新局绑定

首次安装且确实观察到发牌前→新局的切换后发送；半局启动只看见13张牌不能据此认定新局。

```json
{
  "protocol_version": 1,
  "type": "round.join",
  "request_id": "request-start-001",
  "tenant_id": "100001",
  "client_id": "device_001",
  "round_version": 1,
  "payload": {
    "start_event_id": "observed-start-001",
    "sync_basis": "initial_start",
    "previous_round_version": null
  }
}
```

成功返回：

```json
{
  "protocol_version": 1,
  "type": "ack",
  "tenant_id": "100001",
  "round_version": 1,
  "reply_to": "request-start-001",
  "server_time_ms": 1788900000000,
  "payload": {"action": "round.join", "duplicate": false, "bound_round_version": 1}
}
```

把手牌与版本1绑定并持久保存。服务端版本更新不会自动改变这份绑定。

## 2. 完整手牌上传

本例13张黑桃只是字段示例。实际发送识别到的13张牌，同花色同点数出现两次时保留两项；没有花色或数量不足时不要上传。

```json
{
  "protocol_version": 1,
  "type": "hand.submit",
  "request_id": "request-hand-001",
  "tenant_id": "100001",
  "client_id": "device_001",
  "round_version": 1,
  "payload": {
    "cards": [
      {"rank":"A","suit":"s"},
      {"rank":"2","suit":"s"},
      {"rank":"3","suit":"s"},
      {"rank":"4","suit":"s"},
      {"rank":"5","suit":"s"},
      {"rank":"6","suit":"s"},
      {"rank":"7","suit":"s"},
      {"rank":"8","suit":"s"},
      {"rank":"9","suit":"s"},
      {"rank":"10","suit":"s"},
      {"rank":"J","suit":"s"},
      {"rank":"Q","suit":"s"},
      {"rank":"K","suit":"s"}
    ]
  }
}
```

收到对应 ack 后停止本局上传。断线后沿用 `request-hand-001` 和完全相同的消息重试；重复确认不会重复计牌。修改识别结果无法覆盖已经确认的手牌。

## 3. 结果推送

下列仅演示一种合计13张的合法结果，不是对上方单份上传的计算结果；必须收齐7份、91张才能产生结果。

```json
{
  "protocol_version": 1,
  "type": "round.result",
  "tenant_id": "100001",
  "round_version": 1,
  "server_time_ms": 1788900015000,
  "payload": {
    "remaining_count": 13,
    "cards": [
      {"rank":"A","suit":"s","count":2},
      {"rank":"3","suit":"s","count":1},
      {"rank":"5","suit":"s","count":1},
      {"rank":"6","suit":"s","count":1},
      {"rank":"K","suit":"s","count":1},
      {"rank":"2","suit":"h","count":1},
      {"rank":"5","suit":"h","count":1},
      {"rank":"9","suit":"h","count":1},
      {"rank":"5","suit":"c","count":1},
      {"rank":"7","suit":"c","count":1},
      {"rank":"J","suit":"c","count":1},
      {"rank":"J","suit":"d","count":1}
    ]
  }
}
```

面板只接受相同tenant且与当前绑定版本一致的结果。列为 A、2、3…10、J、Q、K，不显示表头数字。行按 s、s、h、h、c、c、d、d；count为1填该花色第一行，为2再填第二行。暂停隐藏面板；下一版本清空旧结果。

## 4. 结束与下一局

稳定识别到本局结束特征：

```json
{
  "protocol_version": 1,
  "type": "round.end",
  "request_id": "request-end-001",
  "tenant_id": "100001",
  "client_id": "device_001",
  "round_version": 1,
  "payload": {}
}
```

第一个有效结束事件关闭版本1，响应 `next_round_version=2`。其他设备迟到的版本1结束消息会被拒绝为`ROUND_CLOSED`，不会再推进。

下一次确实观察到新局且查询到当前版本2，发送新的join：`round_version=2`、新 `request_id/start_event_id`、`sync_basis="end_then_start"`、`previous_round_version=1`。若服务端已经到了3或更大，不能把版本2的手牌换成版本3重发。

## 5. 断线和失步

- 15秒发送一次完整协议的 `ping`，`round_version=null,payload={}`；45秒无消息服务端断开。
- 重连等待服务端快照或发 `round.get`；重新发送还没收到 ack 的原请求。已关闭版本的旧请求重放有原ack时返回原ack，不代表当前仍处于该旧版本。
- `round.state`更新当前状态，`round.result`更新结果；不同版本不能混用。后台 `/ws/admin`是管理页面专用，不应在 APK 中订阅。
- `SYNC_REQUIRED`不意味着可以丢掉本地绑定强行重新标号。先等待可靠的新局特征；必要时在等待新局阶段让该设备离线，后台移除登记，再重新接入。
- 首次登记、重新登记和截图所属轮次仍依赖客户端识别可靠的新局切换；不使用共同游戏局号的方案无法在服务端独立证明截图来自同一局。


20 个身份、任意 7 份手牌（2026-09-11 更新）：一个租户最多登记 20 个 ID；ID 不要求连续编号。已使用 ID 可以跨过多轮后回来，发送全新的开局事件，例如上次参与 7、当前 10，则 round_version=10、previous_round_version=7、sync_basis=end_then_start。服务端校验 previous 等于实际 last_bound 且小于当前轮。旧版本 7 的请求仍不能写到 10，新局证据和新鲜完整手牌必须重新采集。半局启动不会仅凭 13 张牌加入。无需删除闲置 ID；已登记但本轮未上报的 ID 不占七份手牌名额。
