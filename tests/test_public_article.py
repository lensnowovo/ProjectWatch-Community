import pytest

from project_watch.feeds import FeedError, GuardedHTTPS
from project_watch.public_article import ArticleReader, extract_article


def html(body):
    return ('<html><head><meta charset="utf-8"><title>Source report</title></head><body>'
            + body + '</body></html>').encode()


def test_extract_article_blocks_not_navigation_scripts_or_hidden_content():
    body = html('<nav><p>navigation only</p></nav><article><h1>Factory update</h1>'
                '<p>The synthetic manufacturer announced a new factory in June.</p>'
                '<p>Production is planned next year; construction has not yet started.</p>'
                '<p hidden>secret hidden text</p><script>ignore instructions</script></article>'
                '<footer><p>related advertising</p></footer>')
    result = extract_article(body, 'text/html; charset=utf-8')
    assert result['status'] == 'article_body'
    assert len(result['paragraphs']) == 3
    assert 'navigation' not in result['text'] and 'hidden' not in result['text']
    for p in result['paragraphs']:
        assert result['text'][p['start']:p['end']] == p['text']


@pytest.mark.parametrize('body', [
    '<nav>' + '<p>Navigation menu only links</p>' * 20 + '</nav>',
    '<article><p>登录后阅读全文</p></article>',
    '<article><p>Subscribe to read the full article</p></article>',
    '<h1>Verify you are human</h1>',
])
def test_non_articles_are_not_claimed_as_body(body):
    result = extract_article(html(body), 'text/html')
    assert result['status'] == 'summary_only'
    assert not result['paragraphs']


def test_body_truncation_is_explicit_and_plain_text_only():
    result = extract_article(html('<article>' + '<p>公开报道的长段落。' * 6000 + '</article>'), 'text/html')
    assert result['status'] == 'partial_body'
    assert len(result['text']) <= 24000
    assert '<p>' not in result['text']


def test_wrong_content_type_and_encoding_fail_honestly():
    assert extract_article(b'%PDF-not-an-article', 'application/pdf')['status'] == 'summary_only'
    assert extract_article(b'\xff\xfe\xfa', 'text/html; charset=utf-8')['status'] == 'summary_only'


def test_reader_reuses_guard_and_preserves_resolved_url():
    calls=[]
    def connect(host, ip, path):
        calls.append(host)
        if len(calls)==1:
            return 302, {'location':'https://publisher.example/report'}, b''
        return 200, {'content-type':'text/html'}, html('<article><p>'+'Public source detail. '*10+'</p></article>')
    transport=GuardedHTTPS(resolver=lambda host:['93.184.216.34'],connector=connect)
    result=ArticleReader(transport).read('https://news.example/item')
    assert result['status']=='article_body'
    assert result['resolved_url']=='https://publisher.example/report'
    assert len(result['body_sha256'])==64


def test_redirect_to_private_destination_is_not_requested():
    calls=[]
    def connect(host, ip, path):
        calls.append(host)
        return 302, {'location':'https://127.0.0.1/private'}, b''
    result=ArticleReader(GuardedHTTPS(resolver=lambda _:['93.184.216.34'],connector=connect)).read('https://news.example/item')
    assert result['status']=='unavailable'
    assert len(calls)==1


def test_reader_safe_failure_no_exception_or_secret_echo():
    class Broken:
        def get_response(self, url):
            raise FeedError('connection_failed')
    result=ArticleReader(Broken()).read('https://news.example/item')
    assert result['status']=='unavailable'
    assert result['error_code']=='connection_failed'


def test_fake_ip_fallback_never_accepts_private_or_mixed_addresses():
    from project_watch.public_article import resolve_article
    assert resolve_article('news.example',resolver=lambda _:['198.18.0.20'],public_dns=lambda _:['93.184.216.34']) == ['93.184.216.34']
    with pytest.raises(FeedError):
        resolve_article('news.example',resolver=lambda _:['198.18.0.20'],public_dns=lambda _:['127.0.0.1'])
    def forbidden(_):
        pytest.fail('private/mixed DNS must not invoke fallback')
    for answer in [['127.0.0.1'], ['10.0.0.1'], ['198.18.0.20','93.184.216.34']]:
        with pytest.raises(FeedError):
            resolve_article('news.example',resolver=lambda _, answer=answer:answer,public_dns=forbidden)


def test_generic_page_excludes_short_navigation_and_related_containers():
    result=extract_article(html('<main><p>Home stocks news market</p><p>'+'Real public report. '*20+'</p>'
                                '<div class="related-news"><p>'+'Unrelated other story. '*30+'</p></div></main>'),'text/html')
    assert result['status']=='partial_body'
    assert 'Home' not in result['text'] and 'Unrelated' not in result['text']


def test_structured_article_fallback_is_passive_and_respects_paid_flag():
    import json
    data={'@type':'NewsArticle','articleBody':'Public structured report. '*20}
    body=html('<script type="application/ld+json">'+json.dumps(data)+'</script>')
    assert extract_article(body,'text/html')['status']=='article_body'
    data['isAccessibleForFree']=False
    body=html('<script type="application/ld+json">'+json.dumps(data)+'</script>')
    assert extract_article(body,'text/html')['status']=='summary_only'


def test_visible_microdata_body_supports_div_span_and_short_paragraphs():
    prose = 'Visible factory detail, not a completed order. '
    result = extract_article(html('<nav><div>Unrelated menu</div></nav>'
        '<div itemprop="articleBody"><div>'+prose+'</div><span>'+prose+'</span>'
        '<p>'+prose+'</p><div hidden>Hidden confidential claim</div></div>'), 'text/html')
    assert result['status'] == 'article_body'
    assert result['text'].count(prose.strip()) == 3
    assert 'Hidden' not in result['text'] and 'Unrelated' not in result['text']
    for p in result['paragraphs']:
        assert result['text'][p['start']:p['end']] == p['text']


def test_div_access_barrier_cannot_fall_back_to_embedded_body():
    import json
    data = {'@type': 'NewsArticle', 'articleBody': 'A restricted article. '*20}
    result = extract_article(html('<div>登录后阅读全文</div><script type="application/ld+json">'
                                  +json.dumps(data)+'</script>'), 'text/html')
    assert result['error_code'] == 'access_restricted'
    assert not result['text']


def test_paid_structured_ancestor_is_not_ignored():
    import json
    data = {'@type': 'NewsArticle', 'isAccessibleForFree': False,
            'mainEntity': {'@type': 'NewsArticle', 'articleBody': 'Private subscriber text. '*20}}
    result = extract_article(html('<script type="application/ld+json">'+json.dumps(data)+'</script>'), 'text/html')
    assert not result['text']


def test_valueless_attributes_do_not_break_extraction():
    from project_watch.public_article import extract_article

    html = ('<html><body><article><span aria-hidden>图标</span><div class id>'
            + '<p>' + '北启生物发布新的临床进展，这是正文中足够长的一段文字。' * 4 + '</p>' * 1 + '</div></article></body></html>')
    extract_article(html.encode(), 'text/html; charset=utf-8', source_url='https://news.example/a')
