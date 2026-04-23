"""Tests for the labeling functions and label model."""

import pytest

from backend.app.services.weak_supervision import (
    ABSTAIN,
    LFVote,
    LabelModel,
    LabelingFunction,
    churn_intent_model,
    commitment_model,
    label_interaction,
    lf_cancel_intent,
    lf_commitment_language,
    lf_objection_resolved,
    objection_resolution_model,
)


def test_lf_cancel_intent_fires_on_explicit_phrases():
    vote = lf_cancel_intent(transcript="we're cancelling next month")
    assert vote.label == 1
    assert vote.confidence > 0.5


def test_lf_cancel_intent_returns_negative_on_benign_text():
    vote = lf_cancel_intent(transcript="looking forward to our next meeting")
    assert vote.label == 0


def test_lf_commitment_language_fires_on_commit_phrases():
    vote = lf_commitment_language(transcript="let's do it, send over the contract")
    assert vote.label == 1


def test_lf_objection_resolved_abstains_with_no_turns():
    vote = lf_objection_resolved(turns=[])
    assert vote.label == ABSTAIN


def test_lf_objection_resolved_positive_when_acknowledgement_follows():
    turns = [
        {"speaker_id": "customer", "text": "The price is too expensive."},
        {"speaker_id": "agent", "text": "I see what you mean; let's look at the math."},
    ]
    assert lf_objection_resolved(turns=turns).label == 1


def test_lf_objection_resolved_negative_when_no_acknowledgement():
    turns = [
        {"speaker_id": "customer", "text": "The price is too expensive."},
        {"speaker_id": "agent", "text": "Anyway, moving on."},
    ]
    assert lf_objection_resolved(turns=turns).label == 0


def test_label_model_aggregate_returns_none_on_total_abstain():
    model = LabelModel([
        LabelingFunction("lf_abstain", lambda **_: LFVote(label=ABSTAIN)),
    ])
    result = model.aggregate({"lf_abstain": LFVote(label=ABSTAIN)})
    assert result.label is None
    assert result.support == 0


def test_label_model_aggregate_weighted_by_accuracy():
    # One accurate LF + one noisier LF agreeing → confident positive.
    model = LabelModel([
        LabelingFunction("acc_high", lambda **_: LFVote(label=1), estimated_accuracy=0.9),
        LabelingFunction("acc_low", lambda **_: LFVote(label=1), estimated_accuracy=0.55),
    ])
    result = model.aggregate({
        "acc_high": LFVote(label=1, confidence=0.8),
        "acc_low": LFVote(label=1, confidence=0.8),
    })
    assert result.label == 1
    assert result.probability > 0.8


def test_label_model_fit_updates_accuracies_with_agreement():
    lfs = [
        LabelingFunction("a", lambda **_: LFVote(label=1)),
        LabelingFunction("b", lambda **_: LFVote(label=1)),
        LabelingFunction("c", lambda **_: LFVote(label=0)),
    ]
    model = LabelModel(lfs)
    votes = [
        {"a": LFVote(label=1), "b": LFVote(label=1), "c": LFVote(label=0)},
        {"a": LFVote(label=1), "b": LFVote(label=1), "c": LFVote(label=0)},
        {"a": LFVote(label=1), "b": LFVote(label=1), "c": LFVote(label=0)},
        {"a": LFVote(label=1), "b": LFVote(label=1), "c": LFVote(label=0)},
        {"a": LFVote(label=1), "b": LFVote(label=1), "c": LFVote(label=0)},
    ]
    model.fit(votes)
    # a and b agree with the majority every time; c always disagrees.
    assert model.accuracy("a") > 0.75
    assert model.accuracy("c") < 0.5


def test_label_interaction_returns_all_three_signals():
    turns = [
        {"speaker_id": "customer", "text": "This is too expensive."},
        {"speaker_id": "agent", "text": "Thanks for clarifying — let me show you the ROI."},
    ]
    out = label_interaction(
        transcript="let's do it, send over the contract",
        turns=turns,
        llm_churn_signal="low",
    )
    assert "cancel_intent" in out
    assert "commitment" in out
    assert "objection_resolved" in out
    assert out["commitment"].label == 1
    assert out["objection_resolved"].label == 1


def test_convenience_model_factories_expose_sensible_priors():
    for factory in (churn_intent_model, commitment_model, objection_resolution_model):
        model = factory()
        for lf in model._lfs:  # noqa: SLF001
            assert 0.5 < lf.estimated_accuracy < 1.0
