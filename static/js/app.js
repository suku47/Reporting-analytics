// ── Global State ──
let scene = {}, summary = {}, allTracks = [], allTrajectories = [];
let selectedTrack = null, overlayMode = 'tracks';
let fps = 30, frameW = 1280, frameH = 720, totalFrames = 0;
let hasVideo = false;
let CLASS_COLORS = {};  // populated from /api/class_profile
let CLASS_LABELS = {};  // populated from /api/class_profile
let ALL_CLASSES = [];    // populated from /api/class_profile

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
  return r.json();
}

async function init() {
  try {
    scene = await fetchJSON('/api/scene');
    summary = await fetchJSON('/api/summary');

    // Load class profile from .traf (auto-detects US/UK/custom)
    try {
      const profile = await fetchJSON('/api/class_profile');
      CLASS_COLORS = profile.class_colors || {};
      CLASS_COLORS['UNK'] = '#8b949e';
      ALL_CLASSES = profile.all_classes || [];
      CLASS_LABELS = profile.class_full_names || {};
    } catch (profileErr) {
      console.warn('Class profile load failed, using defaults:', profileErr);
      CLASS_COLORS = { PV:'#58a6ff', SU:'#3fb950', CU:'#d29922', UNK:'#8b949e', MC:'#bc8cff', BUS:'#db6d28', Bus:'#db6d28', PC:'#f778ba', Peds:'#f0883e' };
      ALL_CLASSES = ['PV', 'SU', 'CU', 'Bus', 'MC', 'PC', 'Peds'];
      CLASS_LABELS = { PV:'Passenger Vehicle', SU:'Single-Unit Truck', CU:'Combination-Unit', Bus:'Bus', MC:'Motorcycle', PC:'Pedal Cyclist', Peds:'Pedestrian' };
    }
  } catch (err) {
    document.getElementById('headerMeta').textContent = 'Error loading data: ' + err.message;
    return;
  }

  fps = summary.fps || 30;
  frameW = summary.frame_size[0];
  frameH = summary.frame_size[1];
  totalFrames = parseInt(scene.total_frames || 0);
  hasVideo = (scene.video_available === true || scene.video_available === 'true');

  document.getElementById('headerMeta').textContent =
    (scene.video_path || 'Unknown') + ' | ' + summary.total_tracks + ' vehicles | ' +
    frameW + '×' + frameH + ' @ ' + fps + 'fps';

  document.getElementById('frameSlider').max = totalFrames;

  initCanvas();

  // Always attempt the first frame: without a video the server serves the
  // clean background frame stored inside the .traf (self-contained mode).
  await loadFrame(0);
  render();

  allTracks = await fetchJSON('/api/tracks');
  allTrajectories = await fetchJSON('/api/trajectories?stationary=0');
  render();   // draw all trails immediately — helps staff place gates

  try {
    window.gates = await fetchJSON('/api/gates/count_summary');
  } catch (err) {
    console.warn('Gate summary load failed:', err);
    window.gates = [];
  }

  renderDashboard();
  renderTrackList(allTracks);
  renderGateList();
  render();
}

// ── Tab Switching ──
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
  document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('active'); });
  // Highlight by name (works for programmatic calls too, not just clicks)
  var tabEl = null;
  document.querySelectorAll('.tab').forEach(function(t) {
    if (t.getAttribute('onclick') && t.getAttribute('onclick').indexOf("'" + name + "'") !== -1) tabEl = t;
  });
  if (tabEl) tabEl.classList.add('active');
  else if (typeof event !== 'undefined' && event && event.target) event.target.classList.add('active');
  document.getElementById('panel-' + name).classList.add('active');
}

// ── Overlay Toggle ──
function setOverlay(mode) {
  overlayMode = mode;
  ['btnTracks','btnHeatmap'].forEach(function(id) {
    document.getElementById(id).classList.remove('active');
  });
  document.getElementById('btn' + mode.charAt(0).toUpperCase() + mode.slice(1)).classList.add('active');
  render();
}

