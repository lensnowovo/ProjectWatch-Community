"""Synthetic directory discovery; not a live coverage benchmark."""

from types import SimpleNamespace

import pytest
from test_studio_original_lookup import setup

from project_watch.feeds import PublicResponse
from project_watch.public_article import ArticleReader
from project_watch.studio.http import HttpError

ROOT = 'https://publisher.test/news/'
TITLE = 'Synthetic Optics announces Lens A product update'
CHILD = ROOT + 'product-update/'
CONTEXT = {'names': ['Synthetic Optics'], 'products': ['Lens A'], 'uses': ['drug screening']}


def reader(body, url=ROOT, media='text/html'):
    return ArticleReader(SimpleNamespace(get_response=lambda _: PublicResponse(
        url, {'content-type': media}, body.encode())))


def test_passive_directory_links_visible_same_origin_and_no_extra_fetch():
    content = f'<main><a href="product-update/">{TITLE}</a></main>'
    result = reader(content).read(ROOT)
    assert result['status'] == 'summary_only' and not result['text']
    assert result['links'] == [{'url': CHILD, 'title': TITLE}]


@pytest.mark.parametrize('anchor', [
    '<a href="https://other.test/a">', '<a href="http://publisher.test/a">',
    '<a href="https://u:p@publisher.test/a">', '<a href="https://publisher.test:444/a">',
    '<a href="javascript:alert(1)">', '<a href="#part">', '<a href="/news/">',
    '<a href="/report.pdf">', '<a href="/login/">', '<a href="/a?token=private">',
    '<a href="/a" hidden>', '<nav><a href="/a">', '<aside><a href="/a">',
    '<div style="display:none"><a href="/a">', '<script><a href="/a">',
    '<a href="https://publisher.test\\@127.0.0.1/a">',
])
def test_unsafe_or_decorative_links_not_exposed(anchor):
    assert reader(anchor + TITLE + '</a></nav></aside></div></script>').read(ROOT).get('links', []) == []


@pytest.mark.parametrize('content,media', [
    (f'<p>登录后阅读全文</p><a href="/a">{TITLE}</a>', 'text/html'),
    (f'<a href="/a">{TITLE}</a>', 'application/pdf'),
])
def test_barriers_and_non_html_do_not_reveal_discovery_links(content, media):
    assert not reader(content, media=media).read(ROOT).get('links')


def test_base_is_ignored_and_anchor_output_bounded():
    content = '<base href="https://other.test/">' + ''.join(
        f'<a href="item-{i}/">{TITLE} {i}</a>' for i in range(100))
    links = reader(content).read(ROOT)['links']
    assert len(links) == 20 and all(h['url'].startswith(ROOT) for h in links)


def directory_pipeline(tmp_path, *, budgets=None):
    pipeline, pid, reads, execution = setup(tmp_path, budgets=budgets)
    def read(url):
        reads.append(url)
        if url == ROOT:
            return reader(f'<a href="product-update/">{TITLE}</a>').read(url)
        return {'status': 'article_body', 'resolved_url': url,
                'text': 'Synthetic Optics builds Lens A for drug screening; buyers are pharma; delivery is software.' * 3}
    pipeline.article_reader = SimpleNamespace(read=read)
    return pipeline, pid, reads, execution


def test_directory_discovers_independent_original_and_replays_at_exhausted_budget(tmp_path):
    pipeline, pid, reads, execution = directory_pipeline(tmp_path, budgets={'page': 2})
    reports = []
    def no_lookup(*args, **kwargs):
        raise AssertionError('actual directory links need no title replacement search')
    kwargs = dict(execution=execution, context=CONTEXT, collectors=SimpleNamespace(web_search=no_lookup))
    lead = {'url': ROOT, 'title': 'Synthetic Optics Lens A news'}
    evidence = pipeline.competition_research.originals(pid, [lead], report=reports, **kwargs)
    assert len(evidence) == 1 and reads == [ROOT, CHILD]
    assert evidence[0]['parts'][0]['locator'] == CHILD
    assert evidence[0]['parts'][0]['text'].startswith(TITLE + '\n')
    assert reports[1]['directory_url'] == ROOT and 'discovery_url' not in reports[1]
    replay = []
    assert pipeline.competition_research.originals(pid, [lead], report=replay, **kwargs) == evidence
    assert replay == reports and reads == [ROOT, CHILD]
    assert execution.usage()['page']['requests'] == 2


def test_article_body_does_not_crawl_related_links(tmp_path):
    pipeline, pid, reads, execution = directory_pipeline(tmp_path)
    pipeline.article_reader = SimpleNamespace(read=lambda url: reads.append(url) or {
        'status': 'article_body', 'text': 'Existing article text',
        'links': [{'url': CHILD, 'title': TITLE}], 'resolved_url': ROOT})
    assert len(pipeline.competition_research.originals(pid, [{'url': ROOT}],
        execution=execution, context=CONTEXT)) == 1
    assert reads == [ROOT]


