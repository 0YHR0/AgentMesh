"""Exercise the zero-build result presenter with real JavaScript when Node is available."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).parents[1] / "src" / "agentmesh" / "api" / "console_assets" / "app.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_result_selection_and_safe_readable_text() -> None:
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function resultSources(task)');
const end = source.indexOf('function renderPlanning()', start);
assert.ok(start >= 0 && end > start);
const nodes = new Map();
const node = (id) => {
  if (!nodes.has(id)) nodes.set(id, { textContent: '', innerHTML: '', classList: { toggle() {} } });
  return nodes.get(id);
};
const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]);
const context = {
  t: (text) => text, escapeHtml, $: node, state: { artifacts: [] },
  taskLinkedArtifacts: () => [], document: { querySelectorAll: () => [] }
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);

const direct = {
  execution_mode: 'DIRECT', status: 'COMPLETED', runs: [], subtasks: [],
  output: { summary: '1. Improve search\n2. Fix login\n3. Simplify billing',
    agent: { id: 'editor', kind: 'deepseek-chat-completions' } }
};
assert.equal(context.resultSources(direct).length, 1);
assert.equal(context.readableResultText(direct.output).split('\n').length, 3);
context.renderTaskResult(direct);
assert.ok(node('task-result-content').innerHTML.includes('Improve search'));
assert.ok(node('task-output').textContent.includes('deepseek-chat-completions'));

const coordinated = {
  execution_mode: 'COORDINATED', status: 'COMPLETED', runs: [],
  output: { summary: 'Demo agent completed', agent: { kind: 'deterministic-demo' } },
  subtasks: [
    { key: 'research', depends_on: [], status: 'COMPLETED', input: { role: 'Researcher' },
      output: { summary: 'Evidence',
        agent: { id: 'researcher', kind: 'deepseek-chat-completions' } } },
    { key: 'synthesis', depends_on: ['research'], status: 'COMPLETED', input: { role: 'Editor' },
      output: { summary: 'Final brief',
        agent: { id: 'editor', kind: 'deepseek-chat-completions' } } }
  ]
};
assert.equal(context.resultSources(coordinated)[0].label, 'Editor');
context.renderTaskResult(coordinated);
assert.ok(node('task-result-content').innerHTML.includes('Final brief'));
assert.ok(!node('task-result-content').innerHTML.includes('Demo agent completed'));

const demo = { ...direct, output: { summary: '<img src=x onerror=alert(1)>',
  agent: { id: 'demo-agent', kind: 'deterministic-demo' } } };
context.renderTaskResult(demo);
assert.ok(node('task-result-content').innerHTML.includes('&lt;img'));
assert.ok(!node('task-result-content').innerHTML.includes('<img'));
assert.ok(node('task-result-content').innerHTML.includes('演示结果只验证执行流程'));
"""
    result = subprocess.run(["node", "-e", script, str(APP)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
