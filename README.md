# Sub2API 7d 配额同步插件

将 OpenAI OAuth 账号的 Codex 7d 配额周期同步到 Sub2API 订阅分组。当任一已选账号进入新的 7d 周期时，后台执行器会幂等重置所选分组内有效订阅的额度窗口。

## 功能

- 在 Sub2API 插件管理页多选账号和目标订阅分组。
- 支持 weekly、monthly，以及与二者组合的 daily 配额窗口。
- 首次运行只建立基线，不追溯重置。
- SQLite 事件和逐订阅状态确保同一账号周期最多创建一次任务。
- 通过 Sub2API Admin API 重置配额，由 Sub2API 统一处理数据库事务和缓存失效。
- API 响应丢失时根据额度窗口变化恢复，避免重复重置。
- 支持演练模式和多个安全检测阈值。
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

## 默认检测阈值

- 新周期最小跳变：24 小时。
- 新周期最小剩余时间：120 小时。
- 时间抖动容差：6 小时。

这些字段只用于确认账号确实进入新周期，不会限制订阅刷新频率。

## 验证与日志

```bash
python3 -m unittest -v test_quota_sync.py
sudo systemctl start sub2api-quota-sync.service
sudo journalctl -u sub2api-quota-sync.service -n 50 --no-pager
sudo systemctl list-timers sub2api-quota-sync.timer
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
