"""Public-facing instructions must survive the history-free source export."""
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ('docs/acceptance/', 'docs/plans/', 'docs/handoff/', 'docs/reviews/')
PAGES = ('README.md', 'SUPPORT.md', 'CHANGELOG.md', 'docs/developer-architecture.md',
         'docs/release-support-matrix.md', 'docs/release-checklist.md',
         'docs/windows-clean-pc-acceptance.md', 'docs/macos-clean-pc-acceptance.md',
         'docs/windows-signing-distribution-review.md', 'docs/windows-user-build.md',
         'docs/windows-user-guide.md', 'docs/release-readiness.md',
         'docs/monitoring-acceptance-criteria.md', 'docs/releases/0.2.0rc2-draft.md',
         'docs/release-security-review.md', 'docs/release-third-party-scope.md')


@pytest.mark.parametrize('name', PAGES)
def test_user_release_links_do_not_depend_on_excluded_private_history(name):
    source = ROOT/name
    for target in re.findall(r'\]\(([^\s)]+)\)', source.read_text(encoding='utf-8')):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        path = (source.parent/unquote(parsed.path)).resolve()
        assert path.is_relative_to(ROOT), (name, target)
        relative = path.relative_to(ROOT).as_posix()
        assert not relative.startswith(PRIVATE), (name, target, 'not exported')
        assert path.is_file(), (name, target, 'missing')
