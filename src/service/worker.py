"""Thread-pool job runner.

Decouples "submit" from "render": the API returns immediately and the pool runs
the pipeline in the background, recording status + per-job token usage in the
store. `run_fn` is injectable so tests can supply a fast fake. For multi-node
scale, replace this with a Celery/RQ task calling the same `default_run`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from ..config import Config
from ..log import get_logger
from ..pipeline import Pipeline
from ..usage import Usage
from .jobs import Job, JobStore
from .storage import Storage

log = get_logger(__name__)

# run_fn(cfg, topic) -> (output_path, title, usage)
RunFn = Callable[[Config, "str | None"], "tuple[str, str, Usage]"]


def default_run(cfg: Config, topic: str | None) -> tuple[str, str, Usage]:
    pipe = Pipeline(cfg)
    result = pipe.run_one(topic)
    return result.output_path, result.edl.title, pipe.editor.last_usage


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        cfg: Config,
        storage: Storage,
        run_fn: RunFn = default_run,
        max_workers: int = 2,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.storage = storage
        self.run_fn = run_fn
        self.pricing = cfg.claude.get("pricing")
        self.pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="render"
        )

    def submit(self, job: Job) -> None:
        self.pool.submit(self._run, job)

    def _run(self, job: Job) -> None:
        self.store.update(job.id, status="running", started_at=time.time())
        try:
            out_path, title, usage = self.run_fn(self.cfg, job.topic)
            key = f"{job.id}.mp4"
            self.storage.save(out_path, key)
            self.store.update(
                job.id,
                status="succeeded",
                finished_at=time.time(),
                output_path=key,
                title=title,
                tokens_in=usage.input,
                tokens_out=usage.output,
                cost=round(usage.cost(self.pricing), 4),
            )
            log.info("job %s succeeded (%s)", job.id, title)
        except Exception as exc:  # noqa: BLE001 — record failure; never kill the worker
            log.exception("job %s failed", job.id)
            self.store.update(
                job.id, status="failed", finished_at=time.time(), error=str(exc)[:500]
            )

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
