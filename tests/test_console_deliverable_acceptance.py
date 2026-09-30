"""Check the optional console delivery gate without a browser build step."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).parents[1] / "src" / "agentmesh" / "api" / "console_assets"


def run_node(script: str) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is not installed")
    result = subprocess.run(
        ["node", "-e", script, str(ASSETS / "app.js"), str(ASSETS / "i18n.js")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_acceptance_builder_pins_one_output_and_keeps_missing_facts_unknown() -> None:
    run_node(r"""
const assert = require('node:assert/strict');
const fs = require('node:fs'); const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const nodes = new Map(); const $ = (id) => {
  if (!nodes.has(id)) nodes.set(id, { value: '', checked: false, disabled: false,
    hidden: false, classList: { toggle(_, hidden) { nodes.get(id).hidden = hidden; } } });
  return nodes.get(id);
};
const ctx = { t: (x) => x, $ }; vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function syncAcceptanceOptions()'),
  source.indexOf('function openCreate(')), ctx);
const plan = [{ key: 'research', depends_on: [] }, { key: 'synthesis', depends_on: ['research'] }];
assert.equal(ctx.buildDeliverableAcceptance({enabled: false}, [], ''), null);
const baseline = { enabled: true, paths: 'summary\nreport.text\nsummary', humanReview: true };
const basic = ctx.buildDeliverableAcceptance(baseline, plan, '');
assert.equal(basic.policy.require_human_review, true);
assert.equal(basic.policy.checks.length, 2);
assert.equal(basic.policy.checks[0].kind, 'OUTPUT_PATH_EXISTS');
assert.equal(basic.policy.checks[0].required, true);
assert.equal(JSON.stringify(basic.policy.checks[1].path), '["report","text"]');
const forks = [{ key: 'one', depends_on: [] }, { key: 'two', depends_on: [] }];
assert.throws(() => ctx.buildDeliverableAcceptance(baseline, forks, ''), /primary/);
assert.equal(ctx.buildDeliverableAcceptance(baseline, forks, 'two').policy.checks.length, 2);
for (const paths of ['summary..text', '__proto__.value'])
  assert.throws(() => ctx.buildDeliverableAcceptance({...baseline, paths}, plan, ''), /valid/);
assert.throws(() => ctx.buildDeliverableAcceptance({...baseline, paths: ''}, plan, ''),
  /at least one/);
const rate = { ...baseline, rateEnabled: true, numerator: '12', denominator: '',
  numeratorUnit: 'ticket', denominatorUnit: 'buyer', scale: '100', operator: 'LTE',
  threshold: '12' };
const built = ctx.buildDeliverableAcceptance(rate, plan, '');
assert.equal(JSON.stringify(built.facts), '{"numerator":{"value":12,"unit":"ticket"}}');
const check = built.policy.checks[2];
assert.equal(check.kind, 'RATE_THRESHOLD'); assert.equal(check.scale, 100);
assert.equal(check.numerator.source, 'TASK_INPUT');
assert.equal(JSON.stringify(check.denominator.path), '["acceptance_facts","denominator"]');
assert.equal(check.denominator.unit, 'buyer'); assert.equal(check.required, true);
assert.equal('claim' in check, false);
const claimed = ctx.buildDeliverableAcceptance({...rate, claimPath:'metrics.claimed_rate'},
  plan, '').policy.checks[2];
assert.equal(claimed.claim.source, 'DELIVERABLE');
assert.equal(claimed.claim.unit, 'ticket/buyer');
assert.equal(JSON.stringify(claimed.claim.path), '["metrics","claimed_rate"]');
assert.throws(() => ctx.buildDeliverableAcceptance({...rate, claimPath:'bad..path'},
  plan, ''), /valid/);
const missing = ctx.buildDeliverableAcceptance({...rate, numerator: ''}, plan, '');
assert.equal(Object.keys(missing.facts).length, 0);
const zero = ctx.buildDeliverableAcceptance({...rate, denominator: '0'}, plan, '');
assert.equal(zero.facts.denominator.value, 0);
for (const change of [{scale: ''}, {scale: '0'}, {threshold: ''}, {numeratorUnit: ''},
  {operator: 'EQ'}, {numerator: 'NaN'}, {denominator: '-1'}])
  assert.throws(() => ctx.buildDeliverableAcceptance({...rate, ...change}, plan, ''));
$('execution-mode').value = 'DIRECT'; $('acceptance-enabled').checked = true;
$('acceptance-rate-enabled').checked = true; ctx.syncAcceptanceOptions();
assert.equal($('acceptance-fields').disabled, true);
assert.equal($('acceptance-rate-fields').disabled, true);
$('execution-mode').value = 'COORDINATED'; ctx.syncAcceptanceOptions();
assert.equal($('acceptance-fields').disabled, false);
assert.equal($('acceptance-rate-fields').disabled, false);
$('acceptance-enabled').checked = false; ctx.syncAcceptanceOptions();
assert.equal($('acceptance-fields').disabled, true);
""")


def test_acceptance_panel_separates_execution_and_human_override() -> None:
    run_node(r"""
