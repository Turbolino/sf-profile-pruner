# -*- coding: utf-8 -*-
"""Small deterministic map-reduce helper for parallelising independent,
read-only ``sf`` calls (list metadata, Tooling queries, sobject describe).

Design rules (to avoid the concurrency problems the project already fought):

- Workers are pure: they return their own result and share NO mutable state.
- The caller merges results in the ORIGINAL input order (deterministic output,
  regardless of thread scheduling).
- A failing task never aborts its siblings: it is captured and reported as a
  skipped item by the caller.
- ``max_workers <= 1`` degrades to a plain sequential loop (no threads at all).
- ``control`` (when given) is checked between items so a stop request ends the
  fan-out promptly; the individual ``_run_sf`` calls also honour it.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed


def run_parallel(items, fn, max_workers=8, control=None, on_done=None):
    """Map ``fn`` over ``items``. Returns ``(results, errors)``, both aligned to
    the input order: ``results[i]`` is ``fn(item)`` and ``errors[i]`` is ``None``
    on success or a message string on failure/stop. With ``max_workers <= 1`` (or
    a single item) it runs sequentially. ``on_done(completed, total)`` is called
    from the caller thread as tasks complete — never from a worker."""
    items = list(items)
    n = len(items)
    results = [None] * n
    errors = [None] * n

    def _run_one(i, item):
        if control is not None and control.stop.is_set():
            errors[i] = 'stopped by user'
            return
        try:
            results[i] = fn(item)
        except Exception as e:  # noqa: BLE001 - one task failing must not abort the fan-out
            errors[i] = str(e)

    if max_workers <= 1 or n <= 1:
        for i, item in enumerate(items):
            _run_one(i, item)
            if on_done:
                on_done(i + 1, n)
        return results, errors

    with ThreadPoolExecutor(max_workers=min(max_workers, n)) as ex:
        future_to_i = {ex.submit(fn, item): i for i, item in enumerate(items)}
        done = 0
        for fut in as_completed(future_to_i):
            i = future_to_i[fut]
            try:
                results[i] = fut.result()
            except Exception as e:  # noqa: BLE001
                errors[i] = str(e)
            done += 1
            if on_done:
                on_done(done, n)
    return results, errors
