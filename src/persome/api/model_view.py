"""Offline HTML shell for the canonical personal-model snapshot viewer."""

MEMORY_VIEW_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Persome Personal Model</title>
  <link rel="icon" href="data:,">
  <link rel="stylesheet" href="/model/assets/viewer.css">
  <script type="importmap">
  {"imports": {
    "three": "/model/assets/three.module.js",
    "three/addons/": "/model/assets/jsm/"
  }}
  </script>
</head>
<body>
  <main id="viewer" aria-label="Persome personal model explorer">
    <div id="canvas" aria-hidden="true"></div>

    <header class="topbar">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true"></span>
        <span>Persome</span>
        <span class="brand-subtitle">Personal Model</span>
      </div>
      <div class="layers" role="group" aria-label="Visible model layers">
        <button type="button" data-layer="points" aria-pressed="true" title="Toggle Points">Points</button>
        <button type="button" data-layer="lines" aria-pressed="true" title="Toggle Lines">Lines</button>
        <button type="button" data-layer="faces" aria-pressed="true" title="Toggle Faces">Faces</button>
        <button type="button" data-layer="volumes" aria-pressed="true" title="Toggle Volumes">Volumes</button>
        <button type="button" data-layer="root" aria-pressed="true" title="Toggle Root">Root</button>
      </div>
      <div class="view-actions">
        <button id="rotate" class="icon-button" type="button" aria-label="Toggle rotation" aria-pressed="false" title="Toggle rotation">↻</button>
        <button id="reset" class="icon-button" type="button" aria-label="Reset camera" title="Reset camera">⌂</button>
      </div>
    </header>

    <section id="status" class="status" aria-live="polite">Loading model...</section>

    <aside class="legend" aria-label="Model layer legend">
      <div><span class="swatch point"></span><b>Point</b><span>observed fact</span></div>
      <div><span class="swatch line"></span><b>Line</b><span>evolution or relation</span></div>
      <div><span class="swatch face"></span><b>Face</b><span>stable pattern</span></div>
      <div><span class="swatch volume"></span><b>Volume</b><span>cross-pattern structure</span></div>
      <div><span class="swatch root"></span><b>Root</b><span>current personal model</span></div>
    </aside>

    <aside id="detail" class="detail" aria-live="polite" hidden>
      <button id="close-detail" class="icon-button close" type="button" aria-label="Close details" title="Close details">×</button>
      <p id="detail-kind" class="detail-kind"></p>
      <h1 id="detail-title"></h1>
      <div id="detail-meta" class="detail-meta"></div>
      <div id="detail-receipts" class="detail-receipts"></div>
    </aside>

    <div id="empty" class="empty" hidden>
      <strong>No model yet</strong>
      <span>Keep the daemon running, then build the personal model.</span>
    </div>

    <div id="error" class="error" role="alert" hidden></div>

    <footer class="timeline">
      <button id="play" class="icon-button" type="button" aria-label="Play model history" aria-pressed="false" title="Play model history">▶</button>
      <label for="as-of">As of</label>
      <input id="as-of" type="range" min="0" max="100" step="1" value="100">
      <output id="as-of-label" for="as-of">Now</output>
    </footer>
  </main>
  <script type="module" src="/model/assets/viewer.js"></script>
</body>
</html>
"""
