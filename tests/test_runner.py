"""Which client each eval system runs on, and hand scoring for what the judge must not score."""

import csv
import json
from types import SimpleNamespace

import pytest

from evals import judge
from evals.runner import RUNNERS, client_for, label_for


def llm():
    return SimpleNamespace(model="claude-haiku-5-5", max_spend_per_run_usd=0.05)


def test_a_s_differs_from_a_h_only_in_model_and_cap():
    client = client_for("A-S", llm())
    assert (client.model, client.max_spend_per_run_usd) == ("claude-sonnet-5-5", 0.25)
    assert RUNNERS["A-S"] is RUNNERS["A-H"]


@pytest.mark.parametrize("system", ["B1", "B2", "A-H"])
def test_other_systems_keep_the_usual_cap(system):
    base = llm()
    assert client_for(system, base) is base
    assert base.max_spend_per_run_usd == 0.05


def test_a_s_does_not_change_the_shared_client():
    base = llm()
    client_for("A-S", base)
    assert (base.model, base.max_spend_per_run_usd) == ("claude-haiku-5-5", 0.05)


def test_a_s_label_keeps_ablation_names():
    assert label_for("A-S", True, "hybrid_rerank") == "A-S"
    assert label_for("A-S", False, "hybrid_rerank") == "A-S_no-critic"
    assert label_for("B1", False, "hybrid_rerank") == "B1"


def hand_setup(tmp_path, monkeypatch):
    ids = [f"REQ-{n:03}" for n in range(60)]
    rows = {i: {"request_text": f"ask {i}", "reference_reply": "ref"} for i in ids}
    monkeypatch.setattr(judge, "load_rows", lambda split: rows)
    monkeypatch.setattr(judge, "RESULTS", tmp_path)
    run = tmp_path / "run.jsonl"
    records = [{"request_id": i, "system": "A-S", "split": "dev", "reply": "r"} for i in ids]
    run.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return run, ids


def read_labels(labels):
    with labels.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def fill(labels, score):
    items = read_labels(labels)
    with labels.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(items[0]))
        writer.writeheader()
        writer.writerows({**i, "owner_score": score} for i in items)


def test_hand_export_uses_the_judge_sample(tmp_path, monkeypatch):
    run, ids = hand_setup(tmp_path, monkeypatch)
    labels = tmp_path / "labels.csv"
    judge.hand_export(run, labels)
    assert sorted(i["request_id"] for i in read_labels(labels)) == judge.sample_ids(ids)


def test_hand_score_saves_the_mean_as_the_owner(tmp_path, monkeypatch):
    run, _ = hand_setup(tmp_path, monkeypatch)
    labels = tmp_path / "labels.csv"
    judge.hand_export(run, labels)
    fill(labels, "4")
    judge.hand_score(run, labels)
    saved = json.loads((tmp_path / "run_human.json").read_text(encoding="utf-8"))
    assert (saved["scorer"], saved["mean_score"], len(saved["items"])) == ("owner", 4, 30)


def test_hand_score_refuses_a_blank_score(tmp_path, monkeypatch):
    run, _ = hand_setup(tmp_path, monkeypatch)
    labels = tmp_path / "labels.csv"
    judge.hand_export(run, labels)
    with pytest.raises(SystemExit):
        judge.hand_score(run, labels)
