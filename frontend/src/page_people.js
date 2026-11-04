import { html, out, num, dmy } from "./ui.js";
import { api, acts, mount, toast, S } from "./core.js";

const st = { tab:"overview", q:"", dept:0, loc:0, status:"active", page:1, from:"", to:"", attendanceStatus:"", leaveStatus:"", applicantStage:"" };
let root, opts;
const $ = s => root.querySelector(s);
const isHR = () => ["ceo","admin","hr"].includes(S.user.role);
const canApprove = () => ["ceo","admin","hr","store_manager","warehouse"].includes(S.user.role);
const day = v => v ? dmy(String(v).slice(0,10)) : "—";
const time = v => v ? new Date(String(v).replace(" ","T")).toLocaleTimeString("en-IN",{hour:"2-digit",minute:"2-digit"}) : "—";
const pill = (s) => { const c = ({active:"active",present:"active",approved:"active",hired:"active",open:"active",late:"warn",pending:"warn",screening:"warn",interview:"warn",absent:"delisted",rejected:"delisted",closed:"t",paused:"t",half_day:"warn"})[s] || "t"; return html`<span class="pill ${c}">${String(s||"—").replaceAll("_"," ")}</span>`; };
const empty = (s,n=5) => html`<tr><td colspan="${n}" class="muted" style="padding:28px;text-align:center">${s}</td></tr>`;
const kpi = (label,value,sub) => html`<div class="kpi glass"><div class="l">${label}</div><div class="v">${value}</div><div class="s">${sub}</div></div>`;
const close = () => document.querySelectorAll(".scrim,.drawer,.modal").forEach(n=>n.remove());
function layer(kind, content) {
  close();
  document.body.insertAdjacentHTML("beforeend", out(html`<div class="scrim" data-act="people-close"></div>${kind==="drawer" ? html`<aside class="drawer glass wide">${content}</aside>` : html`<div class="modal glass">${content}</div>`}`));
}
const head = (eyebrow,title,sub="") => html`<div class="row" style="justify-content:space-between;align-items:flex-start"><div><div class="eyebrow">${eyebrow}</div><h2 style="font-size:22px">${title}</h2><p class="muted small">${sub}</p></div><button class="icon" data-act="people-close" aria-label="Close">✕</button></div>`;
const tabs = [["overview","Overview"],["employees","Employees"],["attendance","Attendance"],["roster","Rosters"],["leaves","Leave"],["onboarding","Onboarding"],["hiring","Hiring"]];

export async function render(el) {
  root=el; opts=await api("/people/options");
  mount(el,html`<div class="head"><div><div class="eyebrow">Module 9 · People</div><h1>People & HR</h1><p class="muted">A single view of the team, attendance, leave, shifts and hiring.</p></div></div>
    <div class="tabs" id="ptabs">${tabs.map(([k,l])=>html`<button data-act="people-tab" data-v="${k}" class="${st.tab===k?"on":""}">${l}</button>`)}</div>
    <div id="peoplebody"><div class="card glass"><div class="sk" style="height:140px"></div></div></div>`);
  draw();
}
acts["people-close"]=close;
acts["people-tab"]=a=>{st.tab=a.dataset.v;st.page=1;root.querySelectorAll("#ptabs button").forEach(b=>b.classList.toggle("on",b.dataset.v===st.tab));draw();};
const body=()=>$("#peoplebody");
async function draw(){
  mount(body(),html`<div class="card glass"><div class="sk" style="height:130px"></div></div>`);
  try { await ({overview,employees,attendance,roster,leaves,onboarding,hiring})[st.tab](); }
  catch(e){mount(body(),html`<div class="card glass"><b>Could not load this view</b><p class="muted">${e.message}</p><button class="btn" data-act="people-reload">Try again</button></div>`);}
}
acts["people-reload"]=draw;

