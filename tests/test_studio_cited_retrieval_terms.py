"""Cited lexical retrieval hints, never semantic ownership/competition proof."""
import pytest

from project_watch.studio.competition_research import prioritize_original_leads, selection_matches


def test_cited_scope_tokens_prioritize_actor_and_medical_scope_over_homonym():
    context={'names':['Synthetic Kite'],'products':[], 'uses':['靶向CD19的自体CAR-T细胞免疫治疗产品']}
    unrelated={'url':'https://travel.example.test/kite','title':'Synthetic Kite beach holidays','snippet':'Tourism and surfing'}
    relevant={'url':'https://hospital.example.test/news','title':'Synthetic Kite product news',
              'snippet':'Synthetic Kite develops a CD19-targeted autologous CAR-T therapy.'}
    assert selection_matches(relevant,context)==['company','use']
    assert selection_matches(unrelated,context)==['company']
    assert prioritize_original_leads([unrelated,relevant],context)[0]==relevant


@pytest.mark.parametrize('snippet', ['CD19 targeting antibody', 'CAR-T targeting BCMA', 'CD19A CAR-T', 'CD19 CAR-TX'])
def test_all_complete_cited_ascii_terms_are_needed(snippet):
    context={'names':[],'products':[],'uses':['靶向CD19的自体CAR-T细胞免疫治疗产品']}
    assert selection_matches({'title':'','snippet':snippet},context)==[]


def test_retrieval_case_does_not_rewrite_sources_or_normalize_fact_values():
    context={'names':[],'products':[],'uses':['靶向CD19的自体CAR-T细胞免疫治疗产品']}
    hit={'title':'','snippet':'cd19 autologous car-t therapy'}
    assert selection_matches(hit,context)==['use']
    assert hit['snippet']=='cd19 autologous car-t therapy'
    assert context['uses']==['靶向CD19的自体CAR-T细胞免疫治疗产品']


@pytest.mark.parametrize('value,snippet', [('用途 A','用途 B'),('人体器官模型','其他器官模型'),('A drug screening','drug screening')])
def test_missing_short_or_chinese_qualifiers_do_not_get_a_token_fallback(value,snippet):
    assert selection_matches({'snippet':snippet},{'names':[],'products':[],'uses':[value]})==[]


def test_product_labels_do_not_use_generic_use_term_fallback():
    context={'names':[],'products':['Synthetic Lens A'],'uses':[]}
    assert selection_matches({'snippet':'Synthetic Lens B'},context)==[]


def test_more_than_twelve_use_terms_do_not_get_truncated_to_fit():
    value=' / '.join(f'Word{i}' for i in range(13))
    snippet=' '.join(f'Word{i}' for i in range(12))
    assert selection_matches({'snippet':snippet},{'names':[],'products':[],'uses':[value]})==[]


def test_exact_use_still_matches_with_short_terms():
    assert selection_matches({'snippet':'A drug screening'}, {'uses':['A drug screening']})==['use']


def test_number_terms_and_boundaries_are_preserved():
    context={'uses':['用途120 CAR-T']}
    assert selection_matches({'snippet':'120 car-t'},context)==['use']
    assert selection_matches({'snippet':'1200 car-t'},context)==[]
