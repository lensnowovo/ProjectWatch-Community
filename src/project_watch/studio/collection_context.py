"""Lossless, job-local collection indexes; never rewrite material evidence.

Only unsent oversized collection requests use these views. Exact completed
wire requests must be replayed before calling fit(). No external reads here.
"""
import hashlib
import json
import re
from copy import deepcopy

MAX_WIRE_BYTES = 32768
PAGE_BYTES = 4096
REF = re.compile(r'^context:([0-9a-f]{64})$')
GUIDE = ('Saved collection context, not a factual summary. Full content is retained. '
         'Read context_ref using pw_read(source_id=context_ref, offset=next_offset or 0, view=record). '
         'Offsets count entries or Unicode characters as specified. Read source excerpts before quoting; '
         'source IDs and evidence versions remain unchanged. Index pages are not evidence quotations.')


def wire(value):
    # Same encoding and separators as the Python provider transport.
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


class Index:
    def __init__(self, execution):
        self.execution = execution

    def save(self, value):
        digest = hashlib.sha256(wire(value)).hexdigest()
        stage = 'cc:' + digest[:48]
        old = self.execution.read_checkpoint(stage)
        if old is not None and hashlib.sha256(wire(old['value'])).hexdigest() != digest:
            raise ValueError('collection_context_collision')
        if old is None:
            self.execution.checkpoint_chunked(stage, {'value': value})
        return {'context_ref': 'context:' + digest, 'type': type(value).__name__,
                'total': len(value) if isinstance(value, (str, list, dict)) else 1}

    def load(self, ref):
        match = REF.fullmatch(ref)
        if not match:
            raise ValueError('invalid_collection_context_ref')
        saved = self.execution.read_checkpoint('cc:' + match[1][:48])
        if saved is None or hashlib.sha256(wire(saved['value'])).hexdigest() != match[1]:
            raise ValueError('collection_context_missing_or_changed')
        return saved['value']

    def entry(self, value):
        if len(wire(value)) <= 384:
            return value
        result = self.save(value)
        # Keep each evidence identity together; never infer ownership from a
        # query match. Long labels and body fields remain separately readable.
        if isinstance(value, dict):
            for key in ('id', 'evidence_version_id', 'source_type', 'read_scope', 'title'):
                if key in value:
                    result[key] = value[key] if len(wire(value[key])) <= 256 else self.save(value[key])
        return result

    def read(self, ref, offset=0):
        value = self.load(ref)
        if type(offset) is not int or offset < 0 or not isinstance(value, (str, list, dict)) or offset > len(value):
            raise ValueError('invalid_collection_context_offset')
        result = {**self.save(value), 'offset': offset, 'next_offset': None,
                  'offset_unit': 'characters' if isinstance(value, str) else 'entries',
                  'untrusted_data': True}
        if isinstance(value, str):
            # A named, exact text window with a continuation, never clipped JSON.
            end = min(len(value), offset + 768)
            result.update(value=value[offset:end], next_offset=end if end < len(value) else None)
            while len(wire(result)) > PAGE_BYTES and end > offset:
                end = offset + (end - offset) // 2
                result.update(value=value[offset:end], next_offset=end if end < len(value) else None)
        else:
            entries = list(value.items()) if isinstance(value, dict) else value
            result['value'] = {} if isinstance(value, dict) else []
            end = offset
            while end < len(entries) and end < offset + 8:
                candidate = deepcopy(result)
                if isinstance(value, dict):
                    key, item = entries[end]
                    candidate['value'][key] = self.entry(item)
                else:
                    candidate['value'].append(self.entry(entries[end]))
                candidate['next_offset'] = end + 1 if end + 1 < len(entries) else None
                if len(wire(candidate)) > PAGE_BYTES:
                    break
                result, end = candidate, end + 1
            if end == offset and offset < len(entries):
                # An unbounded object key cannot be safely represented as a
                # normal field. Return that exact key/value pair as a subpage.
                result['value'] = {'entry_ref': self.save(list(entries[offset]) if isinstance(value, dict) else entries[offset])}
                result['next_offset'] = offset + 1 if offset + 1 < len(entries) else None
        if len(wire(result)) > PAGE_BYTES or result['next_offset'] == offset:
            raise ValueError('collection_context_page_limit')
        return result


def fit(body, execution):
    """Return a bounded view or None when irreducible history cannot fit.

    Preserve every message role/tool-call pairing, system prompt and schema.
    Index only user/tool content; never summarize assistant reasoning or invent
    a continuation. The caller closes collection partially on a hard limit.
    """
    before = wire(body)
    if len(before) <= MAX_WIRE_BYTES:
        return body
    result = deepcopy(body)
    index = Index(execution)
    candidates = sorted((i for i, m in enumerate(result['messages'])
                         if m.get('role') in ('user', 'tool') and isinstance(m.get('content'), str)),
                        key=lambda i: len(wire(result['messages'][i]['content'])), reverse=True)
    changed = []
    for position in candidates:
        message = result['messages'][position]
        raw = message['content']
        prefix = ''
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
            if message['role'] == 'user' and '{' in raw:
                start = raw.index('{')
                try:
                    value = json.loads(raw[start:])
                    prefix = raw[:start]
                except ValueError:
                    pass
        # The raw string is separately archived for byte-exact reconstruction.
        raw_ref = index.save(raw)
        ref = index.save(value)
        page = index.read(ref['context_ref']) if isinstance(value, (str, list, dict)) else ref
        compact = prefix + json.dumps({'context_index': page, 'raw_message': raw_ref, 'guide': GUIDE}, ensure_ascii=False)
        if len(wire(compact)) >= len(wire(raw)):
            continue
        message['content'] = compact
        changed.append({'message': position, 'raw_message': raw_ref, 'index': ref})
        if len(wire(result)) <= MAX_WIRE_BYTES:
            break
    after = wire(result)
    receipt = {'version': 1, 'original_sha256': hashlib.sha256(before).hexdigest(),
               'original_bytes': len(before), 'wire_sha256': hashlib.sha256(after).hexdigest(),
               'wire_bytes': len(after), 'limit': MAX_WIRE_BYTES, 'indexes': changed,
               'fits': len(after) <= MAX_WIRE_BYTES}
    execution.checkpoint_chunked('cc-fit:' + receipt['original_sha256'][:40], receipt)
    return result if receipt['fits'] else None
