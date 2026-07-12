const TAU = Math.PI * 2;
const POINT_SPACING = 0.27;
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

function stableHash(text) {
  let value = 2166136261;
  const input = String(text || "");
  for (let index = 0; index < input.length; index += 1) {
    value ^= input.charCodeAt(index);
    value = Math.imul(value, 16777619);
  }
  return (value >>> 0) / 4294967296;
}

function stableTime(item) {
  return String(item?.valid_from || item?.created_at || item?.occurred_at || "");
}

function stableItems(items) {
  return [...items].sort((left, right) => {
    const byTime = stableTime(left).localeCompare(stableTime(right));
    return byTime || String(left.id || "").localeCompare(String(right.id || ""));
  });
}

function add(left, right) {
  return [left[0] + right[0], left[1] + right[1], left[2] + right[2]];
}

function subtract(left, right) {
  return [left[0] - right[0], left[1] - right[1], left[2] - right[2]];
}

function scale(vector, amount) {
  return [vector[0] * amount, vector[1] * amount, vector[2] * amount];
}

function magnitude(vector) {
  return Math.hypot(vector[0], vector[1], vector[2]);
}

function normalize(vector) {
  const length = magnitude(vector);
  return length > 0.000001 ? scale(vector, 1 / length) : [1, 0, 0];
}

function distance(left, right) {
  return magnitude(subtract(left, right));
}

function unitVector(key) {
  const azimuth = stableHash(`${key}:azimuth`) * TAU;
  const y = (stableHash(`${key}:height`) - 0.5) * 1.7;
  const horizontal = Math.sqrt(Math.max(0, 1 - y * y));
  return [Math.cos(azimuth) * horizontal, y, Math.sin(azimuth) * horizontal];
}

function placeStableOrbit(items, positions, options) {
  stableItems(items).forEach((item, index) => {
    const angle = options.phase + index * GOLDEN_ANGLE;
    const radius = options.radius + (index % 3) * options.radialStep;
    const height = ((index % 5) - 2) * options.heightStep;
    positions.set(item.id, [Math.cos(angle) * radius, height, Math.sin(angle) * radius]);
  });
}

function receiptSet(item, field = "source_receipts") {
  return new Set(Array.isArray(item?.[field]) ? item[field].filter(Boolean) : []);
}

function intersectionRatio(left, right) {
  if (!left.size) return 0;
  let overlap = 0;
  left.forEach((value) => {
    if (right.has(value)) overlap += 1;
  });
  return overlap / left.size;
}

function resolveFacePoints(faces, points) {
  const pointByReceipt = new Map(points.filter((point) => point.receipt).map((point) => [point.receipt, point.id]));
  const result = new Map();
  faces.forEach((face) => {
    const receipts = face.member_receipts?.length ? face.member_receipts : face.source_receipts || [];
    const ids = receipts.map((receipt) => pointByReceipt.get(receipt)).filter(Boolean);
    result.set(face.id, [...new Set(ids)]);
  });
  return result;
}

function resolveVolumeFaces(volumes, faces) {
  // Volume members can be internal schema keys; inherited receipts recover the public Face links.
  const faceIds = new Set(faces.map((face) => face.id));
  const faceReceipts = new Map(faces.map((face) => [face.id, receiptSet(face)]));
  const result = new Map();
  const scores = new Map();

  volumes.forEach((volume) => {
    const volumeReceipts = receiptSet(volume);
    const resolved = new Set((volume.members || []).filter((member) => faceIds.has(member)));
    const ranked = faces.map((face) => {
      const score = intersectionRatio(faceReceipts.get(face.id), volumeReceipts);
      scores.set(`${volume.id}:${face.id}`, score);
      if (score >= 0.98) resolved.add(face.id);
      return { id: face.id, score };
    }).sort((left, right) => right.score - left.score || left.id.localeCompare(right.id));

    if (!resolved.size && ranked[0]?.score > 0) {
      const floor = Math.max(0.45, ranked[0].score * 0.86);
      ranked.filter((entry) => entry.score >= floor).slice(0, 2).forEach((entry) => resolved.add(entry.id));
    }
    result.set(volume.id, [...resolved]);
  });
  return { result, scores };
}

