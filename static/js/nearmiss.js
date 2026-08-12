// ── Near Miss tab: DOM-built UI (conflict map + severity list) ──
console.log('nearmiss.js v7 loaded');

async function nmDetect(focus) {
  var btn = document.getElementById('nmBtn');
  var status = document.getElementById('nmStatus');
  if (btn) btn.disabled = true;
  if (status) status.textContent = 'Analyzing conflicts… (pairs scale with traffic, allow up to a minute)';
  try {
    var payload = {
      mode: document.getElementById('nmMode').value,
      pet_threshold: parseFloat(document.getElementById('nmPet').value || '1')
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
    var c = j.counts || {};
    status.innerHTML = j.total + ' conflicts — ' +
      '<span style="color:#FF5D73;">' + (c.critical || 0) + ' critical</span>, ' +
      '<span style="color:#FF8C3C;">' + (c.severe || 0) + ' severe</span>, ' +
      '<span style="color:#FFC53D;">' + (c.moderate || 0) + ' moderate</span>, ' +
      '<span style="color:#9AA7BD;">' + (c.slight || 0) + ' slight</span>' +
      (j.debug_log ? '<br><span style="color:var(--text-muted);font-size:11px;">debug: ' +
        j.debug_log + '</span>' : '');
    window._nmFps = j.fps || 30;
    window._nmHasVideo = !!j.has_video;
    var eb = document.getElementById('nmExportBtn');
    if (eb) eb.style.display =
      (window._nmHasVideo && (j.events || []).length) ? 'block' : 'none';
    var list = document.getElementById('nmList');
    var sevColor = { critical: '#FF5D73', severe: '#FF8C3C', moderate: '#FFC53D', slight: '#9AA7BD' };
    list.innerHTML = (j.events || []).map(function(ev) {
      var metric = ev.pet !== null ? 'PET ' + ev.pet + 's' : 'TTC ' + ev.ttc + 's';
      return '<div class="nm-row" data-a="' + ev.a_id + '" data-b="' + ev.b_id + '" data-frame="' + ev.frame + '" ' +
        'style="cursor:pointer;padding:7px 10px;border-left:3px solid ' + sevColor[ev.severity] + ';' +
        'background:var(--bg-inset);border-radius:6px;margin-bottom:5px;font-size:12px;' +
        'display:flex;align-items:center;gap:8px;">' +
        '<span style="flex:1;min-width:0;">' +
        '<b style="color:' + sevColor[ev.severity] + ';">' + ev.severity.toUpperCase() + '</b> ' +
        metric + ' — ' + ev.a_cls + ' #' + ev.a_id + ' × ' + ev.b_cls + ' #' + ev.b_id +
        ' @ frame ' + ev.frame + (ev.type === 'TTC' ? ' <i>(converging)</i>' : '') + '</span>' +
        '<span class="nm-map" title="Highlight this pair on the conflict map" ' +
        'style="flex:none;font-size:11px;color:var(--text-muted);border:1px solid var(--border,#30363d);' +
        'border-radius:4px;padding:1px 6px;">map</span>' +
        '</div>';
    }).join('') || '<div style="color:var(--text-muted);font-size:12px;">No conflicts under the current thresholds.</div>';
    list.querySelectorAll('.nm-row').forEach(function(row) {
      row.onclick = function() {
        if (window._nmHasVideo) {
          nmPlayClip(parseInt(row.dataset.frame, 10),
                     row.dataset.a, row.dataset.b);
        } else {
          nmDetect([parseInt(row.dataset.a, 10), parseInt(row.dataset.b, 10)]);
        }
      };
      var m = row.querySelector('.nm-map');
      if (m) m.onclick = function(e) {
        e.stopPropagation();
        nmDetect([parseInt(row.dataset.a, 10), parseInt(row.dataset.b, 10)]);
      };
    });
  } catch (e2) {
    if (status) status.textContent = 'Error: ' + e2.message;
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── Clip playback: annotated conflict clip (trails + boxes for the pair) ──
var NM_CLIP_PAD = 4;   // seconds of context before/after (matches server)

async function nmPlayClip(frame, aId, bId) {
  var img = document.getElementById('nmImg');
  var holder = document.getElementById('nmVidHolder');
  var vid = document.getElementById('nmVid');
  var status = document.getElementById('nmStatus');
  if (!vid) return;

  if (status) status.textContent =
    'Rendering clip for #' + aId + ' × #' + bId + '… (first time only, then cached on disk)';
  try {
    var r = await fetch('/api/nearmiss/clip', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ a_id: parseInt(aId, 10), b_id: parseInt(bId, 10),
                             frame: frame })
    });
    var j = await r.json();
    if (j.error) {
      if (status) status.textContent = 'Clip failed: ' + j.error + ' — playing raw video instead.';
      return nmPlayRaw(frame);
    }
    if (j.codec !== 'h264') {
      if (status) status.textContent =
        'Clip saved to disk (' + j.name + ') but not browser-playable — ' +
        'install imageio-ffmpeg for in-browser clips. Playing raw video instead.';
      return nmPlayRaw(frame);
    }
    img.style.display = 'none';
    holder.style.display = 'block';
    vid.ontimeupdate = null;
    vid.src = '/api/nearmiss/clip_file?name=' + encodeURIComponent(j.name) +
              '&t=' + Date.now();
    vid.onerror = function() {
      if (status) status.textContent = 'Clip playback failed — playing raw video instead.';
      nmPlayRaw(frame);
    };
    vid.play();
    if (status) status.innerHTML =
      'Clip: #' + aId + ' × #' + bId + ' @ frame ' + frame +
      (j.cached ? ' (cached)' : '') + ' — saved in nearmiss_clips/';
  } catch (e) {
    if (status) status.textContent = 'Clip error: ' + e.message + ' — playing raw video instead.';
    nmPlayRaw(frame);
  }
}

