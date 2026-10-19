"""Discovery diversity is retrieval priority only, not an evidence-family claim."""
from copy import deepcopy

import pytest
from test_studio_readable_original_leads import prepare

LONG = 'Synthetic Optics makes optical organs for measurement. ' * 7


def lead(index, snippet=LONG, *, host=None):
    return {'url': f'https://{host or "publisher-" + str(index) + ".test"}/{index}',
            'title': 'Synthetic Optics organs', 'snippet': snippet}


@pytest.mark.parametrize('repeat', [LONG, 'Registry metadata. ' + LONG + ' Other registry metadata.'])
def test_long_repeated_excerpt_is_deferred_not_dropped(tmp_path, repeat):
    pipeline, pid, reads, execution = prepare(tmp_path)
    hits = [lead(0), lead(1, repeat), lead(2, 'New hospital product release.'),
            lead(3, 'New manufacturing process release.')]
    before = deepcopy(hits)
    report = []
    result = pipeline.competition_research.originals(pid, hits, execution=execution, report=report)
    assert reads == [hits[i]['url'] for i in (0, 2, 3)]
    assert len(result) == 3
    assert execution.usage()['page']['requests'] == 3
    assert hits == before
    assert all('family_basis' not in r and 'independent' not in r for r in report)
    assert pipeline.competition_research.originals(pid, hits, execution=execution) == result
    assert len(reads) == 3


def test_whitespace_only_repetition_uses_same_priority_hint(tmp_path):
    pipeline, pid, reads, execution = prepare(tmp_path)
    hits = [lead(0), lead(1, LONG.replace(' ', '\n\t')), lead(2, 'Different content'), lead(3, 'More content')]
    pipeline.competition_research.originals(pid, hits, execution=execution)
    assert reads == [hits[i]['url'] for i in (0, 2, 3)]


def test_different_metadata_wrappers_leave_long_shared_excerpt(tmp_path):
    pipeline, pid, reads, execution = prepare(tmp_path)
    hits = [lead(0, 'Introduction. ' + LONG + ' End introduction.'),
            lead(1, 'Company business. ' + LONG + ' Registry details.'),
            lead(2, 'New clinical publication'), lead(3, 'Manufacturing release')]
    pipeline.competition_research.originals(pid, hits, execution=execution)
    assert reads == [hits[i]['url'] for i in (0, 2, 3)]


def test_matching_short_fragments_do_not_accumulate_into_duplicate(tmp_path):
    pipeline, pid, reads, execution = prepare(tmp_path)
    a = ('common phrase' * 8 + ' A DIFFERENT SECTION ') * 4
    b = ('common phrase' * 8 + ' B DIFFERENT SECTION ') * 4
    hits = [lead(0, a), lead(1, b), lead(2, 'New evidence'), lead(3, 'More evidence')]
    pipeline.competition_research.originals(pid, hits, execution=execution)
    assert reads == [h['url'] for h in hits[:3]]


@pytest.mark.parametrize('snippet', ['Short common product statement.', 'x' * 199])
def test_short_identical_snippets_do_not_infer_repetition(tmp_path, snippet):
    pipeline, pid, reads, execution = prepare(tmp_path)
    hits = [lead(i, snippet) for i in range(4)]
    pipeline.competition_research.originals(pid, hits, execution=execution)
    assert reads == [h['url'] for h in hits[:3]]


def test_same_publisher_with_different_excerpts_keeps_order(tmp_path):
    pipeline, pid, reads, execution = prepare(tmp_path)
    hits = [lead(i, chr(65 + i) * 250, host='same.test') for i in range(4)]
    pipeline.competition_research.originals(pid, hits, execution=execution)
    assert reads == [h['url'] for h in hits[:3]]


def test_deferred_bridge_cannot_make_a_transitive_group(tmp_path):
    pipeline, pid, reads, execution = prepare(tmp_path)
    a, b = 'a' * 220, 'b' * 220
    hits = [lead(0, a), lead(1, a + b), lead(2, b), lead(3, 'Different release')]
    pipeline.competition_research.originals(pid, hits, execution=execution)
    assert reads == [hits[i]['url'] for i in (0, 2, 3)]


def test_deferred_leads_fill_spare_slots_in_original_order(tmp_path):
    pipeline, pid, reads, execution = prepare(tmp_path)
    hits = [lead(i) for i in range(4)]
    pipeline.competition_research.originals(pid, hits, execution=execution)
    assert reads == [h['url'] for h in hits[:3]]


def test_lexical_priority_precedes_excerpt_diversity(tmp_path):
    pipeline, pid, reads, execution = prepare(tmp_path)
    hits = [lead(0, 'No target product'), lead(1, LONG), lead(2, LONG), lead(3, 'New organs release')]
    hits[0]['title'] = 'Unrelated material'
    for i in (1, 2, 3):
        hits[i]['title'] = 'Synthetic Optics Organ A'
    context = {'names': ['Synthetic Optics'], 'products': ['Organ A'], 'uses': []}
    pipeline.competition_research.originals(pid, hits, execution=execution, context=context)
    assert reads == [hits[i]['url'] for i in (1, 3, 0)]
