"""Background jobs with progress, for work too slow for a page request
(for example checking a few hundred freshly imported nodes over SSH).

Jobs run in a thread inside the app process and report progress to an
in-memory registry, polled by the page. That fits the single Gunicorn worker
the image runs; a restart loses only the progress display, never the data,
because each step commits its own result.
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import current_app

KEEP_FINISHED_S = 3600
_JOBS = {}
_LOCK = threading.Lock()


def _public(job):
    return {k: job[k] for k in ('id', 'kind', 'title', 'total', 'done', 'ok', 'failed',
                                'state', 'message', 'started', 'finished')}


def _prune():
    cutoff = time.time() - KEEP_FINISHED_S
    for jid in [j for j, v in _JOBS.items() if v['finished'] and v['finished'] < cutoff]:
        _JOBS.pop(jid, None)


def for_user(user_id):
    """The user's jobs, running first, most recent first."""
    with _LOCK:
        _prune()
        mine = [_public(j) for j in _JOBS.values() if j['owner'] == user_id]
    return sorted(mine, key=lambda j: (j['state'] != 'running', -j['started']))


def run_batches(kind, title, owner_id, items, work, batch=48, summary=None):
    """Run work(list_of_items) -> number succeeded, one batch after another
    in a single thread (the batch itself may fan out, as the collector does).
    Progress moves by a batch at a time. Returns the job id."""
    app = current_app._get_current_object()
    jid = uuid.uuid4().hex[:12]
    job = {'id': jid, 'kind': kind, 'title': title, 'owner': owner_id,
           'total': len(items), 'done': 0, 'ok': 0, 'failed': 0, 'state': 'running',
           'message': '', 'started': time.time(), 'finished': None}
    with _LOCK:
        _JOBS[jid] = job

    def runner():
        try:
            for i in range(0, len(items), batch):
                chunk = items[i:i + batch]
                with app.app_context():
                    try:
                        ok = int(work(chunk))
                    except Exception as e:
                        app.logger.warning('%s: batch failed: %s', kind, e)
                        ok = 0
                    finally:
                        from app import db
                        db.session.remove()
                with _LOCK:
                    job['done'] += len(chunk)
                    job['ok'] += ok
                    job['failed'] += len(chunk) - ok
            job['state'] = 'done'
            job['message'] = summary(job) if summary else ''
        except Exception as e:
            job['state'] = 'error'
            job['message'] = str(e)[:300]
        finally:
            job['finished'] = time.time()

    threading.Thread(target=runner, name=f'job-{kind}-{jid}', daemon=True).start()
    return jid


def run_each(kind, title, owner_id, items, work, workers=16, summary=None):
    """Run work(item) -> bool for every item in a pool of threads, inside an
    app context, updating progress as items finish. Returns the job id."""
    app = current_app._get_current_object()
    jid = uuid.uuid4().hex[:12]
    job = {'id': jid, 'kind': kind, 'title': title, 'owner': owner_id,
           'total': len(items), 'done': 0, 'ok': 0, 'failed': 0, 'state': 'running',
           'message': '', 'started': time.time(), 'finished': None}
    with _LOCK:
        _JOBS[jid] = job

    def one(item):
        with app.app_context():
            try:
                ok = bool(work(item))
            except Exception as e:                    # one bad node never stops the job
                app.logger.warning('%s: item %s failed: %s', kind, item, e)
                ok = False
            finally:
                from app import db
                db.session.remove()
        with _LOCK:
            job['done'] += 1
            job['ok' if ok else 'failed'] += 1

    def runner():
        try:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                list(pool.map(one, items))
            job['state'] = 'done'
            job['message'] = summary(job) if summary else ''
        except Exception as e:
            job['state'] = 'error'
            job['message'] = str(e)[:300]
        finally:
            job['finished'] = time.time()

    threading.Thread(target=runner, name=f'job-{kind}-{jid}', daemon=True).start()
    return jid