const assert = require('node:assert/strict');
const fs = require('node:fs'); const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8'); const nodes = new Map();
const $ = (id) => { if (!nodes.has(id)) nodes.set(id, {
  innerHTML: '', textContent: '', hidden: false,
  classList: { toggle(_, hidden) { nodes.get(id).hidden = hidden; } } }); return nodes.get(id); };
const escapeHtml = (x) => String(x ?? '').replace(/[&<>'"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
let feature = true;
const ctx = { $, t: x => x, escapeHtml, featureEnabled: () => feature }; vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function renderDeliverableAcceptance('),
  source.indexOf('function openDeliverableDecision(')), ctx);
ctx.renderDeliverableAcceptance({status: 'COMPLETED'});
assert.equal($('deliverable-acceptance-panel').hidden, true);
ctx.renderDeliverableAcceptance({status: 'COMPLETED',
  deliverable_acceptance: { status: 'NOT_CONFIGURED' }});
assert.equal($('deliverable-acceptance-panel').hidden, true);
const task = { status: 'COMPLETED', deliverable_acceptance: {
  status: 'FAILED', delivery_allowed: false,
  policy_digest: 'policy', deliverable_digest: 'output', checks: [{key: 'summary',
    description: '<img src=x>', status:'FAIL',
    required:true, reason:'<script>bad</script>', evidence: { value: '<img>' }}] } };
ctx.renderDeliverableAcceptance(task);
assert.equal(task.status, 'COMPLETED');
assert.equal($('deliverable-acceptance-panel').hidden, false);
assert.ok($('deliverable-acceptance-summary').textContent.includes('blocked'));
assert.equal($('download-accepted-deliverable').hidden, true);
assert.equal($('accept-deliverable').hidden, false);
assert.ok($('deliverable-acceptance-checks').innerHTML.includes('&lt;img'));
assert.ok(!$('deliverable-acceptance-checks').innerHTML.includes('<script>'));
task.deliverable_acceptance.status = 'HUMAN_ACCEPTED';
task.deliverable_acceptance.delivery_allowed = true;
task.deliverable_acceptance.human_decision = {
  decision:'ACCEPT', actor:'reviewer', reason:'Override'};
ctx.renderDeliverableAcceptance(task);
assert.ok($('deliverable-acceptance-checks').innerHTML.includes('Check: FAIL'));
assert.equal($('download-accepted-deliverable').hidden, false);
assert.equal($('deliverable-acceptance-audit').hidden, false);
feature = false; ctx.renderDeliverableAcceptance(task);
assert.equal($('accept-deliverable').hidden, true);
feature = true; task.status = 'RUNNING'; ctx.renderDeliverableAcceptance(task);
assert.equal($('reject-deliverable').hidden, true);
""")


def test_human_decisions_are_digest_pinned_idempotent_and_server_authenticated() -> None:
    run_node(r"""
const assert = require('node:assert/strict');
const fs = require('node:fs'); const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8'); const nodes = new Map();
const $ = id => { if(!nodes.has(id)) nodes.set(id, {
  value: '', textContent: '', disabled: false,
  classList:{toggle(){}}, reset(){}, showModal(){}, close(){}, focus(){} });
  return nodes.get(id); };
let enabled = true; let fail = true; const calls = []; const reloads = []; let requestIds = 0;
const state = { selected: { id: 'task-1', status: 'COMPLETED',
  deliverable_acceptance: {
  status:'FAILED', policy_digest:'policy-1', deliverable_digest:'output-1',
  delivery_allowed:false } } };
const ctx = { $, state, t: x=>x, featureEnabled:()=>enabled,
  clientRequestId:()=>`id-${++requestIds}`,
  api: async(path, options)=>{calls.push({path,options});
    if(fail) throw Object.assign(new Error('denied'), {status:403});},
  loadTask:async(id)=>reloads.push(id), toast:()=>{} };
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function openDeliverableDecision('),
  source.indexOf('async function downloadAcceptedDeliverable(')), ctx);
