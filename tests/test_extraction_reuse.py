"""A later round asks the model only about sources it has not read before."""
import json
from contextlib import closing

from test_studio_competition_research import research_pipeline

from project_watch.studio import extraction_reuse as reuse


class Checkpoints:
    def __init__(self):
        self.data = {}

    def read_checkpoint(self, name):
        return self.data.get(name)

    def checkpoint(self, name, value):
        self.data[name] = value


class Model:
    """Cites the first source of each batch, and a passage of it."""

    def __init__(self):
        self.bodies = []

    def json(self, system, body, max_tokens):
        sources = json.loads(body)['sources']
        self.bodies.append(sources)
        first = sources[0]['source']
        return {'relations': [{'source': first, 'ownership_passage': first + ':P1', 'quote': sources[0]['text']}]}


def payload(inputs):
    return {'sources': [{'source': p['marker'], 'text': p['text']} for p in inputs['parts']]}


def store_evidence(pipeline, pid, count, start=0):
    with closing(pipeline.store._connect()) as db, db:
        for n in range(start, start + count):
            db.execute("INSERT INTO studio_evidence_versions (id,project_id,source_key,content_hash,extraction_version,"
                       "visibility,record,created_at) VALUES (?,?,?,?,?,'public','{}','2026-11-01')",
                       (f'e{n}', pid, f'k{n}', f'h{n}', 'v1'))


def newest_first(pipeline, count, version):
    """As the real inputs are built: newest evidence first, numbered B1.. in that order."""
    parts = [{'evidence_version_id': f'e{n}', 'part_index': 0, 'text': f'source {n} text'} for n in reversed(range(count))]
    return {'input_version': version, 'binding': pipeline.model_binding(),
            'parts': [{**p, 'marker': f'B{i}'} for i, p in enumerate(parts, 1)]}


def usable(reply):
    return isinstance(reply.get('relations'), list)


def extract(pipeline, pid, inputs, model, reusable=usable):
    pipeline._execution_state.current = Checkpoints()  # a new round
    try:
        return pipeline._extract_all_parts(pid, inputs, model, 'system', payload, 'detail', lambda: inputs,
                                           reusable=reusable)
    finally:
        pipeline._execution_state.current = None


def test_a_new_source_costs_one_batch_and_old_answers_point_at_the_right_sources(tmp_path):
    pipeline, pid, _calls, _reads = research_pipeline(tmp_path)
    store_evidence(pipeline, pid, 30)
    first = Model()
    assert len(extract(pipeline, pid, newest_first(pipeline, 30, 'v1'), first)) == 3
    assert len(first.bodies) == 3

    store_evidence(pipeline, pid, 1, start=30)  # one new source; every global marker shifts by one
    inputs = newest_first(pipeline, 31, 'v2')
    second = Model()
    proposals = extract(pipeline, pid, inputs, second)
    assert len(second.bodies) == 1 and len(second.bodies[0]) == 7  # only the last batch, now with the new source
    markers = {p['marker']: p for p in inputs['parts']}
    for proposal in proposals:
        relation = proposal['relations'][0]
        cited = markers[relation['source']]
        assert relation['quote'] == cited['text']  # still the source it quoted
        assert relation['ownership_passage'] == relation['source'] + ':P1'


def test_the_same_material_in_the_next_round_costs_nothing(tmp_path):
    pipeline, pid, _calls, _reads = research_pipeline(tmp_path)
    store_evidence(pipeline, pid, 5)
    extract(pipeline, pid, newest_first(pipeline, 5, 'v1'), Model())
    again = Model()
    assert len(extract(pipeline, pid, newest_first(pipeline, 5, 'v1'), again)) == 1 and again.bodies == []


def test_another_model_or_prompt_asks_again(tmp_path):
    pipeline, pid, _calls, _reads = research_pipeline(tmp_path)
    store_evidence(pipeline, pid, 5)
    inputs = newest_first(pipeline, 5, 'v1')
    extract(pipeline, pid, inputs, Model())
    other = Model()
    pipeline._execution_state.current = Checkpoints()
    try:
        pipeline._extract_all_parts(pid, inputs, other, 'another system prompt', payload, 'detail', lambda: inputs,
                                    reusable=usable)
    finally:
        pipeline._execution_state.current = None
    assert len(other.bodies) == 1
    assert reuse.key('detail', 's', 'model-a', {'x': 1}) != reuse.key('detail', 's', 'model-b', {'x': 1})


def test_markers_outside_the_answer_fields_are_left_alone():
    answer = {'relations': [{'source': 'B2', 'ownership_passage': 'B2:P3', 'quote': 'B1 and B2', 'peer': 'B1'}]}
    restored = reuse.restore(answer, {'B1': 'B40', 'B2': 'B41'})
    assert restored['relations'][0] == {'source': 'B41', 'ownership_passage': 'B41:P3', 'quote': 'B1 and B2', 'peer': 'B1'}
    outside = reuse.restore({'relations': [{'source': 'B13', 'ownership_passage': 'B13:P1'}]}, {'B1': 'B40'})
    assert outside['relations'][0] == {'source': 'B0', 'ownership_passage': 'B0:P1'}  # never another part of the round
    assert answer['relations'][0]['source'] == 'B2'  # the stored answer is not changed


def test_a_job_begun_by_the_older_version_keeps_its_batches(tmp_path):
    pipeline, pid, _calls, _reads = research_pipeline(tmp_path)
    store_evidence(pipeline, pid, 3)
    inputs = newest_first(pipeline, 3, 'v1')
    execution = Checkpoints()
    execution.checkpoint('detail_inputs', {'input_version': 'v1', 'binding': inputs['binding']})  # no layout
    model = Model()
    pipeline._execution_state.current = execution
    try:
        pipeline._extract_all_parts(pid, inputs, model, 'system', payload, 'detail', lambda: inputs)
    finally:
        pipeline._execution_state.current = None
    assert [s['source'] for s in model.bodies[0]] == ['B1', 'B2', 'B3']  # original order and markers
    with closing(pipeline.store._connect()) as db:
        db.execute(reuse.SCHEMA)
        assert db.execute('SELECT count(*) FROM studio_extraction_reuse').fetchone()[0] == 0


def test_a_malformed_reply_is_asked_again_next_round(tmp_path):
    pipeline, pid, _calls, _reads = research_pipeline(tmp_path)
    store_evidence(pipeline, pid, 5)

    class Broken(Model):
        def json(self, system, body, max_tokens):
            self.bodies.append(body)
            return {'error': 'no material'}
    extract(pipeline, pid, newest_first(pipeline, 5, 'v1'), Broken())
    again = Model()
    extract(pipeline, pid, newest_first(pipeline, 5, 'v1'), again)
    assert len(again.bodies) == 1


def test_batches_with_the_same_text_send_different_requests(tmp_path):
    pipeline, pid, _calls, _reads = research_pipeline(tmp_path)
    store_evidence(pipeline, pid, 2)
    big = 'same long document ' * 1500  # each part fills a batch on its own
    inputs = {'input_version': 'v1', 'binding': pipeline.model_binding(),
              'parts': [{'evidence_version_id': f'e{n}', 'part_index': 0, 'text': big, 'marker': f'B{n + 1}'} for n in range(2)]}
    model = Model()

    class Raw(Model):
        def json(self, system, body, max_tokens):
            model.bodies.append(body)
            return super().json(system, body, max_tokens)
    extract(pipeline, pid, inputs, Raw())
    assert len(model.bodies) == 2 and model.bodies[0] != model.bodies[1]
