import assert from "node:assert/strict";
import test from "node:test";

import {
  SHARE_CARD_HEIGHT,
  SHARE_CARD_WIDTH,
  SHARE_FILE_NAME,
  SHARE_HASHTAGS,
  SHARE_TEXT,
  SHARE_URL,
  buildXIntentUrl,
  shareNarrative,
  shareStats,
} from "../../resources/model_assets/share.mjs";

test("builds an X composer URL with the standard copy, destination, and tags", () => {
  const intent = new URL(buildXIntentUrl());

  assert.equal(intent.origin, "https://x.com");
  assert.equal(intent.pathname, "/intent/tweet");
  assert.equal(intent.searchParams.get("text"), SHARE_TEXT);
  assert.equal(intent.searchParams.get("url"), SHARE_URL);
  assert.equal(intent.searchParams.get("hashtags"), SHARE_HASHTAGS.join(","));
  assert.ok(!intent.href.includes("localhost"));
  assert.ok(!intent.href.includes("/model/"));
});

test("keeps the share artifact fixed and portable", () => {
  assert.equal(SHARE_CARD_WIDTH, 1200);
  assert.equal(SHARE_CARD_HEIGHT, 675);
  assert.equal(SHARE_FILE_NAME, "my-persome-constellation.png");

  assert.deepEqual(
    shareStats({
      stats: {
        points: 424,
        evolution_lines: 120,
        relation_lines: 26,
        faces: 12,
        volumes: 4,
        roots: 1,
      },
    }),
    [
      ["POINTS", 424],
      ["LINES", 146],
      ["FACES", 12],
      ["VOLUMES", 4],
      ["ROOT", 1],
    ],
  );
});

test("selects a bounded personal summary and highest-level key patterns", () => {
  const narrative = shareNarrative({
    root: {
      signature: "A focused systems builder who turns context into auditable work.",
      receipts: ["private-root-receipt"],
    },
    volumes: [
      { id: "volume-low", signature: "Lower-confidence structure.", confidence: 0.7 },
      { id: "volume-high", signature: "Trusted personal AI connects context and correction.", confidence: 0.96 },
    ],
    faces: [
      { id: "face-high", signature: "Focused work starts with a small plan.", confidence: 0.99 },
      { id: "face-second", signature: "Research claims are checked against evidence.", confidence: 0.91 },
    ],
  });

  assert.equal(
    narrative.root,
    "A focused systems builder who turns context into auditable work.",
  );
  assert.deepEqual(narrative.highlights, [
    { kind: "VOLUME", text: "Trusted personal AI connects context and correction." },
    { kind: "VOLUME", text: "Lower-confidence structure." },
    { kind: "FACE", text: "Focused work starts with a small plan." },
  ]);
  assert.ok(!JSON.stringify(narrative).includes("private-root-receipt"));
});
