"""Entry for the rewritten pipeline's job kinds. One path per kind; no legacy admission."""


def run(pipeline, execution):
    kind = execution.job['kind']
    if kind == 'collect':
        from project_watch.studio import collect_job

        project = pipeline.store.get_project(execution.job['project_id']) or {}
        mode = ('first' if not project.get('last_external_at')
                else 'refresh' if execution.job.get('input_version', '').startswith('manual:') else 'incremental')
        return collect_job.run(pipeline, execution, mode=mode)
    if kind == 'analyze':
        from project_watch.studio import analyze_job

        return analyze_job.run(pipeline, execution)
    if kind == 'report':
        from project_watch.studio import report_job

        return report_job.run(pipeline, execution)
    raise ValueError('not a rewritten pipeline job kind')
