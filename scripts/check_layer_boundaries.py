"""Fail-closed static layer registration and import-direction audit.

Does not import product code. Dynamic imports and data disclosure are out of scope.
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

LAYERS = {'Core', 'Contract', 'Insight', 'App', 'Legacy'}


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    line: int
    rule: str = ''


@dataclass
class Result:
    modules: dict[str, str]
    edges: list[Edge] = field(default_factory=list)
    violations: list[Edge] = field(default_factory=list)
    accepted: list[Edge] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    unknown_pages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self):
        return not (self.violations or self.unknown or self.unknown_pages or self.errors)

    def summary(self):
        counts = dict(sorted(Counter(self.modules.values()).items()))
        lines = [f'Modules: {len(self.modules)} {counts}',
                 f'Unknown modules: {len(self.unknown)}; unknown pages: {len(self.unknown_pages)}',
                 f'Existing exception edges: {len(self.accepted)}; violations: {len(self.violations)}']
        lines.extend(f'unclassified: {name}' for name in self.unknown + self.unknown_pages)
        lines.extend(f'{e.source}:{e.line} -> {e.target} [{e.rule}]' for e in self.violations)
        lines.extend(self.errors)
        return '\n'.join(lines)


def load_map(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def frozen_exceptions(exceptions, approved):
    """The approved exact set lives in a separately reviewed test, not the map."""
    errors = []
    for item in exceptions:
        pair = (item.get('source', ''), item.get('target', ''))
        if pair not in approved or any('*' in name for name in pair):
            errors.append(f'Unapproved exception: {pair}')
    return errors


def _module(path, src):
    parts = list(path.relative_to(src).with_suffix('').parts)
    if parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts)


def _known(name, modules):
    # Imports may address a symbol or a non-file namespace package.
    while name:
        if name in modules:
            return name
        name = name.rpartition('.')[0]
    return None


def _imports(tree, source, is_package, modules):
    package = source if is_package else source.rpartition('.')[0]
    targets = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split('.')
                if node.level > len(parts):
                    continue
                base = '.'.join(parts[:len(parts) - node.level + 1])
                base = f'{base}.{node.module}' if node.module else base
            else:
                base = node.module or ''
            names = []
            for alias in node.names:
                child = f'{base}.{alias.name}'
                names.append(child if child in modules else base)
        else:
            continue
        for name in names:
            target = _known(name, modules)
            if target and target != source:
                targets.setdefault(target, node.lineno)
    return [Edge(source, target, line) for target, line in sorted(targets.items())]


def _rule(source, target, layers, root):
    left, right = layers[source], layers[target]
    if left == 'Contract' and right != 'Contract':
        return 'contract_to_implementation'
    if left == 'Core' and right == 'Insight':
        return 'core_to_insight'
    if right == 'Insight' and left != 'Insight' and source != root:
        return 'insight_wiring_outside_root'
    return ''


def audit(root: Path, config):
    src = root / 'src'
    files = {_module(p, src): p for p in sorted((src / 'project_watch').rglob('*.py'))}
    layers = config.get('modules', {})
    result = Result({name: layers[name] for name in files if name in layers})
    result.unknown = sorted(files.keys() - layers.keys())
    for name in sorted(layers.keys() - files.keys()):
        result.errors.append(f'Stale module registration: {name}')
    for name, layer in layers.items():
        if layer not in LAYERS:
            result.errors.append(f'Invalid layer: {name}={layer}')
    composition = config.get('composition_root')
    app_modules = [name for name, layer in layers.items() if layer == 'App']
    if app_modules != [composition] or composition not in files:
        result.errors.append('Exactly one App module must be the registered composition root')
    if composition in files and layers.get(composition) != 'App':
        result.errors.append('Composition root must be App')
    frontend = config.get('frontend', {})
    pages = {p.relative_to(root).as_posix() for p in (root / 'frontend/src/pages').rglob('*.tsx')}
    pages.update(p.relative_to(root).as_posix() for p in (root / 'frontend/src').glob('*.tsx')
                 if p.stem in {'App', 'main'})
    result.unknown_pages = sorted(pages - frontend.keys())
    for path, layer in frontend.items():
        if not (root / path).is_file() or layer not in LAYERS:
            result.errors.append(f'Invalid frontend registration: {path}={layer}')
    exceptions = {}
    for item in config.get('exceptions', []):
        pair = (item.get('source', ''), item.get('target', ''))
        if (any('*' in name for name in pair) or not all(name in files for name in pair)
                or not item.get('reason', '').strip() or not item.get('remove_task', '').strip()
                or pair in exceptions):
            result.errors.append(f'Invalid exact exception: {pair}')
        else:
            exceptions[pair] = item
    used = set()
    for name, path in files.items():
        try:
            tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
        except (SyntaxError, UnicodeError) as exc:
            result.errors.append(f'Cannot parse {name}: {exc}')
            continue
        for edge in _imports(tree, name, path.name == '__init__.py', files):
            result.edges.append(edge)
            if name not in layers or edge.target not in layers:
                continue
            rule = _rule(name, edge.target, layers, composition)
            if rule:
                classified = Edge(name, edge.target, edge.line, rule)
                pair = (name, edge.target)
                if pair in exceptions:
                    used.add(pair)
                    result.accepted.append(classified)
                else:
                    result.violations.append(classified)
    for pair in exceptions.keys() - used:
        result.errors.append(f'Stale exception (remove rather than broaden): {pair}')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--map', type=Path)
    args = parser.parse_args()
    mapping = args.map or args.root / 'scripts/layer_map.json'
    try:
        result = audit(args.root, load_map(mapping))
    except (OSError, ValueError, TypeError) as exc:
        print(f'Invalid layer map: {exc}')
        return 1
    print(result.summary())
    return 0 if result.ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
