import assert from "node:assert/strict";
import test from "node:test";

import {
  SHARE_CARD_HEIGHT,
  SHARE_CARD_WIDTH,
  SHARE_FILE_NAME,
  SHARE_TEXTS,
  buildXIntentUrl,
  humanCard,
} from "../../resources/model_assets/share.mjs";

test("keeps the HUMAN.md Card portrait-sized and portable", () => {
  assert.equal(SHARE_CARD_WIDTH, 1080);
  assert.equal(SHARE_CARD_HEIGHT, 1350);
  assert.equal(SHARE_FILE_NAME, "my-human-card.png");
});

test("uses only signatures from the server share projection", () => {
  const card = humanCard({
    root: {
      signature: "turning personal context into agency",
      human_card: {
        current_root: "raw owner-only copy must be ignored",
      },
    },
  });

  assert.deepEqual(card, {
    optimizesFor: "depth over speed",
    currentRoot: "turning personal context into agency",
    decisionStyle: "evidence first, intuition at the edge",
    aiShould: "challenge premature expansion",
    neverExpose: "private source content",
  });
});

test("retains the approved X composer handoff for the HUMAN.md Card", () => {
  const intent = new URL(buildXIntentUrl({ random: () => 0 }));

  assert.equal(SHARE_TEXTS.length, 3);
  assert.equal(intent.origin, "https://x.com");
  assert.equal(intent.pathname, "/intent/tweet");
  assert.equal(intent.searchParams.get("text"), SHARE_TEXTS[0]);
  assert.equal(intent.searchParams.get("url"), "https://github.com/Intuition-Lab/personal-model");
  assert.ok(!intent.href.includes("localhost"));
});

test("falls back to bounded high-level summaries without leaking sources", () => {
  const card = humanCard({
    root: {
      id: "private-root-id",
      signature: "Turning personal context into agency.",
      source_receipts: ["receipt-secret"],
      path: "/Users/alice/private/root.json",
    },
    faces: [
      { id: "face-b", signature: "Evidence first.", observations: 4, quote: "private quote" },
      { id: "face-a", signature: "Depth over speed.", observations: 9, source_receipts: ["secret"] },
      { id: "face-c", signature: "Challenge premature expansion.", observations: 2 },
    ],
    points: [{ content: "PRIVATE SOURCE CONTENT" }],
    receipts: [{ quote: "PRIVATE RECEIPT" }],
  });

  assert.deepEqual(card, {
    optimizesFor: "Depth over speed.",
    currentRoot: "Turning personal context into agency.",
    decisionStyle: "Evidence first.",
    aiShould: "Challenge premature expansion.",
    neverExpose: "private source content",
  });
  const serialized = JSON.stringify(card);
  assert.ok(!serialized.includes("private-root-id"));
  assert.ok(!serialized.includes("receipt-secret"));
  assert.ok(!serialized.includes("/Users/alice"));
  assert.ok(!serialized.includes("PRIVATE"));
});

test("uses safe defaults while the model is still sparse", () => {
  assert.deepEqual(humanCard({}), {
    optimizesFor: "depth over speed",
    currentRoot: "still forming from local context",
    decisionStyle: "evidence first, intuition at the edge",
    aiShould: "challenge premature expansion",
    neverExpose: "private source content",
  });
});
