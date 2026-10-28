"""Brand resources must reach the launcher, installer and existing shortcuts."""
from pathlib import Path

from PIL import Image

from scripts.windows.render_icon import render_icon


def test_brand_exports_match_vector_source():
    from project_watch.studio.desktop import tray_image
    from scripts.build_brand_assets import raster, svg
    root = Path(__file__).resolve().parents[1]
    static = root/'src/project_watch/static'
    assert (root/'frontend/src/assets/brand-mark.svg').read_text(encoding='utf-8') == svg()
    assert (static/'favicon.svg').read_text(encoding='utf-8') == svg(small=True, tile=True)
    with Image.open(static/'brand-desktop.png') as master:
        assert master.tobytes() == raster(1024).tobytes()
    assert tray_image().tobytes() == raster(64, small=True).tobytes()


def test_mac_icon_export_and_plist(tmp_path):
    import plistlib

    from scripts.build_macos_bundle import write_plist
    root = Path(__file__).resolve().parents[1]
    (tmp_path/'Contents').mkdir()
    write_plist(tmp_path, '0.2.0a1', 'arm64')
    info = plistlib.loads((tmp_path/'Contents/Info.plist').read_bytes())
    assert info['CFBundleIconFile'] == 'ProjectWatch.icns'
    with Image.open(root/'src/project_watch/static/brand-desktop.png') as master:
        master.save(tmp_path/'ProjectWatch.icns', format='ICNS')
    with Image.open(tmp_path/'ProjectWatch.icns') as icon:
        assert icon.size == (1024, 1024)


def test_icon_contains_shell_sizes_and_transparency(tmp_path):
    icon = tmp_path/'brand.ico'
    render_icon(icon)
    with Image.open(icon) as image:
        assert image.format == 'ICO'
        assert image.ico.sizes() == {(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)}
        for size in image.ico.sizes():
            frame = image.ico.getimage(size).convert('RGBA')
            assert frame.getpixel((0, 0))[3] <= 2  # Resampling can leave a sub-1% fringe.
            assert frame.getpixel((size[0]//2, size[1]//2))[3] == 255


def test_windows_brand_wiring():
    root = Path(__file__).resolve().parents[1]
    bundle = (root/'scripts/build_windows_bundle.py').read_text(encoding='utf-8')
    installer = (root/'scripts/windows/Installer.nsi').read_text(encoding='utf-8')
    assert "'/win32icon:'" in bundle
    assert 'render_icon.py' in bundle
    assert '!define MUI_ICON "${BUNDLE}\\ProjectWatch.ico"' in installer
    assert '!define MUI_UNICON "${BUNDLE}\\ProjectWatch.ico"' in installer
    shortcuts = [line for line in installer.splitlines() if 'CreateShortcut' in line]
    assert len(shortcuts) == 3
    assert all('"$INSTDIR\\ProjectWatch.ico" 0' in line for line in shortcuts)
    assert '"DisplayIcon" "$INSTDIR\\ProjectWatch.ico"' in installer
    assert 'Delete "$INSTDIR\\ProjectWatch.ico"' in installer


def test_only_reviewed_brand_bitmaps_pass_release_inventory(tmp_path):
    from zipfile import ZipFile

    from scripts.release_audit import audit_wheel
    root = Path(__file__).resolve().parents[1]
    path = tmp_path/'brand.whl'
    with ZipFile(path, 'w') as archive:
        for name in ('brand-desktop.png', 'brand-tray.png'):
            archive.write(root/'src/project_watch/static'/name, 'project_watch/static/'+name)
    assert audit_wheel(path)[1] == []
    with ZipFile(path, 'w') as archive:
        archive.writestr('project_watch/static/brand-tray.png', b'SYNTHETIC unexpected bytes')
    assert audit_wheel(path)[1] == ['project_watch/static/brand-tray.png: unreviewed brand asset']
