import random
import re
import time

from project_watch.studio.comparisons import _sentences

_OLD = re.compile(r'[^\n。！？!?]+(?:[。！？!?]|$)')


def test_sentence_runs_match_the_original_pattern():
    rng = random.Random(7)
    alphabet = list('ab 中文\n。！？!?x')
    for _ in range(5000):
        text = ''.join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        assert [m.span() for m in _sentences(text)] == [m.span() for m in _OLD.finditer(text)], repr(text)


def test_unpunctuated_lines_stay_linear():
    text = ('无标点的长句子 ' * 4000 + '\n') * 3
    started = time.perf_counter()
    # Only the last run counts: `$` matches before the final newline.
    assert len(list(_sentences(text))) == 1
    assert time.perf_counter() - started < 0.5
