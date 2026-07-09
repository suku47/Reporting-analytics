// ── Track Filtering & Selection ──
let classFilter = null, motionFilter = 'all';

function filterClass(cls) {
  classFilter = cls;
  document.querySelectorAll('#classFilters .filter-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  applyFilters();
}

function filterMotion(mode) {
  motionFilter = mode;
  const btns = document.querySelectorAll('#panel-tracks .filter-row:nth-child(2) .filter-btn');
  btns.forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  applyFilters();
}

function applyFilters() {
  let filtered = allTracks;
  if (classFilter) filtered = filtered.filter(t => t.class_name === classFilter);
  if (motionFilter === 'moving') filtered = filtered.filter(t => !t.is_stationary);
  if (motionFilter === 'stationary') filtered = filtered.filter(t => t.is_stationary);
  renderTrackList(filtered);
}

function renderTrackList(tracks) {
  const el = document.getElementById('trackList');
  if (!tracks.length) {
    el.innerHTML = '<div class="loading">No tracks match filters</div>';
    return;
  }
  el.innerHTML = tracks.slice(0, 2000).map(t => `
    <div class="track-item ${selectedTrack === t.track_id ? 'selected' : ''}"
         onclick="selectTrack(${t.track_id})">
      <span class="track-id">#${t.track_id}</span>
      <span class="track-class ${t.class_name}">${t.class_name}</span>
      <span>${t.is_stationary ? '<span class=track-stat-badge>STAT</span>' :
        (t.entry_edge || '?') + '→' + (t.exit_edge || '?')}</span>
      <span class="track-speed">${t.speed_mean_px.toFixed(1)}</span>
    </div>
  `).join('');
}

function selectTrack(id) {
  selectedTrack = (selectedTrack === id) ? null : id;
  applyFilters();
  render();
  // Trigger jitter analysis if the function exists (from filters.js)
  if (typeof analyzeSelectedTrackJitter === 'function') {
    analyzeSelectedTrackJitter();
  }
}
