console.log('trajectories.js v18 loaded');
// ── Trajectory Plot panel ──
async function generateTrajectoryPlot(autoPreview) {
  try {
    return await _generatePlotInner(autoPreview);
  } catch (e) {
    alert('Generate failed: ' + e.message + '\n\n' + (e.stack || '').split('\n')[1]);
    var b = document.getElementById('tjBtn');
    if (b) b.disabled = false;
  }
}

async function _generatePlotInner(autoPreview) {
  var btn = document.getElementById('tjBtn');
  var status = document.getElementById('tjStatus');
  if (!btn || !status) throw new Error('UI elements missing: tjBtn=' + !!btn + ' tjStatus=' + !!status);
  btn.disabled = true;
  status.textContent = 'Rendering… (a few seconds on busy files)';
  try {
    var payload = {
      legend: document.getElementById('tjLegend').checked,
      skip_stationary: document.getElementById('tjSkipStat').checked
    };
    var picked = Object.keys(window.tjSelectedClasses || {}).filter(function(k){ return window.tjSelectedClasses[k]; });
    if (picked.length) payload.classes = picked;
    else {
      var cls = document.getElementById('tjClasses').value.trim();
      if (cls) payload.classes = cls.split(',').map(function(s){ return s.trim(); }).filter(Boolean);
    }
    var pc = document.getElementById('tjPerClass').value.trim();
    if (pc) payload.per_class = parseInt(pc, 10);
    var fg = document.getElementById('tjFromGate').value;
    var tg = document.getElementById('tjToGate').value;
    if (fg && tg) { payload.from_gate = fg; payload.to_gate = tg; }
    else if (fg || tg) { status.textContent = 'Select BOTH gates for a movement filter, or neither.'; btn.disabled = false; return; }
    var ft = document.getElementById('tjFromTime').value.trim();
    var tt = document.getElementById('tjToTime').value.trim();
    if (ft) payload.from_time = ft;
    if (tt) payload.to_time = tt;

    var r = await fetch('/api/trajectories/render', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    var j = await r.json();
    if (j.error) { status.textContent = 'Failed: ' + j.error; return; }
    var counts = Object.keys(j.file_class_counts || {}).map(function(k) {
      return k + ': ' + j.file_class_counts[k];
    }).join('  ');
    status.textContent = 'Done — ' + counts +
      (j.saved ? '  (saved to trajectory_plots)' : '  (preview only, not saved)') +
      '  — click image for full size';
    var empty = document.getElementById('tjEmpty');
    if (empty) empty.style.display = 'none';
    if (!document.getElementById('tjSaveBtn')) {
      var sb = document.createElement('a');
      sb.id = 'tjSaveBtn'; sb.className = 'btn btn-sm';
      sb.textContent = '⬇ Save PNG';
      sb.style.cssText = 'display:inline-block;margin-top:8px;';
      sb.href = '/api/trajectories/image'; sb.download = 'trajectory_plot.png';
      status.parentElement.appendChild(sb);
    }
    var img = document.getElementById('tjImg');
    img.src = '/api/trajectories/image?t=' + Date.now();
    document.getElementById('tjLink').style.display = 'block';
    var dlBtn = document.getElementById('tjDownload');
    if (dlBtn) dlBtn.style.display = 'block';
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}


// Populate gate dropdowns whenever the panel is used
window.tjSelectedClasses = {};

async function tjBuildExtras() {
  // Class pills auto-populated from the loaded .traf (replaces the text box)
  var input = document.getElementById('tjClasses');
  if (input && !document.getElementById('tjClassPills')) {
    try {
      var oc = await fetchJSON('/api/batch/our_classes');
      var classes = oc.classes || [];
      if (classes.length) {
        input.style.display = 'none';
        var box = document.createElement('div');
        box.id = 'tjClassPills';
        box.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px;';
        classes.forEach(function(c) {
          var b = document.createElement('button');
          b.className = 'filter-btn';
          b.textContent = c;
          b.onclick = function() {
            window.tjSelectedClasses[c] = !window.tjSelectedClasses[c];
            b.classList.toggle('active', !!window.tjSelectedClasses[c]);
          };
          box.appendChild(b);
        });
        var hint = document.createElement('div');
        hint.style.cssText = 'font-size:10px;color:var(--text-muted);width:100%;';
        hint.textContent = 'None selected = all classes';
        box.appendChild(hint);
        input.parentElement.insertBefore(box, input);
      }
    } catch (e) {}
  }
  // AM / PM session shortcuts beside the time fields
  var ft = document.getElementById('tjFromTime');
  if (ft && !document.getElementById('tjAmBtn')) {
    var row = ft.parentElement;
    var wrap = document.createElement('div');
    wrap.style.cssText = 'display:flex;gap:6px;margin-bottom:6px;';
    [['tjAmBtn', 'AM session', '07:00', '10:00'],
     ['tjPmBtn', 'PM session', '15:30', '18:30'],
     ['tjClrBtn', 'Full day', '', '']].forEach(function(cfg) {
      var b = document.createElement('button');
      b.id = cfg[0]; b.className = 'btn btn-sm'; b.textContent = cfg[1];
      b.style.flex = '1';
      b.onclick = function() {
        document.getElementById('tjFromTime').value = cfg[2];
        document.getElementById('tjToTime').value = cfg[3];
      };
      wrap.appendChild(b);
    });
    row.parentElement.insertBefore(wrap, row.nextSibling);
  }
}

async function tjLoadGates() {
  tjBuildExtras();
  try {
    var gs = window.gates && window.gates.length ? window.gates : await fetchJSON('/api/gates');
    ['tjFromGate', 'tjToGate'].forEach(function(id) {
      var sel = document.getElementById(id);
      if (!sel) return;
      var current = sel.value;
      sel.innerHTML = '<option value="">' + (id === 'tjFromGate' ? 'From gate (all)' : 'To gate (all)') + '</option>' +
        gs.map(function(g) { return '<option value="' + g.gate_id + '">' + g.name + '</option>'; }).join('');
      sel.value = current;
    });
  } catch (e) {}
}
document.addEventListener('DOMContentLoaded', function() {
  setTimeout(tjLoadGates, 1500);
  var btn = document.getElementById('tjBtn');
  if (btn) btn.addEventListener('mouseenter', tjLoadGates);
});


// ── Build richer controls at load: class checkboxes, sessions, save toggle ──
document.addEventListener('DOMContentLoaded', function() {
  setTimeout(async function() {
    var clsInput = document.getElementById('tjClasses');
    if (clsInput && !document.getElementById('tjClassBox')) {
      try {
        var oc = await fetchJSON('/api/batch/our_classes');
        var classes = oc.classes || [];
        if (classes.length) {
          var box = document.createElement('div');
          box.id = 'tjClassBox';
          box.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px 12px;margin-bottom:6px;';
          box.innerHTML = '<span style="width:100%;font-size:11px;color:var(--text-muted);">Classes (none checked = all)</span>' +
            classes.map(function(c) {
              return '<label style="font-size:12px;display:flex;gap:4px;align-items:center;cursor:pointer;">' +
                '<input type="checkbox" class="tjClassChk" value="' + c + '"> ' + c + '</label>';
            }).join('');
          clsInput.parentElement.insertBefore(box, clsInput.parentElement.firstChild);
          clsInput.style.display = 'none';
          var mx = document.getElementById('tjPerClass');
          if (mx) mx.title = 'Declutter: randomly keep at most N tracks per class (e.g. 100 of 348 Cars). Blank = draw all.';
        }
      } catch (e) {}
    }
    var ft = document.getElementById('tjFromTime');
    if (ft && !document.getElementById('tjSession')) {
      var sel = document.createElement('select');
      sel.id = 'tjSession';
      sel.className = 'text-input';
      sel.style.cssText = 'width:100%;margin-bottom:6px;';
      sel.innerHTML = '<option value="">Session: full recording</option>' +
        '<option value="am">AM (start → 12:00)</option>' +
        '<option value="pm">PM (12:00 → end)</option>' +
        '<option value="custom">Custom times below</option>';
      sel.onchange = function() {
        var tt = document.getElementById('tjToTime');
        if (sel.value === 'am') { ft.value = ''; tt.value = '12:00'; }
        else if (sel.value === 'pm') { ft.value = '12:00'; tt.value = ''; }
        else if (sel.value === '') { ft.value = ''; tt.value = ''; }
      };
      ft.parentElement.parentElement.insertBefore(sel, ft.parentElement);
    }
    // Remove legacy class pills / session buttons (theirs may be built by
    // script, so sweep several times to win any timing race)
    function tjSweepLegacy() {
      var panel = document.getElementById('panel-trajectory');
      if (!panel) return;
      var kill = ['am session', 'pm session', 'full day', 'none selected = all classes'];
      panel.querySelectorAll('button, span, div, label').forEach(function(el) {
        var t = (el.textContent || '').trim().toLowerCase();
        if (el.children.length <= 1 && kill.indexOf(t) !== -1 &&
            el.id !== 'tjSession') el.remove();
      });
      panel.querySelectorAll('button, .filter-btn').forEach(function(el) {
        var t = (el.textContent || '').trim();
        if (/^(Biker|Bus|Car|LGV|OGV1|OGV2|Taxi|Cyclist|Ped|Auto|Truck|Bike)$/.test(t) &&
            !el.closest('#tjClassBox') && el.tagName !== 'OPTION') el.remove();
      });
    }
    [900, 1800, 3000, 5000].forEach(function(ms) { setTimeout(tjSweepLegacy, ms); });

    // Rebind the Generate button directly — markup onclick may still name
    // the old handler (which now resolves to canvas's drawing function)
    var genBtn = document.getElementById('tjBtn');
    if (genBtn) {
      genBtn.removeAttribute('onclick');
      genBtn.onclick = generateTrajectoryPlot;
    }
    var btn = document.getElementById('tjBtn');
    if (btn && !document.getElementById('tjSave')) {
      var lab = document.createElement('label');
      lab.style.cssText = 'font-size:12px;display:flex;gap:6px;align-items:center;cursor:pointer;margin-bottom:6px;';
      lab.innerHTML = '<input type="checkbox" id="tjSave" checked> Save PNG to disk (uncheck = preview only)';
      btn.parentElement.insertBefore(lab, btn);
      var dl = document.createElement('button');
      dl.id = 'tjDownload';
      dl.className = 'btn';
      dl.style.cssText = 'width:100%;margin-top:6px;display:none;';
      dl.textContent = '⬇ Download PNG';
      dl.onclick = downloadTrajectoryPlot;
      btn.parentElement.insertBefore(dl, btn.nextSibling);
    }
  }, 800);
});


async function downloadTrajectoryPlot() {
  var b = document.getElementById('tjDownload');
  try {
    if (b) { b.disabled = true; b.textContent = 'Preparing…'; }
    var resp = await fetch('/api/trajectories/image?t=' + Date.now());
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    var blob = await resp.blob();
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'trajectory_plot_' + new Date().toISOString().slice(0,16).replace(/[:T]/g,'-') + '.png';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function() { URL.revokeObjectURL(url); }, 4000);
  } catch (e) {
    alert('Download failed: ' + e.message);
  } finally {
    if (b) { b.disabled = false; b.textContent = '⬇ Download PNG'; }
  }
}


// ── Failsafe: capture-phase delegation. Walks up from the click target and
// matches ANY element (button, div, a) labelled 'Generate Trajectory Plot',
// tolerant of nbsp and nested spans. Logs every hit.
document.addEventListener('click', function(ev) {
  var el = ev.target;
  var hops = 0;
  while (el && el !== document.body && hops < 6) {
    var t = (el.textContent || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim().toLowerCase();
    if (t === 'generate trajectory plot') {
      ev.preventDefault();
      ev.stopPropagation();
      console.log('generate via delegation v18 — element:', el.tagName, el.id || '(no id)');
      generateTrajectoryPlot();
      return;
    }
    if (t.length > 60) break;   // climbed past the control into a container
    el = el.parentElement;
    hops++;
  }
}, true);
