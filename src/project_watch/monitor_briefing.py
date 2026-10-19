"""Read-only, reproducible monitoring highlights; never creates reviewed facts.

This first projection covers comparable annual actuals and human-accepted watch
updates. It does not infer causes, score risk, or call a model on page reads.
"""

from project_watch.metric_series import build_metric_series

RULE_VERSION = 'monitor-briefing-v1'


def is_publishable_update(item, project_id):
    return bool(
        item.get('project_id') == project_id
        and item.get('view') in {'internal', 'external'}
        and item.get('headline')
        and item.get('evidence_refs')
        and item.get('change_kind') != 'file_received'
        and item.get('status') not in {'withdrawn', 'rejected'}
    )


def build_briefing(project_id, observations, sources, watch_items=(), watch_changes=()):
    observations = [o for o in observations if o.project_id == project_id]
    sources = [s for s in sources if s.project_id == project_id]
    source_map = {s.id: s for s in sources}
    obs_map = {o.id: o for o in observations}

    def reference(observation):
        source = source_map.get(observation.source_id)
        if (source is None or observation.status != 'confirmed'
                or not observation.source_locator.strip()
                or len(source.content_sha256) != 64
                or any(c not in '0123456789abcdef' for c in source.content_sha256)):
            return None
        return dict(observation_id=observation.id, observation_version=observation.version,
                    source_id=source.id, reference=source.reference,
                    source_sha256=source.content_sha256, locator=observation.source_locator)

    items = []
    metrics = build_metric_series(observations, sources, limit=50, points_per_series=100)
    for series in metrics['series']:
        change = series['latest_change']
        if change['status'] != 'comparable' or not change['absolute']:
            continue
        before = obs_map[change['previous_observation_id']]
        after = obs_map[change['observation_id']]
        refs = [reference(before), reference(after)]
        if not all(refs):
            continue
        direction = '增加' if change['absolute'] > 0 else '减少'
        items.append(dict(
            id=f'financial:{before.id}:{before.version}:{after.id}:{after.version}',
            project_id=project_id, view='internal', change_kind='financial',
            headline=f"{series['name']}较上年{direction} {abs(change['percent']):.2f}%",
            status='confirmed', review_status='reviewed_inputs',
            evidence_refs=refs, occurred_at=None,
            discovered_at=max(before.created_at, after.created_at).isoformat(),
            reporting_period=after.period, rule_version=RULE_VERSION,
            comparison=dict(previous=str(before.number), current=str(after.number),
                            absolute=str(change['absolute']), percent=str(change['percent']),
                            previous_period=before.period, period=after.period,
                            unit=series['unit'], scope=series['scope'], basis=series['basis']),
            why_important='相邻年度、同单位和合并口径的实际数值发生变化。',
            interpretation='按已核对数值计算；增减本身不代表经营好坏。',
            ai_interpretation=None, unknowns=['变化原因及其持续性尚未核实。'],
        ))

    watches = {w.id: w for w in watch_items if w.project_id == project_id and w.status != 'rejected'}
    # Only the current accepted version is shown, not every historical edit.
    for change in watch_changes:
        watch = watches.get(change.watch_item_id)
        if (change.project_id != project_id or change.status != 'accepted' or watch is None
                or change.base_version + 1 != watch.version
                or not change.evidence_observation_id
                or change.evidence_observation_id == watch.observation_id
                or change.evidence_observation_id != watch.current_observation_id):
            continue
        if any(previous.project_id == project_id and previous.watch_item_id == watch.id
               and previous.status == 'accepted' and previous.base_version < change.base_version
               and previous.evidence_observation_id == change.evidence_observation_id
               for previous in watch_changes):
            # Editing ownership or status with the same evidence is not new evidence.
            continue
        evidence = obs_map.get(change.evidence_observation_id)
        original = obs_map.get(watch.observation_id)
        refs = [reference(o) if o is not None else None for o in (original, evidence)]
        if not all(refs):
            continue
        status = {'watching': '继续跟踪', 'action_required': '需要跟进', 'resolved': '本轮核对已结束'}[change.target_status]
        items.append(dict(
            id=f'watch:{change.id}', project_id=project_id, view='internal',
            change_kind='watch_evidence', headline=f'{watch.title} · {status}',
            status='accepted', review_status='human_accepted', evidence_refs=refs,
            occurred_at=None, discovered_at=change.updated_at.isoformat(),
            rule_version=RULE_VERSION, comparison=None, watch_item_id=watch.id,
            statement=evidence.statement, interpretation=change.reason,
            why_important='已有跟踪事项收到新依据，更新已由人采用。',
            ai_interpretation=None,
            unknowns=['这里只表示核对状态变化，不表示相关经营风险已经消失。'],
        ))
    items = sorted((i for i in items if is_publishable_update(i, project_id)),
                   key=lambda i: (i['discovered_at'], i['id']), reverse=True)
    return dict(project_id=project_id, rule_version=RULE_VERSION, items=items,
                coverage=dict(financial='comparable_change' if any(i['change_kind'] == 'financial' for i in items)
                              else 'no_comparable_change',
                              watch='accepted_update' if any(i['change_kind'] == 'watch_evidence' for i in items)
                              else 'no_accepted_update',
                              external_interpretation='not_available',
                              truncated_metrics=metrics['truncated']))


def project_briefing(repo, project_id):
    return build_briefing(project_id, repo.list_observations(project_id),
                          repo.list_sources(project_id), repo.list_watch_items(project_id),
                          repo.list_watch_item_changes(project_id))


