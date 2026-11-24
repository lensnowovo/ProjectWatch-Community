"""Independent steps of one job run a few at a time; a sent request is never abandoned."""
import threading
import time
from types import SimpleNamespace

import pytest
from test_continuation_upgrade import setup

from project_watch.studio import parallel_steps
from project_watch.studio.execution import JobExecution


class Gauge:
    def __init__(self):
        self.lock, self.now, self.peak, self.started, self.expect = threading.Lock(), 0, 0, [], 1

    def step(self, name, seconds=0.05, fail=None):
        def run():
            with self.lock:
                self.started.append(name)
                self.now += 1
                self.peak = max(self.peak, self.now)
            try:
                # Hold briefly until siblings have started (bounded), so overlap does not depend on load.
                deadline = time.monotonic() + max(seconds, 0) + 2
                while seconds and time.monotonic() < deadline and self.now < self.expect:
                    time.sleep(0.005)
                time.sleep(seconds)
                if fail:
                    raise fail
                return name
            finally:
                with self.lock:
                    self.now -= 1
        return run


def test_steps_run_width_at_a_time_and_return_in_order():
    gauge = Gauge()
    gauge.expect = 3
    execution = SimpleNamespace(parallel_width=3)
    assert parallel_steps.run(execution, [gauge.step(i) for i in range(8)], cautious=False) == list(range(8))
    assert gauge.peak == 3


def test_without_a_width_steps_run_in_order_on_the_job_thread():
    gauge, threads = Gauge(), []
    steps = [lambda i=i: threads.append(threading.current_thread()) or gauge.step(i, 0)() for i in range(4)]
    assert parallel_steps.run(SimpleNamespace(), steps) == [0, 1, 2, 3]
    assert set(threads) == {threading.current_thread()} and gauge.peak == 1


def test_after_a_failure_running_steps_finish_and_no_new_step_starts():
    gauge = Gauge()
    steps = [gauge.step(0, 0.01, fail=RuntimeError('paused')), gauge.step(1, 0.2), gauge.step(2, 0.2),
             gauge.step(3), gauge.step(4)]
    with pytest.raises(RuntimeError, match='paused'):
        parallel_steps.run(SimpleNamespace(parallel_width=3), steps, cautious=False)
    assert sorted(gauge.started) == [0, 1, 2] and gauge.now == 0  # 1 and 2 were sent and completed


def test_steps_run_inside_the_job_scope():
    seen, entered = [], []
    from contextlib import contextmanager

    @contextmanager
    def scope():
        entered.append(threading.current_thread().name)
        yield
    execution = SimpleNamespace(parallel_width=2, thread_scope=scope)
    parallel_steps.run(execution, [lambda: seen.append(1), lambda: seen.append(2)], cautious=False)
    assert len(entered) == 2 and all(name.startswith('studio-step') for name in entered)


def test_extraction_batches_are_sent_in_parallel_and_kept_in_order(tmp_path):
    from project_watch.studio.pipeline import Pipeline
    store, pid, job = setup(tmp_path)
    execution = JobExecution(store, job)
    execution.parallel_width = 3
    gauge = Gauge()
    gauge.expect = 3
    inputs = {'input_version': 'stable', 'binding': 'allowed', 'parts': [{'text': str(i)} for i in range(60)]}

    def extract(_system, payload, **_kwargs):
        return gauge.step(payload, 0.05)()
    pipeline = SimpleNamespace(_execution_state=SimpleNamespace(current=execution),
                               store=SimpleNamespace(get_project=lambda _: {'activated': True, 'status': 'active'}),
                               model_binding=lambda: 'allowed')
    proposals = Pipeline._extract_all_parts(pipeline, pid, inputs, SimpleNamespace(json=extract), 'system',
                                            lambda batch: [p['text'] for p in batch['parts']], 'details', lambda: inputs)
    assert gauge.peak == 3 and len(proposals) == 5
    assert [proposal for proposal in proposals] == sorted(proposals, key=lambda p: int(p.split(',')[0].strip('["')))
    assert all(execution.read_checkpoint(f'details_proposal_{i}') == proposals[i] for i in range(5))


def test_a_pause_during_parallel_batches_keeps_the_answers_already_sent(tmp_path):
    from project_watch.studio.llm import ModelError
    from project_watch.studio.pipeline import Pipeline
    store, pid, job = setup(tmp_path)
    execution = JobExecution(store, job)
    execution.parallel_width = 3
    project = {'activated': True, 'status': 'active'}
    calls, lock = [], threading.Lock()
    inputs = {'input_version': 'stable', 'binding': 'allowed', 'parts': [{'text': str(i)} for i in range(72)]}

    def extract(*_args, **_kwargs):
        with lock:
            calls.append(1)
            if len(calls) == 1:
                project['status'] = 'paused'  # paused while the first requests are in flight
        time.sleep(0.05)
        return {'answer': True}
    pipeline = SimpleNamespace(_execution_state=SimpleNamespace(current=execution),
                               store=SimpleNamespace(get_project=lambda _: project), model_binding=lambda: 'allowed')

    def run():
        return Pipeline._extract_all_parts(pipeline, pid, inputs, SimpleNamespace(json=extract),
                                           'system', lambda batch: batch, 'business', lambda: inputs)
    with pytest.raises(ModelError) as paused:
        run()
    sent = len(calls)
    assert paused.value.code == 'project_paused' and 1 <= sent <= 3
    saved = sum(execution.read_checkpoint(f'business_proposal_{i}') is not None for i in range(6))
    assert saved == sent  # every request that was sent has its answer saved
    project['status'] = 'active'
    assert len(run()) == 6 and len(calls) == 6  # resumed: the saved ones replay, none is resent


