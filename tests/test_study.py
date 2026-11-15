import base64
import json

import pytest

from bench import study
from bench.study import StudyError


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def test_save_json_adds_version_and_records_each_changed_value(tmp_path):
    study.save({"pid": "P001", "path": "day1/wajo/after-agent.json", "data": {"plan_satisfaction": 5, "initiative": "about_right"}}, tmp_path)
    saved = json.loads((tmp_path / "P001/day1/wajo/after-agent.json").read_text())
    assert saved["schema_version"] == "1" and saved["plan_satisfaction"] == 5

    r = study.save({"pid": "P001", "path": "day1/wajo/after-agent.json", "data": {"plan_satisfaction": 6, "initiative": "about_right"}}, tmp_path)
    assert r["changed"] == 1
    history = lines(tmp_path / "P001/history.jsonl")
    assert [h["variable"] for h in history] == ["initiative", "plan_satisfaction", "plan_satisfaction"]
    assert history[-1]["before"] == 5 and history[-1]["after"] == 6 and history[-1]["file"] == "day1/wajo/after-agent.json"


def test_unchanged_save_writes_nothing_new(tmp_path):
    body = {"pid": "P002", "path": "session.json", "data": {"agents": ["wajo", "muse"]}}
    study.save(body, tmp_path)
    assert study.save(body, tmp_path)["changed"] == 0
    assert len(lines(tmp_path / "P002/history.jsonl")) == 1


def test_nested_values_are_traced_by_dotted_name(tmp_path):
    study.save({"pid": "P003", "path": "scenario/card.json", "data": {"fields": {"location": {"answer": "Union Sq"}}}}, tmp_path)
    study.save({"pid": "P003", "path": "scenario/card.json", "data": {"fields": {"location": {"answer": "Chelsea"}}}}, tmp_path)
    assert lines(tmp_path / "P003/history.jsonl")[-1]["variable"] == "fields.location.answer"


def test_progress_is_not_in_history(tmp_path):
    study.save({"pid": "P004", "path": "progress.json", "data": {"stage": "setup"}}, tmp_path)
    assert not (tmp_path / "P004/history.jsonl").exists()


def test_text_and_jsonl_files(tmp_path):
    study.save({"pid": "P005", "path": "scenario/interview.md", "text": "# Interview\n"}, tmp_path)
    study.save({"pid": "P005", "path": "day2/muse/conversation.jsonl", "text": '{"role": "agent"}\n'}, tmp_path)
    with pytest.raises(StudyError, match="isn't JSON"):
        study.save({"pid": "P005", "path": "day2/muse/conversation.jsonl", "text": "not json\n"}, tmp_path)
    assert (tmp_path / "P005/scenario/interview.md").read_text() == "# Interview\n"


def test_changes_are_appended(tmp_path):
    for after in ("A", "B"):
        study.save({"pid": "P006", "path": "scenario/changes.jsonl", "append": [{"field": "timing", "before": "x", "after": after, "origin": "day1_chat"}]}, tmp_path)
    changes = lines(tmp_path / "P006/scenario/changes.jsonl")
    assert [c["after"] for c in changes] == ["A", "B"] and all("at" in c for c in changes)


@pytest.mark.parametrize("pid,path", [
    ("P001", "../escape.json"), ("P001", "day3/comparison.json"), ("P001", "day1/Wajo!/after-agent.json"),
    ("P001", "day1/learning-check.json"), ("../x", "session.json"), ("p1", "session.json"),
])
def test_only_the_study_files_can_be_written(tmp_path, pid, path):
    with pytest.raises(StudyError):
        study.save({"pid": pid, "path": path, "data": {}}, tmp_path)


def test_audio(tmp_path):
    r = study.save_audio({"pid": "P007", "name": "pre-description", "mime": "audio/webm;codecs=opus", "b64": base64.b64encode(b"abc").decode()}, tmp_path)
    assert r["path"].startswith("audio/pre-description-") and r["path"].endswith(".webm")
    assert (tmp_path / "P007" / r["path"]).read_bytes() == b"abc"
    with pytest.raises(StudyError):
        study.save_audio({"pid": "P007", "name": "../x", "mime": "audio/webm", "b64": "YWJj"}, tmp_path)
    with pytest.raises(StudyError):
        study.save_audio({"pid": "P007", "name": "x", "mime": "text/html", "b64": "YWJj"}, tmp_path)


def test_load_and_participants(tmp_path):
    study.save({"pid": "P008", "path": "progress.json", "data": {"day": 1, "stage": "pre:shared", "task": "Restaurant"}}, tmp_path)
    study.save({"pid": "P008", "path": "scenario/interview.md", "text": "hi"}, tmp_path)
    files = study.load("P008", tmp_path)["files"]
    assert files["progress.json"]["stage"] == "pre:shared" and files["scenario/interview.md"] == "hi"
    assert study.participants(tmp_path)[0] | {"updated": None} == {"pid": "P008", "updated": None, "day": 1, "stage": "pre:shared", "task": "Restaurant"}


def test_spec_comes_from_the_task_folder(tmp_path):
    (tmp_path / "restaurant").mkdir()
    (tmp_path / "restaurant/run.json").write_text('{"task": "Restaurant"}')
    (tmp_path / "restaurant/study.json").write_text('{"task": "Restaurant", "pre": []}')
    assert study.spec("Restaurant", tmp_path)["task"] == "Restaurant"
    assert study.studies(tmp_path) == ["Restaurant"]
    with pytest.raises(StudyError, match="no study"):
        study.spec("Housewarming", tmp_path)
