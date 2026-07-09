// ── Gate Drawing & Management ──
window.drawingGate = false;
window.gateP1 = null;
window.movements = [];  // [{from_id, from_name, to_id, to_name}, ...]

// ── Gate naming: dropdown of arms (dual notation) instead of free text ──
// Prevents typos like "Nrth" silently breaking the auto movement numbering.
var ARM_OPTIONS = [
  { value: 'North', label: 'North arm — SB approach (traffic heading South)' },
  { value: 'East',  label: 'East arm — WB approach (traffic heading West)' },
  { value: 'South', label: 'South arm — NB approach (traffic heading North)' },
  { value: 'West',  label: 'West arm — EB approach (traffic heading East)' }
];

function askGateName() {
  return new Promise(function(resolve) {
    var taken = {};
    (window.gates || []).forEach(function(g) {
      var k = String(g.name || '').trim().toLowerCase();
      var canon = { n: 'North', north: 'North', s: 'South', south: 'South',
                    e: 'East', east: 'East', w: 'West', west: 'West' }[k];
      if (canon) taken[canon] = true;
    });

    var overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);' +
      'display:flex;align-items:center;justify-content:center;z-index:9999;';
    var opts = ARM_OPTIONS.map(function(o) {
      var dis = taken[o.value] ? ' disabled' : '';
      var suffix = taken[o.value] ? ' — already drawn' : '';
      return '<option value="' + o.value + '"' + dis + '>' + o.label + suffix + '</option>';
    }).join('') + '<option value="__other__">Other (custom name)…</option>';

    overlay.innerHTML =
      '<div style="background:var(--bg-panel,#161b22);border:1px solid var(--border,#30363d);' +
      'border-radius:8px;padding:16px;width:340px;font-size:13px;color:var(--text,#c9d1d9);">' +
      '<div style="font-weight:bold;margin-bottom:8px;">Which arm is this gate on?</div>' +
      '<select id="armSel" style="width:100%;padding:6px;background:var(--bg-dark,#0d1117);' +
      'color:var(--text,#c9d1d9);border:1px solid var(--border,#30363d);border-radius:4px;">' +
      opts + '</select>' +
      '<input id="armCustom" type="text" placeholder="Custom gate name" style="display:none;' +
      'width:100%;margin-top:8px;padding:6px;background:var(--bg-dark,#0d1117);' +
      'color:var(--text,#c9d1d9);border:1px solid var(--border,#30363d);border-radius:4px;">' +
      '<div style="font-size:11px;color:var(--text-muted,#8b949e);margin-top:8px;">' +
      'Compass arms enable automatic client movement numbering. Set one-way arms ' +
      'afterwards from the dropdown on the gate card.</div>' +
      '<div style="display:flex;gap:8px;margin-top:12px;">' +
      '<button id="armCancel" class="btn" style="flex:1;">Cancel</button>' +
      '<button id="armOk" class="btn" style="flex:1;background:var(--accent,#238636);color:#fff;">OK</button>' +
      '</div></div>';
    document.body.appendChild(overlay);

    var sel = overlay.querySelector('#armSel');
    var custom = overlay.querySelector('#armCustom');
    // preselect first arm not yet taken
    for (var i = 0; i < ARM_OPTIONS.length; i++) {
      if (!taken[ARM_OPTIONS[i].value]) { sel.value = ARM_OPTIONS[i].value; break; }
    }
    sel.onchange = function() {
      custom.style.display = sel.value === '__other__' ? 'block' : 'none';
      if (sel.value === '__other__') custom.focus();
    };
    function close(val) { document.body.removeChild(overlay); resolve(val); }
    overlay.querySelector('#armCancel').onclick = function() { close(null); };
    overlay.querySelector('#armOk').onclick = function() {
      if (sel.value === '__other__') {
        var v = custom.value.trim();
        close(v || null);
      } else close(sel.value);
    };
    overlay.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') close(null);
      if (e.key === 'Enter') overlay.querySelector('#armOk').click();
    });
    sel.focus();
  });
}


function startDrawGate() {
  window.drawingGate = true;
  window.gateP1 = null;
  document.getElementById('drawHint').style.display = 'block';
  document.getElementById('btnDrawGate').classList.add('active');
  canvas.style.cursor = 'crosshair';
}

function cancelDrawGate() {
  window.drawingGate = false;
  window.gateP1 = null;
  document.getElementById('drawHint').style.display = 'none';
  document.getElementById('btnDrawGate').classList.remove('active');
  canvas.style.cursor = 'default';
  render();
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && window.drawingGate) cancelDrawGate();
});

