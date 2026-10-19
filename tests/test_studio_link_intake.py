"""Adding materials by URL: readable pages and PDFs become documents; blocked pages explain why. Synthetic."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from project_watch.feeds import FeedError
from project_watch.studio import link_intake

ARTICLE = ("<html><head><title>光启视界获新势力车企定点</title></head><body><article>"
           + "<p>光启视界宣布其 905nm 发射模组获得某新势力车企定点，预计明年量产，单车价值量约两千元。</p>" * 3
           + "</article></body></html>")


class Page:
    def __init__(self, body: bytes, ctype: str, url: str) -> None:
        self.body, self.headers, self.url = body, {"content-type": ctype}, url


class Reader:
    def __init__(self, pages):
        self.transport = self
        self.pages = pages

    def get_response(self, url):
        value = self.pages.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise FeedError("http_failure")
        return value


def test_readable_page_becomes_a_cited_text_document():
    reader = Reader({"https://news.example/a": Page(ARTICLE.encode(), "text/html; charset=utf-8", "https://news.example/a")})
    name, data = link_intake.fetch(reader, "news.example/a")
    text = data.decode()
    assert name == "光启视界获新势力车企定点.txt"
    assert "来源网址：https://news.example/a" in text and "905nm 发射模组" in text


def test_pdf_links_are_kept_as_pdf():
    reader = Reader({"https://ir.example/report.pdf": Page(b"%PDF-1.4 synthetic", "application/pdf", "https://ir.example/report.pdf")})
    assert link_intake.fetch(reader, "https://ir.example/report.pdf") == ("report.pdf", b"%PDF-1.4 synthetic")


@pytest.mark.parametrize("value,expected", [
    (Page(b"<html><body><p>\xe8\xaf\xb7\xe7\x99\xbb\xe5\xbd\x95</p></body></html>", "text/html; charset=utf-8", "u"), "正文"),
    (FeedError("disallowed_address"), "公网"),
    (Page(b"{}", "application/json", "u"), "不是网页"),
])
def test_unreadable_links_say_why(value, expected):
    with pytest.raises(link_intake.LinkError) as exc:
        link_intake.fetch(Reader({"https://x.example/": value}), "https://x.example/")
    assert expected in str(exc.value)
    with pytest.raises(link_intake.LinkError):
        link_intake.normalise("ftp://x.example/file")


def test_link_api_queues_intake(tmp_path):
    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    app.state.studio_pipeline.article_reader = Reader({"https://news.example/a": Page(ARTICLE.encode(), "text/html; charset=utf-8", "https://news.example/a")})
    c = TestClient(app)
    project = c.post("/api/studio/projects", json={"entity": {"legal_name": "光启视界"}, "consent": True}).json()
    r = c.post(f"/api/studio/projects/{project['id']}/links", json={"url": "https://news.example/a"})
    assert r.status_code == 202 and r.json()["name"].endswith(".txt")
    assert c.post(f"/api/studio/projects/{project['id']}/links", json={"url": "https://news.example/missing"}).status_code == 422
