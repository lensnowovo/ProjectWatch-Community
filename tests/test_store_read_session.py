from contextlib import closing

from project_watch.repository import ProjectRepository
from project_watch.studio.store import StudioStore, shared_reads


def _store(tmp_path):
    return StudioStore(ProjectRepository(tmp_path / "s.sqlite3").db_path)


def test_read_session_reuses_one_connection_and_closes_it(tmp_path):
    store = _store(tmp_path)
    with store.read_session():
        with closing(store._connect()) as first:
            pass
        with closing(store._connect()) as second:
            assert second is first
            assert second.execute("SELECT 1").fetchone()[0] == 1
    with closing(store._connect()) as later:
        assert later is not first


def test_borrower_leftovers_roll_back_like_a_real_close(tmp_path):
    store = _store(tmp_path)
    with store.read_session():
        with closing(store._connect()) as db:
            db.execute("BEGIN")
            db.execute("INSERT INTO studio_state VALUES ('leftover','1')")
        with closing(store._connect()) as db, db:
            assert not db.in_transaction
            db.execute("INSERT INTO studio_state VALUES ('kept','1')")
    assert store.get_state("leftover") is None
    assert store.get_state("kept") == "1"


def test_mid_transaction_and_fenced_callers_get_their_own_connection(tmp_path):
    store = _store(tmp_path)
    with store.read_session():
        with closing(store._connect()) as outer:
            outer.execute("BEGIN")
            with closing(store._connect()) as inner:
                assert inner is not outer
            outer.rollback()
        with store.write_scope(lambda db: None), closing(store._connect()) as fenced:
            assert fenced is not outer and fenced.guard is not None


def test_shared_reads_wraps_an_endpoint(tmp_path):
    store = _store(tmp_path)

    @shared_reads(store)
    def endpoint(project_id: str):
        with closing(store._connect()) as a, closing(store._connect()) as b:
            return project_id, a is b

    assert endpoint("p") == ("p", True)
    assert endpoint.__wrapped__.__name__ == "endpoint"
