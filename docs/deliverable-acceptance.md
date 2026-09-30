# Deliverable acceptance

[English](deliverable-acceptance.md) | [简体中文](deliverable-acceptance.zh-CN.md)

**An employee finishing work is not the same as the result being acceptable.**
Optional acceptance checks make that distinction visible for collaborative Tasks.
Simple Tasks do not need this configuration and keep their existing behavior.

## Example: evaluating a pilot launch

A Researcher collects pilot observations, an Analyst compares them, and an Editor
produces a recommendation. You require a `summary` field and a support-ticket rate
of no more than 12 tickets per 100 buyers. The denominator is **buyers**, not orders:
18 tickets and 100 orders cannot establish this rate if buyer count is missing.

In the Console:

1. Create a collaborative Task and bind the three published employees. Set Research
   as the Analyst's dependency, and both as the Editor's dependencies.
2. Open the optional delivery restrictions, select the Editor as primary if the plan
   has multiple terminal work items, and enable deliverable acceptance.
3. Require the result path `summary`. Optionally enable the rate check: enter the
   numerator and denominator values with their exact units, scale `100`, operator
   `LTE` (less than or equal), and threshold `12`. Leave an unavailable value blank;
   do not substitute an unrelated quantity.
4. If you need a person to approve the result even after checks pass, enable human
   review. Run the Task and inspect its independent acceptance panel.

You can also check an optional structured rate claim in the primary output. For
example, a result `support_rate: {value: 10, unit: "ticket/buyer", scale: 100}` is
checked against the configured source quantities. Checking the input facts alone
does **not** prove that every statement in the generated text is correct.

## Results and control

| Acceptance state | Meaning | Accepted-result export |
| --- | --- | --- |
| Not ready | Execution has not produced the pinned primary result | Blocked |
| Passed | All required configured checks pass | Allowed |
| Failed | At least one required check fails | Blocked |
| Needs review | Required evidence is missing, or human review is mandatory | Blocked |
| Human accepted | An authorized person explicitly accepts the exact result/evidence | Allowed; failed/unknown checks remain visible |
| Human rejected | An authorized person rejects the exact result/evidence | Blocked |

Human acceptance is an **audited override**, not proof that a failed check passed.
It requires the `human_resolution` feature and Task resolution permission. The
decision records the actor, reason, and digests of the policy, output, and evidence.
Refresh before deciding if the output snapshot has changed. Execution stays
`COMPLETED`; it is not confused with the execution-time `WAITING_APPROVAL` barrier.

The accepted-result download only returns the pinned primary deliverable. Other
attachments are not implicitly validated. Raw results remain available for audit;
they are not a substitute for the accepted-result endpoint.

## API example

Include this optional property in a Coordinated Task creation request. Input
quantities are explicit facts supplied by the caller, not an independent data audit.

```json
{
  "acceptance_policy": {
    "require_human_review": false,
    "checks": [
      {"key": "summary", "description": "Summary is present", "kind": "OUTPUT_PATH_EXISTS", "path": ["summary"]},
      {
        "key": "support-rate", "description": "At most 12 tickets per 100 buyers",
        "kind": "RATE_THRESHOLD",
        "numerator": {"source": "TASK_INPUT", "path": ["acceptance_facts", "tickets"], "unit": "ticket"},
        "denominator": {"source": "TASK_INPUT", "path": ["acceptance_facts", "buyers"], "unit": "buyer"},
        "scale": 100, "operator": "LTE", "threshold": 12,
        "claim": {"source": "DELIVERABLE", "path": ["support_rate"], "unit": "ticket/buyer"}
      }
    ]
  },
  "input": {"acceptance_facts": {"tickets": {"value": 18, "unit": "ticket"}}}
}
```

This fragment intentionally omits buyers: the required rate check is `UNKNOWN`,
so the result needs review instead of receiving a fabricated pass. Supply the rest
of the normal Task fields, plan, and published Agent bindings when creating it.

Supported checks are `OUTPUT_PATH_EXISTS` (present and non-null),
`OUTPUT_PATH_EQUALS` (typed JSON equality), and `RATE_THRESHOLD` (`LTE`/`GTE`).
Quantity sources are `TASK_INPUT` and `DELIVERABLE`; units must match exactly.
Negative numerators, nonpositive denominators, nonfinite numbers, invalid units,
and inconsistent claims cannot pass. Policies are size bounded and pinned at
creation; a plan patch cannot remove or change the primary selection.

`GET /api/v1/tasks/{id}` includes `deliverable_acceptance`.
`GET /api/v1/tasks/{id}/accepted-deliverable` rejects an unaccepted or unconfigured
result. Human decisions use `POST /api/v1/tasks/{id}/deliverable-acceptance/decision`
with `decision`, `reason`, both expected policy/output digests, and preferably an
`Idempotency-Key`. The server derives the actor from the authenticated principal.

## Boundaries

- This is a deterministic contract checker, not a general truth detector, semantic
  reviewer, or automatic judge of arbitrary prose. A model's self-reported verdict
  does not grant acceptance.
- It does not undo external tool actions already performed during execution.
  Irreversible actions still require their own governance and approvals.
- Configured results not yet accepted do not enter automatic completed-Task memory
  extraction. Human acceptance does not automatically re-trigger extraction.
- Completion notifications still report execution completion, but omit the result
  summary for opted-in Tasks and direct readers to the independent acceptance state.
- Automatic revision loops and a dedicated external publishing connector remain
  future work. Existing raw audit APIs are intentionally retained.
