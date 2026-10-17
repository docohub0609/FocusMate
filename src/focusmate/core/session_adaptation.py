"""Deterministic session-local preference ledger; callers own trust and persistence.

Source links prove identity and scope, not natural-language truth. Only a trusted
caller or independently reviewed parser may normalize signals for this reducer.
No completion state or global Skill edits are produced here.
"""
from copy import deepcopy
import json

from focusmate.contracts.schema_validation import validate


def _identity(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _scope(value):
    return {key: deepcopy(value[key]) for key in ('sessionId', 'goalContext', 'skillRef', 'actionType', 'hintLevel')} | {
        'sectionIds': sorted(set(value['sectionIds']))}


class SessionAdaptationLedger:
    def __init__(self, snapshot=None):
        self._episodes = {}
        self._events = {}
        self._sources = {}
        self._trace = []
        if snapshot is not None:
            if snapshot.get('schemaVersion') != '1.0.0':
                raise ValueError('unsupported snapshot version')
            self._sources = self._merged_sources(snapshot['sources'])
            for episode in snapshot['episodes']:
                self.register_episode(episode, sources=[])
            for event in snapshot['events']:
                self.record_signal(event, sources=[])

    def _merged_sources(self, sources):
        merged = deepcopy(self._sources)
        for source in sources:
            validate('SessionAdaptationSource', source)
            ref = source['sourceId']
            if ref in merged and _identity(merged[ref]) != _identity(source):
                raise ValueError('source identity conflict: ' + ref)
            merged[ref] = deepcopy(source)
        return merged

    @staticmethod
    def _matching(source, episode, kind):
        return bool(source and source['kind'] == kind and all(
            source[key] == episode[key] for key in ('sessionId', 'goalContext')))

    def register_episode(self, help_record, *, sources):
        validate('SessionAdaptationEpisode', help_record)
        episode = deepcopy(help_record)
        ref = episode['episodeId']
        merged = self._merged_sources(sources)
        if ref in self._episodes:
            if _identity(self._episodes[ref]) != _identity(episode):
                raise ValueError('episode identity conflict: ' + ref)
            return False
        if any(existing['sessionId'] == episode['sessionId'] and existing['actionId'] == episode['actionId']
               for existing in self._episodes.values()):
            raise ValueError('one help episode per action')
        for source_ref, kind in [(episode['codeSnapshotRef'], 'code'), (episode['invocationId'], 'invocation'),
                                 (episode['actionId'], 'action')] + [(r, 'trigger') for r in episode['triggerRefs']]:
            if not self._matching(merged.get(source_ref), episode, kind):
                raise ValueError('missing or mismatched episode source: ' + source_ref)
        invocation = merged[episode['invocationId']]
        action = merged[episode['actionId']]
        if (invocation.get('skillRef') != episode['skillRef'] or
            sorted(set(invocation.get('sectionIds', []))) != sorted(set(episode['sectionIds'])) or
            invocation.get('codeSnapshotRef') != episode['codeSnapshotRef'] or
            any(action.get(key) != episode[key] for key in ('invocationId', 'actionType', 'hintLevel', 'codeSnapshotRef'))):
            raise ValueError('episode attribution differs from original invocation/action')
        self._sources, self._episodes[ref] = merged, episode
        return True

    def record_signal(self, signal, *, sources):
        validate('SessionAdaptationSignal', signal)
        event = deepcopy(signal)
        merged = self._merged_sources(sources)
        ref = event['eventId']
        if ref in self._events:
            if _identity(self._events[ref]) != _identity(event):
                raise ValueError('event identity conflict: ' + ref)
            return False
        if event['episodeId'] not in self._episodes:
            raise ValueError('unknown episode')
        # Require the complete referenced provenance closure now. This prevents
        # adding a source later from silently changing an earlier neutral event.
        pending = [event['sourceRef'], *event['evidenceRefs']]
        if 'verifierRef' in event:
            pending.append(event['verifierRef'])
        visited = set()
        while pending:
            source_ref = pending.pop()
            if source_ref in visited:
                continue
            visited.add(source_ref)
            source = merged.get(source_ref)
            if source is None:
                raise ValueError('missing signal source: ' + source_ref)
            pending.extend(source.get('processEvidenceRefs', []))
            pending.extend(source[key] for key in ('codeSnapshotRef', 'previousCodeSnapshotRef') if key in source)
        previous = event.get('supersedes')
        if previous is not None:
            target = self._events.get(previous)
            if not target or any(target[key] != event[key] for key in ('episodeId', 'kind')):
                raise ValueError('correction must replace an earlier event for the same target and kind')
            if any(item.get('supersedes') == previous for item in self._events.values()):
                raise ValueError('conflicting correction')
            source = merged[event['sourceRef']]
            episode = self._episodes[event['episodeId']]
            source_kind = 'feedback' if event['kind'] == 'feedback' else 'verifier'
            if (not self._matching(source, episode, source_kind) or source.get('actionId') != episode['actionId'] or
                (event['kind'] == 'feedback' and (source.get('status') != event['status'] or
                                                event['sourceRef'] not in event['evidenceRefs'])) or
                (event['kind'] == 'verification' and source.get('invocationId') != episode['invocationId'])):
                raise ValueError('correction source is not linked to the same target')
        episode = self._episodes[event['episodeId']]
        before = self.contribution(event['episodeId'])
        before_adjustment = self.adjustment(episode)
        self._sources = merged
        self._events[ref] = event
        resolution = self._resolve(event, merged)
        ordinary_neutral = event['status'] not in ('notHelpful', 'supports')
        self._trace.append({'eventId': ref, 'episodeId': event['episodeId'],
                            'beforeContribution': before, 'afterContribution': self.contribution(event['episodeId']),
                            'beforeAdjustment': before_adjustment, 'afterAdjustment': self.adjustment(episode),
                            'resolution': resolution,
                            'reason': ('neutralStatus' if ordinary_neutral else 'sourceAttributionNotSupported')
                            if resolution == 'neutral' else 'validatedSourceLinks'})
        return True

    def _code_descends(self, ref, episode, sources):
        seen = set()
        while ref and ref not in seen:
            seen.add(ref)
            code = sources.get(ref)
            if not self._matching(code, episode, 'code'):
                return False
            if ref == episode['codeSnapshotRef']:
                return True
            ref = code.get('previousCodeSnapshotRef')
        return False

    def _resolve(self, event, sources):
        episode = self._episodes[event['episodeId']]
        source = sources.get(event['sourceRef'])
        if event['kind'] == 'feedback':
            if (self._matching(source, episode, 'feedback') and source.get('actionId') == episode['actionId'] and
                source.get('status') == event['status'] and event['sourceRef'] in event['evidenceRefs']):
                return 'negative' if event['status'] == 'notHelpful' else 'neutral'
            return 'neutral'
        if event['status'] != 'supports' or event.get('verifierRef') != event['sourceRef']:
            return 'neutral'
        if not self._matching(source, episode, 'verifier') or source.get('status') != 'passed':
            return 'neutral'
        if any(source.get(key) != episode[key] for key in ('actionId', 'invocationId')):
            return 'neutral'
        if not self._code_descends(source.get('codeSnapshotRef'), episode, sources):
            return 'neutral'
        refs = source.get('processEvidenceRefs', [])
        if not refs or not event['evidenceRefs'] or not set(event['evidenceRefs']).issubset(refs):
            return 'neutral'
        for ref in refs:
            process = sources.get(ref)
            if (not self._matching(process, episode, 'process') or
                any(process.get(key) != episode[key] for key in ('actionId', 'invocationId')) or
                process.get('codeSnapshotRef') != source.get('codeSnapshotRef')):
                return 'neutral'
        return 'support'

    def contribution(self, episode_id):
        if episode_id not in self._episodes:
            raise ValueError('unknown episode')
        replaced = {event['supersedes'] for event in self._events.values() if 'supersedes' in event}
        values = [self._resolve(event, self._sources) for ref, event in self._events.items()
                  if event['episodeId'] == episode_id and ref not in replaced]
        support, negative = 'support' in values, 'negative' in values
        return (1 if support else 0) - (1 if negative else 0)

    def _adjustments(self):
        totals = {}
        for ref, episode in self._episodes.items():
            combination = _scope(episode)
            key = _identity(combination)
            entry = totals.setdefault(key, {'combination': combination, 'rawSum': 0})
            entry['rawSum'] += self.contribution(ref)
        return [entry | {'adjustment': max(-2, min(2, entry['rawSum']))} for entry in totals.values()]

    def adjustment(self, combination):
        key = _identity(_scope(combination))
        return next((entry['adjustment'] for entry in self._adjustments()
                     if _identity(entry['combination']) == key), 0)

    def rank_applicable(self, candidates):
        return sorted(deepcopy(candidates), key=lambda candidate: -self.adjustment(candidate))

    def snapshot(self):
        return deepcopy({'schemaVersion': '1.0.0', 'episodes': list(self._episodes.values()),
                         'events': list(self._events.values()), 'sources': list(self._sources.values()),
                         'contributions': {ref: self.contribution(ref) for ref in self._episodes},
                         'adjustments': self._adjustments(), 'trace': self._trace})
