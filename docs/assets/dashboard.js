(function(){
"use strict";

// ── state ──────────────────────────────────────────────────────────────────
let currentDate   = null;
let summaryData   = null;
let vehicleData   = null;
let schedData     = null;
let kartData      = null;
let availDates    = [];

// ── utils ──────────────────────────────────────────────────────────────────
function pctClass(p){ return p==null?"neutral":p>=90?"good":p>=70?"warn":"bad"; }
function fmtPct(p){   return p==null?"—":p.toFixed(1)+"%"; }
function fmtInt(n){   return n==null?"—":new Intl.NumberFormat("el-GR").format(n); }
function fmtTime(iso){
  if(!iso) return "—";
  try{
    const d = new Date(iso);
    if(isNaN(d.getTime())) return iso.substring(11,16)||"—";
    return d.toLocaleTimeString("el-GR",{hour:"2-digit",minute:"2-digit"});
  }catch(e){ return "—"; }
}
function fmtDur(a,b){
  if(!a||!b) return "—";
  const m = Math.round((new Date(b)-new Date(a))/60000);
  return m>0?m+"'":"—";
}
function fmtRel(iso){
  if(!iso) return "—";
  const d = Math.round((Date.now()-new Date(iso))/60000);
  if(d<1) return "μόλις τώρα";
  if(d<60) return d+"λεπ πριν";
  const h=Math.round(d/60);
  if(h<24) return h+"ω πριν";
  return Math.round(h/24)+"μ πριν";
}
function fmtDateGr(iso){
  if(!iso) return "—";
  try{
    return new Date(iso+"T00:00:00").toLocaleDateString("el-GR",
      {day:"numeric",month:"long",year:"numeric"});
  }catch(e){return iso;}
}

async function loadJSON(p){
  const r = await fetch(p,{cache:"no-store"});
  if(!r.ok) throw new Error(p+" → "+r.status);
  return r.json();
}

function dataPath(file){ return `data/${currentDate}/${file}`; }

// ── tabs ───────────────────────────────────────────────────────────────────
document.querySelectorAll(".tab").forEach(t=>{
  t.addEventListener("click",()=>{
    document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(x=>x.classList.remove("active"));
    t.classList.add("active");
    document.getElementById("tab-"+t.dataset.tab).classList.add("active");
  });
});

// ── date picker ────────────────────────────────────────────────────────────
async function initDatePicker(){
  const datesPayload = await loadJSON("data/available_dates.json");
  availDates = datesPayload.dates || [];
  const sel = document.getElementById("date-select");
  sel.innerHTML = "";
  availDates.forEach(d=>{
    const opt = document.createElement("option");
    opt.value = d;
    opt.textContent = fmtDateGr(d) + (d===availDates[0]?" (σήμερα)":"");
    sel.appendChild(opt);
  });
  sel.addEventListener("change", e=>{ if(e.target.value) loadDay(e.target.value); });
  if(availDates.length) loadDay(availDates[0]);
}

async function loadDay(d){
  currentDate = d;
  document.getElementById("date-select").value = d;
  document.getElementById("schedule-table-wrap").innerHTML =
    '<div class="empty-state">Επιλέξτε γραμμή.</div>';
  document.getElementById("kartelakia-wrap").innerHTML =
    '<div class="empty-state">Επιλέξτε γραμμή.</div>';
  document.getElementById("sched-route-select").style.display="none";
  document.getElementById("kart-route-select").style.display="none";

  try{
    const [summary, vehicles, sched, kart] = await Promise.all([
      loadJSON(dataPath("summary.json")),
      loadJSON(dataPath("vehicle_activity.json")).catch(()=>({vehicles:[]})),
      loadJSON(dataPath("schedule_distribution.json")).catch(()=>({trips:[]})),
      loadJSON(dataPath("kartelakia.json")).catch(()=>({slots:[]})),
    ]);
    summaryData = summary;
    vehicleData = vehicles;
    schedData   = sched;
    kartData    = kart;

    renderSummary(summary);
    renderVehicles(vehicles, document.getElementById("veh-search").value);
    buildLineSelectors(sched, kart);

    const health = await loadJSON("data/pipeline_health.json").catch(()=>({recent_runs:[]}));
    renderHealth(health);
  } catch(err){
    console.error("loadDay error:", err);
    document.getElementById("vehicle-table-wrap").innerHTML=
      '<div class="empty-state">Σφάλμα φόρτωσης: '+err.message+'</div>';
  }
}

// ── summary board ──────────────────────────────────────────────────────────
function renderSummary(d){
  document.getElementById("stat-date").textContent       = d.service_date||"—";
  document.getElementById("stat-actual").textContent     = fmtInt(d.system_actual_trips);
  document.getElementById("stat-scheduled").textContent  = fmtInt(d.system_scheduled_trips);
  document.getElementById("stat-routes").textContent     = fmtInt(d.route_count);
  document.getElementById("stat-vehicles").textContent   = fmtInt(d.total_vehicles);
  document.getElementById("last-updated").textContent    = fmtRel(d.generated_at);
  const cel = document.getElementById("stat-completion");
  cel.textContent = fmtPct(d.system_completion_pct);
  cel.className   = "stat-value mono "+pctClass(d.system_completion_pct);
}

// ── vehicles ───────────────────────────────────────────────────────────────
function renderVehicles(data, filter){
  const wrap = document.getElementById("vehicle-table-wrap");
  let rows = (data&&data.vehicles)||[];
  if(filter) rows = rows.filter(v=>
    (v.vehicle_no||"").toLowerCase().includes(filter.toLowerCase())||
    (v.line_id||v.line_code||"").toLowerCase().includes(filter.toLowerCase())
  );

  // Deduplicate: one row per (vehicle_no, line_id)
  // Prefer outbound (Εξερχόμενη) route name, fall back to whatever is available
  const byKey = {};
  rows.forEach(v=>{
    const k = v.vehicle_no+"||"+(v.line_id||v.line_code||"");
    if(!byKey[k]){
      byKey[k] = v;
    } else if(v.direction==="Εξερχόμενη"){
      // Prefer outbound entry for the route name
      byKey[k] = v;
    }
  });
  const deduped = Object.values(byKey)
    .sort((a,b)=>parseInt(a.vehicle_no||0)-parseInt(b.vehicle_no||0));

  if(!deduped.length){
    wrap.innerHTML='<div class="empty-state">Δεν βρέθηκαν οχήματα.</div>'; return;
  }
  let html='<table class="data-table"><thead><tr>'+
    '<th>Αριθμός Οχήματος</th><th>Γραμμή</th><th>Διαδρομή</th>'+
    '</tr></thead><tbody>';
  deduped.forEach(v=>{
    html+='<tr>'+
      '<td><span class="veh-no">'+v.vehicle_no+'</span></td>'+
      '<td class="mono">'+(v.line_id||v.line_code||"—")+'</td>'+
      '<td>'+(v.route_name||"")+'</td>'+
      '</tr>';
  });
  wrap.innerHTML=html+'</tbody></table>';
}

// ── line selectors (shared logic) ──────────────────────────────────────────
function buildLineSelectors(sched, kart){
  // Schedule distribution lines
  const schedLines={};
  (sched.trips||[]).forEach(t=>{ if(t.line_code) schedLines[t.line_code]=t.line_id||t.line_code; });
  buildSelect("sched-line-select", schedLines, lc=>{
    populateRouteSelect("sched-route-select", lc, sched.trips||[], rc=>renderScheduleTable(rc));
  });

  // Kartelakia lines
  const kartLines={};
  (kart.slots||[]).forEach(t=>{ if(t.line_code) kartLines[t.line_code]=t.line_id||t.line_code; });
  buildSelect("kart-line-select", kartLines, lc=>{
    populateRouteSelect("kart-route-select", lc, kart.slots||[], rc=>renderKartelakiaTable(rc));
  });
}

function buildSelect(id, linesMap, onLineSelect){
  const sel = document.getElementById(id);
  sel.innerHTML='<option value="">— Γραμμή —</option>';
  Object.entries(linesMap)
    .sort((a,b)=>a[1].localeCompare(b[1],undefined,{numeric:true}))
    .forEach(([code,lbl])=>{
      const o=document.createElement("option");
      o.value=code; o.textContent="Γραμμή "+lbl;
      sel.appendChild(o);
    });
  sel.onchange=e=>{ if(e.target.value) onLineSelect(e.target.value); };
}

function populateRouteSelect(id, lineCode, trips, onRouteSelect){
  const sel=document.getElementById(id);
  sel.innerHTML='<option value="">— Διαδρομή —</option>';
  const routes={};
  trips.filter(t=>t.line_code===lineCode).forEach(t=>{
    routes[t.route_code]=(t.route_name||t.route_code)+" ("+t.direction+")";
  });
  Object.entries(routes).forEach(([rc,lbl])=>{
    const o=document.createElement("option");
    o.value=rc; o.textContent=lbl;
    sel.appendChild(o);
  });
  sel.style.display="inline-block";
  sel.onchange=e=>{ if(e.target.value) onRouteSelect(e.target.value); };
  if(sel.options.length>1){ sel.selectedIndex=1; onRouteSelect(sel.options[1].value); }
}

// ── schedule distribution ──────────────────────────────────────────────────
function renderScheduleTable(routeCode){
  const wrap = document.getElementById("schedule-table-wrap");
  const trips = (schedData&&schedData.trips||[])
    .filter(t=>t.route_code===routeCode)
    .sort((a,b)=>(a.scheduled_dep||"").localeCompare(b.scheduled_dep||""));

  if(!trips.length){
    wrap.innerHTML='<div class="empty-state">Δεν υπάρχουν δεδομένα.</div>'; return;
  }

  const total=trips.length, exec=trips.filter(t=>t.vehicle_no).length;
  const pct=total?Math.round(exec/total*100):0;

  let html=`<div class="summary-bar"><span>${exec}</span> από <span>${total}</span> δρομολόγια εκτελέστηκαν (<span class="${pctClass(pct)}">${pct}%</span>)</div>`;
  html+='<table class="data-table"><thead><tr>'+
    '<th>Πρόγραμμα</th><th>Αναχώρηση</th><th>Λήξη</th><th>Διάρκεια</th><th>Όχημα</th>'+
    '</tr></thead><tbody>';

  trips.forEach(t=>{
    const missed = !t.vehicle_no;           // no bus ran this scheduled slot
    const incomplete = !missed && !t.ended_at;  // bus departed but never finished
    const dev=t.deviation;
    const devTip=dev==null?"":Math.abs(dev)<0.5?"στην ώρα":dev>0?"+"+dev.toFixed(1)+"λεπ":dev.toFixed(1)+"λεπ";
    const vehHtml=missed
      ? `<span class="slot-pill slot-unknown">${t.slot_label||"—"}</span>`
      : `<span class="veh-no" title="${devTip}">${t.vehicle_no}</span>`;
    // Λήξη/Διάρκεια: dashes if missed OR incomplete (never reached terminus)
    const endCell = (missed || incomplete) ? "—" : fmtTime(t.ended_at);
    const durCell = (missed || incomplete) ? "—" : fmtDur(t.started_at, t.ended_at);
    html+=`<tr class="${missed?"missed-row":""}">
      <td class="mono">${(t.scheduled_dep||"—").substring(0,5)}</td>
      <td class="mono">${missed?"—":fmtTime(t.started_at)}</td>
      <td class="mono">${endCell}</td>
      <td class="mono">${durCell}</td>
      <td>${vehHtml}</td>
    </tr>`;
  });
  wrap.innerHTML=html+'</tbody></table>';
}

// ── kartelakia ─────────────────────────────────────────────────────────────
function renderKartelakiaTable(routeCode){
  const wrap=document.getElementById("kartelakia-wrap");
  const slots=(kartData&&kartData.slots||[])
    .filter(t=>t.route_code===routeCode)
    .sort((a,b)=>(a.scheduled_dep||"").localeCompare(b.scheduled_dep||""));

  if(!slots.length){
    wrap.innerHTML='<div class="empty-state">Δεν υπάρχουν δεδομένα.</div>'; return;
  }

  // Group by slot number to show the pattern
  const slotGroups={};
  slots.forEach(s=>{
    const k=s.slot_number||0;
    if(!slotGroups[k]) slotGroups[k]=[];
    slotGroups[k].push(s);
  });
  const slotCount=Object.keys(slotGroups).filter(k=>k>0).length;

  let html=`<div class="summary-bar"><span>${slotCount}</span> καρτελάκια · <span>${slots.length}</span> προγραμματισμένα δρομολόγια</div>`;
  html+='<table class="data-table"><thead><tr>'+
    '<th>Πρόγραμμα</th><th>Καρτελάκι</th>'+
    '</tr></thead><tbody>';

  slots.forEach(s=>{
    const slotNum=s.slot_number;
    const slotHtml=slotNum
      ? `<span class="slot-pill">Καρτελάκι ${slotNum}</span>`
      : '<span class="slot-pill slot-unknown">—</span>';
    html+=`<tr>
      <td class="mono">${(s.scheduled_dep||"—").substring(0,5)}</td>
      <td>${slotHtml}</td>
    </tr>`;
  });
  wrap.innerHTML=html+'</tbody></table>';
}

// ── pipeline health ────────────────────────────────────────────────────────
function renderHealth(data){
  const list=document.getElementById("health-list");
  const runs=(data&&data.recent_runs)||[];
  if(!runs.length){ list.innerHTML='<div class="empty-state">Καμία εκτέλεση.</div>'; return; }
  list.innerHTML=runs.slice(0,12).map(r=>
    `<div class="health-row">
      <span class="job">${r.job_name}</span>
      <span class="htime">${fmtRel(r.started_at)}</span>
      <span class="detail" title="${(r.detail||"").replace(/"/g,"&quot;")}">${r.detail||""}</span>
      <span class="pill ${r.status||"running"}">${r.status||"?"}</span>
    </div>`
  ).join("");
}

// ── search ─────────────────────────────────────────────────────────────────
document.getElementById("veh-search").addEventListener("input",e=>{
  if(vehicleData) renderVehicles(vehicleData, e.target.value);
});

// ── init ───────────────────────────────────────────────────────────────────
initDatePicker().catch(err=>{
  document.getElementById("vehicle-table-wrap").innerHTML=
    '<div class="empty-state">Σφάλμα εκκίνησης: '+err.message+'</div>';
});

})();
