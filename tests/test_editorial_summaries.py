import json

from project_watch.repository import ProjectRepository
from project_watch.studio.editorial_summaries import fingerprint, read
from project_watch.studio.store import StudioStore


def test_reviewed_copy_is_project_bound_and_marks_changed_material(tmp_path):
    repo=ProjectRepository(tmp_path/'db.sqlite3')
    project=repo.create_project('Synthetic summary')
    store=StudioStore(repo.db_path)
    path=tmp_path/'editorial-summaries.json'
    path.write_text(json.dumps({'version':1,'projects':{project.id:{'basis':fingerprint(store,project.id),
        'generated_at':'2026-11-05T00:00:00Z','pages':{'papers':{'text':'Synthetic reviewed paragraph',
        'refs':[{'label':'Source','url':'javascript:alert(1)'}]}}}}}),encoding='utf-8')
    before=path.read_bytes()
    assert not read(store,project.id)['papers']['historical']
    assert read(store,project.id)['papers']['refs'][0]['url']==''
    assert read(store,'other')=={}
    store.add_items(project.id,'paper',[{'key':'new','title':'Synthetic new evidence','url':'https://example.org/paper','source':'Synthetic','snippet':'Synthetic text','published_at':None}])
    assert read(store,project.id)['papers']['historical']
    assert path.read_bytes()==before

def test_missing_and_malformed_copy_does_not_break_project(tmp_path):
    repo=ProjectRepository(tmp_path/'db.sqlite3')
    store=StudioStore(repo.db_path)
    assert read(store,'a')=={}
    for text in ['[]','null','{','{"version":1,"projects":[]}']:
        (tmp_path/'editorial-summaries.json').write_text(text,encoding='utf-8')
        assert read(store,'a')=={}

def test_click_details_keep_internal_reference_project_bound(tmp_path):
    repo = ProjectRepository(tmp_path/'db.sqlite3')
    project = repo.create_project('Synthetic')
    store = StudioStore(repo.db_path)
    (tmp_path/'editorial-summaries.json').write_text(json.dumps({
        'version':1, 'projects':{project.id:{
            'basis':fingerprint(store,project.id), 'generated_at':'2026-11-05',
            'pages':{'papers':{'text':'方法有所改进。可供产品研发参考。',
                'details':[{'text':'参数留在依据。','refs':[{'kind':'internal','source_id':'other-project-source'}]}]}}
        }}
    }),encoding='utf-8')
    assert read(store,project.id)['papers']['details'] == [{'text':'参数留在依据。','refs':[]}]
