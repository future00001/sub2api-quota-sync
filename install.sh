#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 或 sudo 运行。" >&2
  exit 1
fi

for command in docker python3 flock systemctl install; do
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

if [ ! -e /etc/sub2api-quota-sync.env ]; then
  install -m 0600 "$root_dir/sub2api-quota-sync.env.example" /etc/sub2api-quota-sync.env
fi

systemctl daemon-reload
systemctl enable --now sub2api-quota-sync.timer

echo "sidecar 已安装。请在 Sub2API 插件设置中配置账号和目标分组。"
