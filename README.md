# JiPaiQi Serve

十三水双副牌手牌汇总服务。每个租户对应一张桌子，7个不同客户端各上报13张完整手牌，从两副标准牌（104张，不含大小王、无留底）中按花色和点数的数量相减，得出剩余13张。少于7份不生成确定结果。

## 技术栈与目录

- Python 3.12+、FastAPI、Uvicorn（单进程）、SQLite（WAL）。
- Jinja2页面模板、独立CSS、原生JavaScript ES modules；不需要Node构建。
- HTTP处理管理操作和首次读取；WebSocket推送客户端上报、在线状态、版本变化和结果。
- `app/domain.py`：事务、版本状态机、计数校验、去重。
- `app/main.py`：HTTP路由、WebSocket入口、连接管理和超时任务。
- `app/templates/`：公共布局与总览、工作台、租户、历史页面。
- `app/static/js/components/`：共享手牌行、8×13结果网格、工作台、下拉菜单。
- `tests/`：真实SQLite和HTTP/WebSocket协议测试。
- `scripts/simulate.py`：真实WebSocket模拟7台客户端，显式指定测试租户。
- `scripts/update.sh`：服务器从当前Git分支的upstream更新，备份数据库、安装依赖、重启检查，失败恢复代码与依赖。
- `preview/index.html`：保留已确认的静态设计参考，不连接数据库。

