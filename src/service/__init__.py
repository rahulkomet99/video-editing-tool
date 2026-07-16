"""HTTP service layer: an async render-job API over the existing pipeline.

`api.py` is the FastAPI app; `jobs.py` the SQLite-backed job store; `worker.py`
the thread-pool runner; `storage.py` the output store; `auth.py` API-key auth.
"""
