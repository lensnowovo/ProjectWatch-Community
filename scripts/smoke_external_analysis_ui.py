"""Installed no-network external-analysis fixture; never opens the private pilot.

Serves an isolated synthetic project for UI checks only. Does not certify model
quality or public collection. Every interpretation below is deterministic.
"""
import argparse
import json
import os
import runpy
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.external_analysis import OUTPUT_TOKENS, validate_external_analysis
from project_watch.monitoring import ManualSnapshotAdapter
from project_watch.repository import ProjectRepository


class SyntheticExternal:
    def __init__(self, model):
        self.model = model
        self.config = model.config
        self.calls = 0

    def authorization_id(self):
        return self.model.authorization_id()

    def disclosure(self):
        return {'remaining_runs': 20-self.calls}

    def compile_reading(self, packet, *, before_send):
        return self.model.compile_reading(packet, before_send=before_send)

    def analyze_external(self, packet, *, before_send):
        before_send(json.dumps({'model': self.config.model, 'max_tokens': OUTPUT_TOKENS,
                                'stream': True, 'messages': []}).encode())
        self.calls += 1
        public = next(r for r in packet['references'] if r['ref_id'] == 'news:title')
        internal = next((r for r in packet['references'] if r['kind'] == 'internal'), None)
        refs = [{'ref_id': public['ref_id'], 'quote': public['text']}]
        if internal:
            refs.append({'ref_id': internal['ref_id'], 'quote': internal['text'][:300]})
        return validate_external_analysis(packet, {
            'source_statements': [{'text': '公开标题提到同业公司的经营进展。', 'evidence': [refs[0]]}],
            'relationship': {'assessment': 'potentially_relevant' if internal else 'unclear',
                             'text': '可作为同业经营跟踪线索；双方产品和客户是否重叠仍需核实。' if internal
                                     else '尚无项目背景，暂不能判断这条消息与公司的具体关系。',
                             'evidence': refs},
            'implications': [{'hypothesis': '可能补充同业经营对照，但不能据此推断本项目的收入变化。',
                              'condition': '双方处于相同产品市场，且收入期间与统计口径可比。',
                              'evidence': refs}] if internal else [],
            'unknowns': ['双方是否构成直接竞争。', '新闻涉及的业务范围及收入统计口径。'],
        })


def exercise(root):
    os.environ['PROJECT_WATCH_SETTINGS_TOKEN'] = 'synthetic-local-only'
    journey = runpy.run_path(str(Path(__file__).with_name('smoke_monitor_journey.py')))
    baseline, project_id, _ = journey['exercise'](root)
    model = SyntheticExternal(baseline.state.reading_worker.settings.get())
    repo = ProjectRepository(Path(root)/'watch.sqlite3')
    # When served, exercise the real background loop with no-network adapters.
    app = create_app(repo.db_path, proposal_model=model, start_worker=True,
                     monitoring_adapters={'rss': ManualSnapshotAdapter({})})
    client = TestClient(app, base_url='http://127.0.0.1')
    route = f'/api/workspace/projects/{project_id}/external-analysis'
    headers = {'Origin': 'http://127.0.0.1', 'X-Settings-Token': 'synthetic-local-only'}
    source = next(s for s in repo.list_sources(project_id) if s.reference == '合成公司BP.txt')
    fragment = repo.list_fragments(source.id)[0]
    draft = {'plan_ids': [p.id for p in repo.list_monitoring_plans(project_id)], 'include_future_news': True,
             'max_requests': 2, 'selections': [{'source_id': source.id, 'ordinal': fragment.ordinal,
                                               'offset': 0, 'length': min(3000, len(fragment.text))}]}
    preview = client.post(route+'/preview', headers=headers, json=draft)
    assert preview.status_code == 200, preview.text
    saved = client.post(route+'/authorization', headers=headers,
                        json={**draft, 'consent': True, 'request_key': 'synthetic-installed-external',
                              'disclosure_digest': preview.json()['disclosure_digest']})
    assert saved.status_code == 200, saved.text
    assert app.state.external_analysis_worker.process_one()['state'] == 'succeeded'
    read = client.get(route).json()
    assert len(read['items']) == 1 and model.calls == 1
    assert app.state.external_analysis_worker.process_one() is None and model.calls == 1
    assert repo.list_observations(project_id, status='confirmed') == []
    print(json.dumps({'passed': True, 'private_materials': False, 'real_model_quality': 'not_tested',
                      'real_public_collection': 'not_tested', 'synthetic_external_calls': model.calls}), flush=True)
    return app, project_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', type=int, default=0)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='pw-external-analysis-ui-') as root:
        app, project_id = exercise(root)
        if args.serve:
            import uvicorn
            print(f'SYNTHETIC ONLY: http://127.0.0.1:{args.serve}/#project/{project_id}/external', flush=True)
            uvicorn.run(app, host='127.0.0.1', port=args.serve)


if __name__ == '__main__':
    main()
