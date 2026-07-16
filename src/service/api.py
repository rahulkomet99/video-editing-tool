"""FastAPI job API over the render pipeline.

Endpoints (all but /healthz require an API key when keys are configured):
  POST /jobs              submit a render job -> 202 + job
  GET  /jobs              list your jobs
  GET  /jobs/{id}         job status
  GET  /jobs/{id}/download  the rendered mp4 (when succeeded)
  GET  /usage             your token/cost totals
  GET  /healthz           liveness + ffmpeg check

Run: uvicorn src.service.api:create_app --factory
"""
# NB: no `from __future__ import annotations` here — FastAPI must resolve the
# Depends()/Header() metadata in endpoint signatures at runtime, which stringized
# annotations (and closure-local dependencies) would break.

import os
import shutil
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..config import Config
from ..log import configure, get_logger
from .auth import load_keys, resolve_caller
from .jobs import Job, JobStore
from .storage import LocalStorage
from .worker import JobRunner, RunFn

log = get_logger(__name__)


class JobIn(BaseModel):
    topic: str | None = Field(
        None, description="Optional topic; omit to use the top live trend."
    )


class JobOut(BaseModel):
    id: str
    status: str
    topic: str | None
    title: str | None
    error: str | None
    tokens_in: int
    tokens_out: int
    cost: float
    created_at: float
    finished_at: float | None
    download_url: str | None

    @classmethod
    def of(cls, job: Job) -> "JobOut":
        return cls(
            id=job.id, status=job.status, topic=job.topic, title=job.title,
            error=job.error, tokens_in=job.tokens_in, tokens_out=job.tokens_out,
            cost=job.cost, created_at=job.created_at, finished_at=job.finished_at,
            download_url=(
                f"/jobs/{job.id}/download" if job.status == "succeeded" else None
            ),
        )


def create_app(cfg: Config | None = None, run_fn: RunFn | None = None) -> FastAPI:
    configure()
    cfg = cfg or Config.load(os.getenv("VET_CONFIG", "config.yaml"))
    svc = cfg.service
    store = JobStore(cfg.path(svc.get("db_path", "output/jobs.db")))
    storage = LocalStorage(cfg.path(svc.get("storage_dir", "output/renders")))
    runner_kwargs = {"run_fn": run_fn} if run_fn else {}
    runner = JobRunner(
        store, cfg, storage, max_workers=int(svc.get("max_workers", 2)), **runner_kwargs
    )
    keys = load_keys(cfg)
    if not keys:
        log.warning("No API keys configured — service is OPEN (dev mode).")

    app = FastAPI(title="Auto Video Editor API", version="1.0")

    def caller(
        x_api_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> str:
        presented = x_api_key
        if not presented and authorization and authorization.lower().startswith("bearer "):
            presented = authorization.split(" ", 1)[1]
        cid = resolve_caller(presented, keys)
        if cid is None:
            raise HTTPException(status_code=401, detail="Invalid or missing API key.")
        return cid

    @app.get("/healthz")
    def healthz() -> dict:
        exe = cfg.media.get("ffmpeg", "ffmpeg")
        return {
            "status": "ok",
            "ffmpeg": shutil.which(exe) is not None,
            "workers": int(svc.get("max_workers", 2)),
            "auth": "enabled" if keys else "open",
        }

    @app.post("/jobs", response_model=JobOut, status_code=202)
    def submit(body: JobIn, cid: Annotated[str, Depends(caller)]) -> JobOut:
        job = Job.new(cid, body.topic)
        store.create(job)
        runner.submit(job)
        log.info("queued job %s for %s (topic=%r)", job.id, cid, body.topic)
        return JobOut.of(job)

    @app.get("/jobs", response_model=list[JobOut])
    def list_jobs(cid: Annotated[str, Depends(caller)]) -> list[JobOut]:
        return [JobOut.of(j) for j in store.list(api_key_id=cid)]

    @app.get("/jobs/{job_id}", response_model=JobOut)
    def get_job(job_id: str, cid: Annotated[str, Depends(caller)]) -> JobOut:
        job = store.get(job_id)
        if job is None or job.api_key_id != cid:
            raise HTTPException(status_code=404, detail="Job not found.")
        return JobOut.of(job)

    @app.get("/jobs/{job_id}/download")
    def download(job_id: str, cid: Annotated[str, Depends(caller)]):
        job = store.get(job_id)
        if job is None or job.api_key_id != cid:
            raise HTTPException(status_code=404, detail="Job not found.")
        if job.status != "succeeded" or not job.output_path:
            raise HTTPException(status_code=409, detail=f"Job is {job.status}.")
        path = storage.path_for(job.output_path)
        if path is None:
            raise HTTPException(status_code=404, detail="Output not available.")
        return FileResponse(
            path, media_type="video/mp4", filename=f"{job.title or job.id}.mp4"
        )

    @app.get("/usage")
    def usage(cid: Annotated[str, Depends(caller)]) -> dict:
        return {"caller": cid, **store.usage_summary(api_key_id=cid)}

    app.state.store = store
    app.state.runner = runner
    return app
