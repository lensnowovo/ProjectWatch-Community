"""Opt-in same-host Windows HTTPS worker; no listener, shell or silent replay.

The executable is administrator/operator configuration. Payloads and credentials
travel only through anonymous pipes, never arguments, environment or disk.
"""

import base64
import io
import json
import os
import re
import subprocess
import urllib.error

from project_watch.feeds import MAX_FEED_BYTES, FeedError, GuardedHTTPS, PublicResponse

ENV_NAME = 'PROJECT_WATCH_HOST_PYTHON'
MAX_INPUT = 240_000
MAX_OUTPUT = 2_800_000
ERRORS = frozenset({'disallowed_url', 'dns_failure', 'disallowed_address',
                    'connection_failed', 'oversized_feed', 'invalid_redirect',
                    'rate_limited', 'http_failure', 'too_many_redirects',
                    'tls_verification_failed', 'host_network_unavailable', 'redirect_not_allowed'})


def configured_host_python():
    return validate_host_python(os.environ.get(ENV_NAME))


def validate_host_python(value):
    if value is None:
        return None
    # Explicit WSL-mounted local Windows venv executable only. No PATH lookup,
    # switches, UNC locations or web-supplied commands.
    if (not isinstance(value, str)
            or not re.fullmatch(r'/mnt/[a-z]/[^\r\n\x00]+/python\.exe', value)
            or any(part in {'.', '..'} for part in value.split('/'))
            or not os.path.isfile(value)):
        raise ValueError('invalid host network executable')
    return value


def exchange(executable, payload):
    wire = json.dumps(payload, ensure_ascii=True).encode('ascii')
    if len(wire) > MAX_INPUT:
        raise FeedError('host_network_unavailable')
    # This environment cannot leak inherited model credentials/DSH settings.
    env = {k: v for k, v in os.environ.items()
           if k in {'PATH', 'WSL_INTEROP', 'WSL_DISTRO_NAME', 'SYSTEMROOT', 'WINDIR'}}
    try:
        result = subprocess.run(
            [executable, '-I', '-m', 'project_watch.host_network_worker'],
            input=wire, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=85, check=False, env=env,
        )
        if result.returncode or len(result.stdout) > MAX_OUTPUT:
            raise ValueError('worker unavailable')
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get('version') != 1:
            raise ValueError('invalid worker response')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise FeedError('host_network_unavailable') from None
    if value.get('error'):
        code = value['error'] if isinstance(value['error'], str) and value['error'] in ERRORS else 'host_network_unavailable'
        retry_after = value.get('retry_after')
        retry_after = max(5, min(retry_after, 86400)) if type(retry_after) is int else None
        raise FeedError(code, retry_after=retry_after)
    return value


def decode_body(value):
    try:
        body = base64.b64decode(value['body'], validate=True)
        if len(body) > MAX_FEED_BYTES:
            raise ValueError('oversized response')
        return body
    except (KeyError, ValueError, TypeError):
        raise FeedError('host_network_unavailable') from None


class HostArticleTransport:
    def __init__(self, executable):
        self.executable = executable

    def get_response(self, url):
        GuardedHTTPS._target(url)
        value = exchange(self.executable, {'version': 1, 'operation': 'article', 'url': url})
        try:
            GuardedHTTPS._target(value['url'])
            media = value['content_type']
            if not isinstance(media, str) or len(media) > 256:
                raise ValueError('invalid media type')
            return PublicResponse(value['url'], {'content-type': media}, decode_body(value))
        except (KeyError, TypeError, ValueError):
            raise FeedError('host_network_unavailable') from None


class HostModelOpener:
    def __init__(self, executable):
        self.executable = executable

    def open(self, request, timeout=60):
        GuardedHTTPS._target(request.full_url)
        if request.get_method() != 'POST' or not isinstance(request.data, bytes):
            raise urllib.error.URLError('invalid host model request')
        try:
            value = exchange(self.executable, {
                'version': 1, 'operation': 'model', 'url': request.full_url,
                'body': base64.b64encode(request.data).decode('ascii'),
                'headers': dict(request.header_items()),
            })
            return io.BytesIO(decode_body(value))
        except FeedError:
            raise urllib.error.URLError('host model network unavailable') from None


def configured_model_opener():
    from project_watch.model_transport import PublicModelOpener
    executable = configured_host_python()
    return HostModelOpener(executable) if executable else PublicModelOpener()


def configured_article_transport():
    from project_watch.public_article import resolve_article
    executable = configured_host_python()
    return (HostArticleTransport(executable) if executable else
            GuardedHTTPS(resolver=resolve_article, accept='text/html, application/xhtml+xml'))
