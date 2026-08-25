# 运维手册

## 日常检查

```bash
sudo /opt/grid-assistant/scripts/healthcheck.sh
systemctl is-active hermes-gateway.service hermes-gateway-member2.service rsshub.service
systemctl list-timers grid-assistant.timer grid-assistant-health.timer grid-assistant-backup.timer
df -h /opt/grid-assistant
free -h
```

## 手动运行

```bash
sudo -u gridassistant /opt/grid-assistant/bin/grid-assistant daily --force --no-send
```

去掉 `--no-send` 会真实发送，操作前先确认目标用户。

## 备份与恢复

```bash
sudo /opt/grid-assistant/scripts/backup.sh
sudo /opt/grid-assistant/scripts/restore.sh /opt/grid-assistant/backups/YYYYMMDD-HHMMSS
```

恢复脚本会校验 SHA-256 和 SQLite 完整性，并拒绝项目备份目录之外的路径。恢复演练应优先在复制的数据库上进行。

## 故障定位

- 管理员微信无响应：检查 `hermes-gateway.service` 是否 active。
- 第二用户微信无响应：检查 `hermes-gateway-member2.service` 是否 active，并确认其独立 `HERMES_HOME` 权限。
- 发送到了错误微信：停止手动发送，运行 `test-dual-user.py` 检查 `user_delivery_routes`；不要互换两个网关的 `.env`。
- 新闻为空：检查 `rsshub.service`、`/healthz` 和 `source_health` 表。
- 无早报：检查 timer、交易日历缓存和 `user_reports` 状态。
- 数据异常：系统应停止相应网格，不要手工绕过。
- 内存压力：确认 RSSHub `MemoryMax=384M`，应用 `MemoryMax=768M`，swap 正常。

只查看必要的结构化状态；不要把包含认证信息的完整环境或宽泛日志复制到外部。
