// ── Canvas Setup ──
const canvas = document.getElementById('mainCanvas');
const ctx = canvas.getContext('2d');

function initCanvas() {
  canvas.width = frameW;
  canvas.height = frameH;
}

function render() {
  if (!ctx) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // Background — video frame or dark fill (no darkening overlay; matches DFS look)
  if (currentFrameImage && currentFrameImage.complete && currentFrameImage.naturalWidth > 0) {
    ctx.drawImage(currentFrameImage, 0, 0, frameW, frameH);
  } else {
    ctx.fillStyle = '#0a0e14';
    ctx.fillRect(0, 0, frameW, frameH);
  }

  if (overlayMode === 'tracks' || overlayMode === 'gates') renderTrajectories();
  if (overlayMode === 'heatmap') renderHeatmap();
  if (!window.hideGatesOverlay) renderGatesOnCanvas();
  if (typeof renderConflictOverlay === 'function') renderConflictOverlay();
  renderHUD();
}

function renderTrajectories() {
  // DFS-style trajectory rendering:
  //   - Solid class-colored lines (no pale gradient)
  //   - Round line caps/joins for smooth curves
  //   - Thicker lines (2px base, 3.5px selected)
  //   - Skip short/noisy trajectories by default to reduce clutter
  //   - Selected track drawn last + on top in full color
  //
  // Toggle `window.showAllTracks = true` from the console to bypass the
  // noise filter and render every trajectory regardless of length.

  const minPointsToDraw = (typeof window.showAllTracks !== 'undefined' && window.showAllTracks)
    ? 2 : 6;

  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';

  // Pre-parse color components to avoid doing it inside the hot loop
  const colorCache = {};
  function rgb(cls) {
    if (colorCache[cls]) return colorCache[cls];
    const hex = CLASS_COLORS[cls] || '#8b949e';
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    colorCache[cls] = { r, g, b };
    return colorCache[cls];
  }

  // First pass: draw all non-selected trajectories
  let selectedTraj = null;
  for (const t of allTrajectories) {
    if (typeof hiddenTrackIds !== 'undefined' && hiddenTrackIds.has(t.track_id)) continue;
    const pts = t.trajectory;
    if (!pts || pts.length < minPointsToDraw) continue;

    if (t.track_id === selectedTrack) {
      selectedTraj = t;   // draw last so it sits on top
      continue;
    }

    const { r, g, b } = rgb(t.class_name);
    // Dim everything else when there's a selection so the picked track stands out
    const alpha = selectedTrack ? 0.12 : 0.55;
    ctx.strokeStyle = `rgba(${r},${g},${b},${alpha})`;
    ctx.lineWidth = 2;

    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) {
      ctx.lineTo(pts[i][0], pts[i][1]);
    }
    ctx.stroke();
  }

  // Second pass: the selected track (full opacity, thicker, with label)
  if (selectedTraj) {
    const pts = selectedTraj.trajectory;
    const { r, g, b } = rgb(selectedTraj.class_name);
    const color = CLASS_COLORS[selectedTraj.class_name] || '#8b949e';

    ctx.strokeStyle = `rgba(${r},${g},${b},1.0)`;
    ctx.lineWidth = 3.5;
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) {
      ctx.lineTo(pts[i][0], pts[i][1]);
    }
    ctx.stroke();

    // Start / end markers
    ctx.fillStyle = '#3fb950';
    ctx.beginPath();
    ctx.arc(pts[0][0], pts[0][1], 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#f85149';
    ctx.beginPath();
    ctx.arc(pts[pts.length - 1][0], pts[pts.length - 1][1], 5, 0, Math.PI * 2);
    ctx.fill();

    // Mid-trajectory label
    const mid = pts[Math.floor(pts.length / 2)];
    const speed = (typeof selectedTraj.speed_mean_px === 'number')
      ? selectedTraj.speed_mean_px.toFixed(1) : '--';
    const label = `#${selectedTraj.track_id} ${selectedTraj.class_name} (${speed} px/f)`;
    ctx.font = 'bold 12px DM Sans';
    const tw = ctx.measureText(label).width;
    ctx.fillStyle = 'rgba(0,0,0,0.75)';
    ctx.fillRect(mid[0] + 6, mid[1] - 18, tw + 10, 20);
    ctx.fillStyle = '#fff';
    ctx.fillText(label, mid[0] + 11, mid[1] - 3);

    // Entry / exit edge labels
    ctx.font = '10px JetBrains Mono';
    ctx.fillStyle = color;
    ctx.fillText(`▸ ${selectedTraj.entry_edge || '?'}`, pts[0][0] + 8, pts[0][1] - 2);
    ctx.fillStyle = '#f85149';
    ctx.fillText(`▸ ${selectedTraj.exit_edge || '?'}`,
                 pts[pts.length - 1][0] + 8, pts[pts.length - 1][1] - 2);
  }
}

