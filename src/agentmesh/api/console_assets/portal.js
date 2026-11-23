/* Shared same-origin portal session. Never put bearer credentials in URLs or localStorage. */
(function(root){
  "use strict";
  const key="agentmesh-token";
  function safe(location){return location.protocol==="https:"||["localhost","127.0.0.1","::1"].includes(location.hostname);}
  function workspaceLinks(items){
    const seen=new Set(),result=[];
    for(const item of Array.isArray(items)?items:[]){
      if(item.enabled!==true)continue;
      for(const workspace of item.workspaces||[]){
        const route=workspace.route;
        if(typeof route!=="string"||!/^\/[A-Za-z0-9/_-]+$/.test(route)||route.startsWith("//")||seen.has(route))continue;
        seen.add(route);result.push({route,name:String(workspace.name||item.name||"Workspace"),builtin:item.trust==="built-in"});
      }
    }
    // Installed business workspaces replace starter examples in the primary
    // navigation. Their original routes remain available for compatibility.
    const primary=result.some(item=>!item.builtin)?result.filter(item=>!item.builtin):result;
    return primary.map(({route,name})=>({route,name}));
  }
  function signOut(){
    root.sessionStorage.removeItem(key);
    // Notification only, not a token. Other open portal tabs clear their own session.
    try{root.localStorage.setItem("agentmesh-signout",String(Date.now()));}catch{}
    root.location.assign("/");
  }
  const api={safe,workspaceLinks,signOut};
  if(typeof module!=="undefined"&&module.exports)module.exports=api;
  if(root.document){
    root.AgentMeshPortal=Object.freeze(api);
    root.addEventListener("storage",event=>{
      if(event.key==="agentmesh-signout"){
        root.sessionStorage.removeItem(key);root.location.reload();
      }
    });
  }
})(typeof window!=="undefined"?window:globalThis);
