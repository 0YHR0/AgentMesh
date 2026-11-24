# Automatic employee learning for application authors

Applications can offer personal learning without asking users to configure a policy
or review each record. This is opt-in application behavior, not a change to the default
reviewed-company preset.

In an authorized company workflow, call
`OrganizationalMemoryService.ensure_employee_learning_policy(company_id, employee_ids=[...])`
with active appointed AgentDefinition IDs. Put the returned policy ID in the new task's
`company_context.memory_policy_id`, together with company/workforce bindings.

The additive, idempotent profile reads existing accepted company/project notes and
personal experience. Runtime assembly narrows scope to the **actual pinned employee**,
not a caller-supplied employee ID. Writes are restricted to explicit employee namespaces.
Feedback, patterns, preferences and procedures can be automatically accepted; shared
company/project records cannot be published by this profile. Default limits are eight
recalled records, 2,000 estimated context tokens and a 90-day lifetime.

Runtime assembly supplies the bounded `memory_candidates` output contract, reusing the
normal model call rather than making a separate summarization request. Empty learning
is valid. Capture requires matching successful winning outputs of a completed task,
including existing deliverable-acceptance checks. Coordinated auto-accept consent applies
only to the bound employee's private candidates; shared candidates still require review.

Repeated completion processing is idempotent. Identical accepted personal content is
reused across runs; rejected, revoked or expired text is not automatically resurrected.
This is exact-content deduplication, not semantic conflict resolution.

Set `company_context.memory_learning=false` to disable extraction for one task while
retaining recall. `memory_mode="EXPLICIT"` continues to disable both hidden recall and
capture for controlled experiments or frozen snapshots. Never rewrite old task inputs
when introducing these defaults.

Recalled content is evidence, not instruction or permission authority. Task success is
not proof of a lesson's factual/artistic quality. Applications should show provenance,
provide forget controls, and retain separate spending/publication gates. PostgreSQL
remains authoritative; an external ranker cannot grant access. Local forgetting is not
a promise to delete copies previously exported to another service.
