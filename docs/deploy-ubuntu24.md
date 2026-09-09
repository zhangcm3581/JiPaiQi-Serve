# Ubuntu 24.04 部署

本项目无需 Node、Redis、MySQL。Ubuntu 24.04 自带 Python 3.12，使用一个 Uvicorn 进程和一个 SQLite 文件。此版本按需求无登录系统，能访问该地址的人可直接管理租户；部署在自己控制的网络内。后续需要公开服务时再增加管理端认证与设备凭证。

## 1. 安装 Git 并从 GitHub 部署

以下命令在 Ubuntu 服务器上执行。先将本项目代码提交并推送到 GitHub，服务器通过 Git 克隆；本机 `.venv`、`data`、缓存和联调数据库不上传。将下方的“你的GitHub仓库地址”替换成实际仓库地址，`main`替换成要部署的分支名。

```sh
sudo apt update
sudo apt install -y git curl ca-certificates util-linux python3 python3-venv python3-pip nginx
sudo adduser --system --group --home /var/lib/jpq jpq
sudo install -d -o jpq -g jpq -m 700 /var/lib/jpq
sudo git clone --branch main 你的GitHub仓库地址 /opt/jpq
sudo python3 -m venv /opt/jpq/.venv
sudo /opt/jpq/.venv/bin/python -m pip install -r /opt/jpq/requirements.txt
sudo cp /opt/jpq/deploy/jpq.service /etc/systemd/system/jpq.service
sudo systemctl daemon-reload
sudo systemctl enable --now jpq
curl --fail http://127.0.0.1:8768/health
```

应返回 `{"status":"ok"}`。数据库首次启动自动建表，默认没有租户和演示数据。

公开仓库可以使用HTTPS地址；私有仓库可以使用SSH地址，并为执行`sudo git`的root用户配置只读GitHub Deploy Key。先通过`sudo git ls-remote 你的GitHub仓库地址`确认服务器能读取仓库；不要把访问令牌写进仓库URL或脚本。

如果之前按旧文档复制代码到了`/opt/jpq`，该目录没有`.git`，不能直接更新。先停止服务，将原目录改名保留，例如`sudo mv /opt/jpq /opt/jpq-before-git`（目标目录必须不存在），再从上述`git clone`步骤克隆和创建新虚拟环境，然后启动服务。数据库位于独立的`/var/lib/jpq`，不需要搬动；不要再次创建已有的系统用户。原目录和依赖保留到验证新服务正常后再处理。

进程用户 `jpq` 只需要写 `/var/lib/jpq`；代码和依赖保持 root 所有。SQLite 的 `.sqlite3`、`-wal`、`-shm` 文件均须位于这个可写目录。不要把数据库放在 NFS 等网络共享上。

## 2. Nginx 和 WebSocket

复制Nginx模板后，在`/etc/nginx/sites-available/jpq`里把`jpq.example.com`改为实际域名或内网IP。服务器的自定义配置留在`/etc`中，保持Git工作目录干净，便于之后更新：

```sh
sudo cp /opt/jpq/deploy/nginx.conf /etc/nginx/sites-available/jpq
sudo nano /etc/nginx/sites-available/jpq
sudo ln -s /etc/nginx/sites-available/jpq /etc/nginx/sites-enabled/jpq
sudo nginx -t
sudo systemctl reload nginx
```

页面、HTTP 接口和 WebSocket 共用同一地址。示例：

- 后台：`http://服务器地址/`
- 手机：`ws://服务器地址/ws/client?tenant_id=100001&client_id=device_001`
- 后台实时连接：`ws://服务器地址/ws/admin`

域名访问并已有 DNS 指向时，可用 Let's Encrypt 配置 HTTPS：

```sh
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d 实际域名
sudo certbot renew --dry-run
```

浏览器将自动使用 `wss://`；手机也应填写 `wss://实际域名/ws/client`。HTTP 明文及自签名证书是否被 APK 接受由 APK 的网络安全配置决定。不要通过关闭证书校验解决生产连接问题。

云安全组/防火墙按实际访问网络放行 80/443，8768 只监听本机，不对公网开放。WebSocket 反向代理必须保留 `Upgrade`、`Connection` 和 `Host`。应用层每 15 秒心跳，45 秒无消息会断线，重连后重新拉取快照。

**必须保持 `--workers 1`。** 活动连接和广播在单进程内管理；SQLite 保存业务状态。多 worker/多实例需要额外的跨进程广播机制，目前未引入。

## 3. 初始化与联调

打开后台 → 租户管理 → 创建租户，填数字字符串 ID、备注和当前版本（默认 1）。7 台客户端须使用相同租户 ID、不同且固定的客户端 ID。每份手牌严格 13 张，`s/h/c/d` 花色和字符串点数；先绑定新局再上传。

可以用附带脚本验证一整轮。使用独立测试租户，不要连接真实牌桌租户：

```sh
cd /opt/jpq
.venv/bin/python scripts/simulate.py --tenant 900001 --rounds 2 --end
```

此脚本使用真实 HTTP/WebSocket，会创建测试租户并在 SQLite 中留下两轮记录；不是页面里的假数据。少一份时不计算；收齐时状态变为“结果已锁定”，直到结束/首份数据后的 180 秒截止再进入下一版本。`--delay-last 30 --hold-seconds 60` 可以检查等待动画和实时更新。