// Fallback: seek the original video via the range-streaming endpoint
function nmPlayRaw(frame) {
  var img = document.getElementById('nmImg');
  var holder = document.getElementById('nmVidHolder');
  var vid = document.getElementById('nmVid');
  if (!vid) return;
  var fps = window._nmFps || 30;
  var t0 = Math.max(0, frame / fps - NM_CLIP_PAD);
  var t1 = frame / fps + NM_CLIP_PAD;
  img.style.display = 'none';
  holder.style.display = 'block';
  var seekAndPlay = function() { vid.currentTime = t0; vid.play(); };
  vid.onerror = null;
  if (!vid.src || vid.src.indexOf('/api/video/stream') === -1) {
    vid.src = '/api/video/stream';
    vid.onloadedmetadata = seekAndPlay;
  } else {
    seekAndPlay();
  }
  vid.ontimeupdate = function() {
    if (vid.currentTime >= t1 && !vid.paused) vid.pause();
  };
}

function nmBackToMap() {
  var vid = document.getElementById('nmVid');
  var holder = document.getElementById('nmVidHolder');
  var img = document.getElementById('nmImg');
  if (vid && !vid.paused) vid.pause();
  if (holder) holder.style.display = 'none';
  if (img) img.style.display = 'block';
}

// ── Export all clips to disk (old near_miss_analyzer.py behavior) ──
async function nmExportAll() {
  var btn = document.getElementById('nmExportBtn');
  var status = document.getElementById('nmStatus');
  try {
    var r = await fetch('/api/nearmiss/export_all', { method: 'POST' });
    var j = await r.json();
    if (j.error) { if (status) status.textContent = j.error; return; }
    if (btn) btn.disabled = true;
    var poll = setInterval(async function() {
      try {
        var s = await (await fetch('/api/nearmiss/clip_status')).json();
        if (status) status.textContent =
          'Exporting clips: ' + s.done + ' / ' + s.total +
          (s.error ? ' — ERROR: ' + s.error : '') +
          '  →  ' + (s.out_dir || '');
        if (!s.running) {
          clearInterval(poll);
          if (btn) btn.disabled = false;
          if (status && !s.error) status.textContent =
            'All ' + s.total + ' clips saved to ' + s.out_dir;
        }
      } catch (e) { clearInterval(poll); if (btn) btn.disabled = false; }
    }, 1500);
  } catch (e) {
    if (status) status.textContent = 'Export error: ' + e.message;
  }
}

document.addEventListener('DOMContentLoaded', function() {
  setTimeout(function() {
    var panel = document.getElementById('panel-nearmiss');
    if (!panel || document.getElementById('nmBtn')) return;
    panel.style.maxWidth = 'none';   // kill any stale inline squeeze from cached HTML
    panel.innerHTML =
      '<div class="panel-split" style="grid-template-columns:minmax(0,1fr) 380px;align-items:start;">' +
      '  <div class="panel-main" style="display:flex;flex-direction:column;align-items:stretch;min-width:0;">' +
      '    <img id="nmImg" src="/api/frame_image/0?t=' + Date.now() + '" ' +
      '         style="display:block;width:100%;max-width:100%;height:auto;max-height:70vh;' +
      '         object-fit:contain;object-position:left top;border-radius:14px;' +
      '         box-shadow:0 12px 40px rgba(0,0,0,.45);">' +
      '    <div id="nmVidHolder" style="display:none;width:100%;">' +
      '      <video id="nmVid" controls preload="metadata" ' +
      '             style="display:block;width:100%;max-height:70vh;background:#000;' +
      '             border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.45);"></video>' +
      '      <button class="btn" style="margin-top:8px;" onclick="nmBackToMap()">&#9664; Back to conflict map</button>' +
      '    </div>' +
      '    <div id="nmStatus" style="font-size:12px;color:var(--text-secondary);margin-top:8px;">' +
      '        Choose a mode and click <b>Detect Conflicts</b> — the conflict map replaces the frame ' +
      '        (colored by severity; click a row to highlight that pair\'s paths).</div>' +
      '  </div>' +
      '  <aside class="panel-aside">' +
      '    <div class="section-header">Near Miss Analysis</div>' +
      '    <label class="field-label">Mode</label>' +
      '    <select id="nmMode" class="text-input" style="width:100%;margin-bottom:6px;">' +
      '      <option value="veh_ped">Vehicle × Pedestrian / Cyclist</option>' +
      '      <option value="veh_veh">Vehicle × Vehicle</option>' +
      '    </select>' +
      '    <div style="margin-bottom:8px;">' +
      '      <label class="field-label">PET \u2264 (s)</label>' +
      '      <input type="number" id="nmPet" class="text-input" style="width:100%;" value="1.0" step="0.5">' +
      '    </div>' +
      '    <button class="btn" id="nmBtn" style="width:100%;" onclick="nmDetect()">Detect Conflicts</button>' +
      '    <button class="btn" id="nmExportBtn" style="width:100%;margin-top:6px;display:none;" ' +
      '            onclick="nmExportAll()">Export All Clips to Disk</button>' +
      '    <div id="nmList" style="margin-top:12px;max-height:52vh;overflow-y:auto;"></div>' +
      '  </aside>' +
      '</div>';
  }, 600);
});