canvas.addEventListener('click', async function(e) {
  if (!window.drawingGate) return;
  var rect = canvas.getBoundingClientRect();
  var scaleX = frameW / rect.width, scaleY = frameH / rect.height;
  var x = Math.round((e.clientX - rect.left) * scaleX);
  var y = Math.round((e.clientY - rect.top) * scaleY);

  if (!window.gateP1) {
    window.gateP1 = { x: x, y: y };
    document.getElementById('drawHint').textContent = 'Click second point to complete the gate line (Esc to cancel)';
    render();
  } else {
    var name = await askGateName();
    if (!name) { cancelDrawGate(); return; }

    try {
      var res = await fetch('/api/gates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name, x1: window.gateP1.x, y1: window.gateP1.y, x2: x, y2: y, direction: 'both' })
      });
      if (!res.ok) alert('Failed to create gate');
    } catch (err) { alert('Error: ' + err.message); }

    try { window.gates = await fetchJSON('/api/gates/count_summary'); }
    catch (err) { window.gates = await fetchJSON('/api/gates'); }

    renderGateList();
    window.drawingGate = false;
    window.gateP1 = null;
    document.getElementById('drawHint').style.display = 'none';
    document.getElementById('drawHint').textContent = 'Click two points to draw a gate line';
    document.getElementById('btnDrawGate').classList.remove('active');
    canvas.style.cursor = 'default';
    render();
  }
});

canvas.addEventListener('mousemove', function(e) {
  if (!window.drawingGate || !window.gateP1) return;
  var rect = canvas.getBoundingClientRect();
  var scaleX = frameW / rect.width, scaleY = frameH / rect.height;
  var mx = (e.clientX - rect.left) * scaleX;
  var my = (e.clientY - rect.top) * scaleY;
  render();
  ctx.beginPath();
  ctx.moveTo(window.gateP1.x, window.gateP1.y);
  ctx.lineTo(mx, my);
  ctx.strokeStyle = 'rgba(248,81,73,0.7)';
  ctx.lineWidth = 2;
  ctx.setLineDash([8, 4]);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.beginPath();
  ctx.arc(window.gateP1.x, window.gateP1.y, 6, 0, Math.PI * 2);
  ctx.fillStyle = '#f85149';
  ctx.fill();
});

async function setGateMode(id, mode) {
  try {
    var res = await fetch('/api/gates/' + id + '/arm_mode', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ arm_mode: mode })
    });
    if (!res.ok) { alert('Failed to update arm mode'); return; }
  } catch (err) { alert('Error: ' + err.message); return; }
  try { window.gates = await fetchJSON('/api/gates/count_summary'); }
  catch (err) { window.gates = await fetchJSON('/api/gates'); }
  renderGateList();
  render();
}

async function deleteGate(id) {
  if (!confirm('Delete this gate?')) return;
  try { await fetch('/api/gates/' + id, { method: 'DELETE' }); window.gates = await fetchJSON('/api/gates/count_summary'); }
  catch (err) { window.gates = await fetchJSON('/api/gates'); }
  // Remove movements referencing this gate
  window.movements = window.movements.filter(function(m) { return m.from_id !== id && m.to_id !== id; });
  renderGateList();
  render();
}

async function deleteAllGates() {
  if (!confirm('Delete ALL gates? This cannot be undone.')) return;
  try { await fetch('/api/gates/all', { method: 'DELETE' }); } catch (err) {}
  window.gates = [];
  window.movements = [];
  renderGateList();
  render();
}

// ═══════════════════════════════════════
// GATE LIST RENDERING
// ═══════════════════════════════════════

