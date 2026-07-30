# Market intelligence studio company template

Status: Accepted; installation baseline implemented

## Implementation status

The first installable baseline shipped on 2026-07-30:

- a digest-pinned built-in Company Pack creates eight departments, 17 responsibility-bound
  Positions, and seven published Business Object Types;
- preview exposes every resource, required Feature, permission, credential requirement, and the
  default-deny external-write boundary before installation;
- one API transaction creates the Company, all resources, installation configuration, and Outbox
  evidence, so failed installation cannot leave a partial company;
- the Admin Console provides an English/Chinese one-click installer;
- `examples/market-intelligence-studio` runs an offline
  Research Question -> Source Record -> Claim Register -> reviewed Research Report chain without
  model credentials or network research.

This is the installation and evidence-model baseline, not completion of every slice below.
Appointments, Operating Cycles, Goals, recurring Operations, automatic Memory policy installation,
model-backed production, customer connectors, and commercial adapters remain explicit follow-up
work. External publication, outreach, pricing commitments, invoices, and spend remain disabled.

## Outcome

The first non-software Virtual Company template should be an installable AI market-intelligence and
content studio. It composes versioned Organization, Agent, Workflow, Business Object, Memory,
Connector, Policy, and Office Packs; it does not add industry-specific behavior to the AgentMesh
core. The owner selects a market and commercial constraints. The company repeatedly discovers
questions worth answering, produces evidence-backed reports and derivative content, prepares sales
material, supports customers, and measures verified revenue and delivery cost.

This template is chosen because:

- the primary outputs are reviewable digital Artifacts;
- research, product, analysis, writing, review, sales, customer success, and finance have clear
  boundaries;
- a deterministic offline fixture can exercise the complete company loop;
- model-backed operation can begin without financial write access;
- monetization can progress from free samples to reports, subscriptions, and custom research;
- external outreach, pricing, and spending can remain approval-gated.

The template demonstrates Virtual Company OS capabilities. It does not claim that reports will sell
or guarantee revenue. It is a reference composition, not the mandatory AgentMesh organization
model.

## Default company

```text
Company: AgentMesh Market Intelligence Studio
Mission: Turn verified market evidence into useful, trustworthy business intelligence.
Default cycle: Four weeks
Default currency: Configurable single currency
Default risk posture: External publication and commercial terms require approval
```

## Reference departments and positions

### Executive Office

**Owner**

- chooses market, mission, risk appetite, and capital;
- approves cycle, commercial offer, external publication, and material spend.

**Chief of Staff**

- translates owner intent into Objectives, Key Results, Initiatives, and weekly reviews;
- maintains dependency and exception visibility;
- cannot fabricate business metrics.

**COO**

- operates schedules, capacity, delivery queues, and escalation;
- proposes changes when throughput, quality, or budget crosses a threshold.

### Product

**Product Strategist**

- defines target audience and information product;
- prioritizes research questions;
- maintains acceptance criteria and product roadmap;
- uses customer evidence rather than topic popularity alone.

### Research

**Research Lead**

- creates source strategy and evidence requirements;
- decomposes the research plan;
- identifies unsupported claims and evidence gaps.

**Research Specialist**

- gathers authorized sources;
- records source metadata, date, and relevant excerpts within copyright bounds;
- does not treat search snippets or model recall as verified evidence.

### Analysis

**Market Analyst**

- normalizes evidence;
- compares competitors, segments, pricing, and trends;
- separates observation, inference, estimate, and recommendation;
- produces bounded tables and analysis Artifacts.

**Data Analyst**

- processes approved datasets;
- records transformations and limitations;
- produces reproducible charts and metrics.

### Content and Design

**Writer**

- creates the report narrative and derivative content;
- follows brand, audience, citation, and disclosure policies.

**Designer**

- creates charts, layout, cover, and approved promotional assets;
- records asset provenance and licensing.

### Review and Risk

**Fact Reviewer**

- verifies citations, dates, numbers, and claim support;
- returns structured findings independently of the Writer.

**Editorial Reviewer**

- checks clarity, consistency, positioning, and audience fit.

**Risk Officer**

- checks sensitive claims, customer confidentiality, source rights, outbound actions, and Policy
  obligations;
- escalates legal or regulatory questions to a human or external specialist.

### Growth and Customer

**Growth Strategist**

- proposes channels, samples, landing-page copy, and campaigns;
- uses approved positioning and budget.

