"""Verified, immutable build components. Never hard-link cache files into output."""
import json
import shutil
import uuid

from scripts.windows.maintain import check_files, digest, lock, runtime_digest, safe


def inventory(root):
    root = safe(root)
    result = {}
    for path in root.rglob('*'):
        safe(path)
        if path.is_file():
            result[path.relative_to(root).as_posix()] = digest(path)
    check_files(root, result)
    return result


def component(cache, inputs, destination, builder):
    """Build once per exact input identity; revalidate bytes before every copy."""
    cache, destination = safe(cache), safe(destination)
    if destination.exists() or cache.is_relative_to(destination) or destination.is_relative_to(cache):
        raise ValueError('Cache and new destination must be separate')
    cache.mkdir(parents=True, exist_ok=True)
    key = runtime_digest(inputs)
    entry = cache / key
    with lock(cache / (key + '.lock')):
        hit = entry.exists()
        if not hit:
            stage = cache / ('staging-' + uuid.uuid4().hex)
            stage.mkdir()
            builder(stage / 'tree')
            files = inventory(stage / 'tree')
            if not files:
                raise ValueError('Empty cache component')
            (stage / 'entry.json').write_text(json.dumps({'inputs': inputs, 'files': files}), encoding='utf-8')
            stage.rename(entry)
        safe(entry / 'entry.json')
        record = json.loads((entry / 'entry.json').read_text(encoding='utf-8'))
        if record['inputs'] != inputs or inventory(entry / 'tree') != record['files']:
            raise ValueError('Build cache is corrupt; preserve it for inspection')
        shutil.copytree(entry / 'tree', destination)
        if inventory(destination) != record['files']:
            raise ValueError('Cache copy verification failed')
    return {'key': key, 'hit': hit, 'files': len(record['files'])}
