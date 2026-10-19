"""Conservative static Legacy closure for the PRIVATE Studio-only probe.

Every Core/Contract/App module remains a root, not merely today's UI entry point.
Parent package initializers are dependencies too. Dynamic imports require separate
installed-wheel tests; this report is neither a license nor a disclosure clearance.
"""
from collections import deque

from scripts.check_layer_boundaries import audit

OPTIONAL_CLASSIC = ('project_watch.studio.app', 'project_watch.api')


def legacy_closure(repo, config):
    result = audit(repo, config)
    if not result.ok:
        raise ValueError(result.summary())
    modules = config['modules']
    adjacency = {name: set() for name in modules}
    for edge in result.edges:
        if (edge.source, edge.target) != OPTIONAL_CLASSIC:
            adjacency[edge.source].add(edge.target)
    for name in modules:
        parent = name.rpartition('.')[0]
        if parent in modules:
            adjacency[name].add(parent)
    paths = {name: [name] for name, layer in modules.items()
             if layer in {'Core', 'Contract', 'App'}}
    queue = deque(sorted(paths))
    while queue:
        name = queue.popleft()
        for target in sorted(adjacency[name]):
            if modules[target] == 'Insight':
                # Composition-root optional plugins are handled by the existing
                # Insight-free builder and unavailable-capability contract.
                if modules[name] != 'App':
                    raise ValueError(f'Required Insight dependency: {name} -> {target}')
                continue
            if target not in paths:
                paths[target] = paths[name] + [target]
                queue.append(target)
    if 'project_watch.api' in paths:
        raise ValueError('Classic API is required by an unapproved path: ' + ' -> '.join(paths['project_watch.api']))
    retained = sorted(name for name in paths if modules[name] == 'Legacy')
    return {'retained_legacy_modules': retained,
            'retained_legacy_paths': {name: paths[name] for name in retained},
            'excluded_legacy_modules': sorted(name for name, layer in modules.items()
                                             if layer == 'Legacy' and name not in paths),
            'optional_classic_edge': list(OPTIONAL_CLASSIC),
            'static_only': True}
