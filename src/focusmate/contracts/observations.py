"""Deterministic event organization rules, independent of storage and models."""
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import unified_diff
from hashlib import sha256
import json
import math

from .schema_validation import validate


KINDS = frozenset(('codeSnapshotCreated', 'runRequested', 'submissionRequested',
    'ojResultProduced', 'helpRequested', 'actionFeedbackProvided', 'sessionPaused',
    'sessionResumed', 'publicRunFinished', 'runFinished'))
BOUNDARIES = frozenset(('runRequested', 'submissionRequested', 'helpRequested', 'sessionPaused'))


@dataclass(frozen=True)
class OrganizationPolicy:
    quiet_seconds: float = 2
    max_seconds: float = 30
    diff_max_bytes: int = 8192
    rule_version: str = 't061-v1'

    def __post_init__(self):
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
               for v in (self.quiet_seconds, self.max_seconds)):
            raise ValueError('window durations must be positive finite seconds')
        if self.max_seconds < self.quiet_seconds:
            raise ValueError('maximum window cannot be shorter than quiet period')
        if type(self.diff_max_bytes) is not int or not 1 <= self.diff_max_bytes <= 65536 or not self.rule_version:
            raise ValueError('invalid diff budget or rule version')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def epoch(value):
    date = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(date, datetime) or date.tzinfo is None:
        raise ValueError('time must be timezone aware')
    return date.timestamp()


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def scope_for(event, problem):
    return {'goalContext': deepcopy(event['goalContext']), 'problemRef': deepcopy(problem),
            'contractVersion': event.get('contractVersion'), 'sessionRef': event.get('sessionRef')}


def read_observation(value):
    validate('ObservationV2' if value.get('schemaVersion') == '2.0.0' else 'Observation', value)
    return deepcopy(value)


def _observation(scope, source_ids, kind, trigger, when, policy, evidence, uncertainty, delta):
    result = {'observationId': 'obs-' + digest([scope, source_ids, kind]), 'schemaVersion': '2.0.0',
              'createdAt': timestamp(when), 'goalContext': deepcopy(scope['goalContext']),
              'sourceEventIds': list(source_ids), 'kind': kind, 'trigger': trigger,
              'priority': 'observationReady', 'evidence': evidence, 'uncertainty': uncertainty,
              'delta': delta, 'organization': {'scope': deepcopy(scope), 'policy': deepcopy(policy)}}
    return read_observation(result)


def _finish(pending, when, trigger, snapshot):
    goal = pending['scope']['goalContext']['goalId']
    start = snapshot(goal, pending['startVersion']) if pending['startVersion'] is not None else None
    end = snapshot(goal, pending['endVersion'])
    uncertainty = []
    refs = [s['ref'] for s in (start, end) if s]
    delta = {'changed': None, 'diff': '', 'truncated': False}
    if start is None:
        uncertainty.append('BASELINE_UNAVAILABLE')
    if end is None:
        uncertainty.append('END_SNAPSHOT_UNAVAILABLE')
    if start and end:
        delta['changed'] = start['source'] != end['source']
        budget = pending['policy']['diff_max_bytes']
        # Bound display while retaining references to both immutable complete sources.
        pieces = []
        used = 0
        for line in unified_diff(start['source'].splitlines(keepends=True), end['source'].splitlines(keepends=True),
                                 fromfile=f"v{pending['startVersion']}", tofile=f"v{pending['endVersion']}"):
            encoded = line.encode('utf-8')
            if used + len(encoded) > budget:
                pieces.append(encoded[:budget - used].decode('utf-8', errors='ignore'))
                delta['truncated'] = True
                break
            pieces.append(line); used += len(encoded)
        delta['diff'] = ''.join(pieces)
    evidence = {'startVersion': pending['startVersion'], 'endVersion': pending['endVersion'],
                'codeRefs': refs, 'firstReceivedAt': timestamp(pending['started']),
                'lastReceivedAt': timestamp(pending['last']), 'eventTimes': pending['eventTimes']}
    return _observation(pending['scope'], pending['sourceEventIds'], 'codeChange', trigger,
                        when, pending['policy'], evidence, uncertainty, delta)


def expire(pendings, until, snapshot):
    """Return new state and immutable observations; equality is an expired window."""
    pendings = deepcopy(pendings)
    observations = []
    for key, pending in sorted(list(pendings.items()), key=lambda item: (item[1]['deadline'], item[0])):
        if pending['deadline'] <= until:
            maximum = pending['started'] + pending['policy']['max_seconds']
            trigger = 'maximumWindow' if maximum <= pending['last'] + pending['policy']['quiet_seconds'] else 'quietPeriod'
            observations.append(_finish(pending, pending['deadline'], trigger, snapshot))
            del pendings[key]
    return pendings, observations


def organize(pendings, event, scope, received, policy, snapshot):
    """Apply one validated event after expiry, returning pending state and outputs."""
    pendings = deepcopy(pendings)
    observations = []
    name = event['eventName']; payload = event['payload']; key = digest(scope)
    if name == 'codeSnapshotCreated':
        version = payload['codeVersion']
        if key in pendings and version != pendings[key]['endVersion'] + 1:
            # A delayed/old save is still a fact, but cannot move an active range backwards.
            observations.append(_finish(pendings.pop(key), received, 'versionDiscontinuity', snapshot))
        if key not in pendings:
            prior = version - 1 if version > 1 else None
            if prior is not None and snapshot(scope['goalContext']['goalId'], prior) is None:
                prior = None
            pendings[key] = {'scope': deepcopy(scope), 'startVersion': prior,
                'endVersion': version, 'sourceEventIds': [], 'eventTimes': [],
                'started': received, 'last': received, 'policy': asdict(policy)}
        pending = pendings[key]
        pending['endVersion'] = version
        pending['last'] = received
        pending['sourceEventIds'].append(event['eventId'])
        pending['eventTimes'].append({'eventId': event['eventId'], 'createdAt': event['createdAt']})
        pending['deadline'] = min(pending['started'] + pending['policy']['max_seconds'],
                                  received + pending['policy']['quiet_seconds'])
        return pendings, observations
    if name in BOUNDARIES and key in pendings:
        observations.append(_finish(pendings.pop(key), received, name, snapshot))
    # Only schema-validated event facts are copied, never arbitrary source documents.
    evidence = deepcopy(payload)
    evidence['occurredAt'] = event['createdAt']
    evidence['receivedAt'] = timestamp(received)
    evidence['source'] = event['source']
    evidence['actor'] = event.get('actor', 'legacyUnspecified')
    uncertainty = []
    if name == 'helpRequested' and 'codeVersion' not in payload:
        uncertainty.append('HELP_CODE_UNSPECIFIED')
    if name == 'runRequested' and 'runId' not in payload:
        uncertainty.append('RUN_ASSOCIATION_UNAVAILABLE')
    if name in ('publicRunFinished', 'runFinished', 'ojResultProduced'):
        uncertainty.append('RESULT_COMPARISON_UNAVAILABLE')
    observations.append(_observation(scope, [event['eventId']], name, name, received,
        asdict(policy), evidence, uncertainty, {'newFact': True}))
    return pendings, observations
