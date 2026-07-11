import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";
import { computeClusterLayout } from "./layout.mjs";

const COLORS = {
  points: 0x25c2a0,
  lines: 0xf0b44d,
  faces: 0xd76adf,
  volumes: 0x5ca7ff,
  root: 0xff6b76,
  context: 0x8b939e,
  hierarchy: 0x59616d,
};

const canvasHost = document.getElementById("canvas");
const viewerEl = document.getElementById("viewer");
const statusEl = document.getElementById("status");
const detailEl = document.getElementById("detail");
const detailKindEl = document.getElementById("detail-kind");
const detailTitleEl = document.getElementById("detail-title");
const detailMetaEl = document.getElementById("detail-meta");
const detailReceiptsEl = document.getElementById("detail-receipts");
const emptyEl = document.getElementById("empty");
const errorEl = document.getElementById("error");
const slider = document.getElementById("as-of");
const sliderLabel = document.getElementById("as-of-label");

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0d10);
scene.fog = new THREE.FogExp2(0x0b0d10, 0.026);

const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 100);
const renderer = new THREE.WebGLRenderer({
  antialias: true,
  alpha: false,
  preserveDrawingBuffer: true,
});
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
canvasHost.appendChild(renderer.domElement);

const labelRenderer = new CSS2DRenderer();
labelRenderer.setSize(window.innerWidth, window.innerHeight);
labelRenderer.domElement.style.position = "absolute";
labelRenderer.domElement.style.inset = "0";
labelRenderer.domElement.style.pointerEvents = "none";
canvasHost.appendChild(labelRenderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.07;
controls.minDistance = 5;
controls.maxDistance = 32;
controls.target.set(0, 2.2, 0);

scene.add(new THREE.HemisphereLight(0xf3f5ff, 0x16181c, 2.1));
const keyLight = new THREE.DirectionalLight(0xffffff, 2.4);
keyLight.position.set(4, 10, 7);
scene.add(keyLight);

let graph = new THREE.Group();
scene.add(graph);
let model = { points: [], lines: [], faces: [], volumes: [], root: null, stats: {} };
let modelFingerprint = "";
let cutoff = new Date();
let minTime = new Date();
let maxTime = new Date();
let playTimer = null;
let pointerDown = null;
let selected = null;
let portraitMode = null;
let labels = [];
let pickables = [];
let positions = new Map();
let items = new Map();
let layerObjects = freshLayerObjects();
let currentLayout = null;
let layoutRadius = 6;
let framedRadius = 0;
const layerVisible = { points: true, lines: true, faces: true, volumes: true, root: true };
const raycaster = new THREE.Raycaster();
raycaster.params.Line.threshold = 0.12;
const pointer = new THREE.Vector2();

function freshLayerObjects() {
  return { points: [], lines: [], faces: [], volumes: [], root: [] };
}

function hash(text) {
  let value = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    value ^= text.charCodeAt(i);
    value = Math.imul(value, 16777619);
  }
  return (value >>> 0) / 4294967296;
}