## 启动

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8768 --workers 1
```

打开 http://127.0.0.1:8768 。默认数据库为 `data/jpq.sqlite3`，可通过 `JPQ_DB_PATH` 指定。空数据库自动建表，不自动插入演示数据。

本阶段按需求不设管理员登录、角色权限或设备令牌；打开页面即可管理。`tenant_id`只是数据分区标识，不是身份凭证。页面HTTP和WebSocket默认要求同源（无Origin的原生客户端允许），不配置跨域访问。对外可访问范围由部署环境控制。

## 租户与版本

- `tenant_id`是1～32位数字组成的**字符串**，例如 `"100001"`、`"001002"`，前导零保留。
- 创建租户：ID、备注（单个输入框）、当前版本（默认整数1）、超时秒数（默认180）。
- 每个租户最多登记7个客户端。客户端首次连接自动登记，重连使用固定`client_id`，禁止每次连接生成新ID。
- 版本唯一键：`tenant_id + round_version`；不同租户可以有相同版本。
- 每客户端每轮一份：`tenant_id + round_version + client_id`。
- `waiting → collecting → ready → closed`。结束/超时可从collecting直接到closed。
- 首个合法join使waiting进入collecting；首份**有效手牌入库**设置固定截止时间。重试、重连、后续上报不续期。
- 收齐7份且校验通过后锁定结果，保持本版本；一个已绑定客户端的结束事件即可关闭本轮。
- 关闭与创建下一版本在同一事务完成。关闭原因：`game_end / timeout / manual / version_changed`。
- 历史手牌与结果只读。编辑版本允许不变或推进到大于当前版本的新整数（上限2147483646）；禁止回退/复用，避免迟到请求撞上旧编号。
- 达到数字上限的最后一轮关闭后停用该租户，下一版本为null，需新建租户；不会绕回1或影响其他租户的超时处理。
- 时间戳统一为Unix毫秒；后台时间与“今日”指标统一按北京时间（Asia/Shanghai）显示/统计。
- 停用租户关闭其当前轮次并创建待用版本，拒绝客户端业务写入；重新启用等待新局。
- 服务重启恢复版本、已接收数据、请求确认和截止时间；已超时轮次先关闭再接受新请求。

## 局间同步边界

客户端分别维护服务端版本和手牌绑定版本。查询到更大版本只更新服务端状态、清空旧显示，**不得给旧手牌换上新版本**。

`round.join`携带`start_event_id`和`sync_basis`：

- `initial_start`：首次加入的客户端，在本版本等待/收集期内，确实观察到发牌前到新局的切换；`previous_round_version=null`。不是“启动后看见13张牌”。
- `end_then_start`：已参与客户端观察到上一局结束、再观察到新局；`previous_round_version`须为当前版本减1，且该客户端上次绑定的确为这个版本。
- 同一次start事件无论如何重试只能绑定原版本。离线期间漏过局间切换、半局启动、落后多轮时返回`SYNC_REQUIRED`，等待同步或后台重新登记设备。

客户端“一键配”等按钮在摆牌过程中持续出现，只是界面存在信号，不能每次命中都生成新局事件。摆牌、撤销、手牌减少或恢复、按钮短暂消失不解锁本局；半局启动不能直接 initial_start。服务端版本推进也不是本机观察到结束的证据。这些规则由客户端状态机实现，现有 WebSocket v1 字段无需增加。

未识别共同游戏局号，本服务无法证明截图属于哪局；上述新局证据由可信客户端负责识别。版本号只能隔离已正确绑定的数据，不能修复漏检/误检。正常客户端结束特征须连续确认，单客户端误报结束会提前关闭本轮。

## WebSocket协议（protocol_version=1）

连接：`/ws/client?tenant_id=100001&client_id=device_001`。ID与消息体须一致。每客户端仅一个活动连接，新连接替换旧连接。服务器连接成功推送当前状态及已锁定结果。

请求公共字段（未知字段拒绝）：

```json
{
  "protocol_version": 1,
  "type": "round.get",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "tenant_id": "100001",
  "client_id": "device_001",
  "round_version": null,
  "payload": {}
}
```

| type | round_version | payload |
|---|---|---|
| `round.get` | null或指定版本 | `{}`，查询状态；已关闭历史不补推旧结果 |
| `round.join` | 查询到的当前整数 | `{"start_event_id":"UUID","sync_basis":"initial_start","previous_round_version":null}` |
| `hand.submit` | 已绑定版本 | `{"cards":[{"rank":"A","suit":"s"}, ...]}`，必须13项 |
| `round.end` | 已绑定版本 | `{}`，稳定识别结束后发送 |
| `ping` | null | `{}`，建议每15秒一次，45秒无消息断开 |

花色仅接受 `s=黑桃 / h=红桃 / c=梅花 / d=方块`；点数仅接受字符串`A,2,3,4,5,6,7,8,9,10,J,Q,K`。两副相同牌在上传数组中保留重复元素；不以整手内容跨客户端去重。

服务器公共字段：`protocol_version,type,tenant_id,round_version,server_time_ms,payload`。请求回复另含`reply_to=request_id`。服务器推送不关联任何一个客户端的上报ID。

- `ack`：业务操作**提交数据库后**确认。payload含`action,duplicate`；join另有`bound_round_version`，end另有`next_round_version`。
- `round.state`：payload含`state,enabled,received_count,expected_count,missing_client_ids,unregistered_count,first_received_at_ms,deadline_at_ms,close_reason,bound_round_version`；最后一个字段为当前连接设备的绑定版本（未绑定为null）。客户端状态不包含其他客户端的手牌。
- `round.result`：payload为`{"remaining_count":13,"cards":[{"rank":"A","suit":"s","count":2}]}`，各count合计13、每项1～2。同花色的两行表示最多两张相同牌，列固定A到K，无表头。
- `pong`：心跳确认。
- `error`：payload为`{"code":"ROUND_CLOSED","message":"本轮已关闭"}`，不丢连接，可纠正后发新请求。

去重：请求唯一键为`tenant_id+client_id+request_id`，相同请求返回原确认，改变内容重用ID返回`REQUEST_CONFLICT`。同客户端同版本同手牌（忽略排列顺序）重复提交也确认，不重复扣牌；不同手牌返回`HAND_CONFLICT`。7份数量累计超过任一牌的2张上限返回`DECK_OVERFLOW`，事务不写入这份错误手牌。

`request_id`和`start_event_id`使用UUID或其他持久唯一值，只允许英文字母、数字、下划线、连字符，最长100字；每次新业务操作换ID，重试原操作沿用ID和原消息内容。完整13张上传示例见 [客户端消息示例](docs/client-examples.md)。

其他错误：`INVALID_MESSAGE, TENANT_NOT_FOUND, TENANT_DISABLED, CLIENT_LIMIT, CLIENT_NOT_FOUND, VERSION_MISMATCH, ROUND_NOT_FOUND, ROUND_CLOSED, ROUND_READY, NOT_JOINED, SYNC_REQUIRED, START_EVENT_CONFLICT`。拒绝旧版本业务写入，不自动转交最新版本。超时任务与写入串行，结束/第7份/重复消息并发也不会双重计算或推进两次。

WebSocket消息上限64KiB，HTTP JSON上限64KiB。重连有退避；发送成功不等于入库，必须等待ack。断线可能漏推送，客户端需round.get恢复当前状态，并丢弃不属于绑定版本的结果。

## 管理HTTP与实时页面

| 方法/路径 | 用途 |
|---|---|
| `GET /api/tenants` | 租户、在线设备、当前版本摘要 |
| `POST /api/tenants` | `{tenant_id,note,round_version,timeout_seconds}` |
| `PATCH /api/tenants/{id}` | `{note,round_version,timeout_seconds,enabled}`，字段可选 |
| `GET /api/tenants/{id}/rounds/current` | 当前完整工作台，包括每个客户端上报手牌 |
| `GET /api/tenants/{id}/rounds/{version}` | 历史完整工作台 |
| `POST /api/tenants/{id}/close` | `{round_version}`，携带期望版本避免重复关闭下一轮 |
| `GET /api/rounds?tenant_id=&reason=&limit=20&offset=0` | 分页历史（closed），筛选可选 |
| `GET /api/summary` | 总览真实指标 |
| `DELETE /api/tenants/{id}/clients/{client_id}` | 重新登记失步设备：仅waiting且设备离线时允许 |
| `GET /health` | 存活检测 |

`/ws/admin`推送`admin.snapshot`：完整总览摘要（租户、在线数、指标）。页面发送`{"type":"subscribe","tenant_id":"100001"}`后，额外收到该租户当前完整`admin.round`。切换订阅立即发送快照。首次读取HTTP + WS连接后再次快照，重连同样补齐；历史展开数据走HTTP，不用实时轮次覆盖历史。

页面路径：`/`总览、`/live`实时工作台、`/tenants`租户管理、`/history`版本记录。公共布局、手牌和结果网格复用，实时数据不含任何静态演示值。

## 测试、演示与部署

完整验收与静态HTML报告：

```sh
.venv/bin/python scripts/test_report.py
```

先安装下方测试依赖。报告输出`reports/test-report.html`，自动使用独立服务和临时SQLite，覆盖真实模拟客户端的上报、下发、异常、并发、重连和服务重启，不影响现有后台数据。完整操作与覆盖范围见[测试说明](docs/testing.md)。

```sh
pip install -r requirements-dev.txt
pytest -q
# 服务运行后，使用独立租户演示；不会写入其他租户
python scripts/simulate.py --url http://127.0.0.1:8768 --tenant 900001
```

目标：第7份有效手牌到达后2秒内把结果送达在线客户端，性能由协议集成测试记录，实际手机链路另行验收。

当前验证结果和环境见 [验证记录](docs/verification.md)。直接运行服务不需要前端编译；维护时可用 `ruff format app tests scripts` 和 `npx prettier@3.6.2 --write 'app/static/js/**/*.js' 'app/static/css/*.css' 'app/templates/**/*.html'` 格式化源码。

全新Ubuntu 24.04部署、systemd、Nginx/WSS、备份和升级见 [部署文档](docs/deploy-ubuntu24.md)。手机APK现有HTTP上报模块尚须按本协议改造；此项目不自动修改或安装APK。

服务器首次安装Git并`git clone`部署后，后续将代码推送到GitHub，再运行以下命令更新服务：

```sh
sudo bash /opt/jpq/scripts/update.sh
```

脚本跟随当前分支，不强制切到main；没有新提交不重启，有本地修改或分叉则停止。更新时会停服，数据库及旧依赖备份在`/var/backups/jpq-updates/`，自动回退保留当前数据库。详细行为与私有仓库配置见部署文档第1、5节。


### 2026-09-10 联调回归

本地完整80项Python回归、23项真实TCP/HTTP/WS场景报告见 `reports/full-audit-20260910/server-final.html`。网页连接还需执行 `node --test tests/browser/live_connection.test.cjs`（6项）；覆盖静默断线、浏览器离线、退役连接迟到事件、重连订阅与清理。工作台现在收到离线事件立即显示待同步，35秒无消息则强制恢复连接。HTML禁用iframe嵌入；替换客户端时不在业务锁内等待旧连接关闭。这里的记录不代表线上已经部署，完整客户端联合报告位于相邻JiPaiQi-Mobile的docs/full-integration-audit-2026-09-10.md。
