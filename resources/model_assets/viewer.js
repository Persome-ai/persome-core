import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";
import { computeClusterLayout, zoomMath } from "./layout.mjs";
import {
  SHARE_CARD_HEIGHT,
  SHARE_CARD_WIDTH,
  SHARE_FILE_NAME,
  buildXIntentUrl,
  drawShareCard,
} from "./share.mjs";

const COLORS = {
  points: 0x4ef0c3,
  lines: 0xffc45e,
  faces: 0xff64d6,
  volumes: 0x7798ff,
  root: 0xff6b8a,
  context: 0xa5a0b5,
  hierarchy: 0x6e6688,
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
const modelIdentityEl = document.getElementById("model-identity");
const slider = document.getElementById("as-of");
const sliderLabel = document.getElementById("as-of-label");
const zoomOutButton = document.getElementById("zoom-out");
const zoomResetButton = document.getElementById("zoom-reset");
const zoomInButton = document.getElementById("zoom-in");
const shareButton = document.getElementById("share-x");
const shareNoticeEl = document.getElementById("share-notice");

const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(0x070610, 0.026);

const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 100);
const renderer = new THREE.WebGLRenderer({
  antialias: true,
  alpha: true,
  preserveDrawingBuffer: true,
});
renderer.setClearColor(0x000000, 0);
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.18;
renderer.domElement.setAttribute("aria-hidden", "true");
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
controls.zoomSpeed = 0.62;
controls.zoomToCursor = true;
controls.minDistance = 5;
controls.maxDistance = 32;
controls.target.set(0, 2.2, 0);

scene.add(new THREE.HemisphereLight(0xdad6ff, 0x080710, 2.25));
const keyLight = new THREE.DirectionalLight(0xffeafa, 2.65);
keyLight.position.set(5, 11, 8);
scene.add(keyLight);
const cyanLight = new THREE.PointLight(COLORS.points, 12, 22, 2);
cyanLight.position.set(-5, -1, 3);
scene.add(cyanLight);
const violetLight = new THREE.PointLight(COLORS.volumes, 13, 24, 2);
violetLight.position.set(5, 4, -4);
scene.add(violetLight);

let graph = new THREE.Group();
scene.add(graph);
let model = { points: [], lines: [], faces: [], volumes: [], root: null, stats: {} };
let modelFingerprint = "";
let modelGeneratedAt = "";
let cutoff = new Date();
let minTime = new Date();
let maxTime = new Date();
let playTimer = null;
let pointerDown = null;
let hoverPointer = null;
let hoverDirty = false;
let selected = null;
let selectedItem = null;
let detailMode = "node";
let evidenceRequest = 0;
let portraitMode = null;
let labels = [];
let pickables = [];
let selectionTargets = new Map();
let positions = new Map();
let items = new Map();
let layerObjects = freshLayerObjects();
let currentLayout = null;
let layoutRadius = 6;
let framedRadius = 0;
let pulseGlows = [];
let fitDistance = 12;
let zoomGoalDistance = null;
let lastZoomPercent = null;
let lastFrameTime = performance.now();
let shareReady = false;
const layerVisible = { points: true, lines: true, faces: true, volumes: true, root: true };
const kindLayers = { point: "points", context: "points", face: "faces", volume: "volumes", root: "root" };
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
const zoomDirection = new THREE.Vector3();
const ZOOM_MIN_PERCENT = 50;
const ZOOM_MAX_PERCENT = 400;
const ZOOM_STEP_PERCENT = 25;
const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

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