(async()=>{
  enabled = false; ctx.openDeliverableDecision('ACCEPT');
  assert.equal(state.acceptanceDecision, undefined);
  enabled = true; ctx.openDeliverableDecision('ACCEPT');
  state.selected.deliverable_acceptance.policy_digest = 'changed-policy';
  state.selected.deliverable_acceptance.deliverable_digest = 'changed-output';
  await ctx.submitDeliverableDecision({preventDefault(){}}); assert.equal(calls.length,0);
  $('deliverable-decision-reason').value = 'Reviewed exact main output';
  await ctx.submitDeliverableDecision({preventDefault(){}});
  const first = calls[0]; const body = JSON.parse(first.options.body);
  assert.equal(body.expected_policy_digest, 'policy-1');
  assert.equal(body.expected_deliverable_digest, 'output-1');
  assert.equal(body.decision,'ACCEPT'); assert.equal('actor' in body,false);
  assert.equal('requested_by' in body,false);
  assert.equal(first.path,'/api/v1/tasks/task-1/deliverable-acceptance/decision');
  assert.ok($('deliverable-decision-error').textContent.includes('authorized reviewer'));
  await ctx.submitDeliverableDecision({preventDefault(){}});
  assert.equal(calls[1].options.headers['Idempotency-Key'],
    first.options.headers['Idempotency-Key']);
  $('deliverable-decision-reason').value = 'Changed reason';
  fail = false; await ctx.submitDeliverableDecision({preventDefault(){}});
  assert.notEqual(calls[2].options.headers['Idempotency-Key'],
    first.options.headers['Idempotency-Key']);
  assert.equal(reloads[0],'task-1'); assert.equal(state.acceptanceDecision,null);
})().catch(error=>{ console.error(error); process.exitCode=1; });
""")


def test_acceptance_download_uses_server_gate_and_has_chinese_translations() -> None:
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    script = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert '<input id="acceptance-enabled" type="checkbox">' in html
    assert '<fieldset id="acceptance-fields" disabled>' in html
    assert '<fieldset id="acceptance-rate-fields" class="hidden" disabled>' in html
    assert 'if (acceptance) payload.acceptance_policy = acceptance.policy;' in script
    assert 'input.acceptance_facts = acceptance.facts' in script
    assert 'api(`/api/v1/tasks/${encodeURIComponent(task.id)}/accepted-deliverable`)' in script
    assert 'task.deliverable_acceptance.delivery_allowed !== true) return;' in script
    assert 'not attached files' in html
    run_node(r"""
const assert = require('node:assert/strict');
const fs = require('node:fs'); const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1],'utf8');
const state = { selected: {id:'task/download', status:'COMPLETED'} };
let requests=0; let clicks=0; let blobValue=null; let fail=false; let refreshes=0;
const button={disabled:false}; const ctx = {state, $:()=>button,
  api:async(path)=>{requests++;
    assert.equal(path,'/api/v1/tasks/task%2Fdownload/accepted-deliverable');
    if(fail) throw new Error('Delivery blocked');
    return {subtask_key:'main',output:{summary:'Accepted brief'}};},
  Blob:class {constructor(values){blobValue=values[0];}},
  URL:{createObjectURL:()=> 'blob:main',revokeObjectURL(){}},
  document:{createElement:()=>({click(){clicks++;}})}, setTimeout:fn=>fn(),
  toast:()=>{},loadTask:async()=>refreshes++};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('async function downloadAcceptedDeliverable('),
  source.indexOf('function resultSources(')),ctx);
(async()=>{
  await ctx.downloadAcceptedDeliverable(); assert.equal(requests,0);
  state.selected.deliverable_acceptance={status:'FAILED',delivery_allowed:false};
  await ctx.downloadAcceptedDeliverable(); assert.equal(requests,0);
  state.selected.deliverable_acceptance={status:'NOT_CONFIGURED',delivery_allowed:true};
  await ctx.downloadAcceptedDeliverable(); assert.equal(requests,0);
  state.selected.deliverable_acceptance={status:'PASSED',delivery_allowed:true};
  await ctx.downloadAcceptedDeliverable(); assert.equal(requests,1); assert.equal(clicks,1);
  assert.equal(JSON.parse(blobValue).subtask_key,'main'); assert.equal(button.disabled,false);
  fail=true; await ctx.downloadAcceptedDeliverable();
  assert.equal(clicks,1); assert.equal(refreshes,1);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
    run_node(r"""
const assert = require('node:assert/strict');
const fs = require('node:fs'); const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2],'utf8');
const document = {documentElement:{},createTreeWalker:()=>({nextNode:()=>null}),
  querySelectorAll:()=>[],getElementById:()=>null};
const ctx = {document, NodeFilter:{SHOW_TEXT:4}, window:{}, localStorage:{getItem:()=> 'zh-CN'}};
vm.createContext(ctx); vm.runInContext(source,ctx);
const t = ctx.window.AgentMeshI18n.t;
assert.equal(t('Deliverable acceptance'),'交付物验收');
assert.equal(t('Check: UNKNOWN'),'未知');
assert.equal(t('Acceptance: HUMAN_ACCEPTED'),'验收：人工接受');
assert.equal(t('Download accepted main output'),'下载已验收的主要输出');
""")