function itemStrength(item) {
  return Number(item?.observations || 0) * 2 + Number(item?.confidence || 0);
}

function choosePrimaryVolumes(faces, volumes, volumeFaceIds, scores) {
  const volumeById = new Map(volumes.map((volume) => [volume.id, volume]));
  const candidates = new Map(faces.map((face) => [face.id, []]));
  const loads = new Map(volumes.map((volume) => [volume.id, 0]));
  volumeFaceIds.forEach((faceIds, volumeId) => {
    faceIds.forEach((faceId) => candidates.get(faceId)?.push(volumeId));
  });

  const result = new Map();
  candidates.forEach((volumeIds, faceId) => {
    volumeIds.sort((leftId, rightId) => {
      const byScore = (scores.get(`${rightId}:${faceId}`) || 0) - (scores.get(`${leftId}:${faceId}`) || 0);
      if (Math.abs(byScore) > 0.000001) return byScore;
      const byLoad = (loads.get(leftId) || 0) - (loads.get(rightId) || 0);
      if (byLoad) return byLoad;
      const byStrength = itemStrength(volumeById.get(rightId)) - itemStrength(volumeById.get(leftId));
      return byStrength || leftId.localeCompare(rightId);
    });
    if (volumeIds[0]) {
      result.set(faceId, volumeIds[0]);
      loads.set(volumeIds[0], (loads.get(volumeIds[0]) || 0) + 1);
    }
  });
  return result;
}

function placeFaceCenters(faces, primaryVolumeByFace, positions) {
  const placed = [];
  const siblingsByVolume = new Map();
  stableItems(faces).forEach((face) => {
    const volumeId = primaryVolumeByFace.get(face.id);
    if (!volumeId) return;
    if (!siblingsByVolume.has(volumeId)) siblingsByVolume.set(volumeId, []);
    siblingsByVolume.get(volumeId).push(face.id);
  });
  stableItems(faces).forEach((face) => {
    const volumeId = primaryVolumeByFace.get(face.id);
    const parent = positions.get(volumeId);
    const siblings = siblingsByVolume.get(volumeId) || [face.id];
    const siblingIndex = siblings.indexOf(face.id);
    const fanOffset = (siblingIndex - (siblings.length - 1) / 2) * 0.48;
    const radial = parent ? normalize([parent[0], 0, parent[2]]) : normalize([
      unitVector(`face:${face.id}:radial`)[0],
      0,
      unitVector(`face:${face.id}:radial`)[2],
    ]);
    const baseAngle = Math.atan2(radial[2], radial[0]);
    let candidate = null;
    for (let attempt = 0; attempt < 48; attempt += 1) {
      const shell = 4.65 + Math.floor(attempt / 12) * 0.42;
      const angle = baseAngle + fanOffset + (stableHash(`face:${face.id}:angle:${attempt}`) - 0.5) * 0.14;
      const height = fanOffset * 0.5 + (stableHash(`face:${face.id}:height:${attempt}`) - 0.5) * 0.9;
      const proposed = [Math.cos(angle) * shell, height, Math.sin(angle) * shell];
      if (placed.every((other) => distance(proposed, other) >= 1.42)) {
        candidate = proposed;
        break;
      }
    }
    if (!candidate) candidate = [radial[0] * 6.2, 0, radial[2] * 6.2];
    positions.set(face.id, candidate);
    placed.push(candidate);
  });
}

function preferredFace(leftId, rightId, faceById) {
  if (!leftId) return rightId;
  if (!rightId) return leftId;
  const byStrength = itemStrength(faceById.get(rightId)) - itemStrength(faceById.get(leftId));
  return byStrength > 0 || (byStrength === 0 && rightId < leftId) ? rightId : leftId;
}

