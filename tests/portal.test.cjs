const test=require('node:test'),assert=require('node:assert/strict');
const {safe,workspaceLinks}=require('../src/agentmesh/api/console_assets/portal.js');
test('portal shares only same-origin workspace paths, not credentials or external links',()=>{
  assert.deepEqual(workspaceLinks([{enabled:true,workspaces:[
    {route:'/music-production',name:'Music Production'},
    {route:'//evil.test',name:'Bad'}, {route:'https://evil.test',name:'Bad'},
    {route:'/x?token=secret',name:'Bad'}, {route:'/../x',name:'Bad'},
    {route:'/music-production',name:'Duplicate'}]},
    {enabled:false,workspaces:[{route:'/disabled'}]}]),
    [{route:'/music-production',name:'Music Production'}]);
});
test('portal credential entry is HTTPS or loopback only',()=>{
  assert.equal(safe({protocol:'https:',hostname:'private.ts.net'}),true);
  assert.equal(safe({protocol:'http:',hostname:'127.0.0.1'}),true);
  assert.equal(safe({protocol:'http:',hostname:'100.1.2.3'}),false);
});
test('installed workspace takes primary navigation priority over built-in starter',()=>{
  const starter={enabled:true,trust:'built-in',workspaces:[{route:'/starter',name:'Starter'}]};
  const installed={enabled:true,trust:'local',workspaces:[{route:'/business',name:'Business'}]};
  assert.deepEqual(workspaceLinks([starter,installed]),[{route:'/business',name:'Business'}]);
  assert.deepEqual(workspaceLinks([starter]),[{route:'/starter',name:'Starter'}]);
});