def test_the_first_request_goes_alone_only_while_a_request_is_unresolved(tmp_path):
    store, _pid, job = setup(tmp_path)
    execution = JobExecution(store, job)
    execution.parallel_width = 3
    pending = list(range(7))
    assert parallel_steps.waves(execution, pending) == [[0, 1, 2], [3, 4, 5], [6]]
    with store._connect() as db:
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) VALUES (?,?,?,?,?,?)",
                   (job['id'], 'k', 'model', 'synthetic', 'unknown', '2026-11-22T00:00:00+00:00'))
    assert parallel_steps.waves(execution, pending) == [[0], [1, 2, 3], [4, 5, 6]]


def test_a_job_never_has_more_model_requests_in_flight_than_its_width(tmp_path):
    from project_watch.studio.http import Response
    store, _pid, job = setup(tmp_path)
    execution = JobExecution(store, job)
    execution.parallel_width = 2
    gauge = Gauge()
    gauge.expect = 2

    def send(i):
        def transport():
            gauge.step(i, 0.05)()
            return Response(200, {}, b'{}')
        return lambda: execution.call('model', f'k{i}', transport)
    # Nested parallel steps (4 threads) still keep at most two requests on the wire.
    parallel_steps.run(SimpleNamespace(parallel_width=4), [send(i) for i in range(8)], cautious=False)
    assert gauge.peak == 2


def unresolved_row(store, job, status='unknown'):
    with store._connect() as db:
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) VALUES (?,?,?,?,?,?)",
                   (job['id'], 'held', 'model', 'synthetic', status, '2026-11-22T00:00:00+00:00'))


def test_with_an_unresolved_request_the_first_batch_goes_alone(tmp_path):
    from project_watch.studio.llm import ModelError
    from project_watch.studio.pipeline import Pipeline
    store, pid, job = setup(tmp_path)
    execution = JobExecution(store, job)
    execution.parallel_width = 3
    unresolved_row(store, job)
    calls = []
    inputs = {'input_version': 'stable', 'binding': 'allowed', 'parts': [{'text': str(i)} for i in range(48)]}

    def extract(*_args, **_kwargs):
        calls.append(1)
        raise ModelError('held', code='unknown_not_resent')  # the first pending batch is the held one
    pipeline = SimpleNamespace(_execution_state=SimpleNamespace(current=execution),
                               store=SimpleNamespace(get_project=lambda _: {'activated': True, 'status': 'active'}),
                               model_binding=lambda: 'allowed')
    skipped = []
    with pytest.raises(ModelError):
        Pipeline._extract_all_parts(pipeline, pid, inputs, SimpleNamespace(json=extract), 'system', lambda b: b,
                                    'competition', lambda: inputs, isolate_failures=False, skipped=skipped)
    assert len(calls) == 1  # nothing else was paid for


def test_a_request_waiting_for_its_turn_is_checked_for_pause_when_it_gets_it(tmp_path):
    from project_watch.studio import dispatch
    from project_watch.studio.http import Response
    store, pid, job = setup(tmp_path)
    execution = JobExecution(store, job)
    execution.parallel_width = 1
    release, outcome = threading.Event(), {}

    def slow():
        release.wait(5)
        return Response(200, {}, b'{}')

    def second():
        try:
            execution.call('model', 'second', lambda: Response(200, {}, b'{}'))
            outcome['second'] = 'sent'
        except dispatch.NotSent as exc:
            outcome['second'] = exc.code
    first = threading.Thread(target=lambda: execution.call('model', 'first', slow))
    first.start()
    time.sleep(0.2)
    waiting = threading.Thread(target=second)
    waiting.start()
    time.sleep(0.2)
    with store._connect() as db:
        db.execute("UPDATE studio_projects SET status='paused' WHERE project_id=?", (pid,))
    release.set()
    first.join(5)
    waiting.join(5)
    assert outcome['second'] == 'project_paused'
    with store._connect() as db:
        assert [tuple(r) for r in db.execute("SELECT status FROM studio_job_calls WHERE job_id=?", (job['id'],))] == [('completed',)]


def test_parallel_steps_run_with_the_job_context(tmp_path):
    from project_watch.studio.app import create_studio_app
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    execution = SimpleNamespace(_guard=lambda db: None)
    seen = []
    execution.parallel_width = 2
    execution.thread_scope = pipeline._thread_scope(execution)

    def probe():
        seen.append((getattr(pipeline._execution_state, 'current', None),
                     getattr(pipeline.store._write_context, 'guard', None)))
    parallel_steps.run(execution, [probe, probe], cautious=False)
    assert seen == [(execution, execution._guard)] * 2
    assert getattr(pipeline._execution_state, 'current', None) is None  # nothing leaks to this thread