async function overview(){
 const s=await api("/people/summary");
 mount(body(),html`<div class="kpis">${kpi("Active employees",num(s.active),"Across stores, warehouses and office")}${kpi("Present today",num(s.attendance.present),`${num(s.attendance.absent)} marked absent · ${num(s.attendance.records)} records`)}${kpi("Leave awaiting decision",num(s.pending_leaves),"Requests in the approval queue")}${kpi("Open onboarding tasks",num(s.onboarding_open),"For recent hires")}${kpi("Open positions",num(s.open_jobs),"Across the business")}</div>
 <div class="cols eq"><div class="card solid"><div class="ct"><h2>Recent joins</h2><button class="btn" data-act="people-tab" data-v="employees">View directory</button></div><div class="tblwrap"><table class="tbl"><thead><tr><th>Employee</th><th>Team</th><th>Location</th><th>Joined</th></tr></thead><tbody>${s.recent_joins.map(e=>html`<tr class="click" data-act="people-employee" data-id="${e.id}"><td><b>${e.name}</b><div class="small muted">${e.emp_code}</div></td><td>${e.department}</td><td>${e.location}</td><td>${day(e.join_date)}</td></tr>`)}${s.recent_joins.length? "":empty("No recent joins.",4)}</tbody></table></div></div>
 <div class="card glass"><div class="ct"><h2>People operations</h2><span class="small muted">Quick access</span></div><div class="exc click" data-act="people-tab" data-v="attendance"><span class="dot high"></span><div><b>Attendance & exceptions</b><p>Review daily attendance and missing punches</p></div></div><div class="exc click" data-act="people-tab" data-v="leaves"><span class="dot med"></span><div><b>Leave approvals</b><p>Review pending leave and work-from-home requests</p></div></div><div class="exc click" data-act="people-tab" data-v="onboarding"><span class="dot low"></span><div><b>Onboarding</b><p>Keep new joiners moving through their checklist</p></div></div><div class="exc click" data-act="people-tab" data-v="hiring"><span class="dot low"></span><div><b>Recruitment</b><p>Manage open positions and applicant stages</p></div></div></div></div>`);
}

async function employees(){
 const p=new URLSearchParams({q:st.q,department_id:st.dept,location_id:st.loc,status:st.status,page:st.page,size:30});
 const r=await api("/people/employees?"+p);
 mount(body(),html`<div class="row" style="margin-bottom:12px;flex-wrap:wrap"><input class="input" id="pe-q" placeholder="Search name, employee ID or email" value="${st.q}" style="min-width:220px;flex:1"><select class="input" id="pe-dept"><option value="0">All departments</option>${opts.departments.map(d=>html`<option value="${d.id}" ${+st.dept===d.id?"selected":""}>${d.name}</option>`)}</select><select class="input" id="pe-loc"><option value="0">All locations</option>${opts.locations.map(l=>html`<option value="${l.id}" ${+st.loc===l.id?"selected":""}>${l.name}</option>`)}</select><select class="input" id="pe-status">${["active","inactive","resigned",""].map(v=>html`<option value="${v}" ${st.status===v?"selected":""}>${v||"All statuses"}</option>`)}</select></div>
 <div class="row" style="justify-content:space-between;margin-bottom:8px"><span class="small muted">${num(r.total)} employees · page ${r.page}</span><span class="small muted">Select a person to view their profile</span></div>
 <div class="solid"><div class="tblwrap"><table class="tbl"><thead><tr><th>Employee</th><th>Department</th><th>Designation</th><th>Location</th><th>Joined</th><th>Status</th></tr></thead><tbody>${r.items.map(e=>html`<tr class="click" data-act="people-employee" data-id="${e.id}"><td><b>${e.name}</b><div class="small muted">${e.emp_code} · ${e.email||"No email"}</div></td><td>${e.department}</td><td>${e.designation}</td><td>${e.location}</td><td>${day(e.join_date)}</td><td>${pill(e.status)}</td></tr>`)}${r.items.length?"":empty("No employees match these filters.",6)}</tbody></table></div></div>
 <div class="row" style="justify-content:space-between;margin-top:12px"><span class="small muted">Showing ${r.items.length} of ${num(r.total)}</span><div class="row"><button class="btn" data-act="people-page" data-v="-1" ${r.page<=1?"disabled":""}>Previous</button><button class="btn" data-act="people-page" data-v="1" ${r.page*r.size>=r.total?"disabled":""}>Next</button></div></div>`);
 $("#pe-q").oninput=e=>{st.q=e.target.value;st.page=1;clearTimeout(window.peopleSearchTimer);window.peopleSearchTimer=setTimeout(draw,250);};
 $("#pe-dept").onchange=e=>{st.dept=+e.target.value;st.page=1;draw();};
 $("#pe-loc").onchange=e=>{st.loc=+e.target.value;st.page=1;draw();};
 $("#pe-status").onchange=e=>{st.status=e.target.value;st.page=1;draw();};
}
acts["people-page"]=a=>{st.page=Math.max(1,st.page+ +a.dataset.v);draw();};

