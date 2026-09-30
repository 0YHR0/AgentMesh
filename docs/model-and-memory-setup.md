# Set up a real model and employee memory

English | [简体中文](model-and-memory-setup.zh-CN.md)

This guide describes the merged Console. A keyless deterministic demo may be deployed separately;
its operator-specific address and deployment configuration are not published in this repository.
The [screenshot-based customer-feedback walkthrough](scenarios/customer-feedback-deepseek.md)
records a private real-model acceptance run. Saving a real key requires authenticated HTTPS or SSH
administration.

## Three different configurations

| Configuration | What it means | One per employee? |
|---|---|---|
| Model connection | Provider, model, and credential used for paid inference | No; employees can share a connection |
| Employee version | A fixed set of responsibilities, instructions, capabilities, model, and tools | Each employee has its own versions |
| Company memory | Reviewed knowledge that later tasks can retrieve under policy | Can be shared within a company |

A DeepSeek connection does not automatically provide web search, file access, or music generation.
Those require separately configured tools. Memory is AgentMesh-managed knowledge, not a provider's
chat history.

## First real scenario: analyze customer feedback you supply

This needs one working model connection, but no search engine, external memory server, or paid
tool. Supply a batch of redacted customer comments and ask for priorities with supporting evidence.

Create three employees:

1. **Organizer** extracts issues and original quotes without inventing evidence.
2. **Analyst** waits for the organizer, groups recurring issues, ranks impact, and marks uncertainty.
3. **Editor** waits for the analyst and produces a short report and draft replies for you to check.

When creating the task, enter:

- Goal: identify the three most important problems in this week's customer feedback.
- Materials: paste redacted feedback. Entering a local file path does not upload that file.
- Expected output: an issue table, supporting quotes, priority rationale, and three draft replies.
- Execution: coordinated work, selecting a published employee for each step and the prerequisites
  Organizer → Analyst → Editor.
- Success conditions: tie each claim to supplied material, label unsupported inferences, and do
  not send replies externally.

Starting with one employee is also valid. Three steps make responsibilities and intermediate work
inspectable; they do not guarantee better results. An employee named "Editor" is not equivalent to
enabling system-level independent review, automatic revision loops, or publication approval.

## One-time administrator setup

1. Enable identity authentication and prepare an administrator access token. Anonymous demo mode
   cannot store real model credentials.
2. Configure the same connection-encryption master key for API and Worker, outside the database.
3. Open the Console over HTTPS or a verified local/SSH path and enter the platform access token.
4. Add a model connection, select OpenAI or DeepSeek, and provide the model and provider API key.
5. Explicitly test the connection. This contacts the provider and may incur a small charge;
   successfully saving configuration does not prove that inference works.
6. Create employees, define their responsibilities and instructions, bind the connection, then
   publish each version and set it as the default.

The platform access token and provider API key are different secrets. Never put either in task
content, employee instructions, descriptions, screenshots, or Git.

A domain is not required to begin: SSH can provide an encrypted access path. With Docker, ordinary
port publishing may make the API see a bridge address rather than loopback. Verify the deployment's
transport checks instead of disabling them. Do not enter access tokens or provider keys on a public
HTTP page. Back up the encryption master key separately: losing it makes encrypted keys unusable.

## Give the next task useful memory

The initial path uses existing PostgreSQL and needs no Mem0, MemOS, or additional API key. An
administrator enables company and organizational-memory features, selects a company in Memory,
and explicitly initializes the default policy.

Start with a manual note such as:

> Customer replies should acknowledge the issue and provide a clear next step. Never promise an
> unconfirmed fix date.

The note starts as a candidate. After an authorized reviewer accepts it, select the corresponding
company memory scope in a later task's advanced settings. Only then can retrieval include it in
employee context. Unapproved, revoked, or out-of-scope records must not be recalled. Built-in
retrieval uses bounded keyword matching, not guaranteed semantic recall.

### Optional MemOS Cloud pilot

