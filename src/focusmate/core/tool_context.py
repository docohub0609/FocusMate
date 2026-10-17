"""Keep native tool calls and results atomic in the existing context snapshot."""
from copy import deepcopy

from focusmate.core.context_builder import build_context, MapContextResolver


def prepare_tool_context(context, messages, tools, prompt, *, budget=None):
    pending = set()
    seen = set()
    try:
        for message in messages:
            role = message['role']
            if role == 'tool':
                ref = message['tool_call_id']
                if ref not in pending:
                    raise ValueError()
                pending.remove(ref)
            else:
                if pending:
                    raise ValueError()
                if role not in ('system', 'user', 'assistant'):
                    raise ValueError()
                for call in message.get('tool_calls', []):
                    ref = call['id']
                    if role != 'assistant' or not ref or ref in seen:
                        raise ValueError()
                    seen.add(ref)
                    pending.add(ref)
        if pending:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        return {'status': 'failed', 'failure': {'code': 'INVALID_TOOL_MESSAGE_PAIRS'}}
    request = {'goalContext': deepcopy(context['goalContext']), 'purpose': 'toolUse',
               'policyVersion': 't057-tool-context-v1', 'systemPrompt': prompt,
               'mandatoryPayload': {'messages': deepcopy(messages), 'tools': deepcopy(tools)},
               'dialogueRefs': [], 'sourceRefs': [],
               'budget': budget or {'contextWindowTokens': 65536, 'reservedOutputTokens': 4096, 'safetyMarginTokens': 4096}}
    return build_context(request, MapContextResolver([]))