acts["people-employee"]=async a=>{
 try{
 const e=await api("/people/employees/"+a.dataset.id);
 layer("drawer",html`${head(e.department,e.name,`${e.emp_code} · ${e.designation} · ${e.location}`)}
 <div class="kpis" style="margin-top:14px">${kpi("Status",pill(e.status),"Employment status")}${kpi("Joined",day(e.join_date),"Date of joining")}${kpi("Manager",e.manager?.name||"—",e.manager?.emp_code||"No reporting manager")}</div>
 <div class="card solid" style="margin-top:14px"><div class="ct"><h2>Recent attendance</h2></div><div class="tblwrap"><table class="tbl"><thead><tr><th>Date</th><th>In</th><th>Out</th><th>Hours</th><th>Status</th></tr></thead><tbody>${e.attendance.map(x=>html`<tr><td>${day(x.date)}</td><td>${time(x.punch_in)}</td><td>${time(x.punch_out)}</td><td>${x.hours??"—"}</td><td>${pill(x.status)}</td></tr>`)}${e.attendance.length?"":empty("No attendance records.",5)}</tbody></table></div></div>
 <div class="card solid" style="margin-top:14px"><div class="ct"><h2>Leave history</h2></div><div class="tblwrap"><table class="tbl"><thead><tr><th>Type</th><th>Dates</th><th>Status</th></tr></thead><tbody>${e.leaves.map(x=>html`<tr><td>${x.leave_type}</td><td>${day(x.from_date)} – ${day(x.to_date)}</td><td>${pill(x.status)}</td></tr>`)}${e.leaves.length?"":empty("No leave requests.",3)}</tbody></table></div></div>
 <div class="card solid" style="margin-top:14px"><div class="ct"><h2>Onboarding checklist</h2></div>${e.onboarding.map(x=>html`<div class="exc"><span class="dot ${x.done_at?"low":"med"}"></span><div><b>${x.task}</b><p>Due ${day(x.due_date)} · ${x.done_at?"Completed":"Open"}</p></div></div>`)}${e.onboarding.length?"":html`<p class="muted small">No onboarding tasks assigned.</p>`}</div>`);
 }catch(e){toast(e.message,true);}
};

async function attendance(){
 const r=await api("/people/attendance?"+new URLSearchParams({from_date:st.from||"2026-10-18",to_date:st.to||"2026-10-31",status:st.attendanceStatus}));
 mount(body(),html`<div class="row" style="margin-bottom:12px;flex-wrap:wrap"><label class="small muted">From <input class="input" id="pa-from" type="date" value="${r.from_date}"></label><label class="small muted">To <input class="input" id="pa-to" type="date" value="${r.to_date}"></label><select class="input" id="pa-status"><option value="">All statuses</option>${["present","late","half_day","absent","leave","holiday"].map(s=>html`<option value="${s}" ${st.attendanceStatus===s?"selected":""}>${s.replace("_"," ")}</option>`)}</select>${isHR()?html`<button class="btn pri" data-act="people-mark">Record attendance</button>`:""}</div>
 <div class="row" style="justify-content:space-between;margin-bottom:8px"><b>Attendance records</b><span class="small muted">${num(r.total)} records · ${day(r.from_date)} to ${day(r.to_date)}</span></div>
 <div class="solid"><div class="tblwrap"><table class="tbl"><thead><tr><th>Employee</th><th>Date</th><th>Location</th><th>In</th><th>Out</th><th>Hours</th><th>Status</th></tr></thead><tbody>${r.items.map(x=>html`<tr><td><b>${x.name}</b><div class="small muted">${x.emp_code} · ${x.department}</div></td><td>${day(x.date)}</td><td>${x.location}</td><td>${time(x.punch_in)}</td><td>${time(x.punch_out)}</td><td>${x.hours??"—"}</td><td>${pill(x.status)}</td></tr>`)}${r.items.length?"":empty("No attendance records in this window.",7)}</tbody></table></div></div>`);
 $("#pa-from").onchange=e=>{st.from=e.target.value;draw();};$("#pa-to").onchange=e=>{st.to=e.target.value;draw();};$("#pa-status").onchange=e=>{st.attendanceStatus=e.target.value;draw();};
}
acts["people-mark"]=async()=>{
 try{
 const emps=await api("/people/employees?status=active&size=100");
 layer("modal",html`${head("Attendance","Record attendance","For HR, store and warehouse teams")}
 <label>Employee</label><select class="input" id="pm-emp">${emps.items.map(e=>html`<option value="${e.id}">${e.name} · ${e.emp_code}</option>`)}</select>
 <label>Date</label><input class="input" id="pm-date" type="date" value="2026-10-31">
 <label>Status</label><select class="input" id="pm-status">${["present","late","half_day","absent","leave","holiday"].map(s=>html`<option value="${s}">${s.replace("_"," ")}</option>`)}</select>
 <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="btn" data-act="people-close">Cancel</button><button class="btn pri" data-act="people-save-att">Save record</button></div>`);
 }catch(e){toast(e.message,true);}
};
acts["people-save-att"]=async()=>{
 try{await api("/people/attendance",{method:"POST",body:{employee_id:+$("#pm-emp").value,date:$("#pm-date").value,status:$("#pm-status").value,source:"manual"}});close();toast("Attendance recorded");draw();}
 catch(e){toast(e.message,true);}
};

