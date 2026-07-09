// ── Dashboard ──
function renderDashboard() {
  const s = summary;
  const total = s.total_tracks || 1;
  const cb = s.class_breakdown;

  // Stat cards
  document.getElementById('statGrid').innerHTML = `
    <div class="stat-card"><div class="label">Total Vehicles</div><div class="value blue">${s.total_tracks}</div></div>
    <div class="stat-card"><div class="label">Moving</div><div class="value green">${s.moving_tracks}</div></div>
    <div class="stat-card"><div class="label">Stationary</div><div class="value yellow">${s.stationary_tracks}</div></div>
    <div class="stat-card"><div class="label">Avg Speed</div><div class="value purple">${s.speed_stats.mean.toFixed(1)}<span style="font-size:11px;color:var(--text-muted)"> px/f</span></div></div>
  `;

  // Class bar
  document.getElementById('classBar').innerHTML = Object.entries(cb).map(([cls, cnt]) =>
    `<div style="width:${cnt/total*100}%;background:${CLASS_COLORS[cls]||'#555'}"></div>`
  ).join('');

  document.getElementById('classLegend').innerHTML = Object.entries(cb).map(([cls, cnt]) =>
    `<div class="class-legend-item"><div class="dot" style="background:${CLASS_COLORS[cls]||'#555'}"></div>${cls}: ${cnt}</div>`
  ).join('');

  // Flow chart
  const entries = s.entry_edges, exits = s.exit_edges;
  let flowHTML = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:11px;">';
  flowHTML += '<div><div style="color:var(--text-muted);margin-bottom:4px;">ENTRIES</div>';
  for (const [edge, cnt] of Object.entries(entries)) {
    flowHTML += `<div style="display:flex;align-items:center;gap:6px;margin:2px 0;">
      <div style="flex:1;height:4px;background:var(--border);border-radius:2px;overflow:hidden;">
        <div style="width:${cnt/total*100}%;height:100%;background:var(--accent);"></div>
      </div><span style="min-width:50px;color:var(--text-secondary)">${edge} ${cnt}</span></div>`;
  }
  flowHTML += '</div><div><div style="color:var(--text-muted);margin-bottom:4px;">EXITS</div>';
  for (const [edge, cnt] of Object.entries(exits)) {
    flowHTML += `<div style="display:flex;align-items:center;gap:6px;margin:2px 0;">
      <div style="flex:1;height:4px;background:var(--border);border-radius:2px;overflow:hidden;">
        <div style="width:${cnt/total*100}%;height:100%;background:var(--green);"></div>
      </div><span style="min-width:50px;color:var(--text-secondary)">${edge} ${cnt}</span></div>`;
  }
  flowHTML += '</div></div>';
  document.getElementById('flowChart').innerHTML = flowHTML;

  // Speed histogram
  drawSpeedChart(s.speed_stats.values);

  // Class filter buttons
  let filterHTML = '<button class="filter-btn active" onclick="filterClass(null)">All</button>';
  for (const cls of Object.keys(cb)) {
    filterHTML += `<button class="filter-btn" onclick="filterClass('${cls}')">${cls} (${cb[cls]})</button>`;
  }
  document.getElementById('classFilters').innerHTML = filterHTML;
}

function drawSpeedChart(values) {
  const cvs = document.getElementById('speedChart');
  const c = cvs.getContext('2d');
  const w = cvs.parentElement.clientWidth - 24;
  cvs.width = w; cvs.height = 120;
  if (!values.length) return;

  const bins = 20, max = Math.max(...values), binW = max / bins;
  const counts = new Array(bins).fill(0);
  values.forEach(v => { counts[Math.min(Math.floor(v / binW), bins - 1)]++; });
  const maxCount = Math.max(...counts);
  const barW = w / bins;

  c.fillStyle = '#111820'; c.fillRect(0, 0, w, 120);
  counts.forEach((cnt, i) => {
    const h = (cnt / maxCount) * 100;
    c.fillStyle = 'rgba(88,166,255,0.6)';
    c.fillRect(i * barW + 1, 110 - h, barW - 2, h);
  });
  c.fillStyle = '#5a6370'; c.font = '9px JetBrains Mono';
  c.fillText('0', 2, 118);
  c.fillText(max.toFixed(0) + ' px/f', w - 40, 118);
}