function assignPointClusters(points, lines, faces, facePointIds) {
  const pointById = new Map(points.map((point) => [point.id, point]));
  const faceById = new Map(faces.map((face) => [face.id, face]));
  const directFaceByPoint = new Map();
  const directPointIds = new Set();
  facePointIds.forEach((pointIds, faceId) => {
    pointIds.forEach((pointId) => {
      directPointIds.add(pointId);
      directFaceByPoint.set(pointId, preferredFace(directFaceByPoint.get(pointId), faceId, faceById));
    });
  });

  const parent = new Map(points.map((point) => [point.id, point.id]));
  // Historical revisions stay with the Face reached by any member of their evolution chain.
  const find = (id) => {
    let root = id;
    while (parent.get(root) !== root) root = parent.get(root);
    let current = id;
    while (parent.get(current) !== current) {
      const next = parent.get(current);
      parent.set(current, root);
      current = next;
    }
    return root;
  };
  const union = (left, right) => {
    const leftRoot = find(left);
    const rightRoot = find(right);
    if (leftRoot === rightRoot) return;
    const keep = leftRoot < rightRoot ? leftRoot : rightRoot;
    const merge = leftRoot < rightRoot ? rightRoot : leftRoot;
    parent.set(merge, keep);
  };

  lines.filter((line) => line.kind === "evolution").forEach((line) => {
    if (pointById.has(line.source) && pointById.has(line.target)) union(line.source, line.target);
  });

  const components = new Map();
  points.forEach((point) => {
    const root = find(point.id);
    if (!components.has(root)) components.set(root, []);
    components.get(root).push(point.id);
  });

  const faceByPoint = new Map(directFaceByPoint);
  components.forEach((pointIds) => {
    const counts = new Map();
    pointIds.forEach((pointId) => {
      const faceId = directFaceByPoint.get(pointId);
      if (faceId) counts.set(faceId, (counts.get(faceId) || 0) + 1);
    });
    const dominant = [...counts].sort((left, right) => {
      const byCount = right[1] - left[1];
      if (byCount) return byCount;
      return preferredFace(left[0], right[0], faceById) === right[0] ? 1 : -1;
    })[0]?.[0];
    if (dominant) pointIds.forEach((pointId) => {
      if (!faceByPoint.has(pointId)) faceByPoint.set(pointId, dominant);
    });
  });

  const fileVotes = new Map();
  // Same-source evidence inherits the strongest Face already established for that source.
  faceByPoint.forEach((faceId, pointId) => {
    const file = String(pointById.get(pointId)?.file_name || "").trim();
    if (!file) return;
    if (!fileVotes.has(file)) fileVotes.set(file, new Map());
    const votes = fileVotes.get(file);
    votes.set(faceId, (votes.get(faceId) || 0) + 1);
  });
  const fileFaces = new Map();
  fileVotes.forEach((votes, file) => {
    const faceId = [...votes].sort((left, right) => {
      const byCount = right[1] - left[1];
      if (byCount) return byCount;
      return preferredFace(left[0], right[0], faceById) === right[0] ? 1 : -1;
    })[0]?.[0];
    if (faceId) fileFaces.set(file, faceId);
  });

  points.forEach((point) => {
    if (faceByPoint.has(point.id)) return;
    const file = String(point.file_name || "").trim();
    if (fileFaces.has(file)) faceByPoint.set(point.id, fileFaces.get(file));
  });

  const pointClusterById = new Map();
  points.forEach((point) => {
    const faceId = faceByPoint.get(point.id);
    const source = sourceFamily(point);
    pointClusterById.set(point.id, faceId ? `face:${faceId}` : `source:${source}`);
  });
  return { pointClusterById, directPointIds, faceByPoint };
}