function renderGateList() {
  var el = document.getElementById('gateList');
  var gatesList = window.gates || [];

  if (!gatesList.length) {
    el.innerHTML = '<div style="color:var(--text-muted);font-size:12px;text-align:center;padding:20px;">No gates defined yet.<br>Click "+ Draw New Gate" to add one.</div>';
    document.getElementById('btnExport').style.display = 'none';
    document.getElementById('movementSection').style.display = 'none';
    return;
  }

  document.getElementById('btnExport').style.display = 'block';

  var html = '';
  html += gatesList.map(function(g) {
    var classCounts = (g.counts || []).map(function(c) {
      return '<span style="color:' + (CLASS_COLORS[c.class_name] || '#888') + '">' + c.class_name + ': ' + c.cnt + '</span>';
    }).join(' ');
    var mode = g.arm_mode || 'two';
    var modeSel =
      '<select onchange="setGateMode(' + g.gate_id + ', this.value)" ' +
        'style="margin-top:6px;width:100%;font-size:11px;background:var(--bg-dark,#161b22);' +
        'color:var(--text,#c9d1d9);border:1px solid var(--border,#30363d);border-radius:4px;padding:3px;">' +
        '<option value="two"' + (mode === 'two' ? ' selected' : '') + '>Two-way arm</option>' +
        '<option value="in"' + (mode === 'in' ? ' selected' : '') + '>One-way IN (approach only)</option>' +
        '<option value="out"' + (mode === 'out' ? ' selected' : '') + '>One-way OUT (exit only)</option>' +
      '</select>';
    var modeBadge = mode === 'two' ? '' :
      '<span style="font-size:10px;color:var(--yellow,#d29922);margin-left:6px;">' +
      (mode === 'in' ? '⇥ one-way in' : '⇤ one-way out') + '</span>';
    return '<div class="gate-item">' +
      '<div class="gate-header">' +
        '<span class="gate-name">' + g.name + modeBadge + '</span>' +
        '<span class="gate-count">' + (g.total || 0) + '</span>' +
      '</div>' +
      (classCounts ? '<div class="gate-class-counts">' + classCounts + '</div>' : '') +
      modeSel +
      '<button class="btn btn-sm" style="margin-top:6px;color:var(--red);border-color:var(--red);" onclick="deleteGate(' + g.gate_id + ')">✕ Delete</button>' +
    '</div>';
  }).join('');

  if (gatesList.length > 1) {
    html += '<button class="btn btn-sm" style="width:100%;margin-top:8px;color:var(--red);border-color:var(--red);" onclick="deleteAllGates()">Delete All Gates</button>';
  }
  el.innerHTML = html;

  // Show movement section when 2+ gates
  if (gatesList.length >= 2) {
    document.getElementById('movementSection').style.display = 'block';
    updateMovementDropdowns();
    renderMovementList();
  } else {
    document.getElementById('movementSection').style.display = 'none';
  }
}

// ═══════════════════════════════════════
// MOVEMENT DEFINITIONS
// ═══════════════════════════════════════

function updateMovementDropdowns() {
  var gatesList = window.gates || [];
  var fromSel = document.getElementById('movFrom');
  var toSel = document.getElementById('movTo');

  var optionsHtml = gatesList.map(function(g) {
    return '<option value="' + g.gate_id + '">' + g.name + '</option>';
  }).join('');

  fromSel.innerHTML = optionsHtml;
  toSel.innerHTML = optionsHtml;

  // Default: select different gates
  if (gatesList.length >= 2) {
    toSel.selectedIndex = 1;
  }
}

function addMovement() {
  var fromSel = document.getElementById('movFrom');
  var toSel = document.getElementById('movTo');
  var fromId = parseInt(fromSel.value);
  var toId = parseInt(toSel.value);

  if (fromId === toId) {
    alert('From and To gates must be different.');
    return;
  }

  // Check duplicate
  var exists = window.movements.some(function(m) { return m.from_id === fromId && m.to_id === toId; });
  if (exists) {
    alert('This movement is already defined.');
    return;
  }

  var fromName = fromSel.options[fromSel.selectedIndex].text;
  var toName = toSel.options[toSel.selectedIndex].text;

  window.movements.push({ from_id: fromId, from_name: fromName, to_id: toId, to_name: toName });
  renderMovementList();
}

function addAllMovements() {
  var gatesList = window.gates || [];
  window.movements = [];

  for (var i = 0; i < gatesList.length; i++) {
    for (var j = 0; j < gatesList.length; j++) {
      if (i !== j) {
        window.movements.push({
          from_id: gatesList[i].gate_id, from_name: gatesList[i].name,
          to_id: gatesList[j].gate_id, to_name: gatesList[j].name
        });
      }
    }
  }
  renderMovementList();
}

function removeMovement(idx) {
  window.movements.splice(idx, 1);
  renderMovementList();
}

function clearMovements() {
  window.movements = [];
  renderMovementList();
}

