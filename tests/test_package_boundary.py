import ast
import tomllib
from pathlib import Path

from packaging.version import Version

import project_watch
from scripts.check_layer_boundaries import audit, load_map


def test_package_has_own_version() -> None:
    assert str(Version(project_watch.__version__)) == project_watch.__version__
    manifest = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert project_watch.__version__ == tomllib.loads(manifest.read_text())["project"]["version"]


def test_package_does_not_import_greenpine_runtime() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "project_watch"
    assert source_root.is_dir()
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert all(name.split(".", 1)[0] not in {"modules", "apps"} for name in names)

    project = tomllib.loads((source_root.parents[1] / "pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]
    assert all("songhe-knowledge-platform" not in item for item in dependencies)


def test_registered_open_core_boundaries() -> None:
    root = Path(__file__).resolve().parents[1]
    result = audit(root, load_map(root / 'scripts/layer_map.json'))
    assert result.ok, result.summary()
