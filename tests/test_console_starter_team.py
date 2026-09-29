"""Exercise the three-employee starter without requiring a browser build."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).parents[1] / "src" / "agentmesh" / "api" / "console_assets"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_starter_team_requires_published_capable_employees_and_keeps_review() -> None:
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const html = fs.readFileSync(process.argv[2], 'utf8');
assert.ok(html.includes('id="use-starter-team"'));
assert.ok(html.includes('id="starter-team-status"'));
const start = source.indexOf('const roleDefaults = [');
const end = source.indexOf('function syncExecutionMode()', start);
assert.ok(start >= 0 && end > start);
const nodes = new Map();
const node = (id) => {
  if (!nodes.has(id)) nodes.set(id, { disabled: false, textContent: '', clears: 0,
    replaceChildren() { this.clears += 1; } });
  return nodes.get(id);
};
const agent = (name, capabilities = ['general.task']) => ({ name, default_version_id: `${name}-v1`,
  versions: [{ id: `${name}-v1`, verified_capabilities: capabilities }] });
const state = { agents: [agent('demo-researcher'), agent('demo-analyst'),
  agent('demo-synthesizer')] };
const context = { t: (text) => text, $: node, state,
  publishedDefaultAgents: () => state.agents };
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const roles = vm.runInContext('starterTeamDefaults', context);
assert.deepEqual(Array.from(roles, (role) => role.agent),
  ['demo-researcher', 'demo-analyst', 'demo-synthesizer']);
assert.deepEqual(Array.from(roles[2].depends), ['research', 'analysis']);
const added = [];
context.addRole = (role) => added.push(role);
assert.equal(context.useStarterTeam(), true);
assert.equal(added.length, 3);
assert.equal(node('role-list').clears, 1);
assert.equal(node('use-starter-team').disabled, false);
state.agents = [agent('demo-researcher'), agent('demo-analyst', [])];
assert.deepEqual(Array.from(context.missingStarterEmployees()),
  ['demo-analyst', 'demo-synthesizer']);
assert.equal(context.useStarterTeam(), false);
assert.equal(node('role-list').clears, 1);
assert.equal(node('use-starter-team').disabled, true);
assert.ok(source.includes('$("task-review-step").classList.remove("hidden")'));
assert.ok(source.includes('$("create-and-run").addEventListener("click",' +
  ' () => submitReviewedTask(true))'));
"""
    result = subprocess.run(
        ["node", "-e", script, str(ASSETS / "app.js"), str(ASSETS / "index.html")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
