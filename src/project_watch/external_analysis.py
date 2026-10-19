"""Bounded evidence contract for external analysis; no dispatch, storage or fetch.

This module deliberately cannot authorize a call. Integration must bind the exact
packet to separate external-analysis consent and recheck live versions before
egress and publication. Matching a quote does not prove semantic entailment.
"""
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RULE_VERSION = 'external-analysis-v1'
OUTPUT_TOKENS = 3072
MAX_TEXT_CHARS = 14_000


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Selection(Strict):
    source_id: str = Field(min_length=1, max_length=100)
    ordinal: int = Field(ge=1)
    offset: int = Field(ge=0)
    length: int = Field(ge=4, le=3000)


class Reference(Strict):
    ref_id: str = Field(min_length=1, max_length=100)
    kind: Literal['internal', 'external']
    text: str = Field(min_length=4, max_length=4000, pattern=r'\S')
    source_version: str = Field(pattern=r'^[0-9a-f]{64}$')
    locator: str = Field(min_length=1, max_length=500)
    source_id: str | None = Field(default=None, min_length=1, max_length=100)
    ordinal: int | None = Field(default=None, ge=1)
    offset: int | None = Field(default=None, ge=0)
    excerpt_clipped: bool = False

    @model_validator(mode='after')
    def scope(self):
        private_fields = (self.source_id, self.ordinal, self.offset)
        if self.kind == 'internal':
            if any(v is None for v in private_fields) or len(self.text) > 3000:
                raise ValueError('material locator required')
        elif any(v is not None for v in private_fields) or self.ref_id not in {'news:title', 'news:summary'}:
            raise ValueError('invalid public reference')
        return self


class Packet(Strict):
    rule_version: Literal['external-analysis-v1']
    project_id: str = Field(min_length=1, max_length=100)
    signal_id: str = Field(min_length=1, max_length=100)
    signal_version: str = Field(pattern=r'^[0-9a-f]{64}$')
    category: Literal['company', 'sector', 'competitors', 'policy', 'industry']
    term: str = Field(min_length=1, max_length=160)
    published_at: str = Field(min_length=1, max_length=80)
    source_url: str = Field(min_length=1, max_length=4096)
    content_scope: Literal['headline_and_feed_summary_only']
    references: list[Reference] = Field(min_length=1, max_length=5)

    @model_validator(mode='after')
    def bounds(self):
        refs = self.references
        if (len({r.ref_id for r in refs}) != len(refs)
                or not any(r.ref_id == 'news:title' and r.kind == 'external' for r in refs)
                or sum(r.kind == 'internal' for r in refs) > 3
                or sum(len(r.text) for r in refs) > MAX_TEXT_CHARS
                or any(r.source_version != self.signal_version for r in refs if r.kind == 'external')):
            raise ValueError('invalid external packet scope')
        return self


def selected_material_references(repo, project_id, selections):
    """Validate explicit spans even when there are no public stories yet."""
    if not isinstance(selections, (list, tuple)) or len(selections) > 3:
        raise ValueError('select at most three material spans')
    selected = [Selection.model_validate(s) for s in selections]
    refs = []
    for i, span in enumerate(selected):
        source = repo.get_source(span.source_id)
        if source is None or source.project_id != project_id or source.kind != 'document':
            raise ValueError('material is outside project')
        fragment = next((f for f in repo.list_fragments(source.id)
                         if f.ordinal == span.ordinal and f.project_id == project_id and f.source_id == source.id), None)
        if fragment is None or span.offset + span.length > len(fragment.text):
            raise ValueError('material span no longer exists')
        text = fragment.text[span.offset:span.offset + span.length]
        refs.append(Reference.model_validate(dict(ref_id=f'material:{i}', kind='internal', text=text,
                    source_version=source.content_sha256, locator=fragment.locator,
                    source_id=source.id, ordinal=span.ordinal, offset=span.offset,
                    excerpt_clipped=span.offset > 0 or span.length < len(fragment.text))).model_dump())
    return refs


def parse_packet(packet):
    if packet.get('rule_version') == 'external-analysis-v2':
        from project_watch.external_article_packet import ArticlePacket
        return ArticlePacket.model_validate(packet)
    return Packet.model_validate(packet)


def build_external_packet(repo, project_id, signal_id, *, selections=(), read_articles=False):
    """Read-only preparation, not consent. No automatic private-context inclusion."""
    from project_watch.external_focus import external_reading

    materials = selected_material_references(repo, project_id, selections)
    view = external_reading(repo, project_id)
    item = next((s for g in view['categories'] for s in g['items']
                 if s['id'] == signal_id and s['project_id'] == project_id), None)
    if item is None:
        raise ValueError('public story is not currently eligible in this project')
    published = item['published_at'].isoformat()
    version = digest({k: item[k] for k in ('id', 'title', 'summary', 'url', 'category', 'match_term')}
                     | {'published_at': published})
    refs = []
    for ref_id, locator, text, limit in (
        ('news:title', 'headline', item['title'], 1000),
        ('news:summary', 'feed summary', item.get('summary') or '', 4000),
    ):
        if len(text.strip()) >= 4:
            refs.append(dict(ref_id=ref_id, kind='external', text=text[:limit],
                             source_version=version, locator=locator, excerpt_clipped=len(text) > limit))
    refs.extend(materials)
    packet = Packet.model_validate(dict(
        rule_version=RULE_VERSION, project_id=project_id, signal_id=signal_id,
        signal_version=version, category=item['category'], term=item['match_term'],
        published_at=published, source_url=item['url'],
        content_scope='headline_and_feed_summary_only', references=refs,
    )).model_dump()
    if read_articles:
        from project_watch.external_article_packet import attach_article
        from project_watch.public_article_store import ArticleStore
        return attach_article(packet, ArticleStore(repo, initialize=False).get(project_id, signal_id))
    return packet