// ─────────────────────────────────────────────────────────
// VIDEO PLAYBACK
// ─────────────────────────────────────────────────────────
let isPlaying = false, playTimer = null, currentFrame = 0;
let currentFrameImage = null;
let frameCache = {};
var CACHE_MAX = 50;
let pendingFrameLoad = null;  // track which frame is being loaded

function loadFrame(frameNum) {
  return new Promise(function(resolve) {
    // No hasVideo guard: without a video the server serves the clean
    // background frame stored inside the .traf (self-contained mode).
    frameNum = Math.max(0, Math.min(frameNum, totalFrames));

    // Check cache
    if (frameCache[frameNum]) {
      currentFrameImage = frameCache[frameNum];
      render();
      resolve();
      return;
    }

    // Cancel any pending load
    pendingFrameLoad = frameNum;

    var img = new Image();
    img.onload = function() {
      // Only apply if this is still the frame we want
      if (pendingFrameLoad === frameNum || pendingFrameLoad === null) {
        // Cache management
        var keys = Object.keys(frameCache);
        if (keys.length >= CACHE_MAX) {
          delete frameCache[keys[0]];
        }
        frameCache[frameNum] = img;
        currentFrameImage = img;
        pendingFrameLoad = null;
        render();
      }
      resolve();
    };
    img.onerror = function() {
      console.warn('Failed to load frame ' + frameNum);
      pendingFrameLoad = null;
      resolve();
    };
    img.src = '/api/frame_image/' + frameNum + '?t=' + Date.now();
  });
}

function togglePlay() {
  // Playback is disabled in this layout (no play button in the DOM) —
  // the scene view is a static frame + scrubber for gate drawing.
  if (!document.getElementById('btnPlay')) return;
  isPlaying = !isPlaying;
  document.getElementById('btnPlay').textContent = isPlaying ? '⏸' : '▶';

  if (isPlaying) {
    if (!hasVideo) {
      // No video: just animate the frame counter
      playTimer = setInterval(function() {
        currentFrame = (currentFrame + 1) % (totalFrames + 1);
        document.getElementById('frameSlider').value = currentFrame;
        updateFrameLabel();
      }, 1000 / fps);
    } else {
      // With video: fetch frames at reasonable rate
      var playFps = Math.min(fps, 8);  // cap at 8fps for smooth network playback
      var frameStep = Math.max(1, Math.round(fps / playFps));
      playTimer = setInterval(function() {
        currentFrame += frameStep;
        if (currentFrame > totalFrames) currentFrame = 0;
        document.getElementById('frameSlider').value = currentFrame;
        updateFrameLabel();
        loadFrame(currentFrame);
      }, 1000 / playFps);
    }
  } else {
    clearInterval(playTimer);
    playTimer = null;
  }
}

// Scrub with debounce
var scrubTimeout = null;
function seekFrame(f) {
  currentFrame = parseInt(f);
  updateFrameLabel();
  if (hasVideo) {
    clearTimeout(scrubTimeout);
    scrubTimeout = setTimeout(function() {
      loadFrame(currentFrame);
    }, 100);
  }
}

function updateFrameLabel() {
  var timeSec = (currentFrame / fps).toFixed(1);
  document.getElementById('frameLabel').textContent =
    'Frame ' + currentFrame + ' / ' + totalFrames + ' (' + timeSec + 's)';
}

// Keyboard shortcuts
document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'INPUT') return;

  if (e.key === 'ArrowRight') {
    e.preventDefault();
    currentFrame = Math.min(currentFrame + 1, totalFrames);
    document.getElementById('frameSlider').value = currentFrame;
    updateFrameLabel();
    loadFrame(currentFrame);
  } else if (e.key === 'ArrowLeft') {
    e.preventDefault();
    currentFrame = Math.max(currentFrame - 1, 0);
    document.getElementById('frameSlider').value = currentFrame;
    updateFrameLabel();
    loadFrame(currentFrame);
  } else if (e.key === ' ' && !window.drawingGate) {
    e.preventDefault();
    togglePlay();
  }
});

// Boot
document.addEventListener('DOMContentLoaded', init);
