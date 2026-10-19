"""Exercise the RUNNING local service, not a detached adapter. Preview by default.

Only explicit public terms; no credentials, private document text, or model calls.
Creates a daily scope when --run is passed. Existing scopes remain unchanged.
"""
import argparse
import http.client
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:8796')
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--term', required=True)
    parser.add_argument('--category', choices=['company','sector','competitors','policy','industry'], required=True)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    base = args.base.rstrip('/')
    parsed = urlsplit(base)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1','localhost'}
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise SystemExit('Only an explicit local service is allowed.')
    from uuid import UUID
    UUID(args.project_id)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(base+path, data=data, headers={'Origin':base,'Content-Type':'application/json'})
        with opener.open(req, timeout=10) as response:
            return json.load(response)

    route = f'/api/projects/{args.project_id}/monitoring'
    scope = dict(category=args.category, term=args.term, provider='bing')
    preview = request(route+'/focus?'+urlencode(scope))
    print(json.dumps({'mode':'start' if args.run else 'preview_no_collection',
                      'disclosure':preview['disclosure'], 'eligible':preview['eligible']}, ensure_ascii=True), flush=True)
    if not args.run:
        return
    result = request(route+'/focus', {**scope, 'consent_version':preview['consent_version']})
    plan_id = result['plan']['id']
    # Do not silently retry or resume an existing plan in an acceptance probe.
    for _ in range(45):
        receipts = request(route+f'/plans/{plan_id}/runs')
        if receipts:
            signals = request(route+'/signals')
            found = [s for s in signals if s['plan_id'] == plan_id]
            print(json.dumps({'plan_id':plan_id, 'receipt':receipts[-1],
                'stored_items':len(found), 'sample':[{'title':s['title'],'url':s['url'],
                    'published_at':s['published_at'],'freshness':s['freshness']} for s in found[:3]]},
                ensure_ascii=True), flush=True)
            return
        time.sleep(1)
    raise SystemExit('No finished receipt yet; the durable task remains in the service.')


if __name__ == '__main__':
    try:
        main()
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError):
        raise SystemExit('Local service request failed; no private response body was printed.') from None
