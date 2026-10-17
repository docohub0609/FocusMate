"""Bounded model-output diagnostics; never captures request headers or reasoning."""
from copy import deepcopy
from hashlib import sha256
import json

from focusmate.contracts.skill_files import strict_json

LIMIT = 65536


class ReferenceValidationError(ValueError):
    def __init__(self, code, invalid_refs):
        super().__init__(code)
        self.invalid_refs=sorted(set(invalid_refs))[:24]


def bounded_text(text):
    text = text if isinstance(text, str) else ''
    return {'text': text[:LIMIT], 'originalLength': len(text), 'truncated': len(text) > LIMIT,
            'sha256': sha256(text.encode('utf-8')).hexdigest()}


def coach_plan_json(text, *, finish_reason=None):
    """Recover only one redundant root-object closer, without changing any value.

    This narrow Coach transport rule does not apply to tool arguments or other
    contracts. The decoded object still needs normal schema and semantic review.
    """
    try:
        return strict_json(text), None
    except json.JSONDecodeError as error:
        if (error.msg != 'Extra data' or len(text) > LIMIT
                or finish_reason not in (None, '', 'stop')
                or text[error.pos:].strip() != '}'):
            raise
        original_error = error
        prefix = text[:error.pos].rstrip()
        value = strict_json(prefix)
        if not isinstance(value, dict):
            raise original_error
        return value, {'kind': 'redundantTrailingCloser', 'position': error.pos,
                       'removedSuffix': text[error.pos:], 'original': bounded_text(text),
                       'normalizedSha256': sha256(prefix.encode('utf-8')).hexdigest()}


def response_diagnostic(text, error, *, channel):
    detail = {**bounded_text(text), 'channel': channel, 'errorType': type(error).__name__,
              'message': getattr(error, 'message', str(error))[:500],
              'position': None, 'line': None, 'column': None}
    if isinstance(error, json.JSONDecodeError):
        detail.update(position=error.pos, line=error.lineno, column=error.colno)
        if error.pos < LIMIT:
            detail['syntaxHint'] = _mismatched_closer(text, error.pos)
    return detail


def _mismatched_closer(text, position):
    """Explain a mismatched delimiter at the parser error; never repair the reply."""
    stack=[];quoted=False;escaped=False
    for index,char in enumerate(text[:position+1]):
        if quoted:
            if escaped: escaped=False
            elif char=='\\': escaped=True
            elif char=='"': quoted=False
            continue
        if char=='"': quoted=True
        elif char in '{[': stack.append((char,index))
        elif char in '}]':
            if not stack: return None
            opening,opened=stack[-1]
            expected='}' if opening=='{' else ']'
            if char!=expected:
                if index!=position: return None
                return {'position':index,'openedAt':opened,'expectedCloser':expected,
                        'actualCloser':char,'nearbyText':text[max(0,index-100):index+100],
                        'instruction':'此位置的结束括号与尚未结束的对象/数组不匹配。按Schema重建结构，再返回完整JSON；这只是语法定位，不代表内容正确。'}
            stack.pop()
    return None


def response_audit(value):
    choice = value.get('choices', [{}])[0]
    message = choice.get('message', {})
    usage = value.get('usage') or {}
    return {'responseId': str(value.get('id', ''))[:200], 'model': str(value.get('model', ''))[:200],
            'finishReason': str(choice.get('finish_reason', ''))[:80],
            'usage': {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_cache_hit_tokens')
                      if type(usage.get(k)) is int and usage[k] >= 0},
            'content': bounded_text(message.get('content')),
            'toolArguments': [bounded_text(c.get('function', {}).get('arguments'))
                              for c in message.get('tool_calls', [])[:8]]}


class ModelResponseError(ValueError):
    code = 'MODEL_RESPONSE_INVALID'

    def __init__(self, text, error, *, channel, audit):
        super().__init__(getattr(error, 'message', str(error))[:500])
        self.response_diagnostic = response_diagnostic(text, error, channel=channel)
        self.rejected_response = bounded_text(text)
        self.response_audit = deepcopy(audit)
