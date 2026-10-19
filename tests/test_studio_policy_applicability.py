import pytest
from test_studio_bilateral_competition import setup_pair


@pytest.mark.parametrize(('status', 'region', 'expected'), [('draft', 'Beijing', 'pending'),
    ('in_force', 'Beijing', 'pending'), ('expired', 'Beijing', 'not_current')])
def test_policy_is_not_a_company_benefit_without_applicable_scope(tmp_path, status, region, expected):
    from project_watch.studio.policy_applicability import project_policy
    service, pid, _, facts = setup_pair(tmp_path)
    fact = {**facts[0][0], 'attributes': {'kind': 'policy', 'name': 'Synthetic rule', 'status': status,
            'jurisdictions': [region], 'applicable_to': ['medical devices'], 'effective_from': None, 'effective_to': None}}
    result = project_policy(service, pid, fact)
    assert result['applicability'] == expected and result['investment_impact'] == 'not_evaluated'
