"""API + job-store tests. Uses TestClient and a fake runner — no ffmpeg, no
Claude, no live server."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from src.config import Config
from src.service.jobs import Job, JobStore
from src.service.storage import LocalStorage
from src.usage import Usage


# --------------------------------------------------------------------------- #
# JobStore
# --------------------------------------------------------------------------- #
def test_jobstore_roundtrip_and_usage(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    a = store.create(Job.new("alice", "cats"))
    store.update(a.id, status="succeeded", tokens_in=100, tokens_out=20, cost=0.5)
    got = store.get(a.id)
    assert got.status == "succeeded" and got.tokens_in == 100
    store.create(Job.new("bob", None))
    assert len(store.list(api_key_id="alice")) == 1  # scoped
    summary = store.usage_summary(api_key_id="alice")
    assert summary["jobs"] == 1 and summary["tokens_in"] == 100


def test_localstorage_save_and_path(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"video")
    st = LocalStorage(tmp_path / "store")
    st.save(src, "job1.mp4")
    assert st.path_for("job1.mp4").read_bytes() == b"video"
    assert st.path_for("missing.mp4") is None


# --------------------------------------------------------------------------- #
# API (fake runner)
# --------------------------------------------------------------------------- #
def _app(tmp_path, api_keys, produce_file=True):
    from src.service.api import create_app

    cfg = Config.load("config.yaml")
    cfg.raw["service"] = {
        "db_path": str(tmp_path / "jobs.db"),
        "storage_dir": str(tmp_path / "renders"),
        "max_workers": 2,
        "api_keys": api_keys,
    }

    def fake_run(_cfg, topic):
        out = tmp_path / "fake_out.mp4"
        out.write_bytes(b"rendered")
        return str(out), f"Video about {topic or 'trend'}", Usage(input=120, output=30)

    return create_app(cfg, run_fn=fake_run)


def _wait(client, job_id, headers, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/jobs/{job_id}", headers=headers).json()
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


def test_healthz_open_mode(tmp_path):
    client = TestClient(_app(tmp_path, api_keys=[]))
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["auth"] == "open"


def test_submit_download_and_usage(tmp_path):
    client = TestClient(_app(tmp_path, api_keys=["k1:alice"]))
    h = {"X-API-Key": "k1"}

    r = client.post("/jobs", json={"topic": "robots"}, headers=h)
    assert r.status_code == 202
    job_id = r.json()["id"]

    done = _wait(client, job_id, h)
    assert done["status"] == "succeeded"
    assert done["tokens_in"] == 120 and done["download_url"]

    dl = client.get(f"/jobs/{job_id}/download", headers=h)
    assert dl.status_code == 200 and dl.content == b"rendered"

    usage = client.get("/usage", headers=h).json()
    assert usage["caller"] == "alice" and usage["tokens_out"] == 30


def test_auth_required_when_keys_set(tmp_path):
    client = TestClient(_app(tmp_path, api_keys=["k1:alice"]))
    assert client.post("/jobs", json={}).status_code == 401
    assert client.post("/jobs", json={}, headers={"X-API-Key": "wrong"}).status_code == 401


def test_jobs_are_scoped_per_caller(tmp_path):
    client = TestClient(_app(tmp_path, api_keys=["ka:alice", "kb:bob"]))
    r = client.post("/jobs", json={"topic": "x"}, headers={"X-API-Key": "ka"})
    job_id = r.json()["id"]
    # bob can't see alice's job
    assert client.get(f"/jobs/{job_id}", headers={"X-API-Key": "kb"}).status_code == 404
    assert client.get(f"/jobs/{job_id}", headers={"X-API-Key": "ka"}).status_code == 200


def test_bearer_token_accepted(tmp_path):
    client = TestClient(_app(tmp_path, api_keys=["k1:alice"]))
    r = client.post("/jobs", json={}, headers={"Authorization": "Bearer k1"})
    assert r.status_code == 202
