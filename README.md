# Sub2API 7d 配额同步插件

将 OpenAI OAuth 账号的 Codex 7d 配额周期同步到 Sub2API 订阅分组。当任一已选账号进入新的 7d 周期时，后台执行器按所选同步模式工作：订阅模式下幂等重置所选分组内有效订阅的额度窗口；余额模式下按用户属性 `SubscriptionLimit` 重置用户余额。另提供用户额度划转页面，让用户按邮箱互相划转余额。

## 功能

- 在 Sub2API 插件管理页多选账号和目标订阅分组。
- 同步模式二选一：`subscription`（订阅，默认）或 `balance`（余额）。
- 支持 weekly、monthly，以及与二者组合的 daily 配额窗口。
- 首次运行只建立基线，不追溯重置。
- SQLite 事件和逐订阅/逐用户状态确保同一账号周期最多创建一次任务。
- 通过 Sub2API Admin API 重置配额或余额，由 Sub2API 统一处理数据库事务和缓存失效。
- API 响应丢失时根据额度窗口或目标余额变化恢复，避免重复重置。
- 支持演练模式和多个安全检测阈值。
- 用户额度划转服务：幂等键防重、限流、失败自动补偿。
- 宿主插件被误启用时提供 HTTP 双向流式透传，避免 OAuth 请求中断。

## 架构

Sub2API `v0.1.183` 的插件协议只提供 `openai.oauth.outbound_transport.v1`，没有后台事件 API。因此本项目由两部分组成：

1. 签名 `.s2plugin`：提供插件管理页中的配置 UI。
2. 非 root systemd sidecar：每分钟通过本机 Admin API 读取快照并执行幂等同步。

sidecar 不访问 Docker socket、PostgreSQL 或 Redis。Admin API Key 由 systemd credentials 从
`/etc/sub2api-quota-sync.key` 注入，不放入环境变量或插件配置。运行状态保存在
`/var/lib/sub2api-quota-sync/state.sqlite3`。

插件卡片的宿主“启用”不是业务开关。真正的同步开关是设置页内的“启用自动同步”。

## 兼容性

- Sub2API：`>=0.1.183-0`
- 已测试：`0.1.183`、`0.1.183-custom-r2`
- 运行环境：Linux amd64、Docker Compose、Python 3.11+

## 安装

### 1. 信任发布者公钥

在 Sub2API `config.yaml` 中加入：

```yaml
plugins:
  trusted_publishers:
    hzyhz-sub2api-quota-sync-v1: "1PQAwkKIEo3ISL98p7czBcwGRP8rJK0wx655XGh6M/g="
```

重启 Sub2API 使配置生效。

### 2. 上传原生插件

