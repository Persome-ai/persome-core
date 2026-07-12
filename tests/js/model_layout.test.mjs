import assert from "node:assert/strict";
import test from "node:test";

import {
  computeClusterLayout,
  layoutMath,
  zoomMath,
} from "../../resources/model_assets/layout.mjs";

function point(index, file = "project-runtime.md") {
  const id = `point-${String(index).padStart(3, "0")}`;
  return {
    id,
    receipt: `receipt:${id}`,
    file_name: file,
    created_at: `2026-07-01T10:${String(index).padStart(2, "0")}:00Z`,
    is_latest: true,
    status: "active",
  };
}

function face(id, members, createdAt) {
  const receipts = members.map((index) => `receipt:point-${String(index).padStart(3, "0")}`);
  return {
    id,
    member_receipts: receipts,
    source_receipts: receipts,
    observations: 4,
    confidence: 0.9,
    created_at: createdAt,
  };
}

function fixture(extraPoints = []) {
  const points = [
    ...Array.from({ length: 30 }, (_, index) => point(index)),
    ...Array.from({ length: 6 }, (_, index) => point(index + 30, "event-unmodeled.md")),
    ...extraPoints,
  ];
  const faces = [
    face("face-focus", [0, 1, 2, 3, 4, 5], "2026-07-01T11:00:00Z"),
    face("face-review", [10, 11, 12, 13, 14, 15], "2026-07-01T11:01:00Z"),
    face("face-collaboration", [20, 21, 22, 23, 24, 25], "2026-07-01T11:02:00Z"),
  ];
  const volumes = [
    {
      id: "volume-work",
      members: ["internal-focus", "internal-review"],
      source_receipts: [...faces[0].source_receipts, ...faces[1].source_receipts],
      observations: 5,
      confidence: 0.92,
      created_at: "2026-07-01T12:00:00Z",
    },
    {
      id: "volume-people",
      members: ["internal-collaboration"],
      source_receipts: [...faces[2].source_receipts],
      observations: 3,
      confidence: 0.88,
      created_at: "2026-07-01T12:01:00Z",
    },
  ];
  return {
    points,
    lines: [
      { id: "evolution-1", kind: "evolution", source: "point-006", target: "point-000" },
      { id: "evolution-2", kind: "evolution", source: "point-016", target: "point-010" },
      { id: "relation-1", kind: "relation", source: "self", target: "collaborator" },
    ],
    faces,
    volumes,
    root: { id: "root", members: volumes.map((volume) => volume.id) },
  };
}

test("lays the hierarchy out as centered, three-dimensional clusters", () => {
  const layout = computeClusterLayout(fixture());
  const root = layout.positions.get("root");

  assert.deepEqual(root, [0, 0, 0]);
  assert.equal(layout.diagnostics.rootAtCenter, true);
  assert.ok(layout.diagnostics.averageRadius.volumes > 1.5);
  assert.ok(layout.diagnostics.averageRadius.faces > layout.diagnostics.averageRadius.volumes);
  assert.ok(layout.diagnostics.pointYSpread > 0.75);
  assert.equal(layout.diagnostics.directPoints, 18);
  assert.equal(layout.diagnostics.volumeMembershipEdges, 3);
  assert.ok(layout.diagnostics.sourceClusterPoints >= 6);
  assert.ok(layout.contextIds.includes("self"));
  assert.ok(layoutMath.distance(layout.positions.get("face-focus"), root) > 3);
});

test("keeps existing coordinates stable as later evidence is appended", () => {
  const initialModel = fixture();
  const initial = computeClusterLayout(initialModel);
  const additions = Array.from({ length: 8 }, (_, index) => ({
    ...point(index + 40),
    created_at: `2026-07-02T10:${String(index).padStart(2, "0")}:00Z`,
  }));
  const grown = computeClusterLayout(fixture(additions));

  initialModel.points.forEach((item) => {
    assert.deepEqual(grown.positions.get(item.id), initial.positions.get(item.id));
  });
  initialModel.faces.forEach((item) => {
    assert.deepEqual(grown.positions.get(item.id), initial.positions.get(item.id));
  });
  initialModel.volumes.forEach((item) => {
    assert.deepEqual(grown.positions.get(item.id), initial.positions.get(item.id));
  });
});

test("keeps a point-only degraded model close to the center", () => {
  const layout = computeClusterLayout({
    points: [point(0, "event-first.md")],
    lines: [],
    faces: [],
    volumes: [],
    root: null,
  });

  assert.equal(layout.diagnostics.sourceClusterPoints, 1);
  assert.ok(layout.diagnostics.averageRadius.points < 2);
  assert.ok(layout.diagnostics.bounds.radius < 3);
});

test("steps fitted zoom predictably through rapid actions and clamps its range", () => {
  assert.equal(zoomMath.percentForDistance(12, 12), 100);
  assert.equal(zoomMath.percentForDistance(12, 24), 50);
  assert.equal(zoomMath.percentForDistance(12, 3), 400);
  assert.equal(zoomMath.percentForDistance(12, 120), 50);
  assert.equal(zoomMath.percentForDistance(12, 0.3), 400);

  const firstTap = zoomMath.nextPercent(100, 1);
  const rapidSecondTap = zoomMath.nextPercent(firstTap, 1);
  assert.equal(firstTap, 125);
  assert.equal(rapidSecondTap, 150);
  assert.equal(zoomMath.nextPercent(113, -1), 100);
  assert.equal(zoomMath.nextPercent(50, -1), 50);
  assert.equal(zoomMath.nextPercent(400, 1), 400);
});
