"""Synthetic provider responses in the live response shapes. No provider bytes are committed."""
import re


def paper(affiliation):
    return {'hitCount': 1, 'resultList': {'result': [dict(source='MED', id='123456', title='Synthetic study',
        firstPublicationDate='2025-01-02', authorList={'author': [dict(fullName='A. Author',
            authorAffiliationDetailsList={'authorAffiliation': [dict(affiliation=affiliation)]})]})]}}


def patents():
    return {'hitCount': 2, 'resultList': {'result': [
        dict(source='PAT', id='WO2099123456', title='Synthetic conjugate', firstPublicationDate='2024-01-01',
             affiliation='SYNTHETIC BIOLOGICS SUZHOU CO LTD', patentDetails={'typeCode': 'A1', 'countryCode': 'WO'}),
        dict(source='PAT', id='US2099000001', title='Other owner device', firstPublicationDate='2024-02-02',
             affiliation='UNRELATED MEDICAL INC', patentDetails={'typeCode': 'A1', 'countryCode': 'US'})]}}


def patent_html(number='US123456A1', applicant='Synthetic Institute', family='987654', current='Other Owner',
                versions=('US999998A1',)):
    """Synthetic values in the live result-page layout.

    Layout observed on public US B2, WO A1 and TW A pages (2026-10-30); no
    provider HTML is committed. One top-level article item; the title is a bare
    span; the first dl closes early, so owners and dates are bare dd rows. Nested
    items (other versions, events, family) repeat other numbers, dates and
    owners as decoys. The live pages carry no familyId; family is optional here.
    """
    def day(key, value):
        return f'<dd><time itemprop="{key}" datetime="{value}">{value}</time></dd>'
    kind = re.search(r'[A-Z][0-9]*$', number)[0]
    current_row = f'<dt>Current Assignee</dt><dd itemprop="assigneeCurrent" repeat>\n  {current}\n</dd>' if current else ''
    return ('<html><head><title>' + number + ' - Google Patents</title></head><body>'
        '<article class="result" itemscope itemtype="http://schema.org/ScholarlyArticle">'
        f'<h1 itemprop="pageTitle">{number} - Synthetic protein disclosure - Google Patents</h1>'
        '<span itemprop="title">Synthetic protein disclosure\n  </span>'
        f'<dl><dt>Publication number</dt><dd itemprop="publicationNumber">{number}</dd>'
        f'<meta itemprop="kindCode" content="{kind}"><span>{number}</span>'
        '<dt>Legal status</dt><dd itemprop="legalStatusIfi" itemscope><span itemprop="status">Active</span></dd></dl>'
        '<dt>Application number</dt><dd itemprop="applicationNumber">US2020000001</dd>'
        '<dt>Other versions</dt>' + ''.join('<dd itemprop="directAssociations" itemscope repeat>'
        f'<a href="/patent/{v}/en"><span itemprop="publicationNumber">{v}</span>'
        '(<span itemprop="primaryLanguage">en</span>)</a></dd>' for v in versions) +
        '<dt>Inventor</dt><dd itemprop="inventor" repeat>Synthetic Person</dd>'
        + current_row +
        f'<dt>Original Assignee</dt><dd itemprop="assigneeOriginal" repeat>{applicant}</dd>'
        + day('priorityDate', '2019-01-02') + day('filingDate', '2020-01-02') + day('publicationDate', '2021-02-03')
        + (f'<dt>Family</dt><dd itemprop="familyId">{family}</dd>' if family else '') +
        '<dd itemprop="events" itemscope repeat><time itemprop="date" datetime="2018-05-05">2018-05-05</time>'
        '<span itemprop="title">Application filed by Decoy Holder</span></dd>'
        '<section itemprop="family" itemscope><h1>Family</h1><span itemprop="priorityDate">2017-07-07</span>'
        '<span itemprop="assigneeOriginal">Decoy Holder</span><span itemprop="publicationNumber">US999997A1</span></section>'
        '</article></body></html>')


