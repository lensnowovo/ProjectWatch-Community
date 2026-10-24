"""Release feedback contracts; templates use JSON, a strict subset of YAML."""
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORMS = ('bug_report', 'installation', 'analysis_quality')


@pytest.mark.parametrize('name', FORMS)
def test_feedback_forms_require_context_and_privacy_acknowledgement(name):
    form = json.loads((ROOT / '.github/ISSUE_TEMPLATE' / f'{name}.yml').read_text(encoding='utf-8'))
    assert form['name'] and form['description']
    fields = {item['id']: item for item in form['body'] if 'id' in item}
    assert len(fields) == len([item for item in form['body'] if 'id' in item])
    for key in ('version', 'environment', 'steps', 'expected', 'actual'):
        assert fields[key]['validations']['required'] is True
    assert fields['privacy']['type'] == 'checkboxes'
    option = fields['privacy']['attributes']['options'][0]
    assert option['required'] is True
    assert all(word in option['label'] for word in ('Key', 'BP', '数据库', '完整日志'))
    assert not form.get('assignees')  # No unverified account routing.


def test_unknown_outcomes_do_not_suggest_retrying_or_erasing_receipts():
    for name in FORMS:
        text = (ROOT / '.github/ISSUE_TEMPLATE' / f'{name}.yml').read_text(encoding='utf-8')
        assert '不反复重试' in text
        assert '不删除回执' in text


def test_security_feedback_has_private_route_and_no_open_blank_issue():
    settings = json.loads((ROOT / '.github/ISSUE_TEMPLATE/config.yml').read_text(encoding='utf-8'))
    assert settings['blank_issues_enabled'] is False
    assert any('/docs/security-reporting.md' in item['url'] for item in settings['contact_links'])


def test_first_release_scope_and_maintenance_loop_are_explicit():
    support = (ROOT / 'SUPPORT.md').read_text(encoding='utf-8')
    assert 'Windows x64' in support and 'Apple Silicon' in support
    assert 'Intel' in support and '暂缓' in support
    maintenance = (ROOT / 'docs/maintenance.md').read_text(encoding='utf-8')
    for phrase in ('P0', 'P1', 'P2', '回归测试', '回滚', '不承诺', '不自动'):
        assert phrase in maintenance


def test_retained_public_readme_assets_are_complete_svg():
    for name in ('hero-dark', 'hero-light', 'loop-dark', 'loop-light'):
        path = ROOT / 'docs/assets/readme' / f'{name}.svg'
        source = path.read_text(encoding='utf-8')
        assert 'tokens truncated' not in source
        assert ET.fromstring(source).tag == '{http://www.w3.org/2000/svg}svg'
