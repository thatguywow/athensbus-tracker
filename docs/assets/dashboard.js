(function () {
  "use strict";

  const C = { good:"#3FA796", warn:"#E08E45", bad:"#D9685E", neutral:"#7A8699", blue:"#5B9FE3" };

  let summaryData=null, historyData=null, vehicleData=null, handoffData=null;
  const chartInstances = {};

  // ── utils ──
  function pctClass(p){ return p===null||p===undefined?"neutral":p>=90?"good":p>=70?"warn":"bad"; }
  function fmtPct(p){ return p===null||p===undefined?"—":p.toFixed(1)+"%"; }
  function fmtInt(n){ return n===null||n===undefined?"—":new Intl.NumberFormat("el-GR").format(n); }
  function fmtDev(d){
    if(d===null||d===undefined) return "—";
    const s = Math.abs(d)<0.5?"στην ώρα":d>0?"+"+d.toFixed(1)+" λεπ":d.toFixed(1)+" λεπ";
    return s;
  }
  function fmtTime(iso){
    if(!iso) return "—";
    try{ return new Date(iso).toLocaleTimeString("el-GR",{hour:"2-digit",minute:"2-digit"}); }
    catch(e){ return iso.substring(11,16); }
  }
  function fmtRelTime(iso){
    if(!iso) return "—";
    const diff = Math.round((Date.now()-new Date(iso).getTime())/60000);
    if(diff<1)  return "μόλις τώρα";
    if(diff<60) return diff+"λεπ πριν";
    const h = Math.round(diff/60);
    if(h<24)    return h+"ω πριν";
    return Math.round(h/24)+"μ πριν";
  }
  async function loadJSON(p){ const r=await fetch(p,{cache:"no-store"}); if(!r.ok) throw new Error(p+" → "+r.status); return r.json(); }

  // ── tabs ──
  document.querySelectorAll(".tab").forEach(tab=>{
    tab.addEventListener("click",()=>{
      document.querySelectorAll(".tab").forEach(t=>t.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach(p=>p.classList.remove("active"));
      tab.classList.add("active");
      document.getElementById("tab-"+tab.dataset.tab).classList.add("active");
    });
  });

  // ── summary board ──
  function renderSummary(d){
    document.getElementById("stat-date").textContent        = d.latest_date||"—";
    document.getElementById("stat-actual").textContent      = fmtInt(d.system_actual_trips);
    document.getElementById("stat-scheduled").textContent   = fmtInt(d.system_scheduled_trips);
    document.getElementById("stat-routes").textContent      = fmtInt(d.route_count);
    document.getElementById("last-updated").textContent     = fmtRelTime(d.generated_at);
    const cel = document.getElementById("stat-completion");
    cel.textContent  = fmtPct(d.system_completion_pct);
    cel.className    = "stat-value mono "+pctClass(d.system_completion_pct);
  }

  // ── route cards ──
  function routeCardHTML(r){
    const cls   = pctClass(r.completion_pct);
    const color = C[cls]||C.neutral;
    const w     = r.completion_pct===null?0:Math.min(100,Math.max(0,r.completion_pct));
    const dev   = r.avg_deviation!==null&&r.avg_deviation!==undefined
                  ? (Math.abs(r.avg_deviation)<0.5?"στην ώρα"
                    :r.avg_deviation>0?"+"+r.avg_deviation.toFixed(1)+"λεπ"
                    :r.avg_deviation.toFixed(1)+"λεπ")
                  : "";
    return (
      '<div class="route-card" data-route="'+r.route_code+'">' +
        '<div class="route-top">' +
          '<span class="route-line-code">'+(r.line_code||"?")+'</span>' +
          '<span class="route-dir">'+(r.direction||"")+'</span>' +
        '</div>' +
        '<div class="route-name">'+(r.route_name||"")+'</div>' +
        '<div class="route-pct mono '+cls+'">'+fmtPct(r.completion_pct)+'</div>' +
        '<div class="route-meta">'+fmtInt(r.actual)+' από '+fmtInt(r.scheduled)+' δρομολόγια · '+fmtInt(r.vehicles)+' οχήματα'+(dev?' · '+dev:'')+'</div>' +
        '<div class="bar-track"><div class="bar-fill" style="width:'+w+'%;background:'+color+';"></div></div>' +
        '<div class="route-detail">' +
          '<div style="position:relative;height:150px;"><canvas id="chart-'+r.route_code+'"></canvas></div>' +
          '<div class="detail-meta">' +
            '<div>Σειρές δρομολόγων: <span>'+(r.slot_count||"—")+'</span></div>' +
            '<div>Μ.Ο. καθυστέρησης: <span>'+(dev||"—")+'</span></div>' +
          '</div>' +
        '</div>' +
      '</div>'
    );
  }

  function renderRouteGrid(routes, isFiltered){
    const grid = document.getElementById("route-grid");
    if(!routes||!routes.length){
      grid.innerHTML = '<div class="empty-state">'+(isFiltered?"Δεν βρέθηκαν διαδρομές.":"Δεν υπάρχουν δεδομένα ακόμα.")+'</div>';
      return;
    }
    document.getElementById("route-count-label").textContent="("+routes.length+")";
    grid.innerHTML = routes.map(routeCardHTML).join("");
    grid.querySelectorAll(".route-card").forEach(card=>{
      card.addEventListener("click",()=>{
        const wasExp = card.classList.contains("expanded");
        grid.querySelectorAll(".route-card.expanded").forEach(c=>c.classList.remove("expanded"));
        if(!wasExp){ card.classList.add("expanded"); renderRouteChart(card.dataset.route); }
      });
    });
  }

  function renderRouteChart(rc){
    const canvas = document.getElementById("chart-"+rc);
    if(!canvas||chartInstances[rc]) return;
    if(typeof Chart==="undefined"){
      canvas.parentElement.innerHTML='<div class="empty-state" style="padding:.8rem;">Βιβλιοθήκη γραφήματος μη διαθέσιμη.</div>';
      return;
    }
    const series = (historyData&&historyData.by_route&&historyData.by_route[rc])||[];
    if(!series.length){
      canvas.parentElement.innerHTML='<div class="empty-state" style="padding:.8rem;">Δεν υπάρχει ιστορικό.</div>';
      return;
    }
    chartInstances[rc] = new Chart(canvas,{
      type:"line",
      data:{
        labels: series.map(d=>d.date.slice(5)),
        datasets:[{
          label:"Εκτέλεση %",
          data: series.map(d=>d.completion_pct),
          borderColor:"#5B9FE3", backgroundColor:"rgba(91,159,227,.12)",
          fill:true, tension:.15, pointRadius:2, spanGaps:true,
        }]
      },
      options:{
        responsive:true, maintainAspectRatio:false,
        plugins:{legend:{display:false}},
        scales:{
          y:{min:0,max:110,ticks:{color:"#7A8699",font:{size:10}},grid:{color:"rgba(245,243,238,.08)"}},
          x:{ticks:{color:"#7A8699",font:{size:10},maxTicksLimit:8},grid:{display:false}},
        }
      }
    });
  }

  function applyFilters(){
    if(!summaryData) return;
    const q    = document.getElementById("search").value.trim().toLowerCase();
    const sort = document.getElementById("sort").value;
    let routes = summaryData.routes.slice();
    if(q) routes = routes.filter(r=>
      (r.line_code||"").toLowerCase().includes(q)||
      (r.route_name||"").toLowerCase().includes(q)
    );
    if(sort==="completion_asc")  routes.sort((a,b)=>(a.completion_pct??-1)-(b.completion_pct??-1));
    if(sort==="completion_desc") routes.sort((a,b)=>(b.completion_pct??-1)-(a.completion_pct??-1));
    if(sort==="line")            routes.sort((a,b)=>(a.line_code||"").localeCompare(b.line_code||"",undefined,{numeric:true}));
    renderRouteGrid(routes, Boolean(q));
  }

  // ── vehicle table ──
  function renderVehicles(data, filter){
    const wrap = document.getElementById("vehicle-table-wrap");
    let vehicles = (data&&data.vehicles)||[];
    if(filter) vehicles = vehicles.filter(v=>(v.vehicle_no||"").toLowerCase().includes(filter.toLowerCase()));
    if(!vehicles.length){
      wrap.innerHTML='<div class="empty-state">Δεν βρέθηκαν οχήματα.</div>';
      return;
    }
    let html = '<table class="vehicle-table"><thead><tr>'+
      '<th>Αριθμός</th><th>Γραμμή</th><th>Διαδρομή</th><th>Κατεύθυνση</th>'+
      '<th>Σειρά</th><th>Δρομολόγια</th><th>1η Αναχ.</th><th>Τελ. Αναχ.</th><th>Σύνολο (λεπ)</th>'+
      '</tr></thead><tbody>';
    vehicles.forEach(v=>{
      html += '<tr>'+
        '<td><span class="veh-no">'+v.vehicle_no+'</span></td>'+
        '<td>'+(v.line_code||"")+'</td>'+
        '<td>'+(v.route_name||"")+'</td>'+
        '<td>'+(v.direction||"")+'</td>'+
        '<td>'+(v.slot_number!==null&&v.slot_number!==undefined?'<span class="slot-pill">'+v.slot_number+'</span>':"—")+'</td>'+
        '<td>'+fmtInt(v.trip_count)+'</td>'+
        '<td class="mono">'+fmtTime(v.first_departure)+'</td>'+
        '<td class="mono">'+fmtTime(v.last_departure)+'</td>'+
        '<td class="mono">'+(v.total_mins?Math.round(v.total_mins):"—")+'</td>'+
        '</tr>';
    });
    html += '</tbody></table>';
    wrap.innerHTML = html;
  }

  // ── handoffs ──
  function renderHandoffs(data){
    const list = document.getElementById("handoff-list");
    document.getElementById("stat-handoffs").textContent = fmtInt((data&&data.handoffs||[]).length);
    const handoffs = (data&&data.handoffs)||[];
    if(!handoffs.length){
      list.innerHTML='<div class="empty-state">Δεν υπάρχουν αλλαγές βάρδιας για αυτή την ημέρα.</div>';
      return;
    }
    list.innerHTML = handoffs.map(h=>
      '<div class="handoff-row">'+
        '<span class="mono">'+(h.line_code||"")+'</span>'+
        '<span class="veh-no">'+h.outgoing_vehicle+'</span>'+
        '<span class="handoff-arrow">→</span>'+
        '<span class="veh-no">'+h.incoming_vehicle+'</span>'+
        '<span class="mono">'+fmtTime(h.handoff_time)+'</span>'+
        '<span class="slot-pill">Σειρά '+h.slot_number+'</span>'+
      '</div>'
    ).join("");
  }

  // ── health ──
  function renderHealth(data){
    const list  = document.getElementById("health-list");
    const runs  = (data&&data.recent_runs)||[];
    if(!runs.length){ list.innerHTML='<div class="empty-state">Δεν υπάρχουν εκτελέσεις.</div>'; return; }
    list.innerHTML = runs.slice(0,12).map(r=>
      '<div class="health-row">'+
        '<span class="job">'+r.job_name+'</span>'+
        '<span class="time">'+fmtRelTime(r.started_at)+'</span>'+
        '<span class="detail" title="'+(r.detail||"").replace(/"/g,"&quot;")+'">'+( r.detail||"")+'</span>'+
        '<span class="pill '+(r.status||"running")+'">'+( r.status||"?")+'</span>'+
      '</div>'
    ).join("");
  }

  // ── init ──
  async function init(){
    try{
      const [summary, history, vehicles, handoffs, health] = await Promise.all([
        loadJSON("data/summary.json"),
        loadJSON("data/history.json").catch(()=>({by_route:{}})),
        loadJSON("data/vehicle_activity.json").catch(()=>({vehicles:[]})),
        loadJSON("data/handoffs.json").catch(()=>({handoffs:[]})),
        loadJSON("data/pipeline_health.json").catch(()=>({recent_runs:[]})),
      ]);
      summaryData  = summary;
      historyData  = history;
      vehicleData  = vehicles;
      handoffData  = handoffs;

      renderSummary(summary);
      applyFilters();
      renderVehicles(vehicles, "");
      renderHandoffs(handoffs);
      renderHealth(health);
    } catch(err){
      document.getElementById("route-grid").innerHTML=
        '<div class="empty-state">Σφάλμα φόρτωσης: '+String(err.message||err)+'</div>';
    }
  }

  document.getElementById("search").addEventListener("input", applyFilters);
  document.getElementById("sort").addEventListener("change", applyFilters);
  document.getElementById("veh-search").addEventListener("input", e=>{
    renderVehicles(vehicleData, e.target.value);
  });

  init();
})();
