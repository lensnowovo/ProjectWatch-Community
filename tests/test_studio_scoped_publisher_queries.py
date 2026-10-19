"""Cited query disambiguation/reservation, not real competitor accuracy."""
from copy import deepcopy

import pytest
from test_studio_competition_research import research_pipeline

from project_watch.studio.competition_research import (
    original_query_context,
    prioritize_original_leads,
    query_key,
    research_inputs,
)
from project_watch.studio.execution import JobExecution


def context(value='靶向CD19的自体CAR-T细胞免疫治疗产品'):
    return {'query_contexts':[{'product_name':'Fictional Lens Delta','uses':[value]}]}


def test_peer_spare_publisher_query_retains_cited_scope_not_target_brand():
    inputs=context()
    before=deepcopy(inputs)
    result=original_query_context(inputs,'Synthetic Kite',target=False)
    assert result['queries']==[
        'Synthetic Kite 靶向CD19的自体CAR-T细胞免疫治疗产品 产品',
        'Synthetic Kite CD19 CAR-T 官网']
    assert result['products']==[] and result['uses']==inputs['query_contexts'][0]['uses']
    assert 'Lens' not in ' '.join(result['queries'])
    assert inputs==before


def test_target_spare_query_keeps_own_cited_product_and_scope():
    result=original_query_context(context(),'Synthetic Optics',target=True)
    assert result['queries'][1]=='Synthetic Optics Fictional Lens Delta CD19 CAR-T 官网'
    assert result['products']==['Fictional Lens Delta']


@pytest.mark.parametrize('value', ['人体器官模型','用途 A','A drug screening',
    ' / '.join(f'Word{i}' for i in range(13))])
def test_unusable_ascii_fallback_retains_whole_raw_use_in_query(value):
    result=original_query_context(context(value),'Synthetic Actor',target=False)
    assert result['queries'][1]==f'Synthetic Actor {value} 官网'
    assert len(result['queries'])==2


def test_all_cited_uses_retain_their_terms_without_cross_scope_fact_creation():
    inputs=context()
    inputs['query_contexts'][0]['uses']=['CD19 CAR-T','drug screening']
    result=original_query_context(inputs,'Synthetic Actor',target=False)
    assert result['queries'][1]=='Synthetic Actor CD19 CAR-T drug screening 官网'
    assert result['uses']==['CD19 CAR-T','drug screening'] and result['products']==[]


def test_two_existing_contexts_do_not_get_a_third_publisher_query():
    inputs=context()
    inputs['query_contexts'].append({'product_name':'Fictional Other','uses':['drug screening']})
    result=original_query_context(inputs,'Synthetic Actor',target=False)
    assert result['queries']==['Synthetic Actor 靶向CD19的自体CAR-T细胞免疫治疗产品 产品',
        'Synthetic Actor drug screening 产品']


def hit(index,query,*,scoped=True):
    return {'url':f'https://publisher.example.test/{index}','title':'Synthetic Actor',
        'snippet':'CD19 CAR-T therapy' if scoped else 'Literature and online degrees',
        '_retrieval_query_keys':[query_key(query)]}


def test_company_only_membership_cannot_force_scoped_read_slot():
    ctx={'names':['Synthetic Actor'],'uses':['CD19 CAR-T'],'products':[],
        'queries':['scoped first','publisher second']}
    hits=[hit(i,'scoped first') for i in range(3)]+[hit(9,'publisher second',scoped=False)]
    before=deepcopy(hits)
    result=prioritize_original_leads(hits,ctx)
    assert [h['url'] for h in result[:3]]==[hits[i]['url'] for i in range(3)]
    assert hits==before and result[-1]['url']==hits[-1]['url'], 'Do not delete unscoped leads'


def test_scoped_second_context_still_gets_a_distinct_read_slot():
    ctx={'names':['Synthetic Actor'],'uses':['CD19 CAR-T'],'products':[],
        'queries':['scoped first','publisher second']}
    hits=[hit(i,'scoped first') for i in range(3)]+[hit(9,'publisher second')]
    assert prioritize_original_leads(hits,ctx)[1]['url']==hits[-1]['url']


def test_no_declared_scope_keeps_company_only_reservation_compatibility():
    ctx={'names':['Synthetic Actor'],'queries':['first','second']}
    hits=[hit(i,'first',scoped=False) for i in range(3)]+[hit(9,'second',scoped=False)]
    assert prioritize_original_leads(hits,ctx)[1]['url']==hits[-1]['url']


def test_empty_uses_do_not_add_an_invented_scope():
    inputs={'query_contexts':[{'product_name':'Synthetic Product','uses':[]}]}
    assert original_query_context(inputs,'Synthetic Actor',target=False)['queries']==[
        'Synthetic Actor 产品','Synthetic Actor 官网']


def test_old_v17_checkpoint_stops_before_search_model_and_page(tmp_path):
    pipeline,pid,calls,reads=research_pipeline(tmp_path)
    pipeline.schedule_competition(pid)
    job=pipeline.store.claim()
    execution=JobExecution(pipeline.store,job)
    inputs=research_inputs(pipeline,pid)
    inputs['prompt_version']='research-v17-atomic-peers'
    execution.checkpoint('competition_inputs',inputs)
    with pytest.raises(ValueError,match='old matching contract'):
        pipeline.run(job)
    assert not calls and not reads
