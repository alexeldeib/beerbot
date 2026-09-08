"use strict";
let groupInviteId=null,signedGroupEmail=currentOwner||null,managedGroup=null,groupAction=null,groupLoad=0,inviteLoad=0;
try { groupInviteId=sessionStorage.getItem('beerbot-group-invite'); } catch { /* optional tab state */ }
function captureGroupInvitation(){
  const incomingInvite=new URLSearchParams(location.hash.slice(1)).get('invite');
  if(incomingInvite && /^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(incomingInvite)){
    groupInviteId=incomingInvite;
    try {sessionStorage.setItem('beerbot-group-invite',groupInviteId);} catch { /* memory remains */ }
    history.replaceState(null,'',location.pathname+location.search);
  }
}
captureGroupInvitation();
window.addEventListener('hashchange',()=>{captureGroupInvitation();showGroupInvitation();});
function clearGroupInvitation(){groupInviteId=null;try{sessionStorage.removeItem('beerbot-group-invite');}catch{}$("invitation-card").hidden=true;document.body.classList.remove('has-invitation');}
async function prepareGroupInvitation(email){
  if(groupInviteId)await api(`invitations/${groupInviteId}/prepare`,{email});
}
async function showGroupInvitation(){
  const generation=++inviteLoad;
  document.body.classList.toggle('has-invitation',!!groupInviteId);
  if(!groupInviteId){$("invitation-card").hidden=true;return;}
  $("invitation-card").hidden=false;$("join-form").hidden=true;$("invitation-error").textContent="";
  try{
    const invitation=await api(`invitations/${groupInviteId}`);
    if(generation!==inviteLoad)return;
    $("invitation-heading").textContent=`Join ${invitation.group_name}`;
    $("invitation-description").textContent=signedGroupEmail?`${invitation.inviter} invited you. You're signed in as ${signedGroupEmail}.`:`${invitation.inviter} invited you. Enter the email this invitation was made for below, then verify your code.`;
    if(signedGroupEmail){
      const info=await api('groups');if(generation!==inviteLoad)return;
      $("join-name-field").hidden=!info.needs_name;$("join-name").required=info.needs_name;$("join-form").hidden=false;
    }
  }catch(error){if(generation===inviteLoad){$("invitation-error").textContent=error.message;$("invitation-description").textContent="Ask the group owner for a new link if needed.";}}
}
window.addEventListener('beerbot:signed-in',event=>{signedGroupEmail=event.detail.email;showGroupInvitation();});
window.addEventListener('beerbot:signed-out',()=>{signedGroupEmail=null;showGroupInvitation();});
window.addEventListener('beerbot:save-confirmed',event=>{if(event.detail.path.endsWith('/accept'))clearGroupInvitation();});
$("dismiss-invitation").addEventListener('click',clearGroupInvitation);
$("join-form").addEventListener('submit',event=>{event.preventDefault();submit(event.currentTarget,async()=>{
  await saveCommand(`invitations/${groupInviteId}/accept`,{name:$("join-name-field").hidden?null:$("join-name").value.trim()});
  clearGroupInvitation();await loadDashboard();status("You've joined the group. You can now log your own drinks there.");
},'invitation-error');});