从 [Releases](https://github.com/future00001/sub2api-quota-sync/releases) 下载最新 `.s2plugin`，在 Sub2API 插件管理页上传。生产使用的签名私钥不在仓库或插件包中。

### 3. 安装 sidecar

```bash
git clone https://github.com/future00001/sub2api-quota-sync.git
cd sub2api-quota-sync
sudo ./install.sh
```

安装脚本会根据插件目录所有者设置 sidecar 的非 root UID/GID，默认是 `1000:1000`。

### 4. 配置 Admin API Key

在 Sub2API 管理设置中生成 Admin API Key，然后以 root 身份写入专用凭据文件：

```bash
sudo install -m 0600 /dev/null /etc/sub2api-quota-sync.key
sudoedit /etc/sub2api-quota-sync.key
```

密钥文件只能包含一行完整密钥。默认 API 地址是 `http://127.0.0.1:18080`；如 Sub2API 使用其他
本机地址，请修改 `/etc/sub2api-quota-sync.env` 中的 `SUB2API_BASE_URL`。

### 5. 配置同步

打开“7d 订阅配额同步”设置：

1. 勾选账号和目标分组。
2. 保持“演练模式”，打开“启用自动同步”并保存。
3. 等待一分钟后点击“测试已保存配置”。
4. 确认账号、分组和有效订阅数量正确后关闭演练模式。

从 `v0.3.x` 升级后，首次 API 版运行只建立新的本地基线，不会追溯重置。旧的 PostgreSQL 状态表
不会删除，但执行器不再读取或写入它们。

## 余额模式

订阅模式下用户之间无法划转额度。余额模式把配额放进用户余额（可划转），在设置页把“同步模式”
改为“余额模式”即可。余额模式不依赖分组：检测到新 7d 周期时，执行器读取**全部有效用户**，
把每个用户的余额**重置为**其用户属性 `SubscriptionLimit` 的值（Admin API `set`，不是累加）。

部署前提：

- 在 Sub2API 管理后台的“用户属性配置”中添加 key 为 `SubscriptionLimit` 的属性（文本类型即可），
  并为目标用户填写数值。未填写或不是正数的用户会被跳过并告警，不会被清零。
- 注意：仍处于有效订阅中的用户，请求计费走订阅窗口，余额不会被消耗；订阅过期或撤销后
  计费才自动走余额。
- 可用环境变量 `BALANCE_ATTRIBUTE_KEY` 更换属性 key（默认 `SubscriptionLimit`）。

## 额度划转

`install.sh` 同时安装常驻服务 `sub2api-quota-sync-transfer.service`（默认监听
`127.0.0.1:18083`），让用户按邮箱互相划转余额。划转具备幂等键防重复提交、每用户限流、
失败自动补偿（充值失败会把扣减退回），全部记录保存在
`/var/lib/sub2api-quota-sync/transfer.sqlite3`。

启用步骤：

1. 在宿主机 nginx 的站点配置中加入反向代理并重载：

   ```nginx
   location /transfer/ {
       proxy_pass http://127.0.0.1:18083;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
   }
   ```

2. 在 Sub2API 管理设置的自定义菜单（`custom_menu_items`）中添加一项：名称如“额度划转”，
   URL 为 `https://<你的域名>/transfer/`，可见性选“用户”。

3. 用户打开侧栏菜单即可看到划转页面。页面 iframe 会自动携带用户 JWT，服务用它调用
   `GET /api/v1/user/profile` 验证身份，只能划出自己的余额。

可调项（`/etc/sub2api-quota-sync.env`）：`TRANSFER_LISTEN`、`TRANSFER_MIN_AMOUNT`（默认 1）、
`TRANSFER_RATE_LIMIT`（默认每用户每分钟 5 次）、`TRANSFER_DB`。修改后
`systemctl restart sub2api-quota-sync-transfer.service`。

## 默认检测阈值

- 新周期最小跳变：24 小时。
- 新周期最小剩余时间：120 小时。
- 时间抖动容差：6 小时。
- 观察快照不覆盖已确认的周期边界，避免提前吃掉真实重置事件。
- 所有自动重置都必须确认账号 7d 用量刷新：最新百分比不高于 5%，且比上次有效采样下降至少 1 个百分点；缺少百分比或没有下降时不清零。
- 在上述必要条件之外，正常周期还要求快照越过已确认边界、恢复至少 120 小时并满足最小跳变阈值。
- 提前重置需要同时满足：用量下降至少 1 个百分点且回到 5% 以下、新周期起点落在前后两次采样区间（允许 5 分钟偏差）、边界推进超过时间抖动容差（默认 6 小时）、剩余时间恢复至少 120 小时且不超过 7 天加 5 分钟。
- 秒级/分钟级倒计时变化不会单独刷新额度；时间抖动容差同时限制提前重置的最小边界推进，单次百分比修正不再触发同步。

这些字段只用于确认账号确实进入新周期，不会限制订阅刷新频率。

## 验证与日志

```bash
python3 -m unittest -v test_quota_sync.py test_transfer_server.py
sudo systemctl start sub2api-quota-sync.service
sudo journalctl -u sub2api-quota-sync.service -n 50 --no-pager
sudo systemctl list-timers sub2api-quota-sync.timer
sudo journalctl -u sub2api-quota-sync-transfer.service -n 50 --no-pager
```

确认服务未持有 root 或 Docker 权限：

```bash
systemctl show sub2api-quota-sync.service -p User -p Group -p NoNewPrivileges
systemctl cat sub2api-quota-sync.service
```

## 构建原生插件

原生运行时需要 Go 1.27：

```bash
cd native
go test ./...
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o dist/plugin .
```

`native/package_plugin.py` 用于签名打包。不要把生成的 Ed25519 私钥提交到 Git。

## 许可证

本项目采用 [LGPL-3.0](LICENSE)。`native/pluginapi` 来源于 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api)，详见 [NOTICE](NOTICE)。

