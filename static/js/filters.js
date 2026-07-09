// ── Track Filtering & Smoothing ──
var pendingDeleteIds = [];
var hiddenTrackIds = new Set();  // tracks visually hidden by filter preview

async function smoothTrajectories(window) {
  if (!confirm('Smooth all trajectories with window=' + window + '?\nThis modifies the .traf file.')) return;
  
  var res = await fetch('/api/filters/smooth', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({window: window})
  });
  var data = await res.json();
  alert('Smoothed ' + data.smoothed + ' trajectories (window=' + data.window + ')');
  
  // Reload trajectories
  allTrajectories = await fetchJSON('/api/trajectories?stationary=0');
  render();
}

async function previewFilter() {
  var params = {
    min_displacement: parseFloat(document.getElementById('fMinDisp').value) || 30,
    min_frames: parseInt(document.getElementById('fMinFrames').value) || 10,
    max_sinuosity: parseFloat(document.getElementById('fMaxSin').value) || 5.0,
    max_jitter: parseFloat(document.getElementById('fMaxJitter').value) || 1.2,
    remove_edge_only: document.getElementById('fEdgeOnly').checked,
    require_gate_crossing: document.getElementById('fGateRequired').checked,
  };

  var res = await fetch('/api/filters/apply', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(params)
  });
  var data = await res.json();

  // Show preview
  var el = document.getElementById('filterPreview');
  var removedList = Object.entries(data.removed);

  if (removedList.length === 0) {
    el.innerHTML = '<div style="color:var(--green);padding:6px;">✓ All ' + data.total + ' tracks pass filters. Nothing to remove.</div>';
    document.getElementById('btnDeleteFiltered').style.display = 'none';
    hiddenTrackIds.clear();
    render();
    return;
  }

  var html = '<div style="color:var(--yellow);padding:4px 0;">Would remove ' + data.remove + ' of ' + data.total + ' tracks:</div>';
  html += '<div style="max-height:150px;overflow-y:auto;margin:4px 0;">';
  removedList.forEach(function(item) {
    var tid = item[0], reasons = item[1];
    html += '<div style="display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid var(--border);">';
    html += '<span style="color:var(--accent);">#' + tid + '</span>';
    html += '<span style="color:var(--text-muted);font-size:10px;">' + reasons.join(', ') + '</span>';
    html += '</div>';
  });
  html += '</div>';
  el.innerHTML = html;

  // Store IDs for deletion and visually hide them
  pendingDeleteIds = removedList.map(function(item) { return parseInt(item[0]); });
  hiddenTrackIds = new Set(pendingDeleteIds);

  document.getElementById('btnDeleteFiltered').style.display = 'block';
  document.getElementById('btnDeleteFiltered').textContent = '🗑 Delete ' + pendingDeleteIds.length + ' Tracks';

  // Re-render with hidden tracks
  render();
}

async function applyFilterDelete() {
  if (!pendingDeleteIds.length) return;
  if (!confirm('Permanently delete ' + pendingDeleteIds.length + ' tracks?\nThis cannot be undone.')) return;

  var res = await fetch('/api/filters/delete_tracks', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({track_ids: pendingDeleteIds})
  });
  var data = await res.json();

  alert('Deleted ' + data.deleted + ' tracks. ' + data.remaining + ' remaining.');

  // Reload everything
  pendingDeleteIds = [];
  hiddenTrackIds.clear();
  document.getElementById('btnDeleteFiltered').style.display = 'none';
  document.getElementById('filterPreview').innerHTML = '';

  allTracks = await fetchJSON('/api/tracks');
  allTrajectories = await fetchJSON('/api/trajectories?stationary=0');
  summary = await fetchJSON('/api/summary');

  // Recompute gate crossings
  try { window.gates = await fetchJSON('/api/gates/count_summary'); } catch(e) {}

  renderDashboard();
  renderTrackList(allTracks);
  renderGateList();
  render();
}

async function deleteSelectedTrack() {
  if (!selectedTrack) {
    alert('Select a track first in the TRACKS tab.');
    return;
  }
  if (!confirm('Delete track #' + selectedTrack + '?')) return;

  await fetch('/api/filters/delete_tracks', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({track_ids: [selectedTrack]})
  });

  selectedTrack = null;
  allTracks = await fetchJSON('/api/tracks');
  allTrajectories = await fetchJSON('/api/trajectories?stationary=0');
  summary = await fetchJSON('/api/summary');
  renderDashboard();
  renderTrackList(allTracks);
  render();
}


// ──────────────────────────────────────────────────────────
// TRAJECTORY TRIMMING
// ──────────────────────────────────────────────────────────

var pendingTrimData = null;
var selectedTrackTrimInfo = null;