async function roster(){
 const start=st.from||"2026-10-31",end=st.to||"2026-11-06";
 const r=await api("/people/roster?"+new URLSearchParams({from_date:start,to_date:end}));
 mount(body(),html`<div class="row" style="margin-bottom:12px;flex-wrap:wrap"><label class="small muted">From <input class="input" id="pr-from" type="date" value="${start}"></label><label class="small muted">To <input class="input" id="pr-to" type="date" value="${end}"></label>${isHR()?html`<button class="btn pri" data-act="people-assign">Assign shift</button>`:""}</div>
 <div class="solid"><div class="tblwrap"><table class="tbl"><thead><tr><th>Date</th><th>Employee</th><th>Location</th><th>Shift</th><th>Hours</th></tr></thead><tbody>${r.map(x=>html`<tr><td>${day(x.date)}</td><td><b>${x.name}</b><div class="small muted">${x.emp_code} · ${x.department}</div></td><td>${x.location}</td><td>${x.shift_code}</td><td>${x.start_time}–${x.end_time}</td></tr>`)}${r.length?"":empty("No shifts scheduled for this window.",5)}</tbody></table></div></div>`);
 $("#pr-from").onchange=e=>{st.from=e.target.value;draw();};$("#pr-to").onchange=e=>{st.to=e.target.value;draw();};
}
acts["people-assign"]=async()=>{
 try{
 const [emps]=await Promise.all([api("/people/employees?status=active&size=100")]);
 layer("modal",html`${head("Roster","Assign a shift","Create or replace one employee's shift for a date")}
 <label>Employee</label><select class="input" id="prs-emp">${emps.items.map(e=>html`<option value="${e.id}" data-loc="${e.location_id}">${e.name} · ${e.location}</option>`)}</select>
 <label>Date</label><input class="input" id="prs-date" type="date" value="2026-11-01">
 <label>Shift</label><select class="input" id="prs-shift">${opts.shifts.map(s=>html`<option value="${s.id}">${s.code} · ${s.start_time}–${s.end_time}</option>`)}</select>
 <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="btn" data-act="people-close">Cancel</button><button class="btn pri" data-act="people-save-roster">Save shift</button></div>`);
 }catch(e){toast(e.message,true);}
};
acts["people-save-roster"]=async()=>{
 try{const s=$("#prs-emp").selectedOptions[0];await api("/people/roster",{method:"POST",body:{employee_id:+s.value,date:$("#prs-date").value,shift_id:+$("#prs-shift").value,location_id:+s.dataset.loc}});close();toast("Shift assigned");draw();}
 catch(e){toast(e.message,true);}
};