**Sales Researcher**

- identifies candidate customer organizations using authorized sources;
- creates Lead objects and qualification evidence;
- cannot send unsolicited communication without an approved outreach Operation.

**Customer Success**

- records customer requirements and feedback;
- coordinates accepted custom deliverables;
- proposes product improvements and relationship memories.

### Finance

**Finance Controller**

- attributes model, Tool, data, and production cost;
- prepares pricing and margin estimates;
- reconciles invoice/cash evidence when an adapter is configured;
- maintains separation between forecast and verified revenue.

## Default products

The template may enable:

1. **Free market brief** — acquisition sample with strict scope.
2. **Paid deep-dive report** — one-time digital deliverable.
3. **Recurring intelligence subscription** — scheduled updates.
4. **Custom research engagement** — customer-specific Goal Contract and acceptance criteria.
5. **Content derivative package** — approved article, newsletter, presentation, and social copy
   generated from an accepted report.

Every product has a versioned Offer template with scope, exclusions, evidence standard, delivery
time, estimated cost, price guidance, and required approval.

## Four-week operating cycle

Example Objective:

> Validate and deliver a trustworthy intelligence product for one approved niche.

Example Key Results:

- at least one report passes all fact-review criteria;
- source coverage reaches the approved threshold;
- production stays within the cycle allocation;
- an approved sample is published;
- customer interest and paid outcomes are recorded with explicit verification status.

Activity counts such as “100 sources read” or “50 leads generated” are operational metrics, not
proof of product-market fit.

## Core workflow

```text
Owner thesis
  → Product question proposal
  → Market and evidence preflight
  → Owner/Chief of Staff topic approval
  → Research plan
  → Parallel source and data collection
  → Analysis
  → Product decision: stop, revise, or continue
  → Report draft
  → Fact and editorial review
  → Bounded revision
  → Risk and publication approval
  → Final report Artifact
  → Optional sample/campaign/sales proposal
  → Customer and revenue evidence
  → Cycle review and candidate Memory
```

Stop decisions are valid outcomes. The company should avoid spending more budget on a weak topic
merely because a workflow has started.

## Default recurring operations

| Operation | Schedule/trigger | Output |
|---|---|---|
| `market-signal-scan` | weekly | candidate questions with evidence |
| `source-freshness-review` | weekly | stale-source exceptions |
| `report-production` | approved topic | reviewed report Artifact |
| `content-derivative` | report accepted | bounded content package |
| `lead-qualification` | authorized lead batch | qualified Lead revisions |
| `customer-health-review` | weekly for active customers | risks and next actions |
| `studio-finance-review` | weekly | verified/estimated economics |
| `cycle-management-review` | end of cycle | evidence bundle and next-cycle proposal |

Operations are disabled until their Agent appointments, Tools, budget, Memory Policy, and approval
requirements pass preflight.

## Artifacts

Required Artifact types:

- research question and scope;
- source register;
- evidence matrix;
- dataset snapshot reference;
- analysis notebook/result;
- claim register;
- report draft and final report;
- fact-review findings;
- editorial findings;
- risk review;
- product and pricing proposal;
- campaign or sales package;
- customer acceptance evidence;
- cycle business review.

Artifacts identify producing Run, Agent Version, source references, content digest, and review
status.

## Source and content policy

- sources record URL/identifier, publisher, date, retrieval time, and usage basis;
- source content is bounded and copyright-aware;
- report claims link to evidence;
- generated text cannot cite nonexistent sources;
- model knowledge is labelled as unverified until supported;
- conflicting sources remain visible;
- customer-confidential evidence is excluded from public derivatives;
- generated visual assets record prompt, model, provenance, and review.

## Memory use

### Company memory

- approved market thesis;
- brand and disclosure policy;
- pricing and risk principles;
- cycle decisions.

### Organization-unit memory

- effective source strategy;
- review checklist;
- analysis procedure;
- content style and design system;
- qualification and customer-success SOP.

### Employee memory

- reviewed feedback;
- successful task patterns;
- repeated evidence or quality failures;
- qualification history.

### Relationship memory

- customer preferences;
- approved commitments;
- delivery history;
- unresolved issues;
- consent and retention constraints.

Task output becomes candidate Memory only after review. Complete research content remains an
Artifact, not duplicated into employee Memory.

## Tools and integrations

Initial deterministic template:

- checked-in source and dataset fixtures;
- local Artifact store;
- deterministic Agents;
- no email, web search, payment, or model API.

