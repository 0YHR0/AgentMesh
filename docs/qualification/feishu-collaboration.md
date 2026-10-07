# Live Feishu collaboration qualification

[Setup and limits](../integrations/feishu-notifications.md) · [简体中文配置](../integrations/feishu-notifications.zh-CN.md)

Validated on 2026-10-07 in an authenticated, private single-team deployment using the owner's
existing bot and controlled test group. Credentials and deployment addresses are not published.

## Scenario and observed result

A coordinated original-song planning task used four separately published employees backed by
the live DeepSeek model connection: creative director, researcher, lyricist, and composer.
The director supplied creative constraints; the researcher used the supplied evidence; lyrics
and arrangement work consumed pinned predecessor results. No live chart research, audio
generation or audio listening is claimed by this text-only qualification.

The task completed, and the private scenario entered its waiting-for-audio stage. PostgreSQL
delivery records showed:

| Event | Delivered | Attempts per event |
| --- | ---: | ---: |
| Employee started work, including pinned incoming deliveries | 4 | 1 |
| Employee provisional result | 4 | 1 |
| Task completed | 1 | 1 |

All nine deliveries received successful acknowledgements from the real Feishu send API.
This confirms API acceptance, not a claim that a human read the messages. Transfers are actual
pinned collaboration evidence, not model internal reasoning or invented chat.

## Regression evidence

- 50 focused configuration, projection, filtering and notifier tests passed.
- 23 tests passed against an isolated PostgreSQL database, including atomic rollback,
  duplicate-save protection, gate defaults, tenant/task/run binding and delivery after completion.
- The full non-PostgreSQL regression suite passed.
- GitHub CI passed quality, unit coverage, PostgreSQL integration, Compose E2E,
  dependency review and CodeQL checks.

## Limits

The shared public demo keeps live collaboration sync disabled. New deployments default to
metadata-only notifications with both the parent feature gate and collaboration opt-in off.
Private business excerpts require explicit content opt-in. Credential-pattern filtering is not
semantic confidentiality; do not place secrets in Task objectives or employee summaries.
No prompts, raw memory, raw provider errors or full creative artifacts are selected for cards.
This is outbound notification, not two-way group chat or approval from Feishu.

The live run did not exercise provider outage, exhausted retries, scale, rate limits, audio
review or duplicate delivery after Feishu's deduplication window; those are not qualified here.