## 精确周期对齐（2026-09-27）

订阅同步需要宿主 `/admin/subscriptions/:id/reset-quota` 支持 `cycle_reset_at`。
部署前先应用 `patches/sub2api-cycle-reset.patch` 并构建宿主，再更新执行器。
补丁基于本机 Sub2API 定制源码；应用前检查版本及上下文，不能只升级执行器。

确认新周期后，执行器把账号下次重置时间传给宿主。宿主在事务和行锁内清空选中的额度，
周窗口起点设置为账号下次重置时间减 7 天，因此周倒计时准确对齐；月窗口同样从账号本次
周期起点重新计算 30 天，日窗口仍保持每天零点刷新。
相同周期重试不会再次清空已产生的用量，未激活订阅也可以首次同步。
插件的同步事件和逐订阅状态仍用于失败恢复，不能通过删除状态库来强制同步。

验证：`python3 -m unittest -v test_quota_sync.py test_transfer_server.py`。
宿主补丁验证：`go test ./internal/service ./internal/handler/admin -run 'Test(CycleSync|BulkSubscriptionAction|Weekly)' -count=1`。

## 单次定时使用重置卡（v0.6.0）

在插件设置底部开启“一次性预约”，选择账号和**北京时间（UTC+8）**，保存即可。
例如卡片在 2026-11-07 07:22 到期，可预约 2026-11-07 02:00。日期带年份，
后台约每分钟检查一次，浏览器关闭后仍会执行。新功能默认关闭，不会自动创建预约。

- 独立于订阅自动同步开关，但遵守演练模式：演练时不消费卡片，并在到点后关闭本次预约。
- 卡片信息来自宿主账号缓存；如果没有到期信息，先在账号页面查询重置卡，再刷新插件选项。
- 调用宿主现有手动用卡接口一次；调用前会确认预约时的到期卡仍可用。
- 先将任务持久化到 SQLite，再关闭开关，然后发出一次用卡请求。执行成功后显示结果。
- 响应超时、进程中断或结果未知时不会重试，避免误用第二张卡；请人工核对后另建预约。
- 错过时间超过 15 分钟、卡片失效或开启了宿主按阈值自动用卡时，关闭预约而不消费。
- 防重状态存储在 STATE_PATH 同目录的 reset-card-schedule.sqlite3，不得删除来重跑任务。
- 正常再次预约会生成新任务 ID；普通保存不改变 ID。账号按阈值自动用卡与本功能不要同时开启。

验证：python3 -m unittest -v test_quota_sync.py test_transfer_server.py test_reset_card_schedule.py；
原生插件：go test ./... 与 go vet ./...；界面回归使用 native/ui/test-harness.html，
它只模拟保存和执行结果，不会连接生产 API。

本次保留了服务器 2026-10-23 已生效的窗口费用下降/周期漂移跟随逻辑；不回退到旧源码实现。
兼容性提示不全局关闭：插件宿主状态恢复为 disabled（业务同步仍由自己的开关控制），
并将实际验证过的 0.2.18-custom-r1 写入清单。

### 发布者密钥轮换

打包器支持 --key-id 指定独立发布者 ID（默认保留旧 ID）。新增发布者时先备份宿主配置，
追加新的公钥而非覆盖旧条目，重启宿主后上传重新签名的插件；不要关闭签名校验。
本部署 v0.6.0 使用 hzyhz-sub2api-quota-sync-v2，公钥为
6W618fATkOF9QDiOpCzbQJ79pMMJn1ZLqklYQvgeAQ0=。私钥不进入版本控制或插件包。
