"""Task-level liveness: restarting reads must never replay an exchange action."""
import asyncio
import logging
import os
import random
import threading
import time

from . import config as cfg
from .infra import ReadError, atomic_json

logger = logging.getLogger('follow_agent.task_health')
_states = {}
_lock = threading.Lock()


def update(name, status, **fields):
    with _lock:
        _states[name] = {**_states.get(name, {}), **fields, 'status': status, 'updated_at': time.time()}
        path = cfg.get_config_path().parent / 'task_health.json'
        atomic_json(path, {'pid': os.getpid(), 'tasks': _states})


async def wait(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


def read_failure(exc):
    """Recognize read-only errors preserved as a cause by baseline/metadata code."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (ReadError, TimeoutError, ConnectionError)):
            return exc
        exc = exc.__cause__
    return None


async def supervise(name, factory, stop):
    failures = 0
    while not stop.is_set():
        update(name, 'starting')
        try:
            await factory()
            if stop.is_set():
                break
            # Unexpected early return is visible, not mislabeled as healthy.
            update(name, 'blocked', error='task returned unexpectedly')
            await stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            cause = read_failure(exc)
            if cause is None or isinstance(cause, ReadError) and not cause.transient:
                update(name, 'blocked', error=type(exc).__name__)
                logger.error('Task %s blocked (%s); manual investigation required', name, type(exc).__name__)
                await stop.wait()
                break
            failures += 1
            delay = max(getattr(cause, 'retry_after', 0), random.uniform(10, min(300, 10 * 2 ** min(failures, 5))))
            update(name, 'retrying', error=str(cause), failures=failures, retry_at=time.time() + delay)
            logger.warning('Task %s temporarily unavailable; retry in %.1fs', name, delay)
            await wait(stop, delay)
    update(name, 'stopped')
