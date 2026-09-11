# 一次提交手牌协议（客户端 0.4.20）

连接 URL 和 protocol_version=1 保持兼容。每租户最多20个登记ID，同轮任意7个不同ID各提交13张。服务端连接时推送当前轮次；客户端在本机新开局第一帧冻结目标轮次，两个新鲜一致的完整手牌观测后立即发一条消息，不等绑定确认。

```json
{
  "protocol_version": 1,
  "type": "hand.submit",
  "request_id": "unique-request-id",
  "tenant_id": "100001",
  "client_id": "emu_A",
  "round_version": 8,
  "payload": {
    "start_event_id": "unique-local-game-event",
    "cards": [{"rank": "A", "suit": "s"}, {"rank": "2", "suit": "s"}, {"rank": "3", "suit": "s"}, {"rank": "4", "suit": "s"}, {"rank": "5", "suit": "s"}, {"rank": "6", "suit": "s"}, {"rank": "7", "suit": "s"}, {"rank": "8", "suit": "s"}, {"rank": "9", "suit": "s"}, {"rank": "10", "suit": "s"}, {"rank": "J", "suit": "s"}, {"rank": "Q", "suit": "s"}, {"rank": "K", "suit": "s"}]
  }
}
```

服务器在一次事务中确认当前轮次、校验牌、登记本轮参与事件、累计两副牌限制、保存手牌和确认消息。无效数据导致整个事务回滚，不提前占本轮名额。相同请求ID+内容原样返回已保存确认（先于轮次关闭检查）；相同请求ID改内容拒绝。相同开局事件不能更换轮次或牌，一轮同ID不能提交第二个开局事件。跨轮返回不需要previous_round_version。

收齐7份后锁定结果，仍等真实结束或超时才增加轮次。仅本轮手牌已保存的连接能收到结果；重连和round.get也使用同样筛选。结果含client_id和start_event_id，客户端凭本次提交关联接收，不再依赖绑定ack已到达。仍校验结果13张、目标轮次、身份和事件；比牌或结束不重新打开面板。

普通连接中断沿用原请求、事件、轮次重试。进程退出/升级丢失画面连续性时不把旧牌改标到当前轮：服务端已换轮则清掉旧候选并重新观察发牌前→开局；服务端仍在已接受的本机轮次则等待真实结束。第一帧开局之前未同步服务端轮次时不猜版本。

兼容：不带start_event_id的旧hand.submit仍要求旧round.join；旧端结果会携带其旧join事件ID但不要求旧端识别新增字段。升级先服务端后客户端。服务端不得把旧请求自动换成新版本。租户仍由操作者确保对应同一真实游戏，协议没有共同游戏局号。

## 本版验证（2026-09-11）

- `python -m pytest -q`：92项通过（2条依赖弃用警告）。
- `node --test tests/browser/*.test.cjs`：10项通过。
- `tests/test_atomic_submit.py`：无join收齐七份、非法数据事务回滚、事件/请求去重、跨轮返回。
- `tests/test_scenarios.py`：20路真实WebSocket，07→12→07四轮换号，旁观者结果过滤，重连原请求复用，旧轮拒绝。
- 配套Android：57项JVM、22项模拟器常规、1项27轮端到端、1项4404采样录像测试通过。普通24轮本机端到端平均410ms、最慢601ms；这是本机隔离网络测试，不是线上七台模拟器的公网实测。

完整报告随客户端仓库交付：`docs/atomic-submit-test-report-2026-09-11.md`。
