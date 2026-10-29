"""Release inventory is fail-closed on unexpected data and secret shapes."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.release_audit import (
    _SOURCE_BINARY_ASSETS,
    _public_studio_fixture,
    audit_tracked_source,
    audit_wheel,
    dependency_licenses,
)


@pytest.mark.parametrize('asset', sorted(_SOURCE_BINARY_ASSETS))
@pytest.mark.parametrize('fault', [None, 'asset', 'license'])
def test_tracked_public_assets_require_exact_bytes_and_corresponding_license(tmp_path, asset, fault):
    import shutil

    source = Path(__file__).resolve().parents[1]
    license_name = _SOURCE_BINARY_ASSETS[asset][1]
    for name in (asset, license_name):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="projectwatch-local"\n')
    if fault:
        (tmp_path / (asset if fault == 'asset' else license_name)).write_bytes(b'Synthetic replacement')
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    subprocess.run(['git', 'add', '.'], cwd=tmp_path, check=True, capture_output=True)
    _, findings = audit_tracked_source(tmp_path)
    assert bool(findings) == bool(fault), findings
    if fault:
        assert any('unreviewed binary asset' in value for value in findings)


def test_standalone_repository_includes_root_configuration_and_rejects_private_data(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "greenpine-project-watch"\n', encoding="utf-8"
    )
    (tmp_path / ".gitignore").write_text("# synthetic ignore\n", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
    workflow = tmp_path / ".github" / "workflows" / "test.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: synthetic test\n", encoding="utf-8")
    fixture = tmp_path / "tests" / "fixtures" / "unmarked.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text('{"private":"not a test marker"}', encoding="utf-8")
    (tmp_path / "private.sqlite3").write_bytes(b"synthetic database")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    count, findings = audit_tracked_source(tmp_path)
    assert count == 6
    assert findings == [
        "private.sqlite3: non-text tracked artifact",
        "tests/fixtures/unmarked.json: fixture lacks synthetic marker",
    ]


def test_wheel_inventory_accepts_only_runtime_assets(tmp_path: Path) -> None:
    wheel = tmp_path / "synthetic.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("project_watch/api.py", "# synthetic module\n")
        archive.writestr("project_watch/static/app.js", "// synthetic script\n")
        archive.writestr("project_watch/static/upload_batch.mjs", "// synthetic module\n")
        archive.writestr("project_watch/static/favicon.svg", '<svg xmlns="http://www.w3.org/2000/svg"/>')
        archive.writestr("greenpine_project_watch-0.1.dist-info/METADATA", "Name: synthetic\n")
    assert audit_wheel(wheel) == (5, [])


def test_frontend_and_windows_launcher_text_is_scanned_for_secrets(tmp_path: Path) -> None:
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "greenpine-project-watch"\n', encoding='utf-8')
    token = 'ghp_' + 'A' * 36
    for name in ('page.tsx', 'types.ts', 'Start-ProjectWatch.cmd'):
        (tmp_path / name).write_text(f'// synthetic {token}\n', encoding='utf-8')
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    subprocess.run(['git', 'add', '.'], cwd=tmp_path, check=True, capture_output=True)
    count, findings = audit_tracked_source(tmp_path)
    assert count == 4
    assert findings == [f'{name}: github_token' for name in ('Start-ProjectWatch.cmd', 'page.tsx', 'types.ts')]
    assert all(token not in item for item in findings)


def test_wheel_license_is_scanned_for_secrets(tmp_path: Path) -> None:
    token = 'ghp_' + 'A' * 36
    wheel = tmp_path / 'synthetic.whl'
    with ZipFile(wheel, 'w') as archive:
        archive.writestr('synthetic.dist-info/licenses/LICENSE', f'Synthetic {token}')
    count, findings = audit_wheel(wheel)
    assert count == 1
    assert findings == ['synthetic.dist-info/licenses/LICENSE: github_token']
    assert all(token not in item for item in findings)


def test_wheel_inventory_rejects_material_and_redacts_secret(tmp_path: Path) -> None:
    wheel = tmp_path / "unsafe.whl"
    token = "ghp_" + "A" * 36
    with ZipFile(wheel, "w") as archive:
        archive.writestr("project_watch/private-bp.pdf", b"%PDF-synthetic")
        archive.writestr("project_watch/api.py", f"TOKEN = '{token}'\n")
    count, findings = audit_wheel(wheel)
    assert count == 2
    assert any("unexpected wheel member" in item for item in findings)
    assert any("github_token" in item for item in findings)
    assert all(token not in item for item in findings)


def test_wheel_inventory_accepts_studio_fonts_and_license(tmp_path: Path) -> None:
    wheel = tmp_path / "synthetic-studio.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("project_watch/web/assets/newsreader-latin-wght-normal.woff2", b"wOF2synthetic")
        archive.writestr(
            "project_watch/web/assets/noto-serif-sc-chinese-simplified-600-normal.woff2",
            b"wOF2synthetic",
        )
        archive.writestr("project_watch/web/assets/THIRD-PARTY-LICENSES.txt", "Synthetic license text")
    assert audit_wheel(wheel) == (3, [])


@pytest.mark.parametrize("name", [
    "project_watch/web/assets/private.woff2",
    "project_watch/private.woff2",
    "project_watch/web/assets/private.txt",
    "project_watch/web/assets/settings.json",
])
def test_studio_assets_do_not_allow_arbitrary_data(tmp_path: Path, name: str) -> None:
    wheel = tmp_path / "synthetic-unsafe.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(name, b"wOF2synthetic")
    assert audit_wheel(wheel) == (1, [f"{name}: unexpected wheel member"])


def test_studio_font_requires_woff2_header(tmp_path: Path) -> None:
    wheel = tmp_path / "synthetic-invalid-font.whl"
    name = "project_watch/web/assets/newsreader-latin-wght-normal.woff2"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(name, b"synthetic private data")
    assert audit_wheel(wheel) == (1, [f"{name}: invalid WOFF2 header"])


def test_studio_license_is_still_scanned_for_secrets(tmp_path: Path) -> None:
    wheel = tmp_path / "synthetic-invalid-notices.whl"
    name = "project_watch/web/assets/THIRD-PARTY-LICENSES.txt"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(name, "Synthetic token: ghp_" + "A" * 36)
    assert audit_wheel(wheel) == (1, [f"{name}: github_token"])


@pytest.mark.parametrize('product_name', ['projectwatch-local', 'greenpine-project-watch'])
def test_license_inventory_includes_root_and_transitive_runtime_dependency(monkeypatch, product_name) -> None:
    from types import SimpleNamespace

    from scripts import release_audit

    graph = {
        product_name: ['fastapi>=0.1', 'mcp>=2; extra == "mcp"'],
        'fastapi': ['starlette>=0.1'], 'starlette': [], 'mcp': [],
    }

    def distribution(name):
        return SimpleNamespace(metadata={'Name': name, 'License-Expression': 'MIT'},
                               version='2.3.0', requires=graph[name])

    monkeypatch.setattr(release_audit.importlib.metadata, 'distribution', distribution)
    records, findings = dependency_licenses({'mcp'}, product_name)
    assert {item['name'] for item in records} == {product_name, 'fastapi', 'starlette', 'mcp'}
    assert not findings
    records, findings = dependency_licenses(set(), product_name)
    assert {item['name'] for item in records} == {product_name, 'fastapi', 'starlette'}
    assert not findings


def test_container_build_text_is_scanned_but_binary_artifacts_are_rejected(tmp_path: Path) -> None:
    product = tmp_path / "products" / "project-watch"
    product.mkdir(parents=True)
    token = "ghp_" + "A" * 36
    (product / "Dockerfile.parser").write_text(f"# synthetic {token}\n", encoding="utf-8")
    (product / ".dockerignore").write_text("# synthetic exclusions\n", encoding="utf-8")
    (product / "private-bp.pdf").write_bytes(b"%PDF-synthetic")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "products/project-watch"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    count, findings = audit_tracked_source(tmp_path)
    assert count == 3
    assert any("Dockerfile.parser: github_token" in finding for finding in findings)
    assert any("private-bp.pdf: non-text tracked artifact" in finding for finding in findings)
    assert not any(".dockerignore: non-text tracked artifact" in finding for finding in findings)
    assert all(token not in finding for finding in findings)


def test_synthetic_json_fixture_is_scanned_as_text(tmp_path: Path) -> None:
    fixture = (
        tmp_path / "products" / "project-watch" / "tests" / "fixtures" / "monitoring-golden.json"
    )
    fixture.parent.mkdir(parents=True)
    fixture.write_text('{"fixture":"synthetic-watch"}', encoding="utf-8")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "products/project-watch"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    assert audit_tracked_source(tmp_path) == (1, [])


def test_typescript_frontend_sources_are_scanned_for_secrets(tmp_path: Path) -> None:
    """The TypeScript UI is tracked source, so the credential scan must reach it."""
    product = tmp_path / "products" / "project-watch" / "frontend" / "src"
    product.mkdir(parents=True)
    key = "sk-" + "A" * 24
    (product / "api.ts").write_text("// synthetic client\nexport const base = '/api';\n", encoding="utf-8")
    (product / "Panel.tsx").write_text(f"// synthetic panel\nconst key = '{key}';\n", encoding="utf-8")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "products/project-watch"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    count, findings = audit_tracked_source(tmp_path)
    assert count == 2
    assert findings == ["products/project-watch/frontend/src/Panel.tsx: api_key"]
    assert all(key not in finding for finding in findings)


def test_trimmed_public_studio_fixtures_have_narrow_release_exception():
    directory = Path(__file__).parent / "fixtures" / "studio_live"
    paths = sorted(directory.glob("*.json"))
    assert paths
    for path in paths:
        relative = "tests/fixtures/studio_live/" + path.name
        data = path.read_bytes()
        assert _public_studio_fixture(relative, data), path.name
        assert not _public_studio_fixture("tests/fixtures/arbitrary.json", data)
        fixture = json.loads(data)
        fixture["response"]["headers"] = {"Cookie": "private"}
        assert not _public_studio_fixture(relative, json.dumps(fixture).encode())
        fixture = json.loads(data)
        fixture["provenance"]["endpoint"] = "https://private.example/material"
        assert not _public_studio_fixture(relative, json.dumps(fixture).encode())
        fixture = json.loads(data)
        fixture["provenance"]["records"] = 500
        assert not _public_studio_fixture(relative, json.dumps(fixture).encode())


def test_product_declares_its_license() -> None:
    """The audit fails closed without license metadata, so the declaration must stay."""
    repo_root = Path(__file__).resolve().parents[1]
    manifest = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest["project"]["license"] == "LicenseRef-ProjectWatch-Community-1.0"
    assert manifest["project"]["license-files"] == ["LICENSE", "NOTICE", "LICENSES/Apache-2.0.txt"]
    license_text = (repo_root / "LICENSE").read_text(encoding="utf-8")
    assert "ProjectWatch 社区使用许可 1.0" in license_text
    assert "lensnowovo@gmail.com" in license_text
    historical = (repo_root / "LICENSES/Apache-2.0.txt").read_text(encoding="utf-8")
    assert "Apache License" in historical
    assert "Version 2.0, January 2004" in historical
    assert "END OF TERMS AND CONDITIONS" in historical


def test_notice_is_scanned_in_source_and_wheel(tmp_path: Path) -> None:
    token = 'ghp_' + 'A' * 36
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "projectwatch-local"\n')
    (tmp_path / 'NOTICE').write_text(f'Synthetic notice {token}')
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    subprocess.run(['git', 'add', '.'], cwd=tmp_path, check=True, capture_output=True)
    assert audit_tracked_source(tmp_path) == (2, ['NOTICE: github_token'])
    wheel = tmp_path / 'synthetic.whl'
    with ZipFile(wheel, 'w') as archive:
        archive.writestr('synthetic.dist-info/licenses/NOTICE', f'Synthetic notice {token}')
    assert audit_wheel(wheel) == (1, ['synthetic.dist-info/licenses/NOTICE: github_token'])


def test_root_license_is_scanned_as_text(tmp_path: Path) -> None:
    """A suffixless LICENSE is tracked source, not an unreadable binary artifact."""
    product = tmp_path / "products" / "project-watch"
    product.mkdir(parents=True)
    token = "ghp_" + "A" * 36
    (product / "LICENSE").write_text(f"Apache License\nsynthetic {token}\n", encoding="utf-8")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "products/project-watch"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    count, findings = audit_tracked_source(tmp_path)
    assert count == 1
    assert findings == ["products/project-watch/LICENSE: github_token"]
    assert all(token not in finding for finding in findings)


def test_unimportable_probe_is_reported_instead_of_condemning_the_fixture(monkeypatch) -> None:
    """An environment failure must not masquerade as an unmarked fixture."""
    directory = Path(__file__).parent / "fixtures" / "studio_live"
    path = sorted(directory.glob("*.json"))[0]
    relative = "tests/fixtures/studio_live/" + path.name
    data = path.read_bytes()
    assert _public_studio_fixture(relative, data)
    monkeypatch.setitem(sys.modules, "scripts.studio_live_probe", None)
    with pytest.raises(RuntimeError, match="cannot check Studio live fixtures"):
        _public_studio_fixture(relative, data)


def test_command_line_run_can_check_studio_live_fixtures(tmp_path: Path) -> None:
    """Run directly, sys.path[0] is scripts/, which once hid the probe import."""
    repo_root = Path(__file__).resolve().parents[1]
    wheel = tmp_path / "synthetic.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("project_watch/api.py", "# synthetic module\n")
    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "release_audit.py"),
            "--wheel",
            str(wheel),
            "--repo-root",
            str(repo_root),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode in {0, 2}, result.stderr
    findings = json.loads(result.stdout)["findings"]
    assert not [item for item in findings if "studio_live" in item], findings


def test_parser_requirements_lock_is_scanned_as_text(tmp_path: Path) -> None:
    product = tmp_path / "products" / "project-watch"
    product.mkdir(parents=True)
    (product / "requirements-parser.lock").write_text(
        "# synthetic parser lock\npackage==1.0 --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "products/project-watch"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    assert audit_tracked_source(tmp_path) == (1, [])


def test_binary_disguised_as_synthetic_json_is_rejected(tmp_path: Path) -> None:
    fixture = (
        tmp_path / "products" / "project-watch" / "tests" / "fixtures" / "monitoring-golden.json"
    )
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"synthetic\x00private payload")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "products/project-watch"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    count, findings = audit_tracked_source(tmp_path)
    assert count == 1
    assert findings == [
        "products/project-watch/tests/fixtures/monitoring-golden.json: invalid text JSON"
    ]


def test_windows_launcher_csharp_is_scanned_not_exempted(tmp_path: Path) -> None:
    launcher = tmp_path / 'products/project-watch/scripts/windows/Launcher.cs'
    launcher.parent.mkdir(parents=True)
    launcher.write_text('// synthetic token: ' + 'ghp_' + 'x' * 36, encoding='utf-8')
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    subprocess.run(['git', 'add', '.'], cwd=tmp_path, check=True, capture_output=True)
    count, findings = audit_tracked_source(tmp_path)
    assert count == 1
    assert any('github_token' in finding for finding in findings)
    assert not any('non-text' in finding for finding in findings)


def test_nsis_installer_is_scanned_not_exempted(tmp_path: Path) -> None:
    installer = tmp_path / 'products/project-watch/scripts/windows/Installer.nsi'
    installer.parent.mkdir(parents=True)
    installer.write_text('; synthetic token: ' + 'ghp_' + 'x' * 36, encoding='utf-8')
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    subprocess.run(['git', 'add', '.'], cwd=tmp_path, check=True, capture_output=True)
    count, findings = audit_tracked_source(tmp_path)
    assert count == 1
    assert any('github_token' in finding for finding in findings)
    assert not any('non-text' in finding for finding in findings)
