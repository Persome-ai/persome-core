"""Hy-Memory migration ACTIVATION markers (dev_new_memory).

The migration machinery (P0–P3 + evolution-trail recall + D2 schema miner)
lands flag-gated; *activation* is flipping the ``IntentRecognizerConfig``
defaults to True. This module pins that the招牌 flags are activated by default,
so an accidental flip-back is caught.（``recall_use_chain_index`` /
``recall_read_evo_nodes`` 已随 entry_chain 在 PR-7 退役——折叠唯一路径 =
evo_nodes，无需 staging flag。）

The *behavior* behind each flag is covered with explicit-param tests elsewhere
— and the recognizer passes exactly these config values into
``recall.assemble_background`` / ``schema_prior.active_schema_inferences``:

- ``recall_fold_superseded`` → tests/test_intent_p0_recall.py / test_recall_evo_read.py
- ``recall_chain_trail``     → tests/test_recall_evolution_trail.py
- ``schema_prior_enabled``   → tests/test_schema_prior_provider.py

So config-default-True (here) + correct-True-behavior (there) ⇒ the activated
recognizer delivers current-belief recall + evolution trail + predictive schema
priors.
"""

from __future__ import annotations

from persome.config import IntentRecognizerConfig


def test_migration_flags_activated_by_default():
    cfg = IntentRecognizerConfig()
    assert cfg.recall_fold_superseded is True, "current-belief recall not activated"
    assert cfg.recall_chain_trail is True, "evolution-trail recall not activated"
    assert cfg.schema_prior_enabled is True, "predictive schema prior not activated"