function sourceFamily(point) {
  const raw = String(point.file_name || point.tags || "unmodeled").trim();
  if (!raw) return "unmodeled";
  const base = raw.split(/[\\/]/).pop().replace(/\.[^.]+$/, "").toLowerCase();
  const parts = base.split(/[-_]/).filter(Boolean);
  if (parts[0] === "schema" && parts[1]) return `schema-${parts[1]}`;
  return parts[0] || "unmodeled";
}

function placePointClouds(points, pointClusterById, positions, sourceOrbitRadius) {
  const groups = new Map();
  points.forEach((point) => {
    const cluster = pointClusterById.get(point.id);
    if (!groups.has(cluster)) groups.set(cluster, []);
    groups.get(cluster).push(point);
  });

  const sourceGroups = [...groups].filter(([cluster]) => cluster.startsWith("source:")).map(([cluster, members]) => ({
    id: cluster,
    created_at: stableItems(members)[0] ? stableTime(stableItems(members)[0]) : "",
  }));
  placeStableOrbit(sourceGroups, positions, {
    phase: 0.68,
    radius: sourceOrbitRadius,
    radialStep: 0.38,
    heightStep: 0.38,
  });

  groups.forEach((members, cluster) => {
    const faceId = cluster.startsWith("face:") ? cluster.slice(5) : null;
    const center = positions.get(faceId || cluster) || [0, 0, 0];
    stableItems(members).forEach((point, index) => {
      const radius = 0.22 + POINT_SPACING * Math.cbrt(index + 1);
      const offset = scale(unitVector(`point:${point.id}`), radius);
      positions.set(point.id, add(center, offset));
    });
  });
  return groups;
}

function placeContextNodes(lines, pointIds, positions) {
  const ids = new Set();
  lines.filter((line) => line.kind === "relation").forEach((line) => {
    if (!pointIds.has(line.source)) ids.add(line.source);
    if (!pointIds.has(line.target)) ids.add(line.target);
  });
  const contextIds = [...ids].filter(Boolean).sort();
  contextIds.forEach((id) => {
    const radius = 0.72 + stableHash(`context:${id}:radius`) * 0.68;
    positions.set(id, scale(unitVector(`context:${id}`), radius));
  });
  return contextIds;
}

function averageRadius(ids, positions) {
  const values = ids.map((id) => positions.get(id)).filter(Boolean).map(magnitude);
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
}

function layoutBounds(positions) {
  const values = [...positions.values()];
  if (!values.length) return { min: [0, 0, 0], max: [0, 0, 0], size: [0, 0, 0], radius: 1 };
  const min = [...values[0]];
  const max = [...values[0]];
  values.forEach((value) => {
    for (let axis = 0; axis < 3; axis += 1) {
      min[axis] = Math.min(min[axis], value[axis]);
      max[axis] = Math.max(max[axis], value[axis]);
    }
  });
  return {
    min,
    max,
    size: max.map((value, axis) => value - min[axis]),
    radius: Math.max(1, ...values.map(magnitude)) + 0.6,
  };
}