def build_external_briefing(repo, project_id, categories, strategy):
    """Local reading projection. Quotes/questions are NOT an AI impact analysis.

    No network, model calls or writes. Every material link is rechecked against
    its exact source and latest same-name version; old strategies stay in history.
    """
    import ipaddress
    from datetime import UTC, datetime, timedelta
    from urllib.parse import urlsplit

    from project_watch.external_attention import market_commentary_only
    from project_watch.external_events import group_reports
    from project_watch.signal_projection import _article_url_key

    sources = {s.id: s for s in repo.list_sources(project_id) if s.project_id == project_id}
    latest = {}
    for source in sources.values():
        prior = latest.get(source.reference)
        if prior is None or (source.created_at, source.id) > (prior.created_at, prior.id):
            latest[source.reference] = source
    fragments = {}

    def current_ref(ref):
        source = sources.get(ref.get('source_id'))
        if (source is None or source.kind != 'document' or latest[source.reference].id != source.id
                or source.content_sha256 != ref.get('version') or not ref.get('quote')
                or source.reference != ref.get('reference') or not ref.get('locator')):
            return False
        if source.id not in fragments:
            fragments[source.id] = repo.list_fragments(source.id)
        return any(f.project_id == project_id and f.source_id == source.id
                   and f.locator == ref['locator'] and ref['quote'] in f.text for f in fragments[source.id])

    def public_link(url):
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or '').lower().rstrip('.')
            if (parsed.scheme not in {'http', 'https'} or not host or parsed.username or parsed.password
                    or host == 'localhost' or host.endswith(('.local', '.localhost'))):
                return False
            try:
                return ipaddress.ip_address(host).is_global
            except ValueError:
                return '.' in host  # Display gate only; no DNS/network access on page reads.
        except ValueError:
            return False

    scopes = {s['plan_id']: s for s in (strategy or {}).get('scopes', [])}
    if (strategy or {}).get('project_id') != project_id:
        scopes = {}
    now = datetime.now(UTC)
    items, seen = [], set()
    for group in categories:
        for signal in group['items']:
            published = signal.get('published_at')
            if (signal.get('project_id') != project_id or signal.get('status') == 'rejected'
                    or signal.get('freshness') != 'current' or not published
                    or not now-timedelta(days=30) <= published <= now+timedelta(days=1)
                    or not public_link(signal['url'])):
                continue
            key = (_article_url_key(signal['url']), ' '.join(signal['title'].split()).casefold())
            if key in seen:
                continue
            seen.add(key)
            scope = scopes.get(signal['plan_id'])
            refs = (scope or {}).get('source_refs', [])
            valid = bool(scope and scope['category'] == group['key']
                         and scope['anchor'] == signal['match_term'] and refs and all(current_ref(r) for r in refs))
            # Do not silently replace invalidated strategy evidence with another
            # similarly named document from the library.
            relationship = dict(kind='monitoring_scope' if valid else 'unresolved', verified=False,
                                text=f'BP 监控方向：{scope["anchor"]}。' if valid else
                                f'检索匹配“{signal["match_term"]}”，尚未定位有效的 BP 关联依据。')
            unknowns = ['当前依据为消息标题与订阅摘要，尚未核对全文；发布日期不是事项发生日期。']
            if group['key'] in {'company', 'competitors'}:
                unknowns.append('同名主体仍需核实。' if group['key'] == 'company' else
                                '同名主体、产品与客户重合程度仍需核实；候选同行不等于直接竞品。')
            elif group['key'] == 'policy':
                unknowns.append('政策适用地区、主体、生效条件尚未核实，不能据此认定项目受益或受限。')
            else:
                unknowns.append('这是赛道或行业消息，不是项目经营事实；项目实际影响尚未确定。')
            items.append(dict(
                id=signal['id'], project_id=project_id, category=group['key'],
                category_label='同行候选' if group['key'] == 'competitors' else group['title'],
                statement=signal['title'], summary=signal.get('summary') or '', url=signal['url'],
                published_at=published.isoformat(), occurred_at=None,
                content_scope='headline_and_feed_summary_only', origin='source_bound_projection',
                relationship=relationship, question=scope['question'] if valid else None,
                evidence_refs=refs if valid else [], unknowns=unknowns, impact=None,
                attention=signal.get('attention', {}),
                match_term=signal['match_term'],
            ))
    items.sort(key=lambda i: (i['attention'].get('level') == 'read_first', i['published_at'], i['id']), reverse=True)
    # A narrow reading filter, not deletion or a model judgment of importance.
    # Keep business-result headlines even when they also mention a share move.
    selected = [i for i in items if not market_commentary_only(i['statement'])]
    # One report may have been displayed first under a sector query. Reuse only
    # an existing named-company query also present in BOTH its title and summary
    # as a grouping hint; never reclassify the displayed story as a company fact.
    named = {s['match_term'] for g in categories if g['key'] in {'company', 'competitors'}
             for s in g['items'] if s.get('project_id') == project_id and s.get('freshness') == 'current'
             and s.get('status') != 'rejected' and len(s.get('match_term', '')) >= 2}
    grouping = []
    for item in selected:
        anchors = [a for a in named if a in item['statement'] and a in item['summary']]
        grouping.append(dict(item, category='competitors', match_term=anchors[0])
                        if item['category'] != 'policy' and len(anchors) == 1 else item)
    return dict(project_id=project_id, rule_version='external-first-brief-v1', window_days=30, items=selected,
                report_groups=group_reports(project_id, grouping),
                deferred_market_count=len(items)-len(selected),
                coverage=[dict(key=g['key'], title='同行候选' if g['key'] == 'competitors' else g['title'], state=g['coverage'],
                               item_count=len(g['items']),
                               last_success_at=max((p['last_success_at'] for p in g['plans']
                                                    if p.get('last_success_at')), default=None)) for g in categories])
