"""Transactional external Agent reports. No model/search calls; system artifacts stay intact."""

import hashlib
import json
import uuid
from contextlib import closing

from project_watch.studio.application import AccessDenied, ProjectMissing, VersionConflict
from project_watch.studio.store import now


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _state(db, key, value):
    db.execute('INSERT INTO studio_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, _json(value)))


def _get(db, key, default=None):
    row = db.execute('SELECT value FROM studio_state WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def submit(service, project_id, body, token):
    store = service.store
    payload = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        grant = service._authorize(db, token, project_id, 'write')
        actor = grant['id']
        old = db.execute('SELECT payload_hash,receipt FROM studio_operations WHERE instance_id=? AND project_id=? '
                         'AND actor=? AND idempotency_key=?',
                         (service.instance_id, project_id, actor, body.idempotency_key)).fetchone()
        if old:
            if old['payload_hash'] != payload:
                raise VersionConflict('idempotency key already used with another payload')
            return json.loads(old['receipt'])
        project = db.execute('SELECT config_version,status FROM studio_projects WHERE project_id=?', (project_id,)).fetchone()
        if not project or project['status'] == 'removed':
            raise ProjectMissing('active project identity required')
        if project['config_version'] != body.expected_version:
            raise VersionConflict('project configuration changed; read current version')
        sources = {}
        internal = any(s.kind == 'internal' for s in body.sources)
        if internal and not grant['allow_internal']:
            raise AccessDenied('internal analysis requires explicit internal grant')
        for source in body.sources:
            entry = source.model_dump(mode='json')
            if source.kind == 'internal':
                row = db.execute('SELECT s.reference,f.text FROM sources s JOIN document_fragments f ON f.source_id=s.id '
                                 'WHERE s.id=? AND s.project_id=? AND f.locator=? LIMIT 1',
                                 (source.source_id, project_id, source.locator)).fetchone()
                if not row:
                    raise ValueError('internal source/locator is not a parsed document in this project')
                entry.update(title=row['reference'], text=row['text'], read_scope='partial')
            sources[source.id] = entry
        ts = now()
        origin = {'kind':'agent', 'producer':body.producer, 'received_at':ts}
        event_ids = {}
        # Public events only. Interpretations never become private revenue/funding facts.
        for news in body.news:
            key = 'agent_event:' + project_id + ':' + hashlib.sha256((body.producer + ':' + news.key).encode()).hexdigest()
            existing_id = _get(db, key)
            event_id = existing_id or uuid.uuid4().hex
            event_ids[news.key] = event_id
            if existing_id:
                from project_watch.studio.event_revisions import record

                record(db, project_id, event_id)
                db.execute('UPDATE studio_events SET title=?,summary=?,impact=?,category=?,importance=?,event_at=?,updated_at=? '
                           'WHERE id=? AND project_id=?',
                           (news.title,news.summary,news.impact,news.category,news.importance,
                            news.event_at.isoformat() if news.event_at else None,ts,event_id,project_id))
            else:
                db.execute('INSERT INTO studio_events (id,project_id,title,summary,impact,category,importance,event_at,created_at,updated_at,notified_at) '
                           'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (event_id,project_id,news.title,news.summary,news.impact,news.category,news.importance,
                            news.event_at.isoformat() if news.event_at else None,ts,ts,ts))
                _state(db, key, event_id)
            for ref_id in news.refs:
                from project_watch.studio.event_revisions import previous_memberships
                from project_watch.studio.event_revisions import record as record_event

                src = sources[ref_id]
                item_key = 'agent:' + hashlib.sha256((key + ':' + src['url']).encode()).hexdigest()
                existing_items = [row[0] for row in db.execute('SELECT id FROM studio_items WHERE project_id=? AND key=?',
                                                              (project_id, item_key))]
                previous_events = previous_memberships(db, project_id, existing_items)
                extra = {'agent_result':origin, 'read_scope':src['read_scope'], 'source_published_at':src['published_at'],
                         'assertion':'Agent-submitted source extraction, not server-fetched verification'}
                db.execute('INSERT INTO studio_items (id,project_id,key,category,title,url,source,snippet,published_at,collected_at,'
                           'triaged,relevant,importance,summary,impact,extra,body,body_status,event_id) '
                           'VALUES (?,?,?,?,?,?,?,?,?,?,1,1,?,?,?,?,?,?,?) ON CONFLICT(project_id,key) DO UPDATE SET '
                           'title=excluded.title,summary=excluded.summary,impact=excluded.impact,extra=excluded.extra,'
                           'body=excluded.body,body_status=excluded.body_status',
                           (uuid.uuid4().hex,project_id,item_key,news.category,src['title'],src['url'],src['title'],src['text'][:600],
                            src['published_at'],ts,news.importance,news.summary,news.impact,_json(extra),src['text'],
                            'search_summary' if src['read_scope']=='summary' else 'agent_excerpt',event_id))
                for previous_event in previous_events:
                    record_event(db, project_id, previous_event)
            from project_watch.studio.event_revisions import record

            record(db, project_id, event_id)
        record = {'id':uuid.uuid4().hex, 'producer':body.producer, 'actor':actor, 'received_at':ts,
                  'visibility':'internal' if internal else 'public', 'sources':sources,
                  'company':body.company.model_dump() if body.company else None,
                  'brief':body.brief.model_dump() if body.brief else None,
                  'deep':body.deep.model_dump() if body.deep else None, 'event_ids':event_ids}
        _state(db, f'agent_result_batch:{project_id}:{record["id"]}', record)
        history = _get(db, f'agent_result_history:{project_id}', [])
        history.append(record['id'])
        _state(db, f'agent_result_history:{project_id}', history)
        _state(db, f'agent_results:{project_id}', record)
        db.execute('UPDATE studio_projects SET config_version=config_version+1 WHERE project_id=?', (project_id,))
        receipt = {'instance_id':service.instance_id, 'schema_version':1,'project_id':project_id,
                   'operation_id':uuid.uuid4().hex,'version':project['config_version']+1,
                   'saved':True,'operation_status':'completed','job_status':'not_requested','job_id':None,
                   'batch_id':record['id'],'news_count':len(event_ids),'actor':actor}
        db.execute('INSERT INTO studio_operations VALUES (?,?,?,?,?,?,?,?)',
                   (receipt['operation_id'],project_id,service.instance_id,actor,body.idempotency_key,payload,_json(receipt),ts))
        return receipt


def history(store, project_id, *, include_internal=False):
    with closing(store._connect()) as db:
        ids = _get(db, f'agent_result_history:{project_id}', [])
        out = []
        for batch_id in reversed(ids):
            record = _get(db, f'agent_result_batch:{project_id}:{batch_id}')
            if record and (include_internal or record['visibility']=='public'):
                out.append({k:record[k] for k in ('id','producer','received_at','visibility')})
        return {'items':out[:100], 'truncated':len(out)>100}


def current(store, project_id, *, include_internal=False):
    """Read the newest permitted result, so a conversation can revise its own work."""
    with closing(store._connect()) as db:
        for batch_id in reversed(_get(db, f'agent_result_history:{project_id}', [])):
            record = _get(db, f'agent_result_batch:{project_id}:{batch_id}')
            if record and (include_internal or record['visibility']=='public'):
                return {k:v for k,v in record.items() if k!='actor'}
    return None


def latest(store, project_id, artifact):
    with closing(store._connect()) as db:
        for batch_id in reversed(_get(db, f'agent_result_history:{project_id}', [])):
            record = _get(db, f'agent_result_batch:{project_id}:{batch_id}')
            if record and record.get(artifact):
                return record
    return None


def _origin(record):
    return {'kind':'agent','producer':record['producer'],'received_at':record['received_at'],'batch_id':record['id']}


def _refs(record, ids):
    out = []
    for code in ids:
        src = record['sources'][code]
        ref = {'code':code,'kind':src['kind'],'label':src['title'],'text':src['title'],
               'date':src.get('published_at') or '', 'read_scope':src['read_scope']}
        if src['kind']=='internal':
            ref.update(source_id=src['source_id'],source_name=src['title'],locator=src['locator'])
        else:
            ref.update(url=src['url'],source=src['title'],source_id='',source_name=src['title'],locator='')
        out.append(ref)
    return out


def company_view(record):
    company = record['company']
    def point(claim):
        return {'text':claim['text'], 'refs':_refs(record,claim['refs'])}
    return {'one_liner':company['one_liner']['text'],'summary':company['summary']['text'],
            'sections':[{'title':s['title'],'points':[point(p) for p in s['points']]} for s in company['sections']],
            'highlights':[point(p) for p in company['highlights']], 'risks':[], 'milestones':[],
            'questions':company['questions'],'documents':len({s['source_id'] for s in record['sources'].values() if s['kind']=='internal'}),
            'updated_at':record['received_at'], 'origin':_origin(record)}


def brief_view(record):
    brief = record['brief']
    return {'headline':brief['headline'],'summary':brief['summary']['text'],'tone':brief['tone'],
            'watch':[{'text':p['text'],'refs':_refs(record,p['refs'])} for p in brief['watch']],
            'changes':[],'updated_at':record['received_at'],'origin':_origin(record)}


def deep_view(record):
    data = record['deep']
    def section(raw, keys, mapping=None):
        out = dict(raw)
        for key in keys:
            out[(mapping or {}).get(key,key)] = [{'text':p['text'],'refs':_refs(record,p['refs'])} for p in raw[key]]
            if mapping and key in mapping:
                out.pop(key)
        evidence = [p for key in keys for p in raw[key]]
        distinct = {(record['sources'][ref]['source_id'] or record['sources'][ref]['url'])
                    for p in evidence for ref in p['refs']}
        if not evidence:
            for field in ('verdict','maturity','consensus'):
                if field in out:
                    out[field] = 'unclear'
        if len(distinct)<2:
            out['confidence']='low'
        elif len(distinct)<3 and out['confidence']=='high':
            out['confidence']='medium'
        return out
    position = section(data['position'], ('supporting','against'), {'supporting':'for'})
    technology = section(data['technology'], ('academic','industry','risks'))
    if not technology['academic'] and not technology['industry']:
        technology['consensus']='unclear'
    industry = section(data['industry'], ('drivers','headwinds'))
    rings = {key: section(data[key], ('supporting','against'), {'supporting':'for'})
             for key in ('operations','chain') if data.get(key) is not None}
    refs = {r['code']:r for s in (position,technology,industry,*rings.values()) for v in s.values() if isinstance(v,list)
            for p in v if isinstance(p,dict) for r in p.get('refs',[])}
    counts = {'D':sum(s['kind']=='internal' for s in record['sources'].values()),
              'E':sum(s['kind']=='public' for s in record['sources'].values())}
    return {'analysis':{'headline':data['headline'],'position':position,'technology':technology,'industry':industry,
                        **rings,'gaps':data['gaps'],'cited':list(refs.values())},
            'generated_at':record['received_at'],'fingerprint':record['id'],'counts':{k:v for k,v in counts.items() if v},
            'total':len(record['sources']),'model':record['producer'],'origin':_origin(record)}


def project_projection(store, project_id, result):
    for name, transform, field in (('company',company_view,'profile'),('brief',brief_view,'brief')):
        record = latest(store, project_id, name)
        current = result.get(field)
        if record and (not current or current.get('updated_at','') <= record['received_at']):
            result[field] = transform(record)
    return result


def insight_projection(store, project_id, current):
    record = latest(store, project_id, 'deep')
    if record and (not current or current.get('generated_at','') <= record['received_at']):
        return deep_view(record)
    return current


def insight_history(store, project_id, existing):
    entries = list(existing)
    with closing(store._connect()) as db:
        for batch_id in _get(db, f'agent_result_history:{project_id}', []):
            record = _get(db, f'agent_result_batch:{project_id}:{batch_id}')
            if record and record.get('deep'):
                analysis = deep_view(record)['analysis']
                entries.append({'generated_at':record['received_at'],'headline':analysis['headline'],
                                'position':analysis['position']['verdict'],'maturity':analysis['technology']['maturity'],
                                'consensus':analysis['technology']['consensus'],'industry':analysis['industry']['verdict'],
                                'origin':_origin(record)})
    return sorted(entries,key=lambda e:e['generated_at'],reverse=True)[:10]