export function computeClusterLayout(model) {
  const points = stableItems(model.points || []);
  const lines = model.lines || [];
  const faces = stableItems(model.faces || []);
  const volumes = stableItems(model.volumes || []);
  const root = model.root || null;
  const positions = new Map();

  const facePointIds = resolveFacePoints(faces, points);
  const resolvedVolumes = resolveVolumeFaces(volumes, faces);
  const volumeFaceIds = resolvedVolumes.result;
  const primaryVolumeByFace = choosePrimaryVolumes(faces, volumes, volumeFaceIds, resolvedVolumes.scores);

  if (root) positions.set(root.id, [0, 0, 0]);
  placeStableOrbit(volumes, positions, {
    phase: 0.2,
    radius: 2,
    radialStep: 0.12,
    heightStep: 0.14,
  });
  placeFaceCenters(faces, primaryVolumeByFace, positions);

  const assignments = assignPointClusters(points, lines, faces, facePointIds);
  const sourceOrbitRadius = faces.length ? 7.2 : (volumes.length || root ? 4.2 : 1.2);
  const groups = placePointClouds(points, assignments.pointClusterById, positions, sourceOrbitRadius);
  const pointIds = new Set(points.map((point) => point.id));
  const contextIds = placeContextNodes(lines, pointIds, positions);

  const volumeIds = new Set(volumes.map((volume) => volume.id));
  const rootVolumeIds = root?.members?.filter((id) => volumeIds.has(id)) || [];
  if (root && !rootVolumeIds.length) rootVolumeIds.push(...volumes.map((volume) => volume.id));

  const bounds = layoutBounds(positions);
  const assignedToFace = [...assignments.faceByPoint.keys()].length;
  const primaryVolumeLoads = new Map();
  primaryVolumeByFace.forEach((volumeId) => {
    primaryVolumeLoads.set(volumeId, (primaryVolumeLoads.get(volumeId) || 0) + 1);
  });
  const pointPositions = points.map((point) => positions.get(point.id)).filter(Boolean);
  const pointY = pointPositions.map((position) => position[1]);
  const pointRadius = Math.max(0.085, Math.min(0.16, 0.17 - Math.log10(Math.max(points.length, 1)) * 0.024));

  return {
    positions,
    facePointIds,
    volumeFaceIds,
    rootVolumeIds,
    pointClusterById: assignments.pointClusterById,
    directPointIds: assignments.directPointIds,
    contextIds,
    pointRadius,
    diagnostics: {
      version: "hierarchical-cluster-v1",
      rootAtCenter: !root || magnitude(positions.get(root.id) || [0, 0, 0]) < 0.000001,
      clusters: groups.size,
      directPoints: assignments.directPointIds.size,
      inferredPoints: Math.max(0, assignedToFace - assignments.directPointIds.size),
      sourceClusterPoints: Math.max(0, points.length - assignedToFace),
      usedVolumeClusters: primaryVolumeLoads.size,
      maxFacesPerVolume: Math.max(0, ...primaryVolumeLoads.values()),
      faceMembershipEdges: [...facePointIds.values()].reduce((sum, ids) => sum + ids.length, 0),
      volumeMembershipEdges: [...volumeFaceIds.values()].reduce((sum, ids) => sum + ids.length, 0),
      averageRadius: {
        volumes: averageRadius(volumes.map((volume) => volume.id), positions),
        faces: averageRadius(faces.map((face) => face.id), positions),
        points: averageRadius(points.map((point) => point.id), positions),
      },
      pointYSpread: pointY.length ? Math.max(...pointY) - Math.min(...pointY) : 0,
      bounds,
    },
  };
}

export const layoutMath = { distance, magnitude, stableHash };

function clampZoomPercent(value, minPercent = 50, maxPercent = 400) {
  return Math.max(minPercent, Math.min(maxPercent, Math.round(value)));
}

function zoomPercentForDistance(
  fitDistance,
  distance,
  minPercent = 50,
  maxPercent = 400,
) {
  if (!Number.isFinite(distance) || distance <= 0 || fitDistance <= 0) return 100;
  return clampZoomPercent((fitDistance / distance) * 100, minPercent, maxPercent);
}

function nextZoomPercent(
  currentPercent,
  direction,
  stepPercent = 25,
  minPercent = 50,
  maxPercent = 400,
) {
  const snapped = direction > 0
    ? Math.floor(currentPercent / stepPercent) * stepPercent
    : Math.ceil(currentPercent / stepPercent) * stepPercent;
  return clampZoomPercent(
    snapped + direction * stepPercent,
    minPercent,
    maxPercent,
  );
}

export const zoomMath = {
  clampPercent: clampZoomPercent,
  nextPercent: nextZoomPercent,
  percentForDistance: zoomPercentForDistance,
};