function renderHeatmap() {
  const grid = 16;
  const cols = Math.ceil(frameW / grid), rows = Math.ceil(frameH / grid);
  const density = new Float32Array(cols * rows);
  let maxD = 0;

  for (const t of allTrajectories) {
    for (const p of t.trajectory) {
      const c = Math.floor(p[0] / grid), r = Math.floor(p[1] / grid);
      if (c >= 0 && c < cols && r >= 0 && r < rows) {
        density[r * cols + c]++;
        maxD = Math.max(maxD, density[r * cols + c]);
      }
    }
  }
  if (maxD === 0) return;

  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const v = density[r * cols + c] / maxD;
      if (v > 0.02) {
        const h = (1 - v) * 240; // Blue → Red
        ctx.fillStyle = `hsla(${h},100%,50%,${Math.min(v * 0.8, 0.65)})`;
        ctx.fillRect(c * grid, r * grid, grid, grid);
      }
    }
  }
}

function renderGatesOnCanvas() {
  const gatesList = window.gates || [];
  for (const g of gatesList) {
    // Gate line
    ctx.beginPath(); ctx.moveTo(g.x1, g.y1); ctx.lineTo(g.x2, g.y2);
    ctx.strokeStyle = 'rgba(248,81,73,0.85)'; ctx.lineWidth = 2.5;
    ctx.lineCap = 'round';
    ctx.stroke();

    const mx = (g.x1 + g.x2) / 2, my = (g.y1 + g.y2) / 2;

    // Compact gate label with count (smaller, less opaque than before)
    const label = `${g.name} (${g.total || 0})`;
    ctx.font = '600 10px DM Sans';
    const tw = ctx.measureText(label).width;
    ctx.fillStyle = 'rgba(248,81,73,0.75)';
    const lx = mx - tw / 2 - 5, ly = my - 16;
    ctx.beginPath();
    ctx.roundRect(lx, ly, tw + 10, 16, 3);
    ctx.fill();
    ctx.fillStyle = '#fff';
    ctx.fillText(label, mx - tw / 2, my - 5);

    // Endpoint dots (smaller)
    ctx.beginPath(); ctx.arc(g.x1, g.y1, 3, 0, Math.PI * 2);
    ctx.fillStyle = '#f85149'; ctx.fill();
    ctx.beginPath(); ctx.arc(g.x2, g.y2, 3, 0, Math.PI * 2);
    ctx.fillStyle = '#f85149'; ctx.fill();
  }

  // Gate drawing preview
  if (window.drawingGate && window.gateP1) {
    ctx.beginPath(); ctx.arc(window.gateP1.x, window.gateP1.y, 6, 0, Math.PI * 2);
    ctx.fillStyle = '#f85149'; ctx.fill();
    ctx.strokeStyle = '#f85149'; ctx.lineWidth = 1;
    ctx.stroke();
  }
}