function addAtmosphere() {
  const count = 420;
  const positions = new Float32Array(count * 3);
  for (let index = 0; index < count; index += 1) {
    const radius = 10 + hash(`star:${index}:radius`) * 22;
    const theta = hash(`star:${index}:theta`) * Math.PI * 2;
    const phi = Math.acos(2 * hash(`star:${index}:phi`) - 1);
    positions[index * 3] = radius * Math.sin(phi) * Math.cos(theta);
    positions[index * 3 + 1] = radius * Math.cos(phi);
    positions[index * 3 + 2] = radius * Math.sin(phi) * Math.sin(theta);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  const stars = new THREE.Points(
    geometry,
    new THREE.PointsMaterial({
      color: 0xc9c2ff,
      size: 0.035,
      transparent: true,
      opacity: 0.38,
      depthWrite: false,
      sizeAttenuation: true,
    })
  );
  stars.rotation.z = 0.18;
  scene.add(stars);
}

const glowTextures = new Map();

function glowTexture(color) {
  if (glowTextures.has(color)) return glowTextures.get(color);
  const canvas = document.createElement("canvas");
  canvas.width = 128;
  canvas.height = 128;
  const context = canvas.getContext("2d");
  const cssColor = `#${color.toString(16).padStart(6, "0")}`;
  const gradient = context.createRadialGradient(64, 64, 0, 64, 64, 64);
  gradient.addColorStop(0, `${cssColor}e6`);
  gradient.addColorStop(0.16, `${cssColor}7a`);
  gradient.addColorStop(0.5, `${cssColor}20`);
  gradient.addColorStop(1, `${cssColor}00`);
  context.fillStyle = gradient;
  context.fillRect(0, 0, 128, 128);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  glowTextures.set(color, texture);
  return texture;
}

function addGlow(position, color, size, opacity, layer, pulse = 0.06) {
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({
      map: glowTexture(color),
      color,
      transparent: true,
      opacity,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    })
  );
  sprite.position.copy(position);
  sprite.scale.setScalar(size);
  sprite.renderOrder = -1;
  sprite.userData.glowBase = size;
  sprite.userData.glowPhase = hash(`${position.x}:${position.y}:${position.z}`) * Math.PI * 2;
  sprite.userData.glowPulse = pulse;
  register(sprite, layer);
  pulseGlows.push(sprite);
  return sprite;
}

addAtmosphere();

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

function selectionKey(kind, id) {
  return `${kind}:${id}`;
}

function registerSelectionTarget(kind, id, target) {
  const key = selectionKey(kind, id);
  target.userData.ref = { kind, id };
  const targets = selectionTargets.get(key) || [];
  targets.push(target);
  selectionTargets.set(key, targets);
}

function registerPickable(object, layer, kind, item) {
  registerSelectionTarget(kind, item.id, object);
  register(object, layer);
  pickables.push(object);
  items.set(selectionKey(kind, item.id), item);
  return object;
}

function addLabel(text, position, layer, priority, kind, item, context = false) {
  const element = document.createElement("button");
  element.type = "button";
  element.className = `model-label${context ? " context" : ""}`;
  element.textContent = shortLabel(text);
  element.title = String(text || "");
  element.dataset.kind = kind;
  element.setAttribute("aria-label", `Open ${kind} details: ${shortLabel(text, 80)}`);
  element.setAttribute("aria-controls", "detail");
  element.setAttribute("aria-expanded", "false");
  const label = new CSS2DObject(element);
  label.position.copy(position);
  label.userData.priority = priority;
  registerSelectionTarget(kind, item.id, label);
  element.addEventListener("pointerdown", (event) => event.stopPropagation());
  element.addEventListener("click", (event) => {
    event.stopPropagation();
    showDetails(kind, item);
  });
  register(label, layer);
  labels.push(label);
  return label;
}

function addLine(start, end, color, opacity, dashed, layer = "lines") {
  const geometry = new THREE.BufferGeometry().setFromPoints([start, end]);
  const material = dashed
    ? new THREE.LineDashedMaterial({ color, transparent: true, opacity, dashSize: 0.12, gapSize: 0.09 })
    : new THREE.LineBasicMaterial({ color, transparent: true, opacity });
  const line = new THREE.Line(geometry, material);
  if (dashed) line.computeLineDistances();
  register(line, layer);
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
  selectionTargets = new Map();
  positions = new Map();
  items = new Map();
  layerObjects = freshLayerObjects();
  pulseGlows = [];
}