async function loadAppGroups(preferred){
  const result=await api('groups');
  const selected=preferred||$("managed-group").value;
  $("managed-group").replaceChildren(...result.groups.map(g=>new Option(g.name,g.id)));
  if(result.groups.some(g=>g.id===selected))$("managed-group").value=selected;
  $("groups-empty").hidden=!!result.groups.length;$("managed-group").disabled=!result.groups.length;
  await loadManagedGroup();
}
async function loadManagedGroup(){
  const generation=++groupLoad,workspace=$("managed-group").value;
  managedGroup=null;$("group-management").hidden=true;$("share-invitation").hidden=true;
  if(!workspace)return;
  const group=await api(`groups/${encodeURIComponent(workspace)}`);
  if(generation!==groupLoad)return;
  managedGroup=group;const owner=group.role==='owner';
  $("group-management").hidden=false;$("owner-controls").hidden=!owner;$("managed-name").value=group.name;
  $("pending-invitations").hidden=!owner;$("leave-group").hidden=owner;$("owner-leave-hint").hidden=!owner;
  $("group-members").replaceChildren(...group.members.map(member=>{
    const row=text('div','');row.append(text('p',`${member.display_name}${member.is_you?' (you)':''}`),text('p',member.role,'muted'));
    if(owner && !member.is_you && member.role==='member'){
      const buttons=text('div','','entry-controls'),remove=text('button','Remove','quiet'),transfer=text('button','Make owner','quiet');
      remove.addEventListener('click',()=>confirmGroupAction(`Remove ${member.display_name}?`,'They will lose group and logging access. Their personal history will remain.',`groups/${group.id}/remove`,{account_id:member.account_id}));
      transfer.addEventListener('click',()=>confirmGroupAction(`Transfer ownership to ${member.display_name}?`,'You will become a member. Pending invitation links will be revoked.',`groups/${group.id}/transfer`,{account_id:member.account_id}));
      buttons.append(remove,transfer);row.append(buttons);
    }return row;
  }));
  $("group-invitations").replaceChildren(...group.invitations.map(invite=>{
    const row=text('div',''),buttons=text('div','','entry-controls'),copy=text('button','Show link','quiet'),revoke=text('button','Revoke','quiet');
    row.append(text('p',invite.email),text('p',`Expires ${new Date(invite.expires_at).toLocaleDateString()}`,'muted'));
    copy.addEventListener('click',()=>shareInvitation(`${location.origin}/app#invite=${invite.id}`));
    revoke.addEventListener('click',()=>confirmGroupAction('Revoke this invitation?','The link will no longer allow the recipient to join.',`groups/${group.id}/invitations/${invite.id}/revoke`,{}));
    buttons.append(copy,revoke);row.append(buttons);return row;
  }));
  if(owner && !group.invitations.length)$("group-invitations").append(text('p','No pending invitations.','hint'));
}
function shareInvitation(url){$("invitation-link").value=url;$("share-invitation").hidden=false;}
function confirmGroupAction(title,description,path,values){
  groupAction={path,values};$("group-action-title").textContent=title;$("group-action-description").textContent=description;$("group-action-error").textContent='';$("group-action-dialog").showModal();
}
$("manage-groups").addEventListener('click',async()=>{$("groups-error").textContent='';$("groups-dialog").showModal();try{await loadAppGroups();}catch(error){$("groups-error").textContent=error.message;}});
$("managed-group").addEventListener('change',()=>{loadManagedGroup().catch(error=>$("groups-error").textContent=error.message);});
$("create-from-groups").addEventListener('click',()=>{$("groups-dialog").close();$("group-error").textContent='';$("group-dialog").showModal();});
$("copy-invitation").addEventListener('click',async()=>{try{await navigator.clipboard.writeText($("invitation-link").value);$("groups-error").textContent='Link copied. Share it with the invited friend.';}catch{$("invitation-link").select();$("groups-error").textContent='Select and copy the invitation link above.';}});
$("rename-group-form").addEventListener('submit',event=>{event.preventDefault();submit(event.currentTarget,async()=>{
  await saveCommand(`groups/${managedGroup.id}/rename`,{name:$("managed-name").value.trim()});await loadAppGroups();await loadDashboard();$("groups-error").textContent='Group renamed.';
},'groups-error');});
$("invite-friend-form").addEventListener('submit',event=>{event.preventDefault();submit(event.currentTarget,async()=>{
  const result=await saveCommand(`groups/${managedGroup.id}/invite`,{email:$("friend-email").value.trim()});await loadManagedGroup();shareInvitation(result.url);$("groups-error").textContent='Invitation created. Copy the link and share it with your friend.';
},'groups-error');});
$("leave-group").addEventListener('click',()=>confirmGroupAction(`Leave ${managedGroup.name}?`,'You will lose group and logging access. Your personal history will remain.',`groups/${managedGroup.id}/leave`,{}));
$("group-action-form").addEventListener('submit',event=>{event.preventDefault();submit(event.currentTarget,async()=>{
  await saveCommand(groupAction.path,groupAction.values);$("group-action-dialog").close();await loadAppGroups();await loadDashboard();$("groups-error").textContent='Group updated.';
},'group-action-error');});
showGroupInvitation();
