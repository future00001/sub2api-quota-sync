#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 或 sudo 运行。" >&2
  exit 1
fi

for command in python3 flock systemctl install stat sed grep getent cut groupadd useradd nologin; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "缺少命令: $command" >&2
    exit 1
  fi
done

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

install -d -m 0755 /opt/sub2api-quota-sync
install -m 0755 "$root_dir/quota_sync.py" /opt/sub2api-quota-sync/quota_sync.py
install -m 0644 "$root_dir/README.md" /opt/sub2api-quota-sync/README.md
install -m 0644 "$root_dir/sub2api-quota-sync.service" /etc/systemd/system/sub2api-quota-sync.service
install -m 0644 "$root_dir/sub2api-quota-sync.timer" /etc/systemd/system/sub2api-quota-sync.timer

plugin_root=/opt/sub2api/deploy/data/plugins/installed/com.hzyhz.sub2api-quota-sync
service_uid=${SUB2API_UID:-1000}
service_gid=${SUB2API_GID:-1000}
if [ -d "$plugin_root" ]; then
  service_uid=$(stat -c %u "$plugin_root")
  service_gid=$(stat -c %g "$plugin_root")
fi
case "$service_uid:$service_gid" in
  0:*|*:0|*[!0-9:]*|:*|*:)
    echo "拒绝使用 root 或无效 UID/GID 运行 sidecar: $service_uid:$service_gid" >&2
    exit 1
    ;;
esac

service_group=$(getent group "$service_gid" | cut -d: -f1 || true)
if [ -z "$service_group" ]; then
  if getent group sub2api-quota-sync >/dev/null 2>&1; then
    echo "sub2api-quota-sync 组已存在但 GID 不是 $service_gid" >&2
    exit 1
  fi
  groupadd --gid "$service_gid" sub2api-quota-sync
  service_group=sub2api-quota-sync
fi

service_user=$(getent passwd "$service_uid" | cut -d: -f1 || true)
if [ -z "$service_user" ]; then
  if getent passwd sub2api-quota-sync >/dev/null 2>&1; then
    echo "sub2api-quota-sync 用户已存在但 UID 不是 $service_uid" >&2
    exit 1
  fi
  useradd --uid "$service_uid" --gid "$service_gid" --no-create-home \
    --home-dir /nonexistent --shell "$(command -v nologin)" \
    --comment "Sub2API quota sync sidecar" sub2api-quota-sync
  service_user=sub2api-quota-sync
fi

sed -i "s/^User=.*/User=$service_user/; s/^Group=.*/Group=$service_group/" /etc/systemd/system/sub2api-quota-sync.service

if [ ! -e /etc/sub2api-quota-sync.key ]; then
  install -m 0600 /dev/null /etc/sub2api-quota-sync.key
fi

if [ ! -e /etc/sub2api-quota-sync.env ]; then
  install -m 0600 "$root_dir/sub2api-quota-sync.env.example" /etc/sub2api-quota-sync.env
else
  if [ ! -s /etc/sub2api-quota-sync.key ] && grep -q '^SUB2API_ADMIN_API_KEY=.' /etc/sub2api-quota-sync.env; then
    legacy_key=$(sed -n 's/^SUB2API_ADMIN_API_KEY=//p' /etc/sub2api-quota-sync.env | tail -n 1)
    printf '%s\n' "$legacy_key" > /etc/sub2api-quota-sync.key
    chmod 0600 /etc/sub2api-quota-sync.key
  fi
  sed -i '/^POSTGRES_CONTAINER=/d; /^REDIS_CONTAINER=/d; /^REDIS_DB=/d; /^SUB2API_ADMIN_API_KEY=/d; /^SUB2API_ADMIN_API_KEY_FILE=/d' /etc/sub2api-quota-sync.env
  grep -q '^SUB2API_BASE_URL=' /etc/sub2api-quota-sync.env || printf '%s\n' 'SUB2API_BASE_URL=http://127.0.0.1:18080' >> /etc/sub2api-quota-sync.env
  grep -q '^REQUEST_TIMEOUT_SECONDS=' /etc/sub2api-quota-sync.env || printf '%s\n' 'REQUEST_TIMEOUT_SECONDS=15' >> /etc/sub2api-quota-sync.env
  grep -q '^STATE_PATH=' /etc/sub2api-quota-sync.env || printf '%s\n' 'STATE_PATH=/var/lib/sub2api-quota-sync/state.sqlite3' >> /etc/sub2api-quota-sync.env
fi

systemctl daemon-reload
systemctl enable --now sub2api-quota-sync.timer

echo "sidecar 已安装。请在 Sub2API 插件设置中配置账号和目标分组。"
