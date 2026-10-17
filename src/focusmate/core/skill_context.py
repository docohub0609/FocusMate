"""T054's validated caller materials adapted to T056; no business decisions."""
from copy import deepcopy

from focusmate.core.context_builder import build_context, MapContextResolver, hash_content


DEFAULT_BUDGET = {'contextWindowTokens': 65536, 'reservedOutputTokens': 16384,
                  'safetyMarginTokens': 4096}
DEFAULT_REVIEW_BUDGET = {**DEFAULT_BUDGET, 'contextWindowTokens': 131072}


def prepare_skill_context(data, prompt, provider, *, review, deadline, clock, options=None):
    options = deepcopy(options or {})
    if set(options) - {'budget', 'reviewBudget', 'summaryBudget', 'recentMessageCount', 'maxSummaryMessages', 'previousSummary'}:
        return {'status': 'failed', 'failure': {'code': 'INVALID_CONTEXT_OPTIONS'}}
    mandatory = deepcopy(data)
    dialogue = mandatory.pop('dialogue')
    goal = data['goalContext']
    records = []
    turn = -1
    for sequence, message in enumerate(dialogue):
        # T054 has user/assistant messages only. A user starts the next turn;
        # following assistant messages remain attached to that user's request.
        if message['role'] == 'user' or turn < 0:
            turn += 1
        records.append({'ref': message['ref'], 'goalContext': goal, 'kind': 'dialogue',
            'content': message, 'version': 'caller-snapshot-1', 'sequence': sequence,
            'turnId': str(turn), 'contentHash': hash_content(message)})
    source_refs = []
    materials = mandatory['materials']
    sources = [('goal', materials['goalRequirement']), ('problemFact', materials['userRequest']),
               *[('problemFact', x) for x in materials['problemFacts']],
               *[('runEvidence', x) for x in materials['runEvidence']]]
    sources.append(('code', {**materials['code'], 'ref': materials['code']['snapshotRef']}))
    for kind, value in sources:
        source_refs.append(value['ref'])
        records.append({'ref': value['ref'], 'goalContext': goal, 'kind': kind,
            'version': value.get('codeSnapshotRef', value.get('snapshotRef', 'caller-snapshot-1')),
            'content': value, 'contentHash': hash_content(value)})
    analysis_budget = options.pop('budget', DEFAULT_BUDGET)
    review_budget = options.pop('reviewBudget', DEFAULT_REVIEW_BUDGET)
    summary_budget = options.pop('summaryBudget', DEFAULT_REVIEW_BUDGET)
    if any(not isinstance(b, dict) for b in (analysis_budget, review_budget, summary_budget)):
        return {'status': 'failed', 'failure': {'code': 'INVALID_BUDGET'}}
    public_config = getattr(provider, 'public_config', {})
    maximum_output = public_config.get('maxTokens') if isinstance(public_config, dict) else None
    if type(maximum_output) is int and any(b.get('reservedOutputTokens', 0) < maximum_output
            for b in (analysis_budget, review_budget, summary_budget)):
        return {'status': 'failed', 'failure': {'code': 'OUTPUT_RESERVE_TOO_SMALL'}}
    request = {'goalContext': goal, 'purpose': 'skillReview' if review else 'skillExecution',
        'policyVersion': 't056-skill-v1', 'systemPrompt': prompt, 'mandatoryPayload': mandatory,
        'dialogueRefs': [m['ref'] for m in dialogue], 'sourceRefs': source_refs,
        'budget': review_budget if review else analysis_budget, 'summaryBudget': summary_budget, **options}
    return build_context(request, MapContextResolver(records), provider=provider,
                         deadline=deadline, clock=clock)
