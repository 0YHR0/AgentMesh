# Feishu notifications (opt-in)

[简体中文](feishu-notifications.zh-CN.md) | [Documentation index](../README.md)

This integration sends **outbound notifications only**. AgentMesh remains the source of truth for
Tasks, employee handoffs, results, and approvals. A Feishu card reports a Task becoming
`COMPLETED`, `FAILED`, or `WAITING_APPROVAL`, or a governed action awaiting approval; it does not
create Tasks or approve them from chat.
The gate is off in every profile, including `full`.

## Setup

1. Create a Feishu custom app, enable its bot, grant message-send permission, publish/approve the
   app as required by your workspace, and add the bot to a test group. Follow the
   [Feishu message API guide](https://open.feishu.cn/document/server-docs/im-v1/message/create).
2. Obtain its App ID, App Secret, and the target group's `chat_id`. Store the secret in a protected
   `.env` file, never in Git, chat, or a browser on HTTP. This first slice has one configured group
   per deployment/tenant.
3. Add `feishu_notifications=true` to the existing `AGENTMESH_FEATURE_GATES` value. **Do not
   replace** any gates your deployment already uses. Set:

   ```dotenv
   AGENTMESH_FEISHU_APP_ID=cli_...
   AGENTMESH_FEISHU_APP_SECRET=...
   AGENTMESH_FEISHU_CHAT_ID=oc_...
   AGENTMESH_FEISHU_INCLUDE_CONTENT=false
   ```

   `AGENTMESH_FEISHU_TASK_BASE_URL` is optional and must be a public HTTPS Console origin. Without
   it, cards show a Task ID without an insecure HTTP link. `INCLUDE_CONTENT=false` avoids sending
   the Task objective or output to Feishu; set it to `true` only if your data policy allows excerpts.
4. Apply migrations and restart API/Worker with the new gate, then start the optional notifier:

   ```bash
   docker compose up -d --build migrate api worker
   docker compose --profile feishu up -d --build feishu-notifier
   docker compose --profile feishu ps
   ```

5. Run a small test Task. Its terminal or waiting state should appear once in the Feishu test group.
   The bot must be in the group and allowed to speak. No inbound callback URL is required for this
   outbound-only release. If `policy_approval` is also enabled, pending governed actions produce
   separate approval cards.

## Live employee collaboration

After enabling the notification gate, explicitly opt in to coordinated employee updates:

```dotenv
AGENTMESH_FEISHU_SYNC_COLLABORATION=true
# Only if every group member may see business excerpts:
AGENTMESH_FEISHU_INCLUDE_CONTENT=true
AGENTMESH_FEISHU_SEND_INTERVAL_SECONDS=1
```

Restart API, Worker and notifier. This extra setting defaults off. Only coordinated execution
workers with pinned work-item evidence emit start, received-delivery, provisional-result and
failure cards; technical supervisors are not presented as employee discussion. Existing Tasks
are not automatically backfilled. Multiple incoming deliveries are grouped into one start card.

These are actual work-item transfers and bounded public business summaries, not chain-of-thought,
full prompts, raw memory or free-form chat. Metadata-only mode hides summaries. Content opt-in
uses credential-pattern filtering, **not a guarantee of semantic confidentiality**: normal
business conclusions may use permitted knowledge. Use a controlled group and reviewed data
policy. Low-volume notifications normally arrive within seconds; queues, rate limits and retries
can delay delivery. Replying in Feishu does not execute a command, teach memory or approve work.

## Delivery and operations

The [live qualification record](../qualification/feishu-collaboration.md) documents a real
four-employee model run and nine acknowledged Feishu messages, with test scope and limitations.

Task transitions and notification jobs commit atomically in PostgreSQL. The notifier independently
claims jobs, sends with the stable job UUID as Feishu's deduplication UUID, and retries transient
errors with bounded backoff. After eight failed attempts a job becomes `DEAD`; inspect the
`feishu_notifications` table and notifier logs before manually resetting it. A stale
`WAITING_APPROVAL` job is skipped if the Task has already left that state; pending governed approval
jobs are skipped if approved, rejected, or expired. Notification failures do
not roll back or fail Tasks. At-least-once delivery and network ambiguity mean duplicate cards are
still possible if Feishu's deduplication window has expired.

If you disable the gate, stop `feishu-notifier` too. Existing pending jobs are retained, so decide
whether to deliver or discard them before re-enabling. The feature does not read chat history or
grant Feishu users AgentMesh permissions. Inbound Task creation and card approvals require a
separate authenticated integration and are not part of this gate.
