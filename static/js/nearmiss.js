// ── Near Miss tab: DOM-built UI (conflict map + severity list) ──
console.log('nearmiss.js v1 loaded');

async function nmDetect(focus) {
  var btn = document.getElementById('nmBtn');
  var status = document.getElementById('nmStatus');
  if (btn) btn.disabled = true;
  if (status) status.textContent = 'Analyzing conflicts… (pairs scale with traffic, allow up to a minute)';
  try {
    var payload = {
      mode: document.getElementById('nmMode').value,
      pet_threshold: parseFloat(document.getElementById('nmPet').value || '3'),
      ttc_threshold: parseFloat(document.getElementById('nmTtc').value || '1.5')
    };
    if (focus) payload.focus = focus;
    var r = await fetch('/api/nearmiss/detect', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    var j = await r.json();
    if (j.error) { status.textContent = 'Failed: ' + j.error; return; }
    var img = document.getElementById('nmImg');
    img.src = '/api/nearmiss/image?t=' + Date.now();
    img.style.display = 'block';
    var e = document.getElementById('nmEmpty');
    if (e) e.style.display = 'none';
    var c = j.counts || {};
    status.innerHTML = j.total + ' conflicts — ' +
      '<span style="color:#FF5D73;">' + (c.critical || 0) + ' critical</span>, ' +
      '<span style="color:#FF8C3C;">' + (c.severe || 0) + ' severe</span>, ' +
      '<span style="color:#FFC53D;">' + (c.moderate || 0) + ' moderate</span>, ' +
      '<span style="color:#9AA7BD;">' + (c.slight || 0) + ' slight</span>';
    var list = document.getElementById('nmList');
    var sevColor = { critical: '#FF5D73', severe: '#FF8C3C', moderate: '#FFC53D', slight: '#9AA7BD' };
    list.innerHTML = (j.events || []).map(function(ev) {
      var metric = ev.pet !== null ? 'PET ' + ev.pet + 's' : 'TTC ' + ev.ttc + 's';
      return '<div class="nm-row" data-a="' + ev.a_id + '" data-b="' + ev.b_id + '" ' +
        'style="cursor:pointer;padding:7px 10px;border-left:3px solid ' + sevColor[ev.severity] + ';' +
        'background:var(--bg-inset);border-radius:6px;margin-bottom:5px;font-size:12px;">' +
        '<b style="color:' + sevColor[ev.severity] + ';">' + ev.severity.toUpperCase() + '</b> ' +
        metric + ' — ' + ev.a_cls + ' #' + ev.a_id + ' × ' + ev.b_cls + ' #' + ev.b_id +
        ' @ frame ' + ev.frame + (ev.type === 'TTC' ? ' <i>(converging)</i>' : '') + '</div>';
    }).join('') || '<div style="color:var(--text-muted);font-size:12px;">No conflicts under the current thresholds.</div>';
    list.querySelectorAll('.nm-row').forEach(function(row) {
      row.onclick = function() {
        nmDetect([parseInt(row.dataset.a, 10), parseInt(row.dataset.b, 10)]);
      };
    });
  } catch (e2) {
    if (status) status.textContent = 'Error: ' + e2.message;
  } finally {
    if (btn) btn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', function() {
  setTimeout(function() {
    var panel = document.getElementById('panel-nearmiss');
    if (!panel || document.getElementById('nmBtn')) return;
    panel.innerHTML =
      '<div class="panel-split" style="grid-template-columns:minmax(0,1fr) 380px;align-items:start;">' +
      '  <div class="panel-main" style="display:flex;flex-direction:column;align-items:flex-start;">' +
      '    <div class="tj-empty" id="nmEmpty">Choose a mode and click <b>Detect Conflicts</b> — ' +
      '        the conflict map renders here (colored by severity; click a row to highlight that pair\'s paths).</div>' +
      '    <img id="nmImg" style="display:none;max-width:880px;max-height:62vh;border-radius:14px;' +
      '         box-shadow:0 12px 40px rgba(0,0,0,.45);">' +
      '    <div id="nmStatus" style="font-size:12px;color:var(--text-secondary);margin-top:8px;"></div>' +
      '  </div>' +
      '  <aside class="panel-aside">' +
      '    <div class="section-header">Near Miss Analysis</div>' +
      '    <label class="field-label">Mode</label>' +
      '    <select id="nmMode" class="text-input" style="width:100%;margin-bottom:6px;">' +
      '      <option value="veh_ped">Vehicle × Pedestrian / Cyclist</option>' +
      '      <option value="veh_veh">Vehicle × Vehicle</option>' +
      '    </select>' +
      '    <div style="display:flex;gap:8px;margin-bottom:8px;">' +
      '      <div style="flex:1;"><label class="field-label">PET ≤ (s)</label>' +
      '        <input type="number" id="nmPet" class="text-input" style="width:100%;" value="3.0" step="0.5"></div>' +
      '      <div style="flex:1;"><label class="field-label">TTC ≤ (s)</label>' +
      '        <input type="number" id="nmTtc" class="text-input" style="width:100%;" value="1.5" step="0.5"></div>' +
      '    </div>' +
      '    <button class="btn" id="nmBtn" style="width:100%;" onclick="nmDetect()">Detect Conflicts</button>' +
      '    <div id="nmList" style="margin-top:12px;max-height:52vh;overflow-y:auto;"></div>' +
      '  </aside>' +
      '</div>';
  }, 600);
});