function shortLabel(text, max = 34) {
  const value = String(text || "Untitled").replace(/\s+/g, " ").trim();
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function strongestIds(values, limit) {
  return new Set([...values].sort((left, right) => {
    const leftStrength = Number(left.observations || 0) * 2 + Number(left.confidence || 0);
    const rightStrength = Number(right.observations || 0) * 2 + Number(right.confidence || 0);
    return rightStrength - leftStrength || String(left.id).localeCompare(String(right.id));
  }).slice(0, limit).map((item) => item.id));
}

function itemTime(item) {
  const raw = item.valid_from || item.created_at || item.occurred_at;
  const value = raw ? new Date(raw) : null;
  return value && !Number.isNaN(value.getTime()) ? value : null;
}

function visibleAt(item) {
  const time = itemTime(item);
  return !time || time <= cutoff;
}

function register(object, layer) {
  object.userData.layer = layer;
  layerObjects[layer].push(object);
  graph.add(object);
  return object;
}

function registerPickable(object, layer, kind, item) {
  object.userData.ref = { kind, id: item.id };
  register(object, layer);
  pickables.push(object);
  items.set(`${kind}:${item.id}`, item);
  return object;
}

function addLabel(text, position, layer, priority, context = false) {
  const element = document.createElement("div");
  element.className = `model-label${context ? " context" : ""}`;
  element.textContent = shortLabel(text);
  element.title = String(text || "");
  const label = new CSS2DObject(element);
  label.position.copy(position);
  label.userData.priority = priority;
  register(label, layer);
  labels.push(label);
  return label;
}

function addLine(start, end, color, opacity, dashed, item, layer = "lines") {
  const geometry = new THREE.BufferGeometry().setFromPoints([start, end]);
  const material = dashed
    ? new THREE.LineDashedMaterial({ color, transparent: true, opacity, dashSize: 0.12, gapSize: 0.09 })
    : new THREE.LineBasicMaterial({ color, transparent: true, opacity });
  const line = new THREE.Line(geometry, material);
  if (dashed) line.computeLineDistances();
  if (item) {
    line.userData.ref = { kind: "line", id: item.id };
    items.set(`line:${item.id}`, item);
  }
  register(line, layer);
  if (item) pickables.push(line);
  return line;
}

function disposeGraph() {
  scene.remove(graph);
  graph.traverse((object) => {
    if (object.element) object.element.remove();
    if (object.geometry) object.geometry.dispose();
    if (Array.isArray(object.material)) object.material.forEach((material) => material.dispose());
    else if (object.material) object.material.dispose();
  });
  graph = new THREE.Group();
  scene.add(graph);
  labels = [];
  pickables = [];
  positions = new Map();
  items = new Map();
  layerObjects = freshLayerObjects();
}

function addPoint(point, position, baseRadius, showLabel, promoted) {
  const active = point.is_latest && point.status === "active";
  const clusterScale = promoted ? 1 : 0.72;
  const radius = (active ? baseRadius : baseRadius * 0.68) * clusterScale;
  const geometry = new THREE.SphereGeometry(radius, 14, 10);
  const material = new THREE.MeshStandardMaterial({
    color: COLORS.points,
    emissive: COLORS.points,
    emissiveIntensity: active ? (promoted ? 0.42 : 0.15) : 0.05,
    transparent: true,
    opacity: active ? (promoted ? 0.96 : 0.46) : (promoted ? 0.2 : 0.09),
    roughness: 0.35,
  });
  const mesh = registerPickable(new THREE.Mesh(geometry, material), "points", "point", point);
  mesh.position.copy(position);
  if (showLabel) {
    addLabel(point.content, position.clone().add(new THREE.Vector3(0, radius + 0.16, 0)), "points", active ? 55 : 25);
  }
}

function addContextNode(id) {
  const position = positions.get(id);
  if (!position) return;
  const item = { id, content: id === "self" ? "USER" : id, kind: "context" };
  const mesh = registerPickable(
    new THREE.Mesh(
      new THREE.BoxGeometry(0.14, 0.14, 0.14),
      new THREE.MeshStandardMaterial({ color: COLORS.context, roughness: 0.5 })
    ),
    "points",
    "context",
    item
  );
  mesh.position.copy(position);
  if (id === "self" || hash(id) < 0.16) {
    addLabel(item.content, position.clone().add(new THREE.Vector3(0, 0.24, 0)), "points", 30, true);
  }
}

function addClusterHalo(memberPositions, center) {
  if (memberPositions.length < 2) return;
  const radius = Math.min(1.9, Math.max(0.55, ...memberPositions.map((member) => member.distanceTo(center))) + 0.18);
  const mesh = register(
    new THREE.Mesh(
      new THREE.SphereGeometry(1, 16, 10),
      new THREE.MeshBasicMaterial({
        color: COLORS.faces,
        transparent: true,
        opacity: 0.035,
        wireframe: true,
        depthWrite: false,
      })
    ),
    "faces"
  );
  mesh.position.copy(center);
  mesh.scale.setScalar(radius);
  mesh.renderOrder = 1;
}

function addFace(face, showLabel) {
  const position = positions.get(face.id);
  if (!position) return;
  const memberIds = currentLayout?.facePointIds.get(face.id) || [];
  const memberPositions = memberIds.map((id) => positions.get(id)).filter(Boolean);
  addClusterHalo(memberPositions, position);
  const node = registerPickable(
    new THREE.Mesh(
      new THREE.OctahedronGeometry(0.28, 0),
      new THREE.MeshStandardMaterial({
        color: COLORS.faces,
        emissive: COLORS.faces,
        emissiveIntensity: 0.24,
        roughness: 0.4,
      })
    ),
    "faces",
    "face",
    face
  );
  node.position.copy(position);
  if (showLabel) addLabel(face.signature, position.clone().add(new THREE.Vector3(0, 0.4, 0)), "faces", 75);
  memberPositions.forEach((member) => addLine(member, position, COLORS.hierarchy, 0.1, true, null));
}

function addVolume(volume, showLabel) {
  const position = positions.get(volume.id);
  if (!position) return;
  const mesh = registerPickable(
    new THREE.Mesh(
      new THREE.IcosahedronGeometry(0.48, 1),
      new THREE.MeshStandardMaterial({
        color: COLORS.volumes,
        emissive: COLORS.volumes,
        emissiveIntensity: 0.2,
        transparent: true,
        opacity: 0.34,
        wireframe: true,
      })
    ),
    "volumes",
    "volume",
    volume
  );
  mesh.position.copy(position);
  if (showLabel) addLabel(volume.signature, position.clone().add(new THREE.Vector3(0, 0.55, 0)), "volumes", 90);
  const memberIds = currentLayout?.volumeFaceIds.get(volume.id) || [];
  memberIds.map((id) => positions.get(id)).filter(Boolean)
    .forEach((member) => addLine(member, position, COLORS.volumes, 0.22, false, null));
}

function addRoot(root) {
  const position = positions.get(root.id);
  if (!position) return;
  const mesh = registerPickable(
    new THREE.Mesh(
      new THREE.DodecahedronGeometry(0.62, 0),
      new THREE.MeshStandardMaterial({
        color: COLORS.root,
        emissive: COLORS.root,
        emissiveIntensity: 0.52,
        roughness: 0.3,
      })
    ),
    "root",
    "root",
    root
  );
  mesh.position.copy(position);
  addLabel(root.signature, position.clone().add(new THREE.Vector3(0, 0.66, 0)), "root", 120);
  const parentIds = currentLayout?.rootVolumeIds || [];
  parentIds.map((id) => positions.get(id)).filter(Boolean)
    .forEach((member) => addLine(member, position, COLORS.root, 0.32, false, null));
}

function addModelLine(line) {
  if (!visibleAt(line)) return;
  const start = positions.get(line.source);
  const end = positions.get(line.target);
  if (!start || !end) return;
  const evolution = line.kind === "evolution";
  const sourceCluster = currentLayout?.pointClusterById.get(line.source);
  const targetCluster = currentLayout?.pointClusterById.get(line.target);
  const sameCluster = sourceCluster && sourceCluster === targetCluster;
  const opacity = evolution ? (sameCluster ? 0.34 : 0.08) : 0.34;
  addLine(start, end, evolution ? COLORS.lines : 0x6ebf8e, opacity, !evolution, line);
}

function addGround() {
  const size = Math.max(12, layoutRadius * 2.4);
  const divisions = Math.max(10, Math.min(24, Math.round(size / 1.2)));
  const grid = new THREE.GridHelper(size, divisions, 0x30353d, 0x1b1f24);
  grid.position.y = (currentLayout?.diagnostics.bounds.min[1] || -3) - 0.8;
  const materials = Array.isArray(grid.material) ? grid.material : [grid.material];
  materials.forEach((material) => {
    material.transparent = true;
    material.opacity = 0.2;
  });
  graph.add(grid);
}

function buildScene() {
  disposeGraph();
  const visiblePoints = model.points.filter(visibleAt).sort((a, b) => a.id.localeCompare(b.id));
  const visibleFaces = model.faces.filter(visibleAt);
  const visibleVolumes = model.volumes.filter(visibleAt);
  const visibleRoot = model.root && visibleAt(model.root) ? model.root : null;
  const visibleLines = model.lines.filter(visibleAt);
  currentLayout = computeClusterLayout({
    points: visiblePoints,
    lines: visibleLines,
    faces: visibleFaces,
    volumes: visibleVolumes,
    root: visibleRoot,
  });
  positions = new Map(
    [...currentLayout.positions].map(([id, position]) => [id, new THREE.Vector3(...position)])
  );
  layoutRadius = currentLayout.diagnostics.bounds.radius;
  scene.fog.density = Math.max(0.006, Math.min(0.018, 0.09 / Math.max(layoutRadius, 5)));
  addGround();

  const labelEveryPoint = visiblePoints.length <= 60;
  const labeledFaces = strongestIds(visibleFaces, 10);
  const labeledVolumes = strongestIds(visibleVolumes, 6);
  visiblePoints.forEach((point) => {
    const active = point.is_latest && point.status === "active";
    const promoted = currentLayout.pointClusterById.get(point.id)?.startsWith("face:") || false;
    const showLabel = labelEveryPoint || (promoted && (
      currentLayout.directPointIds.has(point.id) || (active && hash(point.id) < 0.035)
    ));
    addPoint(point, positions.get(point.id), currentLayout.pointRadius, showLabel, promoted);
  });
  currentLayout.contextIds.forEach(addContextNode);
  visibleLines.forEach(addModelLine);
  visibleFaces.forEach((face) => addFace(face, labeledFaces.has(face.id)));
  visibleVolumes.forEach((volume) => addVolume(volume, labeledVolumes.has(volume.id)));
  if (visibleRoot) addRoot(visibleRoot);

  applyLayerVisibility();
  renderStatus();
  emptyEl.hidden = visiblePoints.length > 0;
  frameLayout(false);
  const renderedLines = visibleLines.filter((line) => positions.has(line.source) && positions.has(line.target)).length;
  window.__persomeViewerState = {
    schemaVersion: model.schema_version,
    commit: model.build?.core_commit || null,
    stats: model.stats,
    rendered: {
      points: visiblePoints.length,
      lines: renderedLines,
      faces: visibleFaces.length,
      volumes: visibleVolumes.length,
      root: Boolean(visibleRoot),
      context: currentLayout.contextIds.length,
    },
    layers: { ...layerVisible },
    layout: currentLayout.diagnostics,
  };
  window.__persomeLayoutState = currentLayout.diagnostics;
  viewerEl.dataset.schemaVersion = String(model.schema_version || "");
  viewerEl.dataset.renderedPoints = String(visiblePoints.length);
  viewerEl.dataset.renderedLines = String(renderedLines);
  viewerEl.dataset.renderedFaces = String(visibleFaces.length);
  viewerEl.dataset.renderedVolumes = String(visibleVolumes.length);
  viewerEl.dataset.renderedRoot = String(Boolean(visibleRoot));
  viewerEl.dataset.coreCommit = model.build?.core_commit || "";
  viewerEl.dataset.layoutVersion = currentLayout.diagnostics.version;
}

function renderStatus() {
  const stats = model.stats || {};
  statusEl.replaceChildren();
  const parts = [
    ["Points", stats.points || 0],
    ["Lines", (stats.evolution_lines || 0) + (stats.relation_lines || 0)],
    ["Faces", stats.faces || 0],
    ["Volumes", stats.volumes || 0],
    ["Root", stats.roots || 0],
  ];
  parts.forEach(([label, value], index) => {
    if (index) statusEl.append("  ·  ");
    const name = document.createElement("span");
    name.textContent = `${label} `;
    const count = document.createElement("b");
    count.textContent = String(value);
    statusEl.append(name, count);
  });
  if (model.build?.status) statusEl.append(`  ·  Build ${model.build.status}`);
}

function applyLayerVisibility() {
  Object.entries(layerObjects).forEach(([layer, objects]) => {
    objects.forEach((object) => {
      object.visible = layerVisible[layer];
      if (object.element) object.element.hidden = !layerVisible[layer];
    });
  });
  document.querySelectorAll("[data-layer]").forEach((button) => {
    button.setAttribute("aria-pressed", String(layerVisible[button.dataset.layer]));
  });
}

function appendMeta(label, value) {
  if (value === null || value === undefined || value === "") return;
  const row = document.createElement("div");
  const name = document.createElement("strong");
  name.textContent = `${label}: `;
  row.append(name, String(value));
  detailMetaEl.appendChild(row);
}

function receiptValues(item) {
  const values = [
    item.receipt,
    item.source_evidence?.receipt,
    ...(item.member_receipts || []),
    ...(item.source_receipts || []),
  ].filter(Boolean);
  return [...new Set(values)];
}

function showDetails(kind, item) {
  selected = { kind, id: item.id };
  detailKindEl.textContent = kind;
  detailTitleEl.textContent = item.content || item.signature || item.label || item.id;
  detailMetaEl.replaceChildren();
  detailReceiptsEl.replaceChildren();
  appendMeta("ID", item.id);
  appendMeta("Layer", item.layer || item.level);
  appendMeta("Status", item.status);
  appendMeta("Predicate", item.label || item.predicate);
  appendMeta("Confidence", item.confidence);
  appendMeta("Observations", item.observations);
  appendMeta("Valid from", item.valid_from);
  appendMeta("Members", item.members?.length);

  const receipts = receiptValues(item);
  if (receipts.length) {
    const heading = document.createElement("strong");
    heading.textContent = "Receipts";
    detailReceiptsEl.appendChild(heading);
    receipts.slice(0, 12).forEach((receipt) => {
      const row = document.createElement("div");
      row.textContent = receipt;
      detailReceiptsEl.appendChild(row);
    });
  }
  detailEl.hidden = false;

  if (kind === "point") {
    fetch(`/model/node?id=${encodeURIComponent(item.id)}`)
      .then((response) => response.ok ? response.json() : null)
      .then((data) => {
        if (!data || !selected || selected.kind !== kind || selected.id !== item.id) return;
        (data.raw || []).slice(0, 3).forEach((raw) => {
          const row = document.createElement("div");
          row.textContent = `${raw.ts ? `${String(raw.ts).slice(0, 16)}  ` : ""}${raw.text || ""}`;
          detailReceiptsEl.appendChild(row);
        });
      })
      .catch(() => {});
  }
}

function updateTimelineBounds() {
  const dated = [
    ...model.points,
    ...model.lines,
    ...model.faces,
    ...model.volumes,
    ...(model.root ? [model.root] : []),
  ].map(itemTime).filter(Boolean).sort((a, b) => a - b);
  minTime = dated[0] || new Date();
  maxTime = new Date();
  updateCutoff();
}

function updateCutoff() {
  const fraction = Number(slider.value) / 100;
  cutoff = new Date(minTime.getTime() + (maxTime.getTime() - minTime.getTime()) * fraction);
  sliderLabel.textContent = fraction >= 1 ? "Now" : cutoff.toISOString().slice(0, 10);
}

function fingerprint(nextModel) {
  return JSON.stringify({
    points: nextModel.points.map((item) => [
      item.id, item.status, item.is_latest, item.content, item.valid_from,
    ]),
    lines: nextModel.lines.map((item) => [item.id, item.predicate, item.valid_from]),
    faces: nextModel.faces.map((item) => [
      item.id, item.status, item.observations, item.signature, item.members,
    ]),
    volumes: nextModel.volumes.map((item) => [
      item.id, item.status, item.observations, item.signature, item.members,
    ]),
    root: nextModel.root ? [nextModel.root.id, nextModel.root.signature] : null,
    build: [
      nextModel.build?.build_id || null,
      nextModel.build?.status || null,
      nextModel.build?.core_commit || null,
    ],
  });
}

async function loadModel(force = false) {
  try {
    const response = await fetch("/model/graph", { cache: "no-store" });
    if (!response.ok) throw new Error(`Model endpoint returned HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload.model || !Array.isArray(payload.model.points)) {
      throw new Error("Model endpoint returned an invalid snapshot");
    }
    const nextFingerprint = fingerprint(payload.model);
    if (!force && nextFingerprint === modelFingerprint) return;
    model = payload.model;
    modelFingerprint = nextFingerprint;
    updateTimelineBounds();
    buildScene();
    errorEl.hidden = true;
  } catch (error) {
    errorEl.textContent = `Unable to load the personal model. ${error.message || error}`;
    errorEl.hidden = false;
  }
}

function frameLayout(force) {
  const portrait = window.innerWidth / window.innerHeight < 0.72;
  portraitMode = portrait;
  const radius = Math.max(4.8, layoutRadius);
  if (!force && framedRadius > 0 && radius <= framedRadius * 1.16) return;
  const direction = new THREE.Vector3(portrait ? 0.58 : 0.72, portrait ? 1.25 : 1.05, 1).normalize();
  const distance = Math.max(portrait ? 15 : 12, radius * (portrait ? 3.0 : 2.55));
  camera.position.copy(direction.multiplyScalar(distance));
  controls.target.set(0, 0, 0);
  controls.minDistance = Math.max(2.8, radius * 0.3);
  controls.maxDistance = Math.max(32, radius * 5);
  framedRadius = radius;
  controls.update();
}

function resetCamera() {
  frameLayout(true);
}

function cullLabels() {
  const occupied = [];
  const maxLabels = window.innerWidth < 760 ? 8 : 20;
  let shown = 0;
  const projected = new THREE.Vector3();
  const candidates = labels.filter((label) => label.visible).map((label) => {
    label.getWorldPosition(projected);
    projected.project(camera);
    const x = (projected.x * 0.5 + 0.5) * window.innerWidth;
    const y = (-projected.y * 0.5 + 0.5) * window.innerHeight;
    const width = Math.min(window.innerWidth < 760 ? 130 : 220, (label.element.textContent.length * 6.2) + 12);
    return { label, x, y, width, priority: label.userData.priority || 0, depth: projected.z };
  }).sort((a, b) => b.priority - a.priority);

  candidates.forEach((candidate) => {
    const { label, x, y, width, depth } = candidate;
    const box = { x0: x - width / 2, x1: x + width / 2, y0: y - 9, y1: y + 9 };
    const overlaps = occupied.some((other) => box.x0 < other.x1 && box.x1 > other.x0 && box.y0 < other.y1 && box.y1 > other.y0);
    const hidden = depth < -1 || depth > 1 || overlaps || shown >= maxLabels;
    label.element.classList.toggle("hidden", hidden);
    if (!hidden) {
      occupied.push(box);
      shown += 1;
    }
  });
  window.__persomeLabelHealth = { total: labels.length, shown, max: maxLabels };
}

function samplePixels() {
  if (window.__persomeModelRender?.lit > 0) return;
  const gl = renderer.getContext();
  const width = gl.drawingBufferWidth;
  const height = gl.drawingBufferHeight;
  let lit = 0;
  let checked = 0;
  for (let y = 1; y < 8; y += 1) {
    for (let x = 1; x < 12; x += 1) {
      const pixel = new Uint8Array(4);
      gl.readPixels(Math.floor(width * x / 12), Math.floor(height * y / 8), 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
      checked += 1;
      if (pixel[0] + pixel[1] + pixel[2] > 96) lit += 1;
    }
  }
  window.__persomeModelRender = { width, height, checked, lit };
  canvasHost.dataset.litPixels = String(lit);
}

document.querySelectorAll("[data-layer]").forEach((button) => {
  button.addEventListener("click", () => {
    const layer = button.dataset.layer;
    layerVisible[layer] = !layerVisible[layer];
    applyLayerVisibility();
    if (window.__persomeViewerState) window.__persomeViewerState.layers = { ...layerVisible };
  });
});

document.getElementById("rotate").addEventListener("click", (event) => {
  controls.autoRotate = !controls.autoRotate;
  controls.autoRotateSpeed = 0.7;
  event.currentTarget.setAttribute("aria-pressed", String(controls.autoRotate));
});
document.getElementById("reset").addEventListener("click", resetCamera);
document.getElementById("close-detail").addEventListener("click", () => {
  selected = null;
  detailEl.hidden = true;
});

slider.addEventListener("input", () => {
  updateCutoff();
  buildScene();
});

document.getElementById("play").addEventListener("click", (event) => {
  const button = event.currentTarget;
  if (playTimer) {
    window.clearInterval(playTimer);
    playTimer = null;
    button.textContent = "▶";
    button.setAttribute("aria-pressed", "false");
    return;
  }
  if (Number(slider.value) >= 100) slider.value = "0";
  button.textContent = "Ⅱ";
  button.setAttribute("aria-pressed", "true");
  playTimer = window.setInterval(() => {
    const next = Number(slider.value) + 2;
    slider.value = String(Math.min(100, next));
    updateCutoff();
    buildScene();
    if (next >= 100) button.click();
  }, 240);
});

renderer.domElement.addEventListener("pointerdown", (event) => {
  pointerDown = { x: event.clientX, y: event.clientY };
});
renderer.domElement.addEventListener("pointerup", (event) => {
  if (!pointerDown) return;
  const movement = Math.abs(event.clientX - pointerDown.x) + Math.abs(event.clientY - pointerDown.y);
  pointerDown = null;
  if (movement > 5) return;
  const bounds = renderer.domElement.getBoundingClientRect();
  pointer.x = ((event.clientX - bounds.left) / bounds.width) * 2 - 1;
  pointer.y = -((event.clientY - bounds.top) / bounds.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects(pickables.filter((object) => object.visible), false)[0];
  if (!hit?.object.userData.ref) {
    selected = null;
    detailEl.hidden = true;
    return;
  }
  const ref = hit.object.userData.ref;
  const item = items.get(`${ref.kind}:${ref.id}`);
  if (item) showDetails(ref.kind, item);
});

window.addEventListener("resize", () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
  labelRenderer.setSize(window.innerWidth, window.innerHeight);
  const portrait = window.innerWidth / window.innerHeight < 0.72;
  if (portrait !== portraitMode) resetCamera();
});

window.addEventListener("error", (event) => {
  errorEl.textContent = `Viewer error: ${event.message}`;
  errorEl.hidden = false;
});
window.addEventListener("unhandledrejection", (event) => {
  errorEl.textContent = `Viewer error: ${event.reason?.message || event.reason}`;
  errorEl.hidden = false;
});

function animate() {
  controls.update();
  cullLabels();
  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
  samplePixels();
  window.requestAnimationFrame(animate);
}

resetCamera();
animate();
await loadModel(true);
window.setInterval(() => loadModel(false), 5000);
