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

// Chunked rendering: ALL tracks are shown, appended in chunks as the list
// scrolls, so files with thousands of tracks don't freeze the sidebar.
const TRACK_CHUNK = 500;
let _tlFiltered = [];   // current filtered set being rendered
let _tlRendered = 0;    // how many rows are in the DOM so far

function _trackRowHTML(t) {
  return `
    <div class="track-item ${selectedTrack === t.track_id ? 'selected' : ''}"
         onclick="selectTrack(${t.track_id})" title="${trackTimeTooltip(t)}">
      <span class="track-id">#${t.track_id}</span>
      <span class="track-class ${t.class_name}">${t.class_name}</span>
      <span>${t.is_stationary ? '<span class=track-stat-badge>STAT</span>' :
        (t.entry_edge || '?') + '→' + (t.exit_edge || '?')}</span>
      <span class="track-time">${fmtVideoTime(t.first_frame)}</span>
      <span class="track-speed">${t.speed_mean_px.toFixed(1)}</span>
      <button class="track-del" title="Delete track #${t.track_id}"
              onclick="deleteTrackFromList(event, ${t.track_id})">✕</button>
    </div>`;
}

function _appendTrackChunk(el) {
  if (_tlRendered >= _tlFiltered.length) return;
  const next = _tlFiltered.slice(_tlRendered, _tlRendered + TRACK_CHUNK);
  el.insertAdjacentHTML('beforeend', next.map(_trackRowHTML).join(''));
  _tlRendered += next.length;
  const more = _tlFiltered.length - _tlRendered;
  let foot = document.getElementById('trackListFoot');
  if (!foot) {
    foot = document.createElement('div');
    foot.id = 'trackListFoot';
    foot.className = 'loading';
    el.appendChild(foot);
  }
  el.appendChild(foot);  // keep footer at the bottom after appends
  foot.textContent = more > 0
    ? `Showing ${_tlRendered} of ${_tlFiltered.length} — scroll for more`
    : `${_tlFiltered.length} tracks`;
}

// When a track is selected (e.g. clicked on the canvas), make sure its row
// is actually rendered (appending chunks as needed for high track numbers)
// and scroll the sidebar list to it, so it can be deleted without manually
// scrolling thousands of rows.
function _ensureSelectedVisible(el) {
  const idx = _tlFiltered.findIndex(t => t.track_id === selectedTrack);
  if (idx < 0) return;                      // filtered out — nothing to show
  while (_tlRendered <= idx) {
    const before = _tlRendered;
    _appendTrackChunk(el);
    if (_tlRendered === before) break;      // safety: no progress
  }
  const row = el.querySelectorAll('.track-item')[idx];
  if (row) row.scrollIntoView({ block: 'center' });
}

function renderTrackList(tracks) {
  const el = document.getElementById('trackList');
  _tlFiltered = tracks;
  _tlRendered = 0;
  if (!tracks.length) {
    el.innerHTML = '<div class="loading">No tracks match filters</div>';
    return;
  }
  el.innerHTML = '';
  _appendTrackChunk(el);
  if (selectedTrack !== null) _ensureSelectedVisible(el);
  if (!el._chunkBound) {
    el.addEventListener('scroll', () => {
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 300) {
        _appendTrackChunk(el);
      }
    });
    el._chunkBound = true;
  }
}

// ── Track timing helpers ──
// Video-elapsed time (what you seek to in a media player): frame / fps
function fmtVideoTime(frame) {
  if (frame == null || !fps) return '--:--';
  const s = Math.floor(frame / fps);
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  const p = n => String(n).padStart(2, '0');
  return hh > 0 ? `${hh}:${p(mm)}:${p(ss)}` : `${p(mm)}:${p(ss)}`;
}

// Wall-clock time from scene.video_start_time (recording timestamp), if present
function fmtWallClock(frame) {
  if (frame == null || !fps || !scene.video_start_time) return null;
  const t = new Date(new Date(scene.video_start_time).getTime()
                     + (frame / fps) * 1000);
  if (isNaN(t.getTime())) return null;
  const p = n => String(n).padStart(2, '0');
  return `${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`;
}

function trackTimeTooltip(t) {
  const parts = [
    `Video ${fmtVideoTime(t.first_frame)} \u2192 ${fmtVideoTime(t.last_frame)}`,
    `Frames ${t.first_frame}\u2013${t.last_frame}`,
  ];
  const w1 = fmtWallClock(t.first_frame), w2 = fmtWallClock(t.last_frame);
  if (w1 && w2) parts.push(`Clock ${w1} \u2192 ${w2}`);
  return parts.join('  |  ');
}

// ── Per-row delete (reuses the same backend cascade as the
//    Trajectory Analysis tab's "Delete Selected Track" button) ──
async function deleteTrackFromList(ev, id) {
  ev.stopPropagation();  // don't trigger selectTrack on the row
  if (!confirm('Permanently delete track #' + id + '?\nThis cannot be undone.')) return;

  const res = await fetch('/api/filters/delete_tracks', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({track_ids: [id]})
  });
  if (!res.ok) {
    alert('Delete failed (' + res.status + ')');
    return;
  }

  if (selectedTrack === id) selectedTrack = null;

  // Refresh everything that derives from tracks (mirrors applyFilterDelete)
  allTracks = await fetchJSON('/api/tracks');
  allTrajectories = await fetchJSON('/api/trajectories?stationary=0');
  try { summary = await fetchJSON('/api/summary'); } catch (e) {}
  try { window.gates = await fetchJSON('/api/gates/count_summary'); } catch (e) {}

  renderDashboard();
  applyFilters();       // re-render list with current class/motion filters
  if (typeof renderGateList === 'function') renderGateList();
  render();
}

function selectTrack(id) {
  selectedTrack = (selectedTrack === id) ? null : id;

  // Jump the scene view to the moment this track appears, so its trajectory
  // can be verified against the actual frame (and the video at that time).
  if (selectedTrack !== null) {
    const t = allTracks.find(x => x.track_id === id);
    if (t && t.first_frame != null && typeof loadFrame === 'function') {
      currentFrame = t.first_frame;
      const slider = document.getElementById('frameSlider');
      if (slider) slider.value = t.first_frame;
      loadFrame(t.first_frame);
    }
  }

  applyFilters();
  render();
  // Trigger jitter analysis if the function exists (from filters.js)
  if (typeof analyzeSelectedTrackJitter === 'function') {
    analyzeSelectedTrackJitter();
  }
}
