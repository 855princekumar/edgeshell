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


LOG_KEEP = 400


def _public(job, full=False):
    out = {k: job[k] for k in ('id', 'kind', 'title', 'total', 'done', 'ok', 'failed',
                               'state', 'message', 'started', 'finished')}
    out['result'] = job.get('result')
    out['items'] = [dict(i) for i in job.get('items', [])]
    log = job.get('log', [])
    out['log'] = list(log if full else log[-12:])
    return out


def get(job_id, user_id):
    with _LOCK:
        job = _JOBS.get(job_id)
        return _public(job, full=True) if job and job['owner'] == user_id else None


class Task:
    """Handle given to run_task's function: report steps, items and log lines."""

    def __init__(self, job):
        self.job = job

    def log(self, text):
        with _LOCK:
            for line in str(text).replace(chr(13), '').splitlines() or ['']:
                self.job['log'].append(line[:500])
            del self.job['log'][:-LOG_KEEP]

    def item(self, key, **fields):
        """Create or update one item (a node): name, state, step, message."""
        with _LOCK:
            for it in self.job['items']:
                if it['key'] == key:
                    it.update(fields)
                    return
            self.job['items'].append(dict(key=key, **fields))

    def progress(self, done, total=None, message=None):
        """Set progress directly, for work that is not a list of nodes."""
        with _LOCK:
            self.job['done'] = done
            if total is not None:
                self.job['total'] = total
            if message is not None:
                self.job['message'] = message

    def result(self, **fields):
        """Attach a result to the finished job (for example a download link)."""
        with _LOCK:
            self.job['result'] = dict(fields)

    def finish_item(self, ok):
        with _LOCK:
            self.job['done'] += 1
            self.job['ok' if ok else 'failed'] += 1


def run_task(kind, title, owner_id, total, fn, summary=None):
    """Run fn(task) in a background thread inside an app context. fn reports
    progress through the Task handle. Returns the job id."""
    app = current_app._get_current_object()
    jid = uuid.uuid4().hex[:12]
    job = {'id': jid, 'kind': kind, 'title': title, 'owner': owner_id, 'total': total,
           'done': 0, 'ok': 0, 'failed': 0, 'state': 'running', 'message': '',
           'started': time.time(), 'finished': None, 'items': [], 'log': []}
    with _LOCK:
        _JOBS[jid] = job

    def runner():
        task = Task(job)
        try:
            with app.app_context():
                try:
                    fn(task)
                finally:
                    from app import db
                    db.session.remove()
            job['state'] = 'done'
            job['message'] = summary(job) if summary else ''
        except Exception as e:
            app.logger.exception('%s failed', kind)
            job['state'] = 'error'
            job['message'] = str(e)[:300]
        finally:
            job['finished'] = time.time()

    threading.Thread(target=runner, name=f'job-{kind}-{jid}', daemon=True).start()
    return jid


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
