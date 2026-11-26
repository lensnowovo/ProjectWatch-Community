import pytest

from scripts.windows.build_cache import component


def test_cache_hit_skips_builder_and_output_is_independent(tmp_path):
    calls = []
    def build(tree):
        calls.append(1)
        tree.mkdir()
        (tree / 'dependency').write_bytes(b'original')
    cache = tmp_path / 'cache'
    first = component(cache, {'abi': 'cp313', 'dependency': 'a'}, tmp_path / 'one', build)
    (tmp_path / 'one/dependency').write_bytes(b'user edit')
    second = component(cache, {'abi': 'cp313', 'dependency': 'a'}, tmp_path / 'two', build)
    assert not first['hit'] and second['hit'] and len(calls) == 1
    assert (tmp_path / 'two/dependency').read_bytes() == b'original'
    third = component(cache, {'abi': 'cp313', 'dependency': 'b'}, tmp_path / 'three', build)
    assert not third['hit'] and len(calls) == 2


@pytest.mark.parametrize('damage', ['modify', 'extra', 'missing'])
def test_corrupt_cache_fails_without_rebuilding(tmp_path, damage):
    def build(tree):
        tree.mkdir()
        (tree / 'file').write_bytes(b'original')
    cache = tmp_path / 'cache'
    record = component(cache, {'input': 'one'}, tmp_path / 'one', build)
    tree = cache / record['key'] / 'tree'
    if damage == 'modify':
        (tree / 'file').write_bytes(b'changed')
    elif damage == 'extra':
        (tree / 'extra').write_bytes(b'extra')
    else:
        (tree / 'file').unlink()
    with pytest.raises(ValueError, match='corrupt'):
        component(cache, {'input': 'one'}, tmp_path / 'two', lambda _: pytest.fail('rebuild'))
    assert not (tmp_path / 'two').exists()


def test_failed_build_is_not_published(tmp_path):
    def fail(tree):
        tree.mkdir()
        (tree / 'partial').write_bytes(b'partial')
        raise OSError('disk full')
    with pytest.raises(OSError):
        component(tmp_path / 'cache', {}, tmp_path / 'out', fail)
    assert not (tmp_path / 'out').exists()
    assert list((tmp_path / 'cache').glob('staging-*'))


def test_cache_cannot_be_output(tmp_path):
    with pytest.raises(ValueError):
        component(tmp_path / 'cache', {}, tmp_path / 'cache/out', lambda _: None)


def test_linked_cache_tree_is_rejected(tmp_path):
    from scripts.windows.build_cache import inventory
    actual = tmp_path/'actual'
    actual.mkdir()
    (actual/'file').write_bytes(b'keep')
    linked = tmp_path/'linked'
    try:
        linked.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip('OS requires symlink privilege')
    with pytest.raises(ValueError, match='Linked'):
        inventory(linked)
    assert (actual/'file').read_bytes() == b'keep'