function addPoint(point, position, baseRadius, showLabel, promoted) {
  const active = point.is_latest && point.status === "active";
  const clusterScale = promoted ? 1 : 0.72;
  const radius = (active ? baseRadius : baseRadius * 0.68) * clusterScale;
  const geometry = new THREE.SphereGeometry(radius, 14, 10);
  const material = new THREE.MeshStandardMaterial({
    color: COLORS.points,
    emissive: COLORS.points,
    emissiveIntensity: active ? (promoted ? 0.62 : 0.24) : 0.06,
    transparent: true,
    opacity: active ? (promoted ? 0.98 : 0.56) : (promoted ? 0.22 : 0.08),
    roughness: 0.26,
  });
  const mesh = registerPickable(new THREE.Mesh(geometry, material), "points", "point", point);
  mesh.position.copy(position);
  if (active && promoted && hash(`${point.id}:glow`) < 0.08) {
    addGlow(position, COLORS.points, radius * 5.4, 0.2, "points", 0.08);
  }
  if (showLabel) {
    addLabel(
      point.content,
      position.clone().add(new THREE.Vector3(0, radius + 0.16, 0)),
      "points",
      active ? 55 : 25,
      "point",
      point
    );
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
    addLabel(
      item.content,
      position.clone().add(new THREE.Vector3(0, 0.24, 0)),
      "points",
      30,
      "context",
      item,
      true
    );
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
  addGlow(position, COLORS.faces, 1.55, 0.22, "faces", 0.07);
  const node = registerPickable(
    new THREE.Mesh(
      new THREE.OctahedronGeometry(0.28, 0),
      new THREE.MeshStandardMaterial({
        color: COLORS.faces,
        emissive: COLORS.faces,
        emissiveIntensity: 0.58,
        roughness: 0.26,
      })
    ),
    "faces",
    "face",
    face
  );
  node.position.copy(position);
  if (showLabel) {
    addLabel(
      face.signature,
      position.clone().add(new THREE.Vector3(0, 0.4, 0)),
      "faces",
      75,
      "face",
      face
    );
  }
  memberPositions.forEach((member) => addLine(member, position, COLORS.hierarchy, 0.1, true));
}

function addVolume(volume, showLabel) {
  const position = positions.get(volume.id);
  if (!position) return;
  addGlow(position, COLORS.volumes, 2.35, 0.25, "volumes", 0.06);
  const mesh = registerPickable(
    new THREE.Mesh(
      new THREE.IcosahedronGeometry(0.48, 1),
      new THREE.MeshStandardMaterial({
        color: COLORS.volumes,
        emissive: COLORS.volumes,
        emissiveIntensity: 0.42,
        transparent: true,
        opacity: 0.52,
        wireframe: true,
      })
    ),
    "volumes",
    "volume",
    volume
  );
  mesh.position.copy(position);
  const core = register(
    new THREE.Mesh(
      new THREE.IcosahedronGeometry(0.17, 1),
      new THREE.MeshBasicMaterial({ color: COLORS.volumes, transparent: true, opacity: 0.68 })
    ),
    "volumes"
  );
  core.position.copy(position);
  if (showLabel) {
    addLabel(
      volume.signature,
      position.clone().add(new THREE.Vector3(0, 0.55, 0)),
      "volumes",
      90,
      "volume",
      volume
    );
  }
  const memberIds = currentLayout?.volumeFaceIds.get(volume.id) || [];
  memberIds.map((id) => positions.get(id)).filter(Boolean)
    .forEach((member) => addLine(member, position, COLORS.volumes, 0.22, false));
}

function addRoot(root) {
  const position = positions.get(root.id);
  if (!position) return;
  addGlow(position, COLORS.root, 4.2, 0.42, "root", 0.075);
  const mesh = registerPickable(
    new THREE.Mesh(
      new THREE.DodecahedronGeometry(0.62, 0),
      new THREE.MeshPhysicalMaterial({
        color: COLORS.root,
        emissive: COLORS.root,
        emissiveIntensity: 0.72,
        roughness: 0.18,
        metalness: 0.08,
        clearcoat: 1,
        clearcoatRoughness: 0.22,
      })
    ),
    "root",
    "root",
    root
  );
  mesh.position.copy(position);
  addLabel(
    root.signature,
    position.clone().add(new THREE.Vector3(0, 0.66, 0)),
    "root",
    120,
    "root",
    root
  );
  const parentIds = currentLayout?.rootVolumeIds || [];
  parentIds.map((id) => positions.get(id)).filter(Boolean)
    .forEach((member) => addLine(member, position, COLORS.root, 0.32, false));
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
  addLine(start, end, evolution ? COLORS.lines : 0x6ebf8e, opacity, !evolution);
}

function addOrbitRing(radius, color, opacity, rotation, layer) {
  const points = Array.from({ length: 129 }, (_, index) => {
    const angle = (index / 128) * Math.PI * 2;
    return new THREE.Vector3(Math.cos(angle) * radius, 0, Math.sin(angle) * radius);
  });
  const line = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(points),
    new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false })
  );
  line.rotation.set(...rotation);
  line.renderOrder = -2;
  register(line, layer);
}

