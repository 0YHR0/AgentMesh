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

Automatic learning from task results is a separate explicit option, off by default. Enabling it
still produces candidates for review rather than automatically turning every sentence into trusted
knowledge. Reviewing a note does not by itself establish factual accuracy.

## What to verify

- Executing employee versions and models match the configuration you selected.
- Each step waits for its prerequisites, and intermediate output remains inspectable.
- The real output meets your criteria; workflow success alone does not prove content quality.
- Memory-enabled tasks record retrievals, and future tasks exclude a revoked note.
- Provider failures produce useful, sanitized messages rather than leaking credentials or raw
  provider responses.

External memory adapters, vector retrieval, more provider protocols, and automatic business actions
are optional extensions, not prerequisites for this scenario.