The private pilot can use MemOS Cloud to order already-authorized company memories
semantically. It is **not** required for normal memory use. The operator must enable
`external_memory=true`, configure a single allowlisted Company ID and a server-side
MemOS key, and explicitly allow remote egress. None of these settings are enabled by
default. The key must not be entered into task text or Git.

In the Memory screen, accept a safe PUBLIC/INTERNAL note under the active policy,
then click **Sync approved notes to MemOS** and confirm the external transfer.
This replaces that Company's dedicated remote mirror (25 notes maximum). Run a
memory-enabled task to observe retrieval. If MemOS is down, AgentMesh falls back to
local exact ranking. Revoked notes stop being eligible locally immediately; sync
again to remove their remote copies. This manual deletion gap makes the pilot
unsuitable for sensitive or regulated data.

Automatic learning from task results is a separate explicit option, off by default. Enabling it
still produces candidates for review rather than automatically turning every sentence into trusted
knowledge. Reviewing a note does not by itself establish factual accuracy.

## Memory subjects and review hints

When adding a company note, **Subject key** is optional. Use a stable key for one fact or decision,
for example `release.label-color` and `release.rollout-thresholds`. A key is normalized to lowercase
and accepts 1–128 ASCII letters/digits and `. _ : / -`. Do not include secrets in it. API note and
candidate creation accept `subject_key` too. Existing notes are not automatically relabeled.

Retrieval returns `conflict_status`, `conflict_reason`, and `competing_memory_ids`:

- `UNKNOWN`: no explicit subject, so compatibility has not been assessed. It does **not** mean
  contradictory records were detected or an accepted policy is inapplicable or withdrawn.
- `NO_COMPETING_RECORDS`: no other authorized active content has the same subject within this
  company, namespace and Memory Type. This is not a guarantee of semantic consistency.
- `REVIEW_REQUIRED`: different active contents share that scoped subject. They might be compatible;
  review the evidence, reject an incorrect candidate, revoke an obsolete note, or propose a replacement
  with `supersedes_id` and approve it. The system does not guess a winner or overwrite a note.

For example, a blue-label rule and a margin threshold are not competing merely because both are
DECISION records. Even two rules with the same subject are not automatically confirmed contradictions.
Assessment considers all authorized active retrieval candidates before the count/token limit, so
showing only one result does not hide a competing record. Superseded, revoked, expired and unreviewed
notes do not count. An approved replacement inherits its predecessor's subject when omitted and
cannot change an existing subject; the old version is retired atomically.

Client compatibility: legacy `conflict` is now `null` for UNKNOWN/REVIEW_REQUIRED and `false` for
NO_COMPETING_RECORDS. Prefer the explicit status. New Run contexts carry the assessment and explain
its limits to the employee; historical snapshots are left unchanged.

## Final deliverables and incomplete replies

For coordinated work, **Primary deliverable** defaults to Automatic: a single final work item
becomes the primary result; several final work items are shown together. You can select a final
work item as primary while keeping the other final outputs available. Intermediate results and
the supervisor's execution record remain inspectable.

API clients can set `output_policy` when creating a coordinated Task:

```json
{"mode":"selected","primary_subtask_key":"song","include_subtask_keys":["song","lyrics"]}
```

The primary key must be a terminal work item. Omit `include_subtask_keys` to include all terminal
outputs. Completed Task responses expose `deliverables` and `primary_deliverable`; `output`
retains the supervisor's execution output for existing clients. The policy is pinned in the Task
input snapshot; existing Tasks without a policy use Automatic.

A provider-reported token-limit truncation fails the Run and blocks dependent work. Publish a new
employee Version with a larger output limit, or shorten the work item, then create a new Task.
AgentMesh does not silently increase limits or accept a cut-off answer as complete.

## What to verify

- Executing employee versions and models match the configuration you selected.
- Each step waits for its prerequisites, and intermediate output remains inspectable.
- The real output meets your criteria; workflow success alone does not prove content quality.
- Memory-enabled tasks record retrievals, and future tasks exclude a revoked note.
- Provider failures produce useful, sanitized messages rather than leaking credentials or raw
  provider responses.

External memory, vector retrieval, more provider protocols, and automatic business actions
are optional extensions, not prerequisites for this scenario.