async function leaves(){
 const r=await api("/people/leaves?"+new URLSearchParams({status:st.leaveStatus}));
 mount(body(),html`<div class="row" style="justify-content:space-between;margin-bottom:12px;flex-wrap:wrap"><div class="seg">${[["","All"],["pending","Pending"],["approved","Approved"],["rejected","Rejected"]].map(([v,l])=>html`<button data-act="people-leave-filter" data-v="${v}" class="${st.leaveStatus===v?"on":""}">${l}</button>`)}</div>${isHR()?html`<button class="btn pri" data-act="people-new-leave">Create request</button>`:""}</div>
 <div class="solid"><div class="tblwrap"><table class="tbl"><thead><tr><th>Employee</th><th>Leave type</th><th>Dates</th><th>Duration</th><th>Location</th><th>Status</th><th></th></tr></thead><tbody>${r.map(x=>html`<tr><td><b>${x.name}</b><div class="small muted">${x.emp_code} · ${x.department}</div></td><td>${x.leave_type}${x.is_wfh?html`<div class="small muted">Work from home</div>`:""}</td><td>${day(x.from_date)} – ${day(x.to_date)}</td><td>${Math.round((new Date(x.to_date)-new Date(x.from_date))/86400000)+1} days</td><td>${x.location}</td><td>${pill(x.status)}</td><td>${x.status==="pending"&&canApprove()?html`<div class="row"><button class="btn" data-act="people-decision" data-id="${x.id}" data-v="approve">Approve</button><button class="btn" data-act="people-decision" data-id="${x.id}" data-v="reject">Reject</button></div>`:""}</td></tr>`)}${r.length?"":empty("No leave requests found.",7)}</tbody></table></div></div>`);
}
acts["people-leave-filter"]=a=>{st.leaveStatus=a.dataset.v;draw();};
acts["people-decision"]=async a=>{
 try{await api("/people/leaves/"+a.dataset.id+"/decision",{method:"POST",body:{decision:a.dataset.v}});toast("Request "+(a.dataset.v==="approve"?"approved":"rejected"));draw();}
 catch(e){toast(e.message,true);}
};
acts["people-new-leave"]=async()=>{
 try{
 const emps=await api("/people/employees?status=active&size=100");
 layer("modal",html`${head("Leave","Create leave request","HR can record a request on behalf of an employee")}
 <label>Employee</label><select class="input" id="pl-emp">${emps.items.map(e=>html`<option value="${e.id}">${e.name} · ${e.emp_code}</option>`)}</select>
 <label>Leave type</label><select class="input" id="pl-type">${opts.leave_types.map(t=>html`<option value="${t.id}">${t.name} · ${t.annual_quota} days/year</option>`)}</select>
 <div class="row"><div style="flex:1"><label>From</label><input class="input" id="pl-from" type="date" value="2026-11-02"></div><div style="flex:1"><label>To</label><input class="input" id="pl-to" type="date" value="2026-11-02"></div></div>
 <label class="row"><input type="checkbox" id="pl-wfh"> Work from home</label>
 <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="btn" data-act="people-close">Cancel</button><button class="btn pri" data-act="people-save-leave">Submit request</button></div>`);
 }catch(e){toast(e.message,true);}
};
acts["people-save-leave"]=async()=>{
 try{await api("/people/leaves",{method:"POST",body:{employee_id:+$("#pl-emp").value,leave_type_id:+$("#pl-type").value,from_date:$("#pl-from").value,to_date:$("#pl-to").value,is_wfh:$("#pl-wfh").checked}});close();toast("Leave request created");draw();}
 catch(e){toast(e.message,true);}
};

async function onboarding(){
 const r=await api("/people/onboarding?open_only=true");
 mount(body(),html`<div class="row" style="justify-content:space-between;margin-bottom:12px"><div><h2>Open onboarding tasks</h2><p class="small muted">Complete the practical steps that help each new joiner get started.</p></div><span class="pill warn">${r.length} open</span></div>
 <div class="kpis">${r.map(x=>html`<div class="card glass"><div class="row" style="justify-content:space-between"><span class="small muted">${x.emp_code}</span>${pill("open")}</div><h2 style="margin-top:8px">${x.task}</h2><p style="margin-top:5px"><b>${x.name}</b></p><p class="small muted">${x.department} · ${x.location}</p><div class="row" style="justify-content:space-between;align-items:center;margin-top:12px"><span class="small muted">Due ${day(x.due_date)}</span>${isHR()?html`<button class="btn pri" data-act="people-onboard-done" data-id="${x.id}">Mark done</button>`:""}</div></div>`)}${r.length?"":html`<div class="card solid"><p>All onboarding tasks are complete.</p></div>`}</div>`);
}
acts["people-onboard-done"]=async a=>{try{await api("/people/onboarding/"+a.dataset.id,{method:"PATCH"});toast("Task completed");draw();}catch(e){toast(e.message,true);}};