function addGround() {
  const ringLayer = model.root ? "root" : (model.volumes.length ? "volumes" : "points");
  const radius = Math.max(5, layoutRadius);
  [0.34, 0.58, 0.84, 1.08].forEach((factor, index) => {
    addOrbitRing(
      radius * factor,
      index % 2 ? COLORS.volumes : COLORS.faces,
      Math.max(0.025, 0.075 - index * 0.012),
      [0.12 + index * 0.035, index * 0.18, 0.05 - index * 0.02],
      ringLayer
    );
  });
  addOrbitRing(radius * 0.72, COLORS.root, 0.055, [Math.PI / 2.8, 0.42, 0.18], ringLayer);
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
    generatedAt: modelGeneratedAt || null,
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
    ["points", "Points", stats.points || 0],
    ["lines", "Lines", (stats.evolution_lines || 0) + (stats.relation_lines || 0)],
    ["faces", "Faces", stats.faces || 0],
    ["volumes", "Volumes", stats.volumes || 0],
    ["root", "Root", stats.roots || 0],
  ];
  parts.forEach(([kind, label, value]) => {
    const stat = document.createElement("span");
    stat.className = "stat";
    stat.dataset.kind = kind;
    const count = document.createElement("b");
    count.textContent = String(value);
    const name = document.createElement("small");
    name.textContent = label;
    stat.append(count, name);
    statusEl.append(stat);
  });
  const buildStatus = model.build?.status;
  if (buildStatus) {
    const build = document.createElement("span");
    const buildStateClass = {
      not_built: "not-built",
      building: "building",
      degraded: "degraded",
      complete: "complete",
    }[buildStatus] || "not-built";
    build.className = `build-state build-state--${buildStateClass}`;
    build.dataset.status = buildStatus;
    const signal = document.createElement("i");
    signal.setAttribute("aria-hidden", "true");
    const label = buildStatus === "not_built"
      ? "Not built"
      : buildStatus === "building"
        ? "Building…"
        : `Build ${buildStatus}`;
    build.append(signal, label);
    statusEl.append(build);
  }
  modelIdentityEl.textContent = model.root?.signature
    || "A living map of what you notice, repeat, and become.";
}

function pauseAutoRotate() {
  if (!controls.autoRotate) return;
  controls.autoRotate = false;
  document.getElementById("rotate").setAttribute("aria-pressed", "false");
}

function showShareNotice(title, message, failed = false) {
  const heading = shareNoticeEl.querySelector("strong");
  const detail = shareNoticeEl.querySelector("small");
  heading.textContent = title;
  detail.textContent = message;
  shareNoticeEl.classList.toggle("failed", failed);
  shareNoticeEl.hidden = false;
  window.clearTimeout(showShareNotice.timer);
  showShareNotice.timer = window.setTimeout(() => {
    shareNoticeEl.hidden = true;
  }, 9000);
}

function setShareBusy(busy) {
  shareButton.disabled = busy || !shareReady;
  shareButton.setAttribute("aria-busy", String(busy));
  shareButton.querySelector("b").textContent = busy ? "Preparing…" : "Share";
}

function createShareCardBlob() {
  renderer.render(scene, camera);
  const canvas = document.createElement("canvas");
  canvas.width = SHARE_CARD_WIDTH;
  canvas.height = SHARE_CARD_HEIGHT;
  const context = canvas.getContext("2d");
  if (!context) return Promise.reject(new Error("Canvas export is unavailable"));
  drawShareCard(context, renderer.domElement, model);
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("The constellation image could not be encoded"));
    }, "image/png");
  });
}

function downloadShareCard(blob) {
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = SHARE_FILE_NAME;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(href), 1000);
}

function paintShareHandoff(popup) {
  if (!popup) return;
  popup.document.title = "Preparing your Persome constellation";
  popup.document.body.innerHTML = `
    <main style="min-height:100vh;display:grid;place-items:center;margin:0;background:#070610;color:#f7f4ff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
      <section style="width:min(440px,calc(100vw - 48px));padding:38px;border:1px solid rgba(255,255,255,.12);border-radius:24px;background:linear-gradient(145deg,rgba(255,100,214,.11),rgba(119,152,255,.08));box-shadow:0 30px 100px rgba(0,0,0,.45)">
        <p style="margin:0 0 18px;color:#ff83cf;font-size:11px;font-weight:750;letter-spacing:.16em">PERSOME · SHARE TO X</p>
        <h1 style="margin:0;font-size:34px;line-height:1.05;letter-spacing:-.045em">Your constellation is downloading.</h1>
        <p style="margin:18px 0 24px;color:#b8b1c7;font-size:15px;line-height:1.65">In X, click the image button and choose <strong style="color:#fff">${SHARE_FILE_NAME}</strong>. Your copy and tags are already filled in.</p>
        <div style="height:3px;overflow:hidden;border-radius:99px;background:rgba(255,255,255,.08)"><i style="display:block;width:100%;height:100%;transform-origin:left;background:linear-gradient(90deg,#ff64d6,#7798ff);animation:load 1.8s ease forwards"></i></div>
        <style>@keyframes load{from{transform:scaleX(0)}to{transform:scaleX(1)}}</style>
      </section>
    </main>`;
}

