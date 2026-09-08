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
async function submit(form, action) {
  const button = form.querySelector('button[type="submit"]'); button.disabled=true; status("");
  try { await action(); } catch(error) { status(error.message || "Could not connect. Please try again."); } finally { button.disabled=false; }
}
$("email-form").addEventListener("submit",event=>{event.preventDefault();submit(event.currentTarget,async()=>{
  await api("login",{email:$("email").value.trim()}); $("email-form").hidden=true;$("code-form").hidden=false;$("code").value="";$("code").focus();
});});
$("code-form").addEventListener("submit",event=>{event.preventDefault();submit(event.currentTarget,async()=>{await api("verify",{code:$("code").value.trim()});await loadDashboard();});});
$("back").addEventListener("click",()=>{$("email-form").hidden=false;$("code-form").hidden=true;status("");$("email").focus();});
$("logout").addEventListener("click",async()=>{try{await api("logout",{});location.reload();}catch(error){status(error.message);}});
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
    $("week-total").textContent=number(data.totals.this_week);$("last-total").textContent=number(data.totals.last_week);
    $("all-total").textContent=number(data.totals.drinks);$("split-total").textContent=number(data.totals.splits);
    $("group").replaceChildren(new Option("All my groups",""),...data.groups.map(g=>new Option(g.name||"GroupMe group",g.group_id)));$("group").value=group;
    const trend=$("trend");trend.replaceChildren();const weeks=[];const start=new Date(`${data.week_start}T12:00:00Z`);
    for(let i=7;i>=0;i--){const date=new Date(start);date.setUTCDate(date.getUTCDate()-i*7);const key=date.toISOString().slice(0,10);weeks.push({date,key,total:data.trend.find(t=>t.week===key)?.drinks||0});}
    const max=Math.max(1,...weeks.map(w=>w.total));
    for(const week of weeks){const cell=text("div","","week");cell.append(text("strong",number(week.total)));const bar=text("div","","bar");bar.style.height=`${Math.max(2,week.total/max*140)}px`;cell.append(bar,text("span",week.date.toLocaleDateString("en-US",{month:"short",day:"numeric",timeZone:"UTC"})));cell.setAttribute("aria-label",`Week of ${week.key}: ${week.total} drinks`);trend.append(cell);}
    $("breakdown").replaceChildren(...data.breakdown.map(row=>{const node=text("div","","drink-row");node.append(text("span",row.drink_type.replaceAll("_"," ")),text("strong",number(row.drinks)));return node;}));
    if(!data.breakdown.length)$("breakdown").append(text("p","No drinks recorded yet.","empty"));
    $("activity").replaceChildren(...data.activity.map(row=>{const node=text("div","","activity-row");const detail=text("div","");detail.append(text("p",row.drink_type.replaceAll("_"," "),"activity-title"),text("p",new Date(row.logged_at).toLocaleString("en-US",{month:"short",day:"numeric",year:"numeric",hour:"numeric",minute:"2-digit",timeZone:"America/New_York"})+" ET"+(row.split_the_g?` · ${row.split_the_g} split the G`:""),"muted"));node.append(detail,text("span",number(row.quantity),"activity-quantity"));return node;}));
    if(!data.activity.length)$("activity").append(text("p","Nothing logged yet. Your next drink logged in GroupMe will appear here.","empty"));
  } catch(error) {
    if(generation!==requestGeneration)return;
    // Never leave another filter's values visible after a failed load.
    $("dashboard").hidden=true;
    if(error.status===401){showLogin();status("");const config=await api("config");if(!config.email_signin_available)status("Email sign-in is being connected. Your GroupMe bot is working as usual.");}
    else status(error.message||"Could not load your history. Refresh to try again.");
  }
}
loadDashboard().catch(()=>status("Could not connect. Refresh to try again."));
