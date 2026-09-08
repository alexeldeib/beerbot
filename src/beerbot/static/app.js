"use strict";
const $ = id => document.getElementById(id);
const number = value => new Intl.NumberFormat().format(value);
const text = (tag, value, className) => { const node = document.createElement(tag); node.textContent = value; if (className) node.className = className; return node; };
const status = message => { $("status").textContent = message; };
async function api(path, body) {
  const response = await fetch(`/app/api/${path}`, body === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) { const error = new Error(typeof data.detail === "string" ? data.detail : "Please check your entry and try again."); error.status=response.status; throw error; }
  return data;
}
async function submit(form, action, errorTarget="status") {
  const button = form.querySelector('button[type="submit"]'); button.disabled=true; status("");
  try { await action(); } catch(error) { $(errorTarget).textContent=error.message || "Could not connect. Please try again."; } finally { button.disabled=false; }
}
let currentOwner="", editing=null, undoing=null, pendingSave=null, writableCatalog=[];
function loggingHint(){
  const selected=writableCatalog.find(w=>w.id===$("log-workspace").value);
  $("logging-hint").textContent=!selected?"Create an app-only group to start, or ask for app logging access to your existing group.":selected.activity_mode==="native"?"App-only group. No chat messages will be sent.":"Included in this group's GroupMe stats. No chat message will be posted.";
}
$("log-workspace").addEventListener("change",loggingHint);
try { pendingSave=JSON.parse(sessionStorage.getItem("beerbot-pending-save")); } catch { /* no draft */ }
function keepPending(value) {
  pendingSave=value;
  try { if(value)sessionStorage.setItem("beerbot-pending-save",JSON.stringify(value));else sessionStorage.removeItem("beerbot-pending-save"); } catch { /* memory still protects same-page retries */ }
  $("retry-save").hidden=!value;
}
async function saveCommand(path, values) {
  const signature=JSON.stringify({path,values});
  if(pendingSave && (pendingSave.owner!==currentOwner || pendingSave.signature!==signature))throw new Error("A previous save needs confirmation. Use ‘Retry last save safely’ before making another change.");
  const pending=pendingSave || {owner:currentOwner,signature,path,values,request_id:crypto.randomUUID()};
  keepPending(pending);
  try { const result=await api(path,{...values,request_id:pending.request_id});keepPending(null);return result; }
  catch(error) { if(error.status && error.status<500)keepPending(null);else status("The save has not been confirmed. Retry it safely; don't submit a second entry.");throw error; }
}
$("retry-save").addEventListener("click",async()=>{
  if(!pendingSave)return;
  const pending=pendingSave;
  try { await saveCommand(pending.path,pending.values);for(const dialog of document.querySelectorAll('dialog[open]'))dialog.close();await loadDashboard();status("Your previous save is confirmed."); }catch(error){status(error.message);}
});
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>$(button.dataset.close).close()));
$("new-group").addEventListener("click",()=>{$("group-error").textContent="";$("group-dialog").showModal();});
$("new-group-form").addEventListener("submit",event=>{event.preventDefault();submit(event.currentTarget,async()=>{
  const result=await saveCommand("groups",{name:$("group-name").value.trim()});$("group-dialog").close();$("group-name").value="";await loadDashboard();$("log-workspace").value=result.workspace_id;loggingHint();status("App group created. You can log your first entry.");
},"group-error");});
function drinkValues(prefix){return {quantity:Number($(prefix+"-quantity").value),drink_type:$(prefix+"-type").value,split_the_g:Number($(prefix+"-splits").value)};}
$("drink-form").addEventListener("submit",event=>{event.preventDefault();submit(event.currentTarget,async()=>{
  await saveCommand("activity",{workspace_id:$("log-workspace").value,...drinkValues("log")});await loadDashboard();status("Drink logged.");
});});
$("edit-form").addEventListener("submit",event=>{event.preventDefault();submit(event.currentTarget,async()=>{
  await saveCommand(`activity/${editing.app_entry_id}/edit`,{revision:editing.revision,...drinkValues("edit")});$("edit-dialog").close();await loadDashboard();status("Entry updated.");
},"edit-error");});
$("undo-form").addEventListener("submit",event=>{event.preventDefault();submit(event.currentTarget,async()=>{
  await saveCommand(`activity/${undoing.app_entry_id}/undo`,{revision:undoing.revision});$("undo-dialog").close();await loadDashboard();status("Entry undone.");
},"undo-error");});
function renderEntry(row){
  const node=text("div","","activity-row"),detail=text("div","");
  detail.append(text("p",row.drink_type.replaceAll("_"," "),"activity-title"),text("p",new Date(row.logged_at).toLocaleString("en-US",{month:"short",day:"numeric",year:"numeric",hour:"numeric",minute:"2-digit",timeZone:"America/New_York"})+" ET"+(row.split_the_g?` · ${row.split_the_g} split the G`:""),"muted"));
  detail.append(text("span",row.app_entry_id?"Logged in app":"Logged in GroupMe","source-label"));
  if(row.editable){const controls=text("div","","entry-controls"),edit=text("button","Edit","quiet"),undo=text("button","Undo","quiet");
    edit.addEventListener("click",()=>{editing=row;$("edit-type").value=row.drink_type;$("edit-quantity").value=row.quantity;$("edit-splits").value=row.split_the_g;$("edit-error").textContent="";$("edit-dialog").showModal();});
    undo.addEventListener("click",()=>{undoing=row;$("undo-description").textContent=`${row.quantity} ${row.drink_type.replaceAll("_"," ")} · ${new Date(row.logged_at).toLocaleDateString()}`;$("undo-error").textContent="";$("undo-dialog").showModal();});
    controls.append(edit,undo);detail.append(controls);
  }
  node.append(detail,text("span",number(row.quantity),"activity-quantity"));return node;
}
$("email-form").addEventListener("submit",event=>{event.preventDefault();submit(event.currentTarget,async()=>{
  await api("login",{email:$("email").value.trim()}); $("email-form").hidden=true;$("code-form").hidden=false;$("code").value="";$("code").focus();
});});
$("code-form").addEventListener("submit",event=>{event.preventDefault();submit(event.currentTarget,async()=>{await api("verify",{code:$("code").value.trim()});await loadDashboard();});});
$("back").addEventListener("click",()=>{$("email-form").hidden=false;$("code-form").hidden=true;status("");$("email").focus();});
$("logout").addEventListener("click",async()=>{try{await api("logout",{});keepPending(null);location.reload();}catch(error){status(error.message);}});
$("group").addEventListener("change",()=>loadDashboard($("group").value));
function showLogin(){ $("dashboard").hidden=true;$("logout").hidden=true;$("login").hidden=false; }
let requestGeneration=0;
async function loadDashboard(group="") {
  const generation=++requestGeneration; status("Loading your history…");
  try {
    const data=await api("me"+(group?`?group=${encodeURIComponent(group)}`:""));
    if(generation!==requestGeneration)return;
    $("login").hidden=true;$("dashboard").hidden=false;$("logout").hidden=false;status("");
    $("greeting").textContent=data.person.name;$("email-display").textContent=data.person.email;
    currentOwner=data.person.email;
    if(pendingSave && pendingSave.owner!==currentOwner)keepPending(null);
    $("retry-save").hidden=!pendingSave;
    if(pendingSave)status("A previous save needs confirmation. Retry it safely before making another change.");
    $("logging").hidden=!data.logging_enabled;
    const selectedWorkspace=$("log-workspace").value;
    $("log-workspace").replaceChildren(...(data.writable_workspaces||[]).map(w=>new Option(w.name,w.id)));
    if((data.writable_workspaces||[]).some(w=>w.id===selectedWorkspace))$("log-workspace").value=selectedWorkspace;
    $("save-drink").disabled=!(data.writable_workspaces||[]).length;
    writableCatalog=data.writable_workspaces||[];loggingHint();
    $("week-total").textContent=number(data.totals.this_week);$("last-total").textContent=number(data.totals.last_week);
    $("all-total").textContent=number(data.totals.drinks);$("split-total").textContent=number(data.totals.splits);
    $("group").replaceChildren(new Option("All my groups",""),...data.groups.map(g=>new Option(g.name||"GroupMe group",g.group_id)));$("group").value=group;
    const trend=$("trend");trend.replaceChildren();const weeks=[];const start=new Date(`${data.week_start}T12:00:00Z`);
    for(let i=7;i>=0;i--){const date=new Date(start);date.setUTCDate(date.getUTCDate()-i*7);const key=date.toISOString().slice(0,10);weeks.push({date,key,total:data.trend.find(t=>t.week===key)?.drinks||0});}
    const max=Math.max(1,...weeks.map(w=>w.total));
    for(const week of weeks){const cell=text("div","","week");cell.append(text("strong",number(week.total)));const bar=text("div","","bar");bar.style.height=`${Math.max(2,week.total/max*140)}px`;cell.append(bar,text("span",week.date.toLocaleDateString("en-US",{month:"short",day:"numeric",timeZone:"UTC"})));cell.setAttribute("aria-label",`Week of ${week.key}: ${week.total} drinks`);trend.append(cell);}
    $("breakdown").replaceChildren(...data.breakdown.map(row=>{const node=text("div","","drink-row");node.append(text("span",row.drink_type.replaceAll("_"," ")),text("strong",number(row.drinks)));return node;}));
    if(!data.breakdown.length)$("breakdown").append(text("p","No drinks recorded yet.","empty"));
    $("activity").replaceChildren(...data.activity.map(renderEntry));
    if(!data.activity.length)$("activity").append(text("p","Nothing logged yet. Your next recorded drink will appear here.","empty"));
  } catch(error) {
    if(generation!==requestGeneration)return;
    // Never leave another filter's values visible after a failed load.
    $("dashboard").hidden=true;
    if(error.status===401){showLogin();status("");const config=await api("config");if(!config.email_signin_available)status("Email sign-in is being connected. Your GroupMe bot is working as usual.");}
    else status(error.message||"Could not load your history. Refresh to try again.");
  }
}
loadDashboard().catch(()=>status("Could not connect. Refresh to try again."));