Model-backed local template:

- configured model Provider;
- read-only search/data MCP Tools;
- document, spreadsheet, chart, and presentation generation;
- optional image generation through reviewed asset pipeline.

Commercial template:

- CRM adapter;
- approved email/calendar adapter;
- website/CMS publication;
- accounting read adapter;
- invoice/offer write adapter;
- all external writes gated by exact credentials, Policy, Permit, and idempotency.

No integration is enabled merely because credentials exist.

## Owner setup flow

1. Choose target market and excluded sectors.
2. Define target customer and product type.
3. Set evidence, citation, and freshness standards.
4. Set cycle budget, deadline, concurrency, and revision bounds.
5. Appoint Agent Versions to required Positions.
6. Bind approved Tools and SecretReferences.
7. Configure outbound, pricing, and financial approval thresholds.
8. Run deterministic qualification and environment preflight.
9. Review and activate the first Operating Cycle.

## Management dashboard

The owner sees:

- active Objectives, Key Results, and Initiatives;
- report pipeline by evidence/review state;
- source freshness and unsupported-claim exceptions;
- production budget reservation and settlement;
- opportunities, approved offers, invoices, and collected cash as separate values;
- customer commitments and overdue deliverables;
- employee workload, review outcomes, and escalation queue;
- organizational Memory candidates awaiting review.

The Office projects organization units, appointments, recurring queues, Handoffs,
Tool/A2A/Policy events,
and verified status. Sensitive customer and financial content remains in authorized inspectors.

## Deterministic showcase

The repository should include an offline fixture:

```text
examples/market-intelligence-studio/
├─ company-template.json
├─ source-fixtures/
├─ dataset/
├─ expected-evidence/
├─ customer-fixture.json
└─ verification.md
```

Scenario:

1. The owner selects a fictional market question.
2. Research discovers one reliable source, one stale source, and one conflicting source.
3. Analysis produces a bounded comparison.
4. Writer creates a draft containing one intentionally unsupported claim.
5. Fact Reviewer rejects the claim.
6. A bounded revision removes or supports it.
7. Risk approval allows local publication.
8. A fictional Opportunity and Offer are created.
9. Finance reports estimated economics but no verified revenue.
10. The cycle review proposes two Memory candidates; one is accepted and one rejected.

This fixture proves truthfulness and governance without claiming business success.

## Delivery slices

### Slice 1 — offline company cycle

- preview and install the template's declarative Packs;
- instantiate Organization Units, Positions, Appointments, cycle, goals, and Operations;
- execute the deterministic showcase;
- produce the complete Artifact and interaction evidence bundle;
- project the company in Console and Office.

### Slice 2 — model-backed report production

- bind real model Agents and read-only research Tools;
- add citation/evidence evaluation;
- generate reviewed report and derivative content;
- retain explicit owner approval before publication.

### Slice 3 — customer and growth operations

- enable CRM/relationship objects;
- qualify authorized leads;
- prepare Offers and customer-specific Goals;
- add approved outbound operations with rate and consent controls.

### Slice 4 — commercial evidence

- enable accounting read/reconciliation;
- issue approved offers/invoices through sandbox adapters;
- show verified revenue, cost, and margin evidence;
- certify unknown-outcome handling before production writes.

## Acceptance criteria

- The offline cycle completes without paid APIs or network access.
- The owner can preview every Pack dependency, permission, credential requirement, and resource
  mutation before installation.
- Removing this template leaves the generic Agent Team execution runtime usable.
- Every report claim is either evidence-linked, labelled inference, or rejected.
- Reviewer failure creates bounded revision evidence rather than overwriting the draft Run.
- External publication, outreach, pricing commitment, invoice, and spend remain separately gated.
- Customer and financial data follows object and Memory authorization.
- Estimated opportunity and margin never appear as verified revenue or cash.
- The owner can pause Operations, reject a topic, disable outbound actions, and close a cycle.
- Restarting workers does not duplicate report publication or commercial actions.
- The final cycle review traces goals, Tasks, Artifacts, Memory candidates, cost, and verified
  business evidence.

## Non-goals

- guaranteeing report sales or profit;
- mass unsolicited outreach;
- autonomous contract execution or payment;
- scraping sources in violation of authorization or usage terms;
- replacing professional legal, tax, investment, or regulated advice;
- publishing unsupported model-generated claims;
- using fictional employee activity as a business KPI.
