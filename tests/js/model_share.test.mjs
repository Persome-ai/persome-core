import assert from "node:assert/strict";
import test from "node:test";

import {
  SHARE_CARD_HEIGHT,
  SHARE_CARD_WIDTH,
  SHARE_FILE_NAME,
  SHARE_TEXTS,
  SHARE_URL,
  buildXIntentUrl,
  pickShareText,
  shareNarrative,
  shareStats,
} from "../../resources/model_assets/share.mjs";

const EXPECTED_SHARE_TEXTS = [
  [
    "I let @PersonalModel_ observe how I use my Mac, and apparently this is what I look like 😳",
    "",
    "#PersonalModel @PersonalModel_",
  ].join("\n"),
  [
    "I let my @PersonalModel_ learn from how I use my Mac. I didn’t expect this is how it sees me 😳",
    "",
    "#PersonalModel @PersonalModel_",
  ].join("\n"),
  [
    "I let @PersonalModel_ observe how I use my Mac, and this is the model it built 🤔",
    "",
    "#PersonalModel @PersonalModel_",
  ].join("\n"),
];

test("keeps the three approved X share variants verbatim", () => {
  assert.deepEqual(SHARE_TEXTS, EXPECTED_SHARE_TEXTS);
});

test("selects each X share variant from an equal third of the random range", () => {
  assert.equal(pickShareText(() => 0), EXPECTED_SHARE_TEXTS[0]);
  assert.equal(pickShareText(() => (1 / 3) - Number.EPSILON), EXPECTED_SHARE_TEXTS[0]);
  assert.equal(pickShareText(() => 1 / 3), EXPECTED_SHARE_TEXTS[1]);
  assert.equal(pickShareText(() => (2 / 3) - Number.EPSILON), EXPECTED_SHARE_TEXTS[1]);
  assert.equal(pickShareText(() => 2 / 3), EXPECTED_SHARE_TEXTS[2]);
  assert.equal(pickShareText(() => 0.999999), EXPECTED_SHARE_TEXTS[2]);
});

test("builds an X composer URL with a selected variant and destination", () => {
  const intent = new URL(buildXIntentUrl({ random: () => 0.5 }));

  assert.equal(intent.origin, "https://x.com");
  assert.equal(intent.pathname, "/intent/tweet");
  assert.equal(intent.searchParams.get("text"), EXPECTED_SHARE_TEXTS[1]);
  assert.equal(intent.searchParams.get("url"), SHARE_URL);
  assert.equal(intent.searchParams.get("hashtags"), null);
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
