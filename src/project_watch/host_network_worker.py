"""One bounded same-machine HTTPS operation. No files, DB, shell or logging."""

import base64
import json
import os
import ssl
import sys
import threading
import urllib.error
import urllib.request

from project_watch.feeds import FeedError, GuardedHTTPS
from project_watch.host_network import ERRORS, MAX_INPUT
from project_watch.model_transport import PublicModelOpener
from project_watch.public_article import resolve_article

HEADERS = {'authorization', 'x-api-key', 'anthropic-version', 'content-type', 'accept'}


def dispatch(value):
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError('invalid protocol')
    operation = value.get('operation')
    expected = {'version', 'operation', 'url'}
    if operation == 'model':
        expected |= {'body', 'headers'}
    if set(value) != expected or operation not in {'model', 'article', 'bing'}:
        raise ValueError('unsupported operation')
    GuardedHTTPS._target(value['url'])
    if operation == 'bing':
        from project_watch.bing_news import fetch_bing_body, resolve_bing
        from project_watch.feeds import _pinned_request
        body = fetch_bing_body(value['url'], resolver=resolve_bing, connector=_pinned_request)
        return {'body': base64.b64encode(body).decode('ascii')}
    if operation == 'article':
        response = GuardedHTTPS(resolver=resolve_article,
                               accept='text/html, application/xhtml+xml').get_response(value['url'])
        return {'url': response.url, 'content_type': response.headers.get('content-type', '')[:256],
                'body': base64.b64encode(response.body).decode('ascii')}
    headers = value['headers']
    if (not isinstance(headers, dict) or len(headers) > len(HEADERS)
            or any(not isinstance(k, str) or k.lower() not in HEADERS
                   or not isinstance(v, str) or len(v) > 4096
                   or any(ord(c) < 32 or ord(c) == 127 for c in v)
                   for k, v in headers.items())):
        raise ValueError('invalid headers')
    body = base64.b64decode(value['body'], validate=True)
    if not 0 < len(body) <= 150_000:
        raise ValueError('invalid request size')
    request = urllib.request.Request(value['url'], data=body, headers=headers, method='POST')
    # Explicit direct class: worker cannot recursively spawn itself. Same public
    # IP pinning, TLS, no redirects/replay, response limits as the WSL transport.
    with PublicModelOpener().open(request, timeout=60) as response:
        result = response.read(2_000_001)
    if len(result) > 2_000_000:
        raise ValueError('oversized response')
    return {'body': base64.b64encode(result).decode('ascii')}


def run(input_stream, output_stream):
    try:
        raw = input_stream.read(MAX_INPUT+1)
        if len(raw) > MAX_INPUT:
            raise ValueError('oversized input')
        result = dispatch(json.loads(raw))
    except FeedError as exc:
        result = {'error': str(exc) if str(exc) in ERRORS else 'host_network_unavailable'}
        if type(exc.retry_after) is int:
            result['retry_after'] = max(5, min(exc.retry_after, 86400))
    except ssl.SSLCertVerificationError:
        result = {'error': 'tls_verification_failed'}
    except urllib.error.HTTPError:
        result = {'error': 'http_failure'}
    except Exception:
        # Never serialize provider messages, keys, requests or exception details.
        result = {'error': 'host_network_unavailable'}
    output_stream.write(json.dumps({'version': 1, **result}).encode('ascii'))
    output_stream.flush()


if __name__ == '__main__':
    # WSL's interop process can die independently of its Windows child. Enforce
    # the deadline in the Windows process as well; never leave a slow publisher
    # holding a helper indefinitely after the Linux caller timed out.
    deadline = threading.Timer(80, lambda: os._exit(74))
    deadline.daemon = True
    deadline.start()
    try:
        run(sys.stdin.buffer, sys.stdout.buffer)
    finally:
        deadline.cancel()