def external_packet_current(repo, packet):
    """Integration gate: call both immediately before sending and before publishing."""
    try:
        parsed = parse_packet(packet)
        selections = [dict(source_id=r.source_id, ordinal=r.ordinal, offset=r.offset, length=len(r.text))
                      for r in parsed.references if r.kind == 'internal']
        current = build_external_packet(repo, parsed.project_id, parsed.signal_id, selections=selections,
                                        read_articles=parsed.rule_version == 'external-analysis-v2')
        return digest(current) == digest(parsed.model_dump())
    except (ValueError, LookupError):
        return False


class Citation(Strict):
    ref_id: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=4, max_length=600, pattern=r'\S')


class Statement(Strict):
    text: str = Field(min_length=1, max_length=350, pattern=r'\S')
    evidence: list[Citation] = Field(min_length=1, max_length=3)


class Relationship(Statement):
    assessment: Literal['potentially_relevant', 'not_relevant', 'unclear']


class Implication(Strict):
    hypothesis: str = Field(min_length=1, max_length=350, pattern=r'\S')
    condition: str = Field(min_length=1, max_length=250, pattern=r'\S')
    evidence: list[Citation] = Field(min_length=2, max_length=4)


class Analysis(Strict):
    source_statements: list[Statement] = Field(min_length=1, max_length=4)
    relationship: Relationship
    implications: list[Implication] = Field(max_length=3)
    unknowns: list[str] = Field(min_length=1, max_length=6)


class AnalysisEvidenceError(ValueError):
    """A fixed diagnostic category, without generated content or reference text."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def validate_external_analysis(packet, output):
    parsed = parse_packet(packet)
    if len(json.dumps(output, ensure_ascii=False)) > 24_000:
        raise ValueError('analysis output exceeds limit')
    analysis = Analysis.model_validate(output)
    refs = {r.ref_id: r for r in parsed.references}

    def numbers(text):
        return set(re.findall(r'\d+(?:[.,]\d+)*%?', text))

    def evidence(item, text, *, scope):
        kinds, quotes, citations = set(), [], []
        for cite in item.evidence:
            ref = refs.get(cite.ref_id)
            if ref is None or cite.quote not in ref.text:
                raise AnalysisEvidenceError('citation_mismatch', 'analysis citation is outside selected evidence')
            kinds.add(ref.kind)
            quotes.append(cite.quote)
            citations.append({'ref_id': cite.ref_id, 'quote': cite.quote})
        if scope == 'public' and kinds != {'external'}:
            raise AnalysisEvidenceError('evidence_scope', 'public statements require only external evidence')
        if scope == 'both' and kinds != {'external', 'internal'}:
            raise AnalysisEvidenceError('evidence_scope', 'project relationship requires both sides of evidence')
        if 'external' not in kinds:
            raise AnalysisEvidenceError('evidence_scope', 'external evidence or supported numbers missing')
        if not numbers(text) <= numbers(' '.join(quotes)):
            raise AnalysisEvidenceError('unsupported_number', 'external evidence or supported numbers missing')
        return citations

    statements = [{'kind': 'source_statement', 'text': s.text,
                   'evidence': evidence(s, s.text, scope='public')} for s in analysis.source_statements]
    relation = analysis.relationship
    link = dict(assessment=relation.assessment, text=relation.text,
                evidence=evidence(relation, relation.text,
                                  scope='any' if relation.assessment == 'unclear' else 'both'))
    if relation.assessment != 'potentially_relevant' and analysis.implications:
        raise AnalysisEvidenceError('unsupported_impact', 'no project impact without supported potential relationship')
    implications = [{'kind': 'conditional_inference', 'hypothesis': i.hypothesis, 'condition': i.condition,
                     'evidence': evidence(i, i.hypothesis + ' ' + i.condition, scope='both')}
                    for i in analysis.implications]
    for unknown in analysis.unknowns:
        if not unknown.strip() or len(unknown) > 250:
            raise AnalysisEvidenceError('output_schema', 'invalid unknown length')
        if not numbers(unknown) <= numbers(' '.join(r.text for r in refs.values())):
            raise AnalysisEvidenceError('unsupported_number', 'unknown contains an unsupported number')
    return dict(rule_version=parsed.rule_version, project_id=parsed.project_id, signal_id=parsed.signal_id,
                packet_digest=digest(parsed.model_dump()), content_scope=parsed.content_scope,
                origin='ai_synthesis', review_status='unreviewed', relationship_verified=False,
                policy_applicability_verified=False, support_status='quotes_located_not_semantic_verification',
                source_statements=statements, relationship=link, implications=implications,
                unknowns=analysis.unknowns)


def external_analysis_prompt(packet):
    from project_watch.external_analysis_compact import compact_prompt
    return compact_prompt(packet)
