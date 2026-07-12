"""Offline HTML shell for the canonical personal-model snapshot viewer."""

from __future__ import annotations

import re

_MODEL_BASE_RE = re.compile(r"^/model(?:/[A-Za-z0-9_-]{32,128})?/$")

_MEMORY_VIEW_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Persome Personal Model</title>
  <base href="__PERSOME_MODEL_BASE__">
  <link rel="icon" href="data:,">
  <link rel="stylesheet" href="assets/viewer.css">
  <script type="importmap">
  {"imports": {
    "three": "./assets/three.module.js",
    "three/addons/": "./assets/jsm/"
  }}
  </script>
</head>
<body>
  <main id="viewer" aria-label="Persome personal model explorer">
    <div id="canvas"></div>

    <header class="topbar">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
        <span class="brand-lockup">
          <strong>Persome</strong>
          <span>Personal Model</span>
        </span>
      </div>
      <div class="layers" role="group" aria-label="Visible model layers">
        <button type="button" data-layer="points" aria-pressed="true" title="Toggle Points"><i></i>Points</button>
        <button type="button" data-layer="lines" aria-pressed="true" title="Toggle Lines"><i></i>Lines</button>
        <button type="button" data-layer="faces" aria-pressed="true" title="Toggle Faces"><i></i>Faces</button>
        <button type="button" data-layer="volumes" aria-pressed="true" title="Toggle Volumes"><i></i>Volumes</button>
        <button type="button" data-layer="root" aria-pressed="true" title="Toggle Root"><i></i>Root</button>
      </div>
      <div class="view-actions">
        <span class="privacy-badge"><i aria-hidden="true"></i>Local only</span>
        <div class="zoom-controls" role="group" aria-label="Zoom controls">
          <button id="zoom-out" class="icon-button" type="button" aria-label="Zoom out" title="Zoom out (−)">−</button>
          <button id="zoom-reset" class="zoom-value" type="button" aria-label="Reset zoom to 100 percent" title="Reset zoom (0)">100%</button>
          <button id="zoom-in" class="icon-button" type="button" aria-label="Zoom in" title="Zoom in (+)">+</button>
        </div>
        <button id="rotate" class="icon-button action-button" type="button" aria-label="Toggle rotation" aria-pressed="false" title="Toggle rotation"><span aria-hidden="true">↻</span><b>Orbit</b></button>
        <button id="reset" class="icon-button action-button" type="button" aria-label="Reset camera" title="Reset camera"><span aria-hidden="true">⌁</span><b>Frame</b></button>
      </div>
    </header>

    <section class="story" aria-labelledby="story-title">
      <p class="story-kicker"><span aria-hidden="true"></span>Live personal model</p>
      <h1 id="story-title">The shape<br><em>of you.</em></h1>
      <p id="model-identity" class="model-identity">A living map of what you notice, repeat, and become.</p>
      <p class="model-flow"><span>Observe</span><i></i><span>Connect</span><i></i><span>Understand</span></p>
    </section>

    <section id="status" class="status" aria-label="Model composition" aria-live="polite">
      <span class="status-loading">Loading your model…</span>
    </section>

    <aside class="legend" aria-label="Model layer legend">
      <p>How to read your model</p>
      <div><span class="swatch point"></span><b>Point</b><span>observed fact</span></div>
      <div><span class="swatch line"></span><b>Line</b><span>evolution or relation</span></div>
      <div><span class="swatch face"></span><b>Face</b><span>stable pattern</span></div>
      <div><span class="swatch volume"></span><b>Volume</b><span>cross-pattern structure</span></div>
      <div><span class="swatch root"></span><b>Root</b><span>current personal model</span></div>
    </aside>

    <aside id="detail" class="detail" aria-labelledby="detail-title" aria-live="polite" hidden>
      <button id="close-detail" class="icon-button close" type="button" aria-label="Close details" title="Close details">×</button>
      <p class="detail-eyebrow"><span id="detail-kind" class="detail-kind"></span><span>Evidence-backed</span></p>
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
      <div class="timeline-copy"><strong>Model evolution</strong><label for="as-of">Travel through your history</label></div>
      <div class="timeline-track"><input id="as-of" type="range" min="0" max="100" step="1" value="100"></div>
      <output id="as-of-label" for="as-of">Now</output>
    </footer>

    <p class="gesture-hint"><span aria-hidden="true">↗</span> Drag to orbit · Scroll or pinch to zoom · Select a thought</p>
  </main>
  <script type="module" src="assets/viewer.js"></script>
</body>
</html>
"""


def render_memory_view(base_path: str = "/model/") -> str:
    """Render the viewer with a strict same-session base URL."""
    if _MODEL_BASE_RE.fullmatch(base_path) is None:
        raise ValueError("invalid model viewer base path")
    return _MEMORY_VIEW_TEMPLATE.replace("__PERSOME_MODEL_BASE__", base_path)