def test_no_context_or_unrelated_links_are_not_followed(tmp_path):
    pipeline, pid, reads, execution = directory_pipeline(tmp_path)
    assert pipeline.competition_research.originals(pid, [{'url': ROOT}], execution=execution) == []
    assert pipeline.competition_research.originals(pid, [{'url': ROOT}], execution=execution,
        context={'names': ['Unrelated Company'], 'products': [], 'uses': []}) == []
    assert reads == [ROOT]


def test_directory_is_only_one_level_and_company_reads_remain_three(tmp_path):
    pipeline, pid, reads, execution = directory_pipeline(tmp_path)
    links = [{'url': ROOT+f'item-{i}/', 'title': TITLE+str(i)} for i in range(8)]
    pipeline.article_reader = SimpleNamespace(read=lambda url: reads.append(url) or {
        'status': 'summary_only', 'text': '', 'error_code': 'insufficient_article_text',
        'resolved_url': url, 'links': links if url == ROOT else [{'url': CHILD, 'title': TITLE}]})
    assert pipeline.competition_research.originals(pid, [{'url': ROOT}], execution=execution, context=CONTEXT) == []
    assert len(reads) == 3 and CHILD not in reads


def test_guard_change_after_directory_prevents_child_read(tmp_path):
    pipeline, pid, reads, execution = directory_pipeline(tmp_path)
    def guard():
        if reads:
            raise ValueError('source changed')
    with pytest.raises(ValueError, match='source changed'):
        pipeline.competition_research.originals(pid, [{'url': ROOT}], execution=execution,
            context=CONTEXT, guard=guard)
    assert reads == [ROOT]


def test_unknown_child_is_not_resent_or_followed_by_other_pages(tmp_path):
    pipeline, pid, reads, execution = directory_pipeline(tmp_path)
    original = pipeline.article_reader.read
    def read(url):
        if url == CHILD:
            reads.append(url)
            raise HttpError('request_outcome_unknown')
        return original(url)
    pipeline.article_reader = SimpleNamespace(read=read)
    for _ in range(2):
        with pytest.raises(HttpError, match='request_outcome_unknown'):
            pipeline.competition_research.originals(pid, [{'url': ROOT}],
                execution=execution, context=CONTEXT)
    assert reads == [ROOT, CHILD] and execution.usage()['page']['unknown'] == 1


def test_untrusted_reader_cannot_inject_unsafe_links_into_receipts(tmp_path):
    from project_watch.studio.competition_research import read_page

    _, _, _, execution = setup(tmp_path)
    fake = SimpleNamespace(read=lambda _: {'status': 'summary_only', 'text': '',
        'error_code': 'insufficient_article_text', 'resolved_url': ROOT,
        'links': [{'url': 'https://other.test/secret?api_key=private', 'title': TITLE},
                  {'url': CHILD, 'title': TITLE, 'secret': 'private diagnostic'}]})
    result = read_page(execution, fake, ROOT)
    assert result['links'] == [{'url': CHILD, 'title': TITLE}]


def test_normal_run_automatically_searches_and_follows_directory(tmp_path):
    from test_studio_competition_research import research_pipeline

    from project_watch.studio.http import Http

    pipeline, pid, _, reads = research_pipeline(tmp_path)
    transport = pipeline.http_factory(None).transport
    queries = []
    def record(method, url, headers, body, timeout):
        import json
        if 'api.tavily.com' in url:
            queries.append(json.loads(body)['query'])
        return transport(method, url, headers, body, timeout)
    pipeline.http_factory = lambda _: Http(record)
    def read(url):
        reads.append(url)
        if url == 'https://alpha.test/product':
            return {'status': 'summary_only', 'text': '', 'resolved_url': url,
                'error_code': 'insufficient_article_text',
                'links': [{'url': 'https://alpha.test/article', 'title': TITLE}]}
        name, product = ('Synthetic Optics', 'Lens A') if 'alpha.test' in url else ('Beta', 'Organ B')
        return {'status': 'article_body', 'text': f'{name} builds {product} for drug screening; buyers are pharma; delivery is software.'}
    pipeline.article_reader = SimpleNamespace(read=read)
    pipeline.schedule_competition(pid)
    pipeline.run(pipeline.store.claim())
    outcome = pipeline.competition_research.saved(pid)
    assert 'Synthetic Optics Lens A drug screening 官网' in queries
    assert 'Synthetic Optics Lens A 官网' not in queries
    assert outcome['status'] == 'completed_bounded_batch' and outcome['supported_count'] == 1
    assert outcome['reading']['target']['body_count'] == 1
    assert outcome['reading']['target']['pages'][1]['directory_url'] == 'https://alpha.test/product'
    assert len(reads) == 3
