"""Atomic checkpoint replacement and restart safety on synthetic SQLite."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
import test_studio_dsh_monitor as fixtures

from project_watch.studio.execution import JobExecution


@pytest.fixture
def execution(tmp_path):
    return fixtures.loop.__wrapped__(tmp_path).execution


def test_chunked_snapshot_survives_new_executor(execution):
    value = {'evidence': '公开材料：完整保留。' * 90000}
    execution.checkpoint_chunked('context-test', value)
    restarted = JobExecution(execution.store, execution.job)
    assert restarted.read_checkpoint('context-test') == value
    # Existing small-checkpoint callers keep their original serialization.
    execution.checkpoint('ordinary', {'a': [1, '中文']})
    assert restarted.read_checkpoint('ordinary') == {'a': [1, '中文']}


def test_failed_chunk_write_rolls_back_old_manifest_and_parts(execution):
    old = {'text': 'old' * 100000}
    execution.checkpoint_chunked('context-test', old)
    with closing(execution.store._connect()) as db, db:
        db.execute("""CREATE TRIGGER fail_new_chunk BEFORE INSERT ON studio_job_checkpoints
            WHEN NEW.stage LIKE 'cpchunk:%' AND NEW.data LIKE '%replacement%'
            BEGIN SELECT RAISE(ABORT, 'synthetic disk write failure'); END""")
    with pytest.raises(Exception, match='synthetic disk write failure'):
        execution.checkpoint_chunked('context-test', {'text': 'replacement' * 100000})
    assert execution.read_checkpoint('context-test') == old


def test_readers_never_mix_manifest_and_replaced_chunks(execution):
    choices = [{'value': n, 'body': str(n) * 250000} for n in range(3)]
    execution.checkpoint_chunked('context-test', choices[0])

    def access(n):
        other = JobExecution(execution.store, execution.job)
        if n % 4 == 0:
            other.checkpoint_chunked('context-test', choices[n % 3])
        actual = other.read_checkpoint('context-test')
        assert actual in choices

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(access, range(80)))
