"""Bounded retries for reads; cross-process caches only for public metadata.

Never use request_json to wrap exchange actions. Follower registration is the
only idempotent write allowed by its caller. No response bodies/auth headers are logged.
"""
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import tempfile
import time

import requests
from hyperliquid.info import Info
from . import config as cfg

logger = logging.getLogger('follow_agent.infra')
RETRYABLE = {408, 429, 500, 502, 503, 504}


class ReadError(RuntimeError):
    def __init__(self, label, status=None, retry_after=0):
        self.status = status
        self.retry_after = retry_after
        self.transient = status is None or status in RETRYABLE
        super().__init__('[INFO_RETRY_EXHAUSTED] ' + (f'{label}: HTTP {status}' if status else f'{label}: transport unavailable'))


def retry_after_seconds(value):
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0.0


def request_json(send, label, *, attempts=4, budget=45):
    deadline = time.monotonic() + budget
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReadError(label)
        try:
            r = send(min(10, remaining))
            try:
                if r.status_code < 400:
                    return r.json()
                error = ReadError(label, r.status_code, retry_after_seconds(r.headers.get('Retry-After')))
                logger.warning('%s status=%s cf_ray=%s retry_after=%s', label, r.status_code,
                               str(r.headers.get('CF-Ray', ''))[:100], error.retry_after)
            finally:
                r.close()
        except (requests.Timeout, requests.ConnectionError):
            error = ReadError(label)
        if not error.transient or attempt + 1 >= attempts:
            raise error
        delay = max(error.retry_after, random.uniform(1, min(20, 2 ** (attempt + 1))))
        if delay >= deadline - time.monotonic():
            raise error
        time.sleep(delay)
    raise ReadError(label)


@contextmanager
def file_lock(path, *, timeout=60):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a+') as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('metadata/registration lock wait exceeded budget')
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def cached_json(root, key, ttl, fetch, validate):
    digest = hashlib.sha256(key.encode()).hexdigest()
    path = Path(root) / (digest + '.json')
    with file_lock(path.with_suffix('.lock')):
        try:
            cached = json.loads(path.read_text())
            if cached.get('retry_until', 0) > time.time():
                raise ReadError('cached request cooldown', cached.get('status'), cached['retry_until'] - time.time())
            age = time.time() - cached['time']
            if 0 <= age < ttl and validate(cached['value']):
                return cached['value']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            value = fetch()
        except ReadError as error:
            if error.transient:
                atomic_json(path, {'retry_until': time.time() + max(5, error.retry_after),
                                   'status': error.status})
            raise
        if not validate(value):
            raise ValueError('invalid metadata/registration response; not cached')
        atomic_json(path, {'time': time.time(), 'value': value})
        return value


def _valid_metadata(kind, data):
    if kind == 'perpDexs':
        return isinstance(data, list) and bool(data) and all(
            x is None or isinstance(x, dict) and isinstance(x.get('name'), str) for x in data)
    return (isinstance(data, dict) and isinstance(data.get('universe'), list)
            and (kind != 'spotMeta' or isinstance(data.get('tokens'), list)))


def post_info(api_url, payload):
    base = api_url.rstrip('/')
    def fetch():
        data = request_json(lambda timeout: requests.post(base + '/info', json=payload, timeout=timeout),
                            'hypercore:' + str(payload.get('type', 'unknown'))[:60])
        if isinstance(data, dict) and 'error' in data:
            raise ValueError('HyperCore returned an error object, not account data')
        if payload.get('type') == 'clearinghouseState' and not (
                isinstance(data, dict) and isinstance(data.get('marginSummary'), dict)
                and 'accountValue' in data['marginSummary'] and 'withdrawable' in data
                and isinstance(data.get('assetPositions'), list)):
            raise ValueError('incomplete HyperCore clearinghouse state')
        if payload.get('type') == 'spotClearinghouseState' and not (
                isinstance(data, dict) and isinstance(data.get('balances'), list)):
            raise ValueError('incomplete HyperCore spot state')
        return data
    kind = payload.get('type')
    if kind not in {'meta', 'spotMeta', 'perpDexs'}:
        return fetch()  # No caching of balances, prices, authorization or NAV evidence.
    root = cfg.get('hyper_metadata_cache_dir') or cfg.get_state_dir() / 'cache' / 'metadata'
    return cached_json(root, base + json.dumps(payload, sort_keys=True), 600, fetch,
                       lambda data: _valid_metadata(kind, data))


class ReadInfo(Info):
    """SDK-compatible read client. Exchange retains its official signing URL."""
    def post(self, url_path, payload=None):
        if url_path != '/info':
            raise ValueError('ReadInfo only supports /info')
        return post_info(self.base_url, payload or {})
