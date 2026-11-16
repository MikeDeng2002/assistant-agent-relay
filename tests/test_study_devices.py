import pytest

from bench.study_devices import DeviceError, Devices

AGENTS = {"instinct": "imessage"}


def test_pairing_is_single_use_and_disconnect_revokes_device(tmp_path):
    devices = Devices(tmp_path / "devices.db")
    code = devices.code("participant-a")["code"]
    device = devices.pair(code, AGENTS, AGENTS)["credential"]
    with pytest.raises(DeviceError, match="expired or was used"):
        devices.pair(code, AGENTS, AGENTS)

    assert devices.status("participant-a")["device"]["online"]
    assert devices.status("participant-b") == {"device": None, "jobs": []}
    devices.disconnect("participant-a")
    with pytest.raises(DeviceError, match="no longer paired"):
        devices.exchange(device, "poll", {"agents": AGENTS}, AGENTS)


def test_run_requires_matching_reply_and_claim_is_atomic(tmp_path):
    devices = Devices(tmp_path / "devices.db")
    owner = "participant-a"
    token = devices.pair(devices.code(owner)["code"], AGENTS, AGENTS)["credential"]
    run = {"agent": "instinct", "kind": "run", "text": "opening", "step": "DAY 1 OPENING"}

    def finish_test(reply=None):
        job = devices.queue(owner, AGENTS, "Restaurant", {"agent": "instinct", "kind": "test"})
        claim = devices.exchange(token, "poll", {"agents": AGENTS}, AGENTS)["job"]
        assert claim["id"] == job["id"]
        assert devices.exchange(token, "poll", {"agents": AGENTS}, AGENTS)["job"] is None
        expected = claim["prompt"].rsplit(": ", 1)[-1]
        return devices.exchange(token, "report", {
            "id": job["id"], "state": "done", "sent_at": "2026-11-15T10:00:00Z",
            "replies": [{"id": "reply-1", "at": "2026-11-15T10:00:01Z", "text": reply or expected}],
        }, AGENTS)

    with pytest.raises(DeviceError, match="Test this connection"):
        devices.queue(owner, AGENTS, "Restaurant", run)
    assert finish_test("a reply from another conversation")["accepted"]
    assert devices.status(owner)["device"]["verified"] == []
    assert finish_test()["accepted"]
    assert devices.status(owner)["device"]["verified"] == ["instinct"]
    queued = devices.queue(owner, AGENTS, "Restaurant", run)
    assert devices.exchange(token, "poll", {"agents": AGENTS}, AGENTS)["job"]["id"] == queued["id"]


def test_address_dialog_returns_input_and_cancel_is_actionable(monkeypatch):
    from types import SimpleNamespace
    from bench import study_connector
    monkeypatch.setattr(study_connector.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout='  +16505550123\n'))
    assert study_connector.address_dialog('instinct', 'phone number') == '+16505550123'
    monkeypatch.setattr(study_connector.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=1, stdout=''))
    with pytest.raises(study_connector.ConnectorError, match='cancelled'):
        study_connector.address_dialog('instinct', 'phone number')


def test_web_setup_runs_in_project_python_and_reports_failure(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from bench import study_connector as connector
    python = tmp_path / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(connector, 'ROOT', tmp_path)
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(connector.subprocess, 'run', run)
    connector.setup_channel('hark')
    assert calls[0][0] == [str(python), '-m', 'bench.cli', 'hark-login']
    assert calls[0][1]['timeout'] == 240
    monkeypatch.setattr(connector.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(connector.ConnectorError, match='Could not complete sign-in'):
        connector.setup_channel('hark')
