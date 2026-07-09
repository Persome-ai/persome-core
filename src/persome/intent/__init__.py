"""Unified intent mechanism (architecture §8: 通用运行时 + 可插拔场景能力包).

This package is the *mechanism* layer for online intent recognition. It defines
one canonical intent ontology, one write-back sink, and one recall contract that
every scenario (passive timeline, meeting, chat, …) plugs into — instead of each
recognizer inventing its own representation and storage.

Public surface:
- ``ontology.Intent`` — the single canonical intent representation.
- ``sink.persist_intent`` — the single write-back entry point (structured table
  of record + FTS-searchable projection into main memory).
- ``store`` — structured query (``recent_intents``) over the ``intents`` table.
- ``recall.assemble_background`` — the single background-assembly contract.
"""

from __future__ import annotations

from .ontology import Intent, IntentEvidence

__all__ = ["Intent", "IntentEvidence"]
