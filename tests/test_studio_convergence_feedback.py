"""Synthetic convergence regressions: no model, search or private inputs."""

import pytest

from project_watch.studio.questions import QuestionLedger


@pytest.mark.parametrize(('text', 'expected'), [
    ('合成器械进入创新医疗器械绿色通道', True),
    ('合成器械进入创新医疗器械特别审查程序', True),
    ('合成器械未进入创新医疗器械绿色通道', True),
    ('合成器械开通患者就医绿色通道', False),
    ('合成器械完成融资', False),
    ('其他设备进入创新医疗器械绿色通道', False),
])
def test_registration_focus_includes_specific_regulatory_channel_only(text, expected):
    watch = {'scope_kind':'question', 'relation_target':'合成器械', 'relation_aliases':[],
             'text':'合成器械注册进度', 'label':'注册进度', 'queries':['合成器械 注册']}
    assert bool(QuestionLedger.public_row_match(watch, {}, {'title':text})) is expected










