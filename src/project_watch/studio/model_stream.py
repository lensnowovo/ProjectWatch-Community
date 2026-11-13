"""Read a complete OpenAI SSE reply without treating partial output as a result."""
import json

# Provider-declared ends of a complete reply. Beyond the OpenAI values, the GLM
# endpoint documents sensitive (safety review), network_error (inference failed)
# and model_context_window_exceeded: each is a conclusive answer that there is no
# usable result, which the caller names; none is an uncertain transport outcome.
# https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E5%AF%B9%E8%AF%9D%E8%A1%A5%E5%85%A8
FINISHES = frozenset({'stop', 'length', 'content_filter', 'sensitive', 'network_error',
                      'model_context_window_exceeded'})


class IncompleteStream(ValueError):
    """No conclusive complete response; callers must not automatically resend."""

    def __init__(self, reason, finish=None):
        super().__init__(reason)
        self.finish = finish  # The provider's undocumented end value, for diagnostics only.


def read_completion(response, limit, observe, identify=None):
    """Normalize SSE to the existing journal response, preserving usage.

    No socket deadline or model retries. Line reads are size bounded; comment
    heartbeats count as transport activity, never as a completed model answer.
    """
    event_bytes = 0
    decoded_bytes = 0
    content, reasoning, event = [], [], []
    finish = None
    usage = None
    identity = {}
    done = False
    while not done:
        raw = response.readline(limit - event_bytes + 1)
        if not raw:
            raise IncompleteStream('missing_end')
        event_bytes += len(raw)
        observe(len(raw))
        if event_bytes > limit:
            raise IncompleteStream('oversized_stream')
        try:
            line = raw.decode('utf-8').rstrip('\r\n')
        except UnicodeDecodeError as exc:
            raise IncompleteStream('invalid_encoding') from exc
        if line.startswith('data:'):
            event.append(line[5:].removeprefix(' '))
        elif line == '' and event:
            data, event = '\n'.join(event), []
            event_bytes = 0
            if data == '[DONE]':
                done = True
                continue
            try:
                chunk = json.loads(data)
            except ValueError as exc:
                raise IncompleteStream('invalid_event') from exc
            if not isinstance(chunk, dict) or 'error' in chunk:
                raise IncompleteStream('error_event')
            for key in ('id', 'request_id', 'model', 'created'):
                if key in chunk:
                    identity[key] = chunk[key]
            if identify:
                identify(chunk.get('request_id') or chunk.get('id'))
            if isinstance(chunk.get('usage'), dict):
                usage = chunk['usage']
            choices = chunk.get('choices', [])
            if not isinstance(choices, list):
                raise IncompleteStream('invalid_choices')
            for choice in choices:
                if not isinstance(choice, dict) or choice.get('index', 0) != 0:
                    raise IncompleteStream('unexpected_choice')
                delta = choice.get('delta') or {}
                if not isinstance(delta, dict) or delta.get('tool_calls'):
                    raise IncompleteStream('unexpected_delta')
                for key, target in (('content', content), ('reasoning_content', reasoning)):
                    value = delta.get(key)
                    if value is not None:
                        if not isinstance(value, str) or (finish is not None and value):
                            raise IncompleteStream('invalid_delta')
                        decoded_bytes += len(value.encode('utf-8'))
                        if decoded_bytes > limit:
                            raise IncompleteStream('oversized_stream')
                        if value:
                            target.append(value)
                end = choice.get('finish_reason')
                if end is not None:
                    if finish is not None:
                        raise IncompleteStream('invalid_finish')  # a second end, not an unknown value
                    if not isinstance(end, str) or end not in FINISHES:
                        raise IncompleteStream('invalid_finish', end if isinstance(end, str) else None)
                    finish = end
        elif line == '':
            event_bytes = 0  # Comments/heartbeats do not accumulate in memory.
    if finish is None:
        raise IncompleteStream('missing_finish')
    result = {**identity, 'choices': [{'index': 0, 'finish_reason': finish,
              'message': {'role': 'assistant', 'content': ''.join(content),
                          'reasoning_content': ''.join(reasoning)}}]}
    if usage is not None:
        result['usage'] = usage
    encoded = json.dumps(result, ensure_ascii=False).encode('utf-8')
    if len(encoded) > limit:
        raise IncompleteStream('oversized_stream')
    return encoded