async function shareToX() {
  const popup = window.open("about:blank", "_blank");
  paintShareHandoff(popup);
  setShareBusy(true);
  pauseAutoRotate();
  try {
    const blob = await createShareCardBlob();
    downloadShareCard(blob);
    showShareNotice(
      "Constellation downloaded",
      `Add ${SHARE_FILE_NAME} with the image button in X.`,
    );
    const intentUrl = buildXIntentUrl();
    window.setTimeout(() => {
      if (popup && !popup.closed) {
        popup.opener = null;
        popup.location.replace(intentUrl);
        popup.focus();
      } else {
        window.location.assign(intentUrl);
      }
      setShareBusy(false);
    }, 1800);
  } catch (error) {
    if (popup && !popup.closed) popup.close();
    showShareNotice("Share image failed", error.message || String(error), true);
    setShareBusy(false);
  }
}

function syncSelectionState() {
  const activeKey = selected ? selectionKey(selected.kind, selected.id) : null;
  selectionTargets.forEach((targets, key) => {
    const active = key === activeKey;
    targets.forEach((target) => {
      if (target.element) {
        target.element.setAttribute("aria-expanded", String(active));
      } else if (target.isMesh) {
        target.scale.setScalar(active ? 1.32 : 1);
      }
    });
  });
  window.__persomeInteractionState = {
    linePickables: pickables.filter((object) => object.isLine).length,
    nodePickables: pickables.length,
    interactiveLabels: labels.filter((label) => Boolean(label.userData.ref)).length,
    selected: selected ? { ...selected } : null,
  };
}