手机 APK 的协议接入属于后续联调项，本项目不会直接修改 APK。

## 4. 备份和恢复

SQLite 开启 WAL。运行时只复制 `.sqlite3` 文件可能漏掉已提交数据；使用 SQLite backup API 得到一致快照：

```sh
sudo install -d -o jpq -g jpq -m 700 /var/backups/jpq
sudo -u jpq /opt/jpq/.venv/bin/python - <<'PY'
import sqlite3
from datetime import datetime, timezone
name = '/var/backups/jpq/jpq-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '.sqlite3'
source = sqlite3.connect('/var/lib/jpq/jpq.sqlite3')
destination = sqlite3.connect(name)
source.backup(destination)
destination.close()
source.close()
print(name)
PY
```

恢复时先停服务，保留整个当前数据目录，然后把选定备份复制到新的 `/var/lib/jpq/jpq.sqlite3`，重建目录权限并启动。不要让旧 WAL 与恢复的数据库混用：

```sh
sudo systemctl stop jpq
sudo mv /var/lib/jpq /var/lib/jpq-before-restore
sudo install -d -o jpq -g jpq -m 700 /var/lib/jpq
sudo install -o jpq -g jpq -m 600 /var/backups/jpq/选定备份.sqlite3 /var/lib/jpq/jpq.sqlite3
sudo systemctl start jpq
```

`jpq-before-restore` 必须是尚不存在的新目录名。恢复旧备份会让版本落后于客户端，恢复后在后台把版本前推至未使用的编号，并在等待新局时重新登记失步客户端；不得把手中旧牌直接换版本重传。

## 5. 从 GitHub 更新服务

代码推送到GitHub后，在服务器执行：

```sh
sudo bash /opt/jpq/scripts/update.sh
```

脚本自动读取当前分支及它的upstream，例如`main → origin/main`，不切换分支。流程如下：

1. 获取更新、确认能快进；存在本地改动、未跟踪文件、分支分叉或另一个更新任务时，停止并提示，不覆盖代码。
2. 若没有新提交，直接结束，不重启服务。
3. 停止`jpq.service`，使用SQLite backup API备份数据库并校验，保存更新前后的提交号和旧虚拟环境。
4. 快进到已经获取的目标提交，重新创建`.venv`并安装新版本依赖，检查依赖和Python语法。
5. 启动服务，同时检查systemd运行状态和`/health`返回值。成功后输出新提交号。
6. 依赖安装、代码检查或启动失败时，自动恢复原提交、原虚拟环境并尝试启动原服务，脚本仍以非零状态退出，便于发现更新失败。

更新期间服务会暂停，时长取决于依赖下载和启动速度，建议在没有进行中的牌局时更新。脚本不会覆盖租户、手牌和版本数据库；**失败回退也不会自动恢复旧数据库备份**，避免覆盖新服务已经收到的数据。若后续版本引入不兼容的表结构变更，需要按该版本的迁移/恢复说明操作，不能只回退代码。

备份位置：`/var/backups/jpq-updates/update-时间-随机串/`，包括：

- `database.sqlite3`：更新前的数据库一致快照。
- `commit-before.txt`、`commit-after.txt`：更新前后的提交号。
- `requirements-before.txt`、`venv-before/`：原依赖；若发生回退，旧虚拟环境已移回项目目录。
- `failed-venv/`：失败时保留的新依赖，供排查。

备份目录仅root可读。脚本不自动清理备份，确认无需回退后按需清理旧备份，避免虚拟环境备份占满磁盘。

默认使用本部署文档的路径。若手动修改过systemd服务路径或端口，更新脚本也须使用一致配置，例如：

```sh
sudo env JPQ_APP_DIR=/opt/jpq JPQ_SERVICE=jpq.service JPQ_DB_PATH=/var/lib/jpq/jpq.sqlite3 JPQ_HEALTH_URL=http://127.0.0.1:8768/health bash /opt/jpq/scripts/update.sh
```

脚本不会自动覆盖`/etc/systemd/system/jpq.service`或Nginx配置。若某次发布明确要求修改服务配置，应按发布说明同步修改，再执行`systemctl daemon-reload`和相应服务重启/重载。

## 6. 排查

```sh
sudo systemctl status jpq
sudo journalctl -u jpq -n 100 --no-pager
sudo journalctl -u jpq -f
sudo nginx -t
```

- 页面能开但一直重连：检查 Nginx WebSocket 请求是否返回 101，Host 是否原样转发，手机/页面是否使用正确 ws/wss 协议。
- 已上报但缺一份：查看该客户端在线状态、绑定版本、13 张校验错误；不会用 6 份推断确定的 13 张。
- `SYNC_REQUIRED`：客户端漏过了局间切换。等待新局，暂停对应客户端，在后台“客户端”窗口于 waiting 状态移除登记，再让客户端从可靠的新局特征重新绑定。
- `DECK_OVERFLOW`：累计某个花色点数超过两张，需修正识别模板；服务端拒绝该份且不扣牌。
- 重启后版本增加：首份手牌起算的截止时间已过，启动时正常关闭超时轮次。

历史和去重记录当前持久保留。定期关注磁盘容量；第一版不自动清理历史，不会删除仍可能被重试引用的记录。