function renderHUD() {
  // Compact top-left info bar
  const timeSec = (currentFrame / fps).toFixed(1);
  const info = `Frame ${currentFrame}  |  ${timeSec}s  |  ${allTrajectories.length} tracks`;
  ctx.font = '10px JetBrains Mono';
  const tw = ctx.measureText(info).width;
  ctx.fillStyle = 'rgba(0,0,0,0.55)';
  ctx.beginPath(); ctx.roundRect(8, 8, tw + 14, 20, 3); ctx.fill();
  ctx.fillStyle = '#e6edf3';
  ctx.fillText(info, 15, 22);

  // Compact class legend — only show classes actually present in data
  // Uses global ALL_CLASSES, CLASS_LABELS, CLASS_COLORS from /api/class_profile
  const classCounts = {};
  for (const cls of ALL_CLASSES) classCounts[cls] = 0;
  for (const t of allTrajectories) {
    if (t.class_name && classCounts.hasOwnProperty(t.class_name)) {
      classCounts[t.class_name]++;
    }
  }
  const visibleClasses = ALL_CLASSES.filter(c => classCounts[c] > 0);

  if (visibleClasses.length > 0) {
    const dotR = 3;
    const lineH = 13;
    const padX = 8, padY = 5;
    ctx.font = '9px JetBrains Mono';

    // Short labels: "Car (278)" instead of full "Car — Cars (278)"
    let maxLabelW = 0;
    for (const cls of visibleClasses) {
      const label = `${cls} (${classCounts[cls]})`;
      const lw = ctx.measureText(label).width;
      if (lw > maxLabelW) maxLabelW = lw;
    }

    const boxW = dotR * 2 + 6 + maxLabelW + padX * 2;
    const boxH = visibleClasses.length * lineH + padY * 2;
    const bx = frameW - boxW - 8;
    const by = 8;

    // Translucent background
    ctx.fillStyle = 'rgba(0,0,0,0.45)';
    ctx.beginPath(); ctx.roundRect(bx, by, boxW, boxH, 3); ctx.fill();

    for (let i = 0; i < visibleClasses.length; i++) {
      const cls = visibleClasses[i];
      const color = CLASS_COLORS[cls] || '#8b949e';
      const y = by + padY + i * lineH + lineH / 2;

      ctx.beginPath();
      ctx.arc(bx + padX + dotR, y, dotR, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();

      ctx.fillStyle = '#e6edf3';
      const label = `${cls} (${classCounts[cls]})`;
      ctx.fillText(label, bx + padX + dotR * 2 + 6, y + 3);
    }
  }

  // Keyboard hints (bottom left, very faint)
  if (!isPlaying) {
    ctx.fillStyle = 'rgba(255,255,255,0.35)';
    ctx.font = '9px DM Sans';
    ctx.fillText('Space: Play  |  ← → : Step  |  Esc: Cancel gate', 10, frameH - 8);
  }
}

function exportCanvasImage() {
  // Re-render to ensure canvas is up to date
  render();

  // Generate filename with timestamp
  const now = new Date();
  const ts = now.getFullYear() +
    String(now.getMonth() + 1).padStart(2, '0') +
    String(now.getDate()).padStart(2, '0') + '_' +
    String(now.getHours()).padStart(2, '0') +
    String(now.getMinutes()).padStart(2, '0') +
    String(now.getSeconds()).padStart(2, '0');
  const mode = overlayMode || 'tracks';
  const filename = `traffic_${mode}_frame${currentFrame}_${ts}.png`;

  // Export canvas as PNG
  const link = document.createElement('a');
  link.download = filename;
  link.href = canvas.toDataURL('image/png');
  link.click();
}

// ── Click on trajectory to select track ──
canvas.addEventListener('click', function(e) {
  // Don't interfere with gate drawing
  if (window.drawingGate) return;

  var rect = canvas.getBoundingClientRect();
  var scaleX = frameW / rect.width;
  var scaleY = frameH / rect.height;
  var mx = (e.clientX - rect.left) * scaleX;
  var my = (e.clientY - rect.top) * scaleY;

  // Find closest trajectory to click point
  var bestTrack = null;
  var bestDist = 15; // max click distance in pixels (tolerance)

  for (var ti = 0; ti < allTrajectories.length; ti++) {
    var t = allTrajectories[ti];
    if (typeof hiddenTrackIds !== 'undefined' && hiddenTrackIds.has(t.track_id)) continue;
    var pts = t.trajectory;
    if (!pts || pts.length < 2) continue;

    // Check each segment of this trajectory
    for (var i = 0; i < pts.length - 1; i++) {
      var d = _distToSegment(mx, my, pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1]);
      if (d < bestDist) {
        bestDist = d;
        bestTrack = t.track_id;
      }
    }
  }

  if (bestTrack !== null) {
    // Select this track
    if (typeof selectTrack === 'function') {
      selectTrack(bestTrack);
    } else {
      selectedTrack = (selectedTrack === bestTrack) ? null : bestTrack;
      render();
    }
    // Switch to tracks tab so user can see the selection
    if (typeof switchTab === 'function') {
      switchTab('tracks');
      // Highlight the tab
      document.querySelectorAll('.tab').forEach(function(tab) { tab.classList.remove('active'); });
      document.querySelectorAll('.tab').forEach(function(tab) {
        if (tab.textContent.trim() === 'TRACKS') tab.classList.add('active');
      });
      document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('active'); });
      var tracksPanel = document.getElementById('panel-tracks');
      if (tracksPanel) tracksPanel.classList.add('active');
    }
    // Scroll to the selected track in the list
    setTimeout(function() {
      var sel = document.querySelector('.track-item.selected');
      if (sel) sel.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 100);
  } else {
    // Clicked empty space — deselect
    if (selectedTrack !== null) {
      selectedTrack = null;
      if (typeof applyFilters === 'function') applyFilters();
      render();
    }
  }
});

function _edgeFadeFactor(x, y, w, h, margin) {
  if (x < -5 || y < -5 || x > w + 5 || y > h + 5) return 0.0;
  var bigMargin = margin * 3;
  var f = 1.0;
  if (x < margin) f = Math.min(f, x / margin);
  if (y < margin) f = Math.min(f, y / margin);
  if (x > w - bigMargin) f = Math.min(f, (w - x) / bigMargin);
  if (y > h - bigMargin) f = Math.min(f, (h - y) / bigMargin);
  f = Math.max(0.0, f);
  return f * f;
}

function _distToSegment(px, py, x1, y1, x2, y2) {
  // Distance from point (px,py) to line segment (x1,y1)-(x2,y2)
  var dx = x2 - x1;
  var dy = y2 - y1;
  var lenSq = dx * dx + dy * dy;
  if (lenSq === 0) return Math.sqrt((px-x1)*(px-x1) + (py-y1)*(py-y1));
  var t = Math.max(0, Math.min(1, ((px-x1)*dx + (py-y1)*dy) / lenSq));
  var projX = x1 + t * dx;
  var projY = y1 + t * dy;
  return Math.sqrt((px-projX)*(px-projX) + (py-projY)*(py-projY));
}