async function hiring(){
 const [jobs,apps]=await Promise.all([api("/people/jobs"),api("/people/applicants?"+new URLSearchParams({stage:st.applicantStage}))]);
 mount(body(),html`<div class="row" style="justify-content:space-between;margin-bottom:12px;flex-wrap:wrap"><div><h2>Open positions</h2><p class="small muted">Track hiring demand and applicant progress.</p></div>${isHR()?html`<button class="btn pri" data-act="people-new-job">Post a position</button>`:""}</div>
 <div class="kpis">${jobs.map(j=>html`<div class="card glass"><div class="row" style="justify-content:space-between">${pill(j.status)}<span class="small muted">${j.applicants} applicants</span></div><h2 style="margin-top:8px">${j.title}</h2><p class="small muted">${j.department} · ${j.location||"Multiple locations"}</p><p class="small muted" style="margin-top:8px">Posted ${day(j.posted_on)} · Closes ${day(j.closes_on)}</p><p class="small" style="margin-top:8px">${j.description}</p></div>`)}${jobs.length?"":html`<div class="card solid">No positions yet.</div>`}</div>
 <div class="row" style="justify-content:space-between;margin:22px 0 10px;flex-wrap:wrap"><div><h2>Applicants</h2><p class="small muted">Move candidates through the recruitment pipeline.</p></div><select class="input" id="pa-stage"><option value="">All stages</option>${["applied","screening","interview","offer","hired","rejected"].map(s=>html`<option value="${s}" ${st.applicantStage===s?"selected":""}>${s}</option>`)}</select></div>
 <div class="solid"><div class="tblwrap"><table class="tbl"><thead><tr><th>Candidate</th><th>Position</th><th>Applied</th><th>Source</th><th>Stage</th><th></th></tr></thead><tbody>${apps.map(a=>html`<tr><td><b>${a.name}</b></td><td>${a.job_title}</td><td>${day(a.applied_on)}</td><td>${a.source}</td><td>${pill(a.stage)}</td><td>${isHR()?html`<select class="input" data-act="people-stage" data-id="${a.id}">${["applied","screening","interview","offer","hired","rejected"].map(s=>html`<option value="${s}" ${a.stage===s?"selected":""}>${s}</option>`)}</select>`:""}</td></tr>`)}${apps.length?"":empty("No applicants match this stage.",6)}</tbody></table></div></div>`);
 $("#pa-stage").onchange=e=>{st.applicantStage=e.target.value;draw();};
 root.querySelectorAll("select[data-act='people-stage']").forEach(el=>el.onchange=async e=>{try{await api("/people/applicants/"+e.target.dataset.id,{method:"PATCH",body:{stage:e.target.value}});toast("Applicant stage updated");}catch(err){toast(err.message,true);}});
}
acts["people-new-job"]=()=>{
 layer("modal",html`${head("Hiring","Post a position","Create a role in the recruitment pipeline")}
 <label>Job title</label><input class="input" id="pj-title" placeholder="e.g. Store Manager">
 <label>Department</label><select class="input" id="pj-dept">${opts.departments.map(d=>html`<option value="${d.id}">${d.name}</option>`)}</select>
 <label>Location</label><select class="input" id="pj-loc"><option value="">Multiple locations / not specified</option>${opts.locations.map(l=>html`<option value="${l.id}">${l.name}</option>`)}</select>
 <div class="row"><div style="flex:1"><label>Posted on</label><input class="input" id="pj-post" type="date" value="2026-10-31"></div><div style="flex:1"><label>Closes on</label><input class="input" id="pj-close" type="date" value="2026-11-30"></div></div>
 <label>Description</label><textarea class="input" id="pj-desc" rows="3" placeholder="Role outcomes and requirements"></textarea>
 <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="btn" data-act="people-close">Cancel</button><button class="btn pri" data-act="people-save-job">Publish position</button></div>`);
};
acts["people-save-job"]=async()=>{
 try{await api("/people/jobs",{method:"POST",body:{title:$("#pj-title").value,department_id:+$("#pj-dept").value,location_id:$("#pj-loc").value?+$("#pj-loc").value:null,posted_on:$("#pj-post").value,closes_on:$("#pj-close").value||null,description:$("#pj-desc").value}});close();toast("Position published");draw();}
 catch(e){toast(e.message,true);}
};