function renderMovementList() {
  var el = document.getElementById('movementList');
  if (!window.movements.length) {
    el.innerHTML = '<div style="color:var(--text-muted);font-size:11px;text-align:center;padding:8px;">No movements defined.<br>Add From→To pairs above, or click "Add All".</div>';
    return;
  }

  var html = '<div style="font-size:11px;color:var(--text-secondary);margin-bottom:4px;">' +
    window.movements.length + ' movement(s) defined ' +
    '<span onclick="clearMovements()" style="color:var(--red);cursor:pointer;text-decoration:underline;margin-left:6px;">Clear all</span></div>';

  html += window.movements.map(function(m, idx) {
    return '<div style="display:flex;align-items:center;justify-content:space-between;padding:3px 6px;margin:2px 0;background:var(--bg-tertiary);border-radius:3px;font-size:12px;">' +
      '<span>' +
        '<span style="color:var(--accent);">' + m.from_name + '</span>' +
        ' <span style="color:var(--text-muted);">→</span> ' +
        '<span style="color:var(--green);">' + m.to_name + '</span>' +
      '</span>' +
      '<span onclick="removeMovement(' + idx + ')" style="color:var(--red);cursor:pointer;font-size:10px;padding:2px 4px;">✕</span>' +
    '</div>';
  }).join('');

  el.innerHTML = html;
}

// ═══════════════════════════════════════
// EXCEL EXPORT WITH MOVEMENTS
// ═══════════════════════════════════════

function exportMovementExcel() {
  var gatesList = window.gates || [];
  if (!gatesList.length) {
    alert('No gates defined. Draw gates first.');
    return;
  }

  if (!window.movements.length) {
    // No movements defined — ask if they want basic export or to define movements first
    if (!confirm('No movements defined.\n\nClick OK for basic gate export, or Cancel to define movements first.')) return;
    // Basic export (old behavior)
    triggerDownload('/api/export/gates_excel');
    return;
  }

  // Build movement params and trigger export
  var movementParam = encodeURIComponent(JSON.stringify(window.movements));
  triggerDownload('/api/export/movement_excel?movements=' + movementParam);
}

function triggerDownload(url) {
  var btn = document.getElementById('btnExport');
  btn.textContent = '⏳ Generating...';
  btn.disabled = true;

  var a = document.createElement('a');
  a.href = url;
  a.download = 'traffic_report.xlsx';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);

  setTimeout(function() {
    btn.textContent = '📥 Export Results to Excel';
    btn.disabled = false;
  }, 2000);
}

// =======================================
// TIME-BINNED EXPORT
// =======================================

var selectedBinMinutes = 15;

function setBinMinutes(minutes) {
  selectedBinMinutes = minutes;
  var btns = document.querySelectorAll('#binBtnGroup .bin-btn');
  btns.forEach(function(b) {
    b.style.background = '';
    b.style.color = '';
    b.style.borderColor = '';
    b.classList.remove('active');
  });
  event.target.style.background = 'var(--accent)';
  event.target.style.color = '#000';
  event.target.style.borderColor = 'var(--accent)';
  event.target.classList.add('active');
}

async function loadTimeRangeInfo() {
  try {
    var info = await fetchJSON('/api/export/time_range_info');
    var hint = document.getElementById('timeRangeHint');
    if (hint) {
      if (info.has_real_time) {
        hint.textContent = 'Video: ' + info.start_display + ' to ' + info.end_display;
      } else {
        hint.textContent = 'Duration: ' + Math.round(info.duration_sec / 60) + ' min (no clock time)';
      }
    }
  } catch (err) {
    console.warn('Time range info not available:', err);
  }
}

function exportTimeBinnedExcel() {
  console.log('[TimeBinned] Export clicked, bin_minutes=' + selectedBinMinutes);
  var gatesList = window.gates || [];
  if (!gatesList.length) {
    alert('No gates defined. Draw gates first.');
    return;
  }
  console.log('[TimeBinned] Gates:', gatesList.length);

  if (!window.movements.length) {
    alert('No movements defined. Add gate pairs first, or click Add All Possible Movements.');
    return;
  }
  console.log('[TimeBinned] Movements:', window.movements.length);

  var btn = document.getElementById('btnExportTimeBinned');
  btn.textContent = 'Generating...';
  btn.disabled = true;

  var movementParam = encodeURIComponent(JSON.stringify(window.movements));
  var url = '/api/export/time_binned_excel?movements=' + movementParam +
            '&bin_minutes=' + selectedBinMinutes;
  console.log('[TimeBinned] Requesting:', url.substring(0, 120) + '...');

  var a = document.createElement('a');
  a.href = url;
  a.download = 'traffic_time_binned_' + selectedBinMinutes + 'min.xlsx';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  console.log('[TimeBinned] Download triggered');

  setTimeout(function() {
    btn.textContent = 'Export Time-Binned Excel';
    btn.disabled = false;
  }, 3000);
}

// Auto-load time range on page load
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', function() {
    setTimeout(loadTimeRangeInfo, 500);
  });
} else {
  setTimeout(loadTimeRangeInfo, 500);
}