async function previewTrimJitter() {
  var threshold = parseFloat(document.getElementById('fTrimThreshold').value) || 1.0;
  var minClean = parseInt(document.getElementById('fTrimMinClean').value) || 5;

  document.getElementById('trimPreview').innerHTML = '<div style="color:var(--text-muted);">Analyzing...</div>';

  var res = await fetch('/api/filters/preview_trim_jitter', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({jitter_threshold: threshold, min_clean_length: minClean})
  });
  var data = await res.json();
  pendingTrimData = data;

  var el = document.getElementById('trimPreview');
  if (data.would_trim === 0) {
    el.innerHTML = '<div style="color:var(--green);padding:6px;">✓ No jittery segments found. All trajectories are clean.</div>';
    document.getElementById('btnApplyTrim').style.display = 'none';
    return;
  }

  var html = '<div style="color:var(--yellow);padding:4px 0;">Would trim ' + data.would_trim + ' of ' + data.total_tracks + ' tracks</div>';
  html += '<div style="color:var(--text-muted);padding:2px 0;">Total points to remove: ' + data.total_points_removed + '</div>';
  html += '<div style="max-height:150px;overflow-y:auto;margin:4px 0;">';
  data.details.forEach(function(d) {
    html += '<div style="display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid var(--border);">';
    html += '<span style="color:var(--accent);">#' + d.track_id + '</span>';
    html += '<span style="color:var(--text-muted);font-size:10px;">' + d.total_points + '→' + d.keep_points + ' pts (−' + d.removing_points + ')</span>';
    html += '</div>';
  });
  html += '</div>';
  el.innerHTML = html;

  document.getElementById('btnApplyTrim').style.display = 'block';
  document.getElementById('btnApplyTrim').textContent = '✂ Trim ' + data.would_trim + ' Tracks';
}

async function applyTrimJitter() {
  if (!pendingTrimData || pendingTrimData.would_trim === 0) return;
  if (!confirm('Trim jittery ends from ' + pendingTrimData.would_trim + ' tracks?\nThis modifies the .traf file.')) return;

  var threshold = parseFloat(document.getElementById('fTrimThreshold').value) || 1.0;
  var minClean = parseInt(document.getElementById('fTrimMinClean').value) || 5;

  var res = await fetch('/api/filters/auto_trim_jitter', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({jitter_threshold: threshold, min_clean_length: minClean})
  });
  var data = await res.json();

  alert('Trimmed ' + data.tracks_trimmed + ' tracks. Removed ' + data.total_points_removed + ' jittery points.');

  // Reset and reload
  pendingTrimData = null;
  document.getElementById('btnApplyTrim').style.display = 'none';
  document.getElementById('trimPreview').innerHTML = '';

  allTracks = await fetchJSON('/api/tracks');
  allTrajectories = await fetchJSON('/api/trajectories?stationary=0');
  summary = await fetchJSON('/api/summary');

  try { window.gates = await fetchJSON('/api/gates/count_summary'); } catch(e) {}

  renderDashboard();
  renderTrackList(allTracks);
  renderGateList();
  render();
}

// Analyze selected track for jitter when it changes
async function analyzeSelectedTrackJitter() {
  var trimSection = document.getElementById('selectedTrackTrim');
  var infoEl = document.getElementById('trackTrimInfo');
  var btnTrim = document.getElementById('btnTrimSelected');

  if (!selectedTrack) {
    trimSection.style.display = 'none';
    selectedTrackTrimInfo = null;
    return;
  }

  trimSection.style.display = 'block';
  infoEl.innerHTML = '<span style="color:var(--text-muted);">Analyzing #' + selectedTrack + '...</span>';

  var threshold = parseFloat(document.getElementById('fTrimThreshold').value) || 1.0;

  try {
    var data = await fetchJSON('/api/filters/jitter_analysis/' + selectedTrack + '?threshold=' + threshold);
    selectedTrackTrimInfo = data;

    if (data.error) {
      infoEl.innerHTML = '<span style="color:var(--red);">' + data.error + '</span>';
      btnTrim.style.display = 'none';
      return;
    }

    // Show segment breakdown
    var html = '<div style="margin-bottom:4px;">Track #' + data.track_id + ' — ' + data.total_points + ' points</div>';
    var hasJitter = false;

    data.segments.forEach(function(seg) {
      var len = seg.end - seg.start + 1;
      var color = seg.type === 'clean' ? 'var(--green)' : 'var(--red)';
      var icon = seg.type === 'clean' ? '━' : '〰';
      html += '<div style="display:flex;gap:6px;padding:1px 0;">';
      html += '<span style="color:' + color + ';">' + icon + '</span>';
      html += '<span>' + seg.type + ': pts ' + seg.start + '–' + seg.end + ' (' + len + ')</span>';
      html += '</div>';
      if (seg.type === 'jittery') hasJitter = true;
    });

    infoEl.innerHTML = html;
    btnTrim.style.display = hasJitter ? 'block' : 'none';

  } catch(e) {
    infoEl.innerHTML = '<span style="color:var(--red);">Analysis failed</span>';
    btnTrim.style.display = 'none';
  }
}

async function trimSelectedTrack() {
  if (!selectedTrackTrimInfo || !selectedTrack) return;

  // Find longest clean segment
  var best = null, bestLen = 0;
  selectedTrackTrimInfo.segments.forEach(function(seg) {
    if (seg.type === 'clean') {
      var len = seg.end - seg.start + 1;
      if (len > bestLen) { bestLen = len; best = seg; }
    }
  });

  if (!best) {
    alert('No clean segment found — consider deleting this track instead.');
    return;
  }

  var removing = selectedTrackTrimInfo.total_points - bestLen;
  if (!confirm('Trim track #' + selectedTrack + '?\nKeeping points ' + best.start + '–' + best.end + ' (' + bestLen + ' pts)\nRemoving ' + removing + ' jittery points.')) return;

  var res = await fetch('/api/filters/trim_track', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({track_id: selectedTrack, keep_start: best.start, keep_end: best.end})
  });
  var data = await res.json();

  if (data.error) {
    alert('Error: ' + data.error);
    return;
  }

  alert('Trimmed: ' + data.old_points + ' → ' + data.new_points + ' points.');

  // Reload
  allTracks = await fetchJSON('/api/tracks');
  allTrajectories = await fetchJSON('/api/trajectories?stationary=0');
  render();
  analyzeSelectedTrackJitter();
}