function clearSelection() {
  evidenceRequest += 1;
  selected = null;
  selectedItem = null;
  detailMode = "node";
  detailEl.hidden = true;
  delete detailEl.dataset.kind;
  syncSelectionState();
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
  if (selected) {
    const key = selectionKey(selected.kind, selected.id);
    const layer = kindLayers[selected.kind];
    if (!items.has(key) || (layer && !layerVisible[layer])) {
      clearSelection();
      return;
    }
  }
  syncSelectionState();
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

function evidenceButton(reference, label = reference, relation = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "evidence-link";
  const relationEl = document.createElement("span");
  relationEl.textContent = relation ? relation.replaceAll("_", " ") : "Evidence";
  const labelEl = document.createElement("b");
  labelEl.textContent = label || reference;
  button.append(relationEl, labelEl);
  button.addEventListener("click", () => loadEvidence(reference));
  return button;
}

function appendEvidenceGroup(title, links, note = "") {
  if (!links?.length) return;
  const heading = document.createElement("strong");
  heading.textContent = title;
  detailReceiptsEl.appendChild(heading);
  if (note) {
    const copy = document.createElement("p");
    copy.className = "evidence-note";
    copy.textContent = note;
    detailReceiptsEl.appendChild(copy);
  }
  links.forEach((link) => {
    const label = link.label || link.reference || link.id;
    detailReceiptsEl.appendChild(
      evidenceButton(link.reference || link.id, label, link.relation),
    );
  });
}

function renderEvidence(data) {
  detailReceiptsEl.replaceChildren();
  const back = document.createElement("button");
  back.type = "button";
  back.className = "evidence-back";
  back.textContent = "← Back to node evidence";
  back.addEventListener("click", () => {
    if (selected && selectedItem) renderNodeEvidence(selected.kind, selectedItem);
  });
  detailReceiptsEl.appendChild(back);

  const heading = document.createElement("strong");
  heading.textContent = `${data.kind || "unknown"} · ${data.status || "unknown"}`;
  detailReceiptsEl.appendChild(heading);
  if (data.summary) {
    const summary = document.createElement("p");
    summary.className = "evidence-summary";
    summary.textContent = data.summary;
    detailReceiptsEl.appendChild(summary);
  }
  const facts = [
    data.timestamp ? `Time: ${data.timestamp}` : "",
    data.path ? `Path: ${data.path}` : "",
    data.canonical_reference ? `Receipt: ${data.canonical_reference}` : "",
  ].filter(Boolean);
  facts.forEach((fact) => {
    const row = document.createElement("div");
    row.className = "evidence-fact";
    row.textContent = fact;
    detailReceiptsEl.appendChild(row);
  });
  appendEvidenceGroup("Direct sources", data.sources);
  appendEvidenceGroup(
    "Nearby context",
    data.context,
    "These captures are close in time. They are investigation clues, not claimed direct proof.",
  );
  if (data.status === "missing") {
    const missing = document.createElement("p");
    missing.className = "evidence-note evidence-missing";
    missing.textContent = "The receipt is retained, but its local payload was not found or has expired.";
    detailReceiptsEl.appendChild(missing);
  }
}

async function loadEvidence(reference) {
  if (!selected || !reference) return;
  detailMode = "evidence";
  const request = ++evidenceRequest;
  detailReceiptsEl.replaceChildren();
  const loading = document.createElement("div");
  loading.className = "evidence-loading";
  loading.textContent = "Resolving evidence…";
  detailReceiptsEl.appendChild(loading);
  try {
    const response = await fetch(`./evidence?ref=${encodeURIComponent(reference)}`, {
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!selected || request !== evidenceRequest || detailMode !== "evidence") return;
    renderEvidence(data);
  } catch (error) {
    if (!selected || request !== evidenceRequest || detailMode !== "evidence") return;
    detailReceiptsEl.replaceChildren();
    const message = document.createElement("p");
    message.className = "evidence-note evidence-missing";
    message.textContent = `Unable to resolve evidence. ${error.message || error}`;
    detailReceiptsEl.appendChild(message);
  }
}

function renderNodeEvidence(kind, item) {
  detailMode = "node";
  evidenceRequest += 1;
  detailReceiptsEl.replaceChildren();

  const receipts = receiptValues(item);
  if (receipts.length) {
    const heading = document.createElement("strong");
    heading.textContent = "Evidence receipts";
    detailReceiptsEl.appendChild(heading);
    receipts.slice(0, 12).forEach((receipt) => {
      detailReceiptsEl.appendChild(evidenceButton(receipt, receipt, "direct_evidence"));
    });
  }

  if (kind === "point") {
    fetch(`./node?id=${encodeURIComponent(item.id)}`)
      .then((response) => response.ok ? response.json() : null)
      .then((data) => {
        if (!data || !selected || detailMode !== "node"
          || selected.kind !== kind || selected.id !== item.id) return;
        (data.raw || []).slice(0, 3).forEach((raw) => {
          const row = document.createElement("div");
          row.className = "evidence-preview";
          row.textContent = `${raw.ts ? `${String(raw.ts).slice(0, 16)}  ` : ""}${raw.text || ""}`;
          detailReceiptsEl.appendChild(row);
        });
      })
      .catch(() => {});
  }
}

function showDetails(kind, item) {
  selected = { kind, id: item.id };
  selectedItem = item;
  pauseAutoRotate();
  syncSelectionState();
  detailEl.dataset.kind = kind;
  detailKindEl.textContent = kind;
  detailTitleEl.textContent = item.content || item.signature || item.label || item.id;
  detailMetaEl.replaceChildren();
  appendMeta("ID", item.id);
  appendMeta("Layer", item.layer || item.level);
  appendMeta("Status", item.status);
  appendMeta("Predicate", item.label || item.predicate);
  appendMeta("Confidence", item.confidence);
  appendMeta("Observations", item.observations);
  appendMeta("Valid from", item.valid_from);
  appendMeta("Members", item.members?.length);
  renderNodeEvidence(kind, item);
  detailEl.hidden = false;
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
    const response = await fetch("./graph", { cache: "no-store" });
    if (!response.ok) throw new Error(`Model endpoint returned HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload.model || !Array.isArray(payload.model.points)) {
      throw new Error("Model endpoint returned an invalid snapshot");
    }
    const nextFingerprint = fingerprint(payload.model);
    if (!force && nextFingerprint === modelFingerprint) return;
    model = payload.model;
    modelGeneratedAt = payload.generated_at || "";
    modelFingerprint = nextFingerprint;
    shareReady = Boolean(
      model.points.length || model.faces.length || model.volumes.length || model.root,
    );
    setShareBusy(false);
    updateTimelineBounds();
    buildScene();
    errorEl.hidden = true;
  } catch (error) {
    shareReady = false;
    setShareBusy(false);
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
  fitDistance = distance;
  zoomGoalDistance = null;
  camera.position.copy(direction.multiplyScalar(distance));
  controls.target.set(0, 0, 0);
  controls.minDistance = distance * 100 / ZOOM_MAX_PERCENT;
  controls.maxDistance = distance * 100 / ZOOM_MIN_PERCENT;
  framedRadius = radius;
  controls.update();
  syncZoomUI();
}

function resetCamera() {
  frameLayout(true);
}

function clampZoomPercent(value) {
  return zoomMath.clampPercent(value, ZOOM_MIN_PERCENT, ZOOM_MAX_PERCENT);
}

function currentZoomPercent() {
  const distance = camera.position.distanceTo(controls.target);
  return zoomMath.percentForDistance(
    fitDistance,
    distance,
    ZOOM_MIN_PERCENT,
    ZOOM_MAX_PERCENT,
  );
}

function syncZoomUI() {
  const distance = camera.position.distanceTo(controls.target);
  const percent = currentZoomPercent();
  if (percent !== lastZoomPercent) {
    zoomResetButton.textContent = `${percent}%`;
    zoomResetButton.setAttribute(
      "aria-label",
      `Reset zoom to 100 percent (currently ${percent} percent)`,
    );
    lastZoomPercent = percent;
  }
  zoomOutButton.disabled = percent <= ZOOM_MIN_PERCENT;
  zoomInButton.disabled = percent >= ZOOM_MAX_PERCENT;
  window.__persomeZoomState = {
    percent,
    distance: Number(distance.toFixed(3)),
    fitDistance: Number(fitDistance.toFixed(3)),
    minPercent: ZOOM_MIN_PERCENT,
    maxPercent: ZOOM_MAX_PERCENT,
    animating: zoomGoalDistance !== null,
  };
}

function requestZoom(percent) {
  const clamped = clampZoomPercent(percent);
  zoomGoalDistance = THREE.MathUtils.clamp(
    fitDistance * 100 / clamped,
    controls.minDistance,
    controls.maxDistance,
  );
}

function stepZoom(direction) {
  const current = zoomGoalDistance === null
    ? currentZoomPercent()
    : zoomMath.percentForDistance(
      fitDistance,
      zoomGoalDistance,
      ZOOM_MIN_PERCENT,
      ZOOM_MAX_PERCENT,
    );
  requestZoom(zoomMath.nextPercent(
    current,
    direction,
    ZOOM_STEP_PERCENT,
    ZOOM_MIN_PERCENT,
    ZOOM_MAX_PERCENT,
  ));
}

function applyZoomAnimation(deltaSeconds) {
  if (zoomGoalDistance === null) return;
  const currentDistance = camera.position.distanceTo(controls.target);
  const nextDistance = REDUCED_MOTION
    ? zoomGoalDistance
    : THREE.MathUtils.damp(currentDistance, zoomGoalDistance, 12, deltaSeconds);
  zoomDirection.copy(camera.position).sub(controls.target);
  if (zoomDirection.lengthSq() < 1e-8) zoomDirection.set(0, 0, 1);
  zoomDirection.setLength(nextDistance);
  camera.position.copy(controls.target).add(zoomDirection);
  if (Math.abs(nextDistance - zoomGoalDistance) < 0.01) {
    zoomDirection.setLength(zoomGoalDistance);
    camera.position.copy(controls.target).add(zoomDirection);
    zoomGoalDistance = null;
  }
}

function cullLabels() {
  const occupied = [...document.querySelectorAll(".story, .legend, .status, .timeline, .detail:not([hidden])")]
    .map((element) => element.getBoundingClientRect())
    .filter((box) => box.width > 0 && box.height > 0)
    .map((box) => ({ x0: box.left - 6, x1: box.right + 6, y0: box.top - 6, y1: box.bottom + 6 }));
  const mobile = window.innerWidth < 760;
  const maxLabels = mobile ? 8 : 20;
  const safeArea = {
    left: mobile ? 8 : 6,
    right: window.innerWidth - (mobile ? 8 : 6),
    top: mobile ? 144 : 104,
    bottom: window.innerHeight - (mobile ? 70 : 64),
  };
  let shown = 0;
  const projected = new THREE.Vector3();
  const candidates = labels.filter((label) => label.visible).map((label) => {
    label.getWorldPosition(projected);
    projected.project(camera);
    const x = (projected.x * 0.5 + 0.5) * window.innerWidth;
    const y = (-projected.y * 0.5 + 0.5) * window.innerHeight;
    const maxWidth = mobile ? 130 : 220;
    const fallbackWidth = (label.element.textContent.length * 6.2) + 16;
    const width = Math.min(maxWidth, Math.max(32, label.element.offsetWidth || fallbackWidth));
    const height = Math.max(21, label.element.offsetHeight || 21);
    return { label, x, y, width, height, priority: label.userData.priority || 0, depth: projected.z };
  }).sort((a, b) => b.priority - a.priority);

  candidates.forEach((candidate) => {
    const { label, x, y, width, height, depth } = candidate;
    const box = { x0: x - width / 2, x1: x + width / 2, y0: y - height / 2, y1: y + height / 2 };
    const overlaps = occupied.some((other) => box.x0 < other.x1 && box.x1 > other.x0 && box.y0 < other.y1 && box.y1 > other.y0);
    const outsideSafeArea = box.x0 < safeArea.left || box.x1 > safeArea.right
      || box.y0 < safeArea.top || box.y1 > safeArea.bottom;
    const hidden = depth < -1 || depth > 1 || outsideSafeArea || overlaps || shown >= maxLabels;
    if (label.element.classList.contains("hidden") !== hidden) {
      label.element.classList.toggle("hidden", hidden);
      label.element.setAttribute("aria-hidden", String(hidden));
      label.element.tabIndex = hidden ? -1 : 0;
    }
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
zoomOutButton.addEventListener("click", () => stepZoom(-1));
zoomResetButton.addEventListener("click", () => requestZoom(100));
zoomInButton.addEventListener("click", () => stepZoom(1));
controls.addEventListener("start", () => {
  zoomGoalDistance = null;
});
document.getElementById("reset").addEventListener("click", resetCamera);
document.getElementById("close-detail").addEventListener("click", clearSelection);
shareButton.addEventListener("click", shareToX);

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

function pickAt(event) {
  const bounds = renderer.domElement.getBoundingClientRect();
  if (!bounds.width || !bounds.height) return null;
  pointer.x = ((event.clientX - bounds.left) / bounds.width) * 2 - 1;
  pointer.y = -((event.clientY - bounds.top) / bounds.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  return raycaster.intersectObjects(pickables.filter((object) => object.visible), false)[0] || null;
}

renderer.domElement.addEventListener("pointerdown", (event) => {
  pointerDown = { x: event.clientX, y: event.clientY };
  hoverDirty = false;
  renderer.domElement.style.cursor = "grabbing";
});
renderer.domElement.addEventListener("pointermove", (event) => {
  if (pointerDown) return;
  hoverPointer = { clientX: event.clientX, clientY: event.clientY };
  hoverDirty = true;
});
renderer.domElement.addEventListener("pointerleave", () => {
  hoverPointer = null;
  hoverDirty = false;
  if (!pointerDown) renderer.domElement.style.cursor = "grab";
});
renderer.domElement.addEventListener("pointercancel", () => {
  pointerDown = null;
  hoverPointer = null;
  hoverDirty = false;
  renderer.domElement.style.cursor = "grab";
});
renderer.domElement.addEventListener("pointerup", (event) => {
  if (!pointerDown) return;
  const movement = Math.abs(event.clientX - pointerDown.x) + Math.abs(event.clientY - pointerDown.y);
  pointerDown = null;
  if (movement > 5) {
    renderer.domElement.style.cursor = "grab";
    return;
  }
  const hit = pickAt(event);
  renderer.domElement.style.cursor = hit ? "pointer" : "grab";
  if (!hit?.object.userData.ref) {
    clearSelection();
    return;
  }
  const ref = hit.object.userData.ref;
  const item = items.get(selectionKey(ref.kind, ref.id));
  if (item) showDetails(ref.kind, item);
});

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && selected) {
    clearSelection();
    return;
  }
  const target = event.target;
  if (
    target instanceof HTMLInputElement
    || target instanceof HTMLTextAreaElement
    || target instanceof HTMLSelectElement
    || target?.isContentEditable
    || event.metaKey
    || event.ctrlKey
    || event.altKey
  ) return;
  if (event.key === "+" || event.key === "=") {
    event.preventDefault();
    stepZoom(1);
  } else if (event.key === "-" || event.key === "_") {
    event.preventDefault();
    stepZoom(-1);
  } else if (event.key === "0") {
    event.preventDefault();
    requestZoom(100);
  }
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

function animate(frameTime = performance.now()) {
  const deltaSeconds = Math.min(Math.max((frameTime - lastFrameTime) / 1000, 0), 0.5);
  lastFrameTime = frameTime;
  applyZoomAnimation(deltaSeconds);
  controls.update();
  syncZoomUI();
  const time = performance.now() * 0.001;
  if (!REDUCED_MOTION) {
    pulseGlows.forEach((glow) => {
      const pulse = 1 + Math.sin(time * 1.2 + glow.userData.glowPhase) * glow.userData.glowPulse;
      glow.scale.setScalar(glow.userData.glowBase * pulse);
    });
  }
  if (hoverDirty && hoverPointer && !pointerDown) {
    renderer.domElement.style.cursor = pickAt(hoverPointer) ? "pointer" : "grab";
    hoverDirty = false;
  }
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