def section(ref='S1'):
    return dict(verdict='steady', confidence='low', summary='合成材料显示研发仍在推进。',
                **{'for': [dict(text='合成材料记录了研发进展。', refs=[ref])], 'against': [], 'change_if': []})


def model_answer(system, user, state):
    if '值得读取正文' in system:
        return {'results': [{'id': i, 'read': False} for i in re.findall(r'id=(\S+)', user)]}
    if '筛选与被监控公司相关的公开信息' in system:
        results = []
        for block in user.split('id=')[1:]:
            item_id = block.split(' |', 1)[0].strip()
            title = re.search(r'标题：(.*)', block).group(1).strip()
            subject = 'Synthetic Biologics' in block or 'SYNTHETIC BIOLOGICS' in block
            results.append(dict(id=item_id, relevant=True, category=None, relation='subject' if subject else 'field',
                                importance='notable', summary='合成研究记录。', impact='与已知计划无直接关联，属研发进展。',
                                source_title=title, source_quote=title))
        state['triage_calls'] = state.get('triage_calls', 0) + 1
        if state.get('misalign_triage') and len(results) > 1:
            # A long batch answered out of order: every verdict carries its neighbour's title.
            state['misalign_triage'] -= 1
            titles = [r['source_title'] for r in results]
            for r, title in zip(results, titles[1:] + titles[:1], strict=True):
                r['source_title'] = r['source_quote'] = title
        return {'results': results}
    if '是否在讲同一事件' in system:
        return {'results': [{'pair': n + 1, 'same': False} for n in range(user.count('pair'))]}
    if '列出这项投资成立所依赖的关键假设' in system:
        return {'assumptions': [{'text': '核心研发管线持续推进', 'basis': '公司资料'},
                                {'text': '专利布局覆盖核心技术', 'basis': '公司资料'}]}
    if '对照每条投资假设' in system:
        ids = re.findall(r'^(A[0-9a-f]{8})：', user, re.M)
        items = re.findall(r'id=(\S+)', user)
        return {'assumptions': [{'id': ids[0], 'status': 'strengthened', 'signals': [{'item': items[0], 'why': '署名研究'}]},
                                {'id': ids[1], 'status': 'weakened', 'signals': [{'item': 'not-given', 'why': '编造'}]}],
                'unforeseen': [{'text': '出现新技术路线', 'items': [items[0]]}, {'text': '无依据', 'items': []}],
                'explore': [{'query': 'Synthetic Rival 新靶点', 'reason': '新竞品'}]}
    if '一级市场投资研究员' in system:
        state['drafts'] += 1
        if state.get('fail_draft'):
            state['fail_draft'] -= 1
            return 500
        return dict(operations=section(), chain=section())
    return {}


def legacy_job(store, project_id, kind, *, status='queued', input_version='legacy'):
    """A job row of a retired kind, as existing databases hold them; the queue no longer creates these."""
    import uuid
    from datetime import UTC, datetime, timedelta

    job_id = uuid.uuid4().hex
    at = datetime.now(UTC)
    lease = (uuid.uuid4().hex, (at + timedelta(seconds=90)).isoformat()) if status == 'running' else ('', None)
    with store._connect() as db:
        db.execute('INSERT INTO studio_jobs (id,project_id,kind,status,created_at,input_version,lease_token,lease_until) '
                   'VALUES (?,?,?,?,?,?,?,?)', (job_id, project_id, kind, status, at.isoformat(), input_version, *lease))
        return dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (job_id,)).fetchone())


EMPTY_RSS = b'<?xml version="1.0"?><rss version="2.0"><channel><title>synthetic</title></channel></rss>'


def run_leased(pipeline, job, work):
    """Run work() inside a claimed job's lease and request journal, as a pipeline stage does."""
    from project_watch.studio.execution import JobExecution

    execution = JobExecution(pipeline.store, job)
    previous = getattr(pipeline._execution_state, 'current', None)
    pipeline._execution_state.current = execution
    try:
        with pipeline.store.write_scope(execution._guard):
            return work()
    finally:
        pipeline._execution_state.current = previous
