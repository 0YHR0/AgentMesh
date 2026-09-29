# 飞书通知（可选）

[English](feishu-notifications.md) | [文档导航](../README.zh-CN.md)

当前只支持**单向通知**。任务、员工交接、结果和审批记录仍以 AgentMesh 为准。任务进入
`COMPLETED`、`FAILED` 或 `WAITING_APPROVAL` 时，以及治理动作等待审批时，飞书测试群会收到
卡片；暂不支持在飞书里
创建任务或直接审批。所有内置配置（包括 `full`）都默认关闭此 Gate。

## 配置步骤

1. 在飞书开放平台创建自建应用，开启机器人和发送消息权限，按企业要求发布/审批应用，并把机器人
   加入测试群。参考[飞书发送消息文档](https://open.feishu.cn/document/server-docs/im-v1/message/create)。
2. 获取 App ID、App Secret 和群 `chat_id`。App Secret 只保存在受保护的服务器 `.env` 中，
   不要放到 Git、聊天消息或 HTTP 网页。目前每个部署/租户配置一个目标群。
3. 在**现有** `AGENTMESH_FEATURE_GATES` 后追加 `feishu_notifications=true`，不要覆盖已有
   Gate，并配置：

   ```dotenv
   AGENTMESH_FEISHU_APP_ID=cli_...
   AGENTMESH_FEISHU_APP_SECRET=...
   AGENTMESH_FEISHU_CHAT_ID=oc_...
   AGENTMESH_FEISHU_INCLUDE_CONTENT=false
   ```

   可选的 `AGENTMESH_FEISHU_TASK_BASE_URL` 必须是公网 HTTPS 的 Console 地址。没有域名/
   HTTPS 时留空，卡片显示任务 ID，不加入不安全的 HTTP 链接。默认不外发任务描述或结果正文；
   只有确认数据策略允许时才把 `INCLUDE_CONTENT` 改为 `true`。
4. 执行迁移、重启 API/Worker，再启动独立通知进程：

   ```bash
   docker compose up -d --build migrate api worker
   docker compose --profile feishu up -d --build feishu-notifier
   docker compose --profile feishu ps
   ```

5. 创建并运行一个小测试任务。任务完成、失败或进入待人工处理时，测试群应收到一条卡片。
   如果还开启了 `policy_approval`，待处理的治理审批也会产生单独卡片。
   机器人需要在群内并有发言权限。单向通知不需要配置飞书回调地址。

## 可靠性与边界

任务状态和通知待办在同一个 PostgreSQL 事务提交。通知进程独立领取待办，使用固定 UUID
去重，失败后有界退避重试；连续失败八次进入 `DEAD`，可先检查 `feishu_notifications`
表和通知日志，再由管理员手动恢复。状态已变化或过期的旧审批通知会跳过。飞书故障不会让任务失败。
在网络结果不明确且飞书去重窗口已过时，仍可能收到重复卡片。

关闭 Gate 后也应停止 `feishu-notifier`；已有待办会保留，重新开启前需决定发送还是丢弃。
此功能不读取群聊记录，也不给飞书用户 AgentMesh 权限。飞书创建任务、卡片审批属于后续需要
独立鉴权的双向集成，不包含在这个 Gate 中。
