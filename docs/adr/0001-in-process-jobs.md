# In-process jobs, no external queue or database

Downloads run as background jobs **inside the FastAPI process**: an in-memory registry holds job state, and a `ThreadPoolExecutor` with 2 workers runs yt-dlp, with up to 5 further jobs queued. There is no Redis, no Celery, and no database — job state and temporary files are lost when the process restarts.

Considered alternatives: an external queue (survives restarts and scales out, but adds two services to operate for a single-user tool), one subprocess per download (killable and crash-isolated, but progress then has to be parsed from stdout with Windows encoding and quoting problems), and a synchronous request (no job state at all, but one to three minutes of dead spinner that also times out behind any reverse proxy).

Consequences worth knowing: cancellation is **cooperative only**, because Python cannot kill a running thread — the worker checks a per-job flag on each `progress_hook` callback. A restart drops in-flight jobs, which is acceptable while the app is localhost-only and single-user. Revisit this ADR if the app ever becomes multi-user or public.
