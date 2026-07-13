function compactText(value, fallback = "Evidence", limit = 140) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return (text || fallback).slice(0, limit);
}

function parseReceipt(reference) {
  const value = String(reference || "").trim();
  if (!value.startsWith("⟨") || !value.endsWith("⟩")) {
    return { id: value, path: "" };
  }
  const inner = value.slice(1, -1);
  const separator = inner.lastIndexOf(":");
  if (separator < 1) return { id: inner, path: "" };
  return { id: inner.slice(0, separator), path: inner.slice(separator + 1) };
}

function humanizePath(path) {
  const name = String(path || "").split("/").pop().replace(/\.(md|json)$/i, "");
  return name
    .replaceAll("_", "-")
    .split("-")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function relationLabel(relation) {
  const labels = {
    direct_evidence: "Direct evidence",
    direct_lineage: "Derived from",
    direct_source: "Direct source",
    nearby_context: "Nearby context",
    previous_version: "Previous version",
    next_version: "Next version",
  };
  return labels[relation] || compactText(String(relation || "Evidence").replaceAll("_", " "));
}

export function receiptIndex(model) {
  const byReference = new Map();
  (model?.points || []).forEach((point) => {
    if (!point.receipt) return;
    byReference.set(point.receipt, {
      id: point.id,
      kind: "point",
      reference: point.receipt,
      relation: "direct_evidence",
      label: compactText(point.content, humanizePath(point.file_name) || "Observed fact"),
      timestamp: point.occurred_at || point.valid_from || point.created_at || null,
      status: point.status || null,
    });
  });
  return byReference;
}

export function nodeEvidenceCards(item, model) {
  const references = [
    item?.receipt,
    item?.source_evidence?.receipt,
    ...(item?.member_receipts || []),
    ...(item?.source_receipts || []),
  ].filter(Boolean);
  const index = receiptIndex(model);
  return [...new Set(references)].map((reference) => {
    const known = index.get(reference);
    if (known) return known;
    const parsed = parseReceipt(reference);
    return {
      id: parsed.id,
      kind: "receipt",
      reference,
      relation: "direct_evidence",
      label: humanizePath(parsed.path) || "Recorded evidence",
      timestamp: null,
      status: null,
    };
  });
}

export function nodeHistoryCards(item, model) {
  const byId = new Map((model?.points || []).map((point) => [point.id, point]));
  const links = [];
  [
    ["previous_version", item?.supersedes || []],
    ["next_version", item?.superseded_by || []],
  ].forEach(([relation, ids]) => {
    ids.forEach((id) => {
      const point = byId.get(id);
      links.push({
        id,
        kind: "point",
        reference: point?.receipt || id,
        relation,
        label: compactText(point?.content, "Recorded version"),
        timestamp: point?.occurred_at || point?.valid_from || point?.created_at || null,
        status: point?.status || null,
      });
    });
  });
  return links;
}

export function evidenceOverview(kind, item, model) {
  const cards = nodeEvidenceCards(item, model);
  const noun = cards.length === 1 ? "source observation" : "source observations";
  return {
    title: cards.length ? `${cards.length} ${noun}` : "No direct evidence receipts",
    copy: kind === "face" || kind === "volume" || kind === "root"
      ? "This model shape is summarized from the evidence below."
      : "Inspect the stored source and its provenance without changing the model.",
    highlights: cards.slice(0, 3),
  };
}

export function evidenceBreadcrumb(data) {
  return compactText(data?.label || data?.summary, humanizePath(data?.path) || data?.kind || "Evidence", 72);
}

function modelNodeLabel(id, model) {
  if (id === "self") return "You";
  const candidates = [
    ["point", model?.points || [], "Observed point"],
    ["face", model?.faces || [], "Model pattern"],
    ["volume", model?.volumes || [], "Model structure"],
    ["root", model?.root ? [model.root] : [], "Personal model"],
  ];
  for (const [, nodes, fallback] of candidates) {
    const node = nodes.find((item) => item.id === id);
    if (node) {
      return compactText(
        node.content || node.signature || node.label || node.title,
        fallback,
        88,
      );
    }
  }
  return "Context node";
}

export function linePresentation(line, model) {
  const fallbackPredicate = line?.kind === "evolution" ? "supersedes" : "relation";
  const predicate = compactText(line?.predicate, fallbackPredicate, 72);
  const label = String(line?.label || "").replace(/\s+/g, " ").trim().slice(0, 88);
  const source = modelNodeLabel(line?.source, model);
  const target = modelNodeLabel(line?.target, model);
  const title = label || predicate;
  return {
    title,
    predicate,
    label: label && label !== predicate ? label : "",
    source,
    target,
    option: `${title}: ${source} → ${target}`,
  };
}

export const evidenceText = { compactText, humanizePath, parseReceipt };
