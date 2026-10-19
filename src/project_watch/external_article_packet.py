"""Versioned public-body contract. The legacy packet/digest remains unchanged."""
from typing import Literal

from pydantic import Field, model_validator

from project_watch.external_analysis import MAX_TEXT_CHARS, Packet, Reference, Strict

ARTICLE_RULE_VERSION = 'external-analysis-v2'


class BodyReference(Reference):
    @model_validator(mode='after')
    def scope(self):
        if self.ref_id.startswith('article:'):
            if self.kind != 'external' or any(v is not None for v in (self.source_id, self.ordinal, self.offset)):
                raise ValueError('invalid public paragraph')
            return self
        return super().scope()


class ArticleEvidence(Strict):
    snapshot_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    status: Literal['article_body', 'partial_body', 'summary_only', 'unavailable']
    resolved_url: str | None = Field(max_length=2048)
    fetched_at: float
    extracted_characters: int = Field(ge=0, le=24000)
    included_characters: int = Field(ge=0, le=MAX_TEXT_CHARS)


class ArticlePacket(Packet):
    rule_version: Literal['external-analysis-v2']
    content_scope: Literal['article_excerpts', 'headline_and_feed_summary_only']
    references: list[BodyReference] = Field(min_length=1, max_length=25)
    article: ArticleEvidence

    @model_validator(mode='after')
    def bounds(self):
        refs = self.references
        body = [r for r in refs if r.ref_id.startswith('article:')]
        ordinary = [r for r in refs if not r.ref_id.startswith('article:')]
        if (len({r.ref_id for r in refs}) != len(refs)
                or not any(r.ref_id == 'news:title' and r.kind == 'external' for r in refs)
                or sum(r.kind == 'internal' for r in refs) > 3
                or sum(len(r.text) for r in refs) > MAX_TEXT_CHARS
                or any(r.source_version != self.signal_version for r in ordinary if r.kind == 'external')
                or any(r.source_version != self.article.snapshot_digest for r in body)
                or bool(body) != (self.content_scope == 'article_excerpts')
                or (body and self.article.status not in {'article_body', 'partial_body'})
                or sum(len(r.text) for r in body) != self.article.included_characters
                or self.article.included_characters > self.article.extracted_characters):
            raise ValueError('invalid article evidence scope')
        return self


def attach_article(packet, snapshot):
    if snapshot is None or snapshot['project_id'] != packet['project_id'] or snapshot['signal_id'] != packet['signal_id']:
        raise ValueError('article attempt not yet recorded')
    refs = packet['references']
    if snapshot['paragraphs']:
        # Public body takes precedence over the publisher's feed summary. Preserve
        # the independently approved private spans byte-for-byte.
        refs = [r for r in refs if r['ref_id'] != 'news:summary']
    budget = MAX_TEXT_CHARS - sum(len(r['text']) for r in refs)
    included = 0
    for p in snapshot['paragraphs']:
        for offset in range(0, len(p['text']), 4000):
            text = p['text'][offset:offset+min(4000, budget)]
            if len(text.strip()) < 4 or len(refs) >= 25:
                break
            refs.append(dict(ref_id=f"article:{p['id']}:{offset}", kind='external', text=text,
                             source_version=snapshot['snapshot_digest'],
                             locator=f"{p['id']} · chars {p['start']+offset}–{p['start']+offset+len(text)}",
                             excerpt_clipped=offset > 0 or len(text) < len(p['text'])))
            included += len(text)
            budget -= len(text)
        if budget < 4 or len(refs) >= 25:
            break
    return ArticlePacket.model_validate(dict(packet, rule_version=ARTICLE_RULE_VERSION, references=refs,
        content_scope='article_excerpts' if included else 'headline_and_feed_summary_only',
        article=dict(snapshot_digest=snapshot['snapshot_digest'], status=snapshot['status'],
                     resolved_url=snapshot['resolved_url'], fetched_at=snapshot['fetched_at'],
                     extracted_characters=len(snapshot['text']), included_characters=included))).model_dump()
