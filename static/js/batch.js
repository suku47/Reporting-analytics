// ── Batch Report panel ──
// Edits a site YAML and runs batch_report.run_from_config server-side.
// The YAML is the single source of truth shared with the CLI.

window.batchTemplateHeaders = [];
window.batchOurClasses = [];
window.batchClassMap = {};   // ourClass -> client header
window.batchPollTimer = null;

function _bv(id) { return document.getElementById(id).value.trim().replace(/^["']+|["']+$/g, '').trim(); }
function _bset(id, v) { document.getElementById(id).value = v == null ? '' : v; }

function batchGatherConfig() {
  var cfg = {
    site: _bv('bSite') || undefined,
    gates_from: window.batchGatesFrom || undefined,
    traf_dir: _bv('bTrafDir') || undefined,
    out: _bv('bOut') || undefined,
    bin: parseInt(_bv('bBin') || '15', 10),
    schedule: _bv('bSchedule') || undefined
  };
  var periods = _bv('bPeriods');
  if (periods) cfg.periods = periods.split(',').map(function(s){ return s.trim(); }).filter(Boolean);
  var tpl = _bv('bTemplate');
  if (tpl) {
    cfg.client_template = tpl;
    var sn = _bv('bSiteNum');
    if (sn) cfg.site_number = sn;
    var ign = document.getElementById('bIgnoreDates');
    if (ign && ign.checked) cfg.ignore_template_dates = true;
    var td = document.getElementById('bTplDate');
    if (td && td.value.trim()) cfg.template_date = td.value.trim();
    var tp = document.getElementById('bTrajPlots');
    if (tp && tp.checked) cfg.trajectory_plots = true;
    var bg = document.getElementById('bBgVideo');
    if (bg && bg.value.trim()) cfg.background_video = bg.value.trim().replace(/^["']+|["']+$/g, '');
    if (Object.keys(window.batchClassMap).length) cfg.classes = window.batchClassMap;
  }
  // strip undefined
  Object.keys(cfg).forEach(function(k){ if (cfg[k] === undefined) delete cfg[k]; });
  return cfg;
}

function batchApplyConfig(cfg) {
  _bset('bSite', cfg.site);
  _bset('bTrafDir', cfg.traf_dir);
  _bset('bOut', cfg.out);
  _bset('bSchedule', cfg.schedule);
  _bset('bBin', cfg.bin || 15);
  _bset('bPeriods', (cfg.periods || []).join(', '));
  _bset('bTemplate', cfg.client_template);
  _bset('bSiteNum', cfg.site_number);
  var ignEl = document.getElementById('bIgnoreDates');
  if (ignEl) ignEl.checked = !!cfg.ignore_template_dates;
  var tdEl = document.getElementById('bTplDate');
  if (tdEl) tdEl.value = cfg.template_date || '';
  var tpEl = document.getElementById('bTrajPlots');
  if (tpEl) tpEl.checked = !!cfg.trajectory_plots;
  var bgEl = document.getElementById('bBgVideo');
  if (bgEl) bgEl.value = cfg.background_video || '';
  window.batchGatesFrom = cfg.gates_from || window.batchGatesFrom;
  window.batchClassMap = cfg.classes || {};
  renderClassMap();
}

async function batchLoadConfig() {
  var path = _bv('bCfgPath');
  if (!path) { alert('Enter the YAML path first'); return; }
  var r = await fetchJSON('/api/batch/config?path=' + encodeURIComponent(path));
  if (r.error) { alert(r.error); return; }
  if (!r.exists) { alert('No file yet — fill the fields and Save to create it.'); return; }
  batchApplyConfig(r.config || {});
  if (_bv('bTemplate')) batchReadTemplate();
}

async function batchSaveConfig() {
  var path = _bv('bCfgPath');
  if (!path) { alert('Enter the YAML path first'); return; }
  var r = await fetch('/api/batch/config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: path, config: batchGatherConfig() })
  });
  var j = await r.json();
  if (j.error) alert(j.error); else alert('Saved: ' + j.saved);
}

async function batchReadTemplate() {
  var tpl = _bv('bTemplate');
  if (!tpl) return;
  var info = await fetchJSON('/api/batch/template_info?path=' + encodeURIComponent(tpl));
  if (info.error) { alert('Template read failed: ' + info.error); return; }
  window.batchTemplateHeaders = info.class_headers || [];
  var oc = await fetchJSON('/api/batch/our_classes');
  window.batchOurClasses = oc.classes || [];
  // Fetch the auto-match the run will use; shown as a summary, not dropdowns.
  window.batchSuggested = {};
  window.batchUnmatched = [];
  try {
    var sug = await fetchJSON('/api/batch/suggest_classes?template=' + encodeURIComponent(tpl));
    window.batchSuggested = sug.suggested || {};
    window.batchUnmatched = sug.unmatched || [];
  } catch (e) {}
  renderClassMap();
}

window.batchEditClasses = false;

function renderClassMap() {
  var el = document.getElementById('bClassMap');
  var sug = window.batchSuggested || {};
  var explicit = window.batchClassMap || {};
  var haveTemplate = window.batchTemplateHeaders.length && window.batchOurClasses.length;

  if (!haveTemplate) {
    el.innerHTML = Object.keys(explicit).length
      ? '<div style="font-size:11px;color:var(--text-muted);">Class overrides loaded from YAML (' +
        Object.keys(explicit).length + '). Click Read next to the template to review.</div>'
      : '';
    return;
  }

  if (!window.batchEditClasses) {
    // Summary view: what the run will actually use
    var parts = window.batchOurClasses.map(function(c) {
      var v = explicit[c] || sug[c];
      return v ? (c + ' → ' + v + (explicit[c] ? ' *' : '')) : null;
    }).filter(Boolean);
    var un = (window.batchUnmatched || []).filter(function(c) { return !explicit[c]; });
    var html = '<label class="field-label">Class mapping (automatic)</label>' +
      '<div style="font-size:11px;line-height:1.7;">' +
      (parts.length ? parts.join('<br>') : 'No classes detected yet') + '</div>';
    if (un.length) {
      html += '<div style="font-size:11px;color:var(--yellow,#d29922);margin-top:4px;">' +
        '⚠ No column found for: ' + un.join(', ') + ' — Customize to map them.</div>';
    }
    if (Object.keys(explicit).length) {
      html += '<div style="font-size:10px;color:var(--text-muted);">* manual override</div>';
    }
    html += '<a href="#" style="font-size:11px;" onclick="window.batchEditClasses=true;renderClassMap();return false;">Customize mapping…</a>';
    el.innerHTML = html;
    return;
  }

  // Edit view: dropdowns pre-selected with override || suggestion
  var html = '<label class="field-label">Class mapping (our class → client column)</label>';
  html += window.batchOurClasses.map(function(c) {
    var current = explicit[c] || sug[c] || '';
    var opts = '<option value="">— not exported —</option>' +
      window.batchTemplateHeaders.map(function(h) {
        var sel = (h.toLowerCase() === String(current).toLowerCase()) ? ' selected' : '';
        return '<option value="' + h + '"' + sel + '>' + h + '</option>';
      }).join('');
    return '<div style="display:flex;gap:6px;align-items:center;margin-bottom:3px;">' +
      '<span style="width:80px;font-size:12px;">' + c + '</span>' +
      '<select class="text-input" style="flex:1;font-size:11px;" ' +
      'onchange="batchSetClass(\'' + c + '\', this.value)">' + opts + '</select></div>';
  }).join('');
  html += '<a href="#" style="font-size:11px;" onclick="window.batchEditClasses=false;renderClassMap();return false;">Done — back to automatic view</a>';
  el.innerHTML = html;
}

function batchSetClass(ourClass, header) {
  if (header) window.batchClassMap[ourClass] = header;
  else delete window.batchClassMap[ourClass];
}

async function batchRun() {
  try {
    return await _batchRunInner();
  } catch (err) {
    alert('Run failed to start: ' + err.message);
    var btn = document.getElementById('bRunBtn');
    if (btn) btn.disabled = false;
  }
}

async function _batchRunInner() {
  // default gates_from to the currently loaded traf
  if (!window.batchGatesFrom) {
    try {
      var cur = await fetchJSON('/api/batch/current_traf');
      window.batchGatesFrom = cur.path;
    } catch (e) {}
  }
  var cfg = batchGatherConfig();
  if (!cfg.gates_from) { alert('No reference .traf — load a file with gates first.'); return; }
  if (!cfg.traf_dir) { alert('Enter the traf folder.'); return; }

  var r = await fetch('/api/batch/run', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config: cfg })
  });
  var j = await r.json();
  if (j.error) { alert(j.error); return; }

  document.getElementById('bRunBtn').disabled = true;
  document.getElementById('bLog').style.display = 'block';
  document.getElementById('bOutputs').innerHTML = '';
  if (window.batchPollTimer) clearInterval(window.batchPollTimer);
  window.batchPollTimer = setInterval(batchPoll, 1000);
}

async function batchPoll() {
  var s = await fetchJSON('/api/batch/status');
  var logEl = document.getElementById('bLog');
  logEl.textContent = (s.log || []).join('\n');
  logEl.scrollTop = logEl.scrollHeight;
  if (s.done) {
    clearInterval(window.batchPollTimer);
    window.batchPollTimer = null;
    document.getElementById('bRunBtn').disabled = false;
    var out = document.getElementById('bOutputs');
    if (s.error) {
      out.innerHTML = '<div style="color:var(--red);font-size:12px;">Failed: ' + s.error + '</div>';
      return;
    }
    var warnBadge = (s.warnings && s.warnings.length)
      ? '<div style="color:var(--yellow,#d29922);font-size:12px;margin-bottom:4px;">⚠ ' +
        s.warnings.length + ' QA warning(s) — see QA sheet</div>'
      : '<div style="color:var(--green,#3fb950);font-size:12px;margin-bottom:4px;">✓ QA pass</div>';
    out.innerHTML = warnBadge + (s.outputs || []).map(function(p) {
      var name = p.split(/[\\/]/).pop();
      return '<a class="btn btn-sm" style="display:block;margin-bottom:4px;" ' +
        'href="/api/batch/download?path=' + encodeURIComponent(p) + '">⬇ ' + name + '</a>';
    }).join('');
  }
}
