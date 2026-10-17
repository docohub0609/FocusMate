"""Join bounded interpretation to the deterministic ledger; no business writes."""
from copy import deepcopy
from jsonschema.exceptions import ValidationError
from focusmate.core.adaptation_interpretation import interpret_effect, digest
from focusmate.core.session_adaptation import SessionAdaptationLedger
from focusmate.contracts.schema_validation import validate


def _source(episode, ref, kind, **fields):
    return {'sourceId': ref, 'kind': kind, 'sessionId': episode['sessionId'],
            'goalContext': deepcopy(episode['goalContext']), **fields}


def _signal(step, interpretation, episode):
    request = step['request']; value = interpretation['interpretation']
    event_id = step['eventId']; source_id = event_id + ':source'
    signal = {'eventId':event_id, 'episodeId':episode['episodeId'], 'kind':request['kind'],
              'status':value['classification'], 'sourceRef':source_id,
              'evidenceRefs':[source_id], 'reason':value['reason']}
    if 'supersedes' in step:
        signal['supersedes'] = step['supersedes']
    if request['kind'] == 'feedback':
        sources = [_source(episode, source_id, 'feedback', actionId=episode['actionId'], status=value['classification'])]
    else:
        verifier = request['verifierResult']
        # These are model-attributed provenance links, not newly executed checks.
        code_ref = verifier['inputVersionRefs'].get('codeSnapshotRef', episode['codeSnapshotRef'])
        cited = {e['ref'] for e in value['evidence']}
        refs = [r for r in verifier['evidenceRefs'] if r in cited]
        process_ids = [event_id + ':evidence:' + str(i) for i in range(len(refs))]
        sources = [_source(episode, ref, 'process', actionId=episode['actionId'],
            invocationId=episode['invocationId'], codeSnapshotRef=code_ref,
            goalContext=deepcopy(verifier['goalContext'])) for ref in process_ids]
        sources.append(_source(episode, source_id, 'verifier', actionId=episode['actionId'],
            invocationId=episode['invocationId'], codeSnapshotRef=code_ref,
            status=verifier['status'], processEvidenceRefs=process_ids,
            goalContext=deepcopy(verifier['goalContext'])))
        signal['verifierRef'] = source_id
        if value['classification'] == 'supports':
            signal['evidenceRefs'] = process_ids
    return signal, sources


def run_adaptation(bundle, provider, *, previous=None, audit_sink=None):
    """Trusted local envelope; user natural language exists only inside materials.

    External callers must obtain episode/verification records from their own store,
    not accept a user's submitted JSON as an authoritative verifier result.
    """
    if (set(bundle) != {'schemaVersion','simulatedMaterials','episodes','steps'}
            or bundle['schemaVersion'] != '1.0.0' or type(bundle['simulatedMaterials']) is not bool):
        raise ValueError('invalid envelope')
    previous = deepcopy(previous)
    if previous and previous['simulatedMaterials'] != bundle['simulatedMaterials']:
        raise ValueError('cannot mix simulated and actual provenance')
    ledger = SessionAdaptationLedger(previous['ledger'] if previous else None)
    episode_inputs = deepcopy(previous.get('episodeInputs', [])) if previous else []
    originals = {r['episode']['episodeId']: r for r in episode_inputs}
    for record in bundle['episodes']:
        if set(record) != {'episode','sources','action'}:
            raise ValueError('invalid help record')
        e, a = record['episode'], record['action']
        if any(a.get(key) != e[key] for key in ('actionId','goalContext','codeSnapshotRef')):
            raise ValueError('action identity mismatch')
        if e['episodeId'] in originals and originals[e['episodeId']] != record:
            raise ValueError('original help changed')
        ledger.register_episode(e, sources=record['sources'])
        if e['episodeId'] not in originals:
            originals[e['episodeId']] = deepcopy(record); episode_inputs.append(deepcopy(record))
    rows = deepcopy(previous.get('steps', [])) if previous else []
    processed = {r['eventId']: r for r in rows if r.get('inputHash') and r['status'] != 'duplicate'
                 and r.get('reason') != 'EVENT_ID_CONFLICT'}
    for position,step in enumerate(bundle['steps']):
        if (not isinstance(step,dict) or not {'eventId','request'} <= step.keys()
                or set(step) - {'eventId','request','codeSources','supersedes'}
                or not isinstance(step['eventId'],str) or not step['eventId'].strip()
                or not isinstance(step['request'],dict)):
            row={'eventId':step.get('eventId') if isinstance(step,dict) else None,
                 'position':position,'status':'rejected','reason':'INVALID_STEP'}
            rows.append(row)
            if audit_sink:audit_sink(row)
            continue
        key = step['eventId']; fingerprint = digest(step)
        row = {'eventId':key,'inputHash':fingerprint,'status':'rejected'}
        if key in processed:
            row['status'] = 'duplicate' if processed[key]['inputHash'] == fingerprint else 'rejected'
            row['reason'] = 'ALREADY_RECORDED' if row['status']=='duplicate' else 'EVENT_ID_CONFLICT'
            rows.append(row)
            if audit_sink: audit_sink(row)
            continue
        try:
            known_codes = {s['sourceId']:s for s in ledger.snapshot()['sources'] if s['kind']=='code'}
            for source in step.get('codeSources',[]):
                validate('SessionAdaptationSource',source)
                if source['kind']!='code' or (source['sourceId'] in known_codes and known_codes[source['sourceId']]!=source):
                    raise ValueError('invalid code source')
                known_codes[source['sourceId']]=source
            candidates = {}
            for action in step['request']['actions']:
                matching = [r for r in originals.values() if r['action']['actionId'] == action['actionId']
                            and r['action']['goalContext'] == action['goalContext']]
                if len(matching) != 1:
                    raise ValueError('unknown or ambiguous action')
                record = matching[0]
                expected = deepcopy(record['action'])
                # A later code version needs a checked ancestry chain in codeSources.
                expected['codeSnapshotRef'] = action['codeSnapshotRef']
                if action != expected:
                    raise ValueError('changed original action')
                code_ref=action['codeSnapshotRef']; seen=set()
                while code_ref not in seen:
                    seen.add(code_ref); code=known_codes.get(code_ref)
                    if not code or any(code[k]!=record['episode'][k] for k in ('sessionId','goalContext')):
                        raise ValueError('unlinked code revision')
                    if code_ref==record['episode']['codeSnapshotRef']:
                        break
                    code_ref=code.get('previousCodeSnapshotRef')
                else:
                    raise ValueError('cyclic code revision')
                candidates[action['actionId']] = record['episode']
            sessions = {e['sessionId'] for e in candidates.values()}
            if len(sessions) != 1:
                raise ValueError('mixed sessions')
            report = interpret_effect(step['request'], provider)
            row['interpretationReport'] = report
            if report['status'] != 'interpreted':
                row['reason'] = 'INTERPRETATION_FAILED'
            elif report['interpretation']['actionId'] is None:
                row['status']='recorded'; row['reason']='UNCERTAIN_TARGET_NO_ADJUSTMENT'
            else:
                episode = candidates[report['interpretation']['actionId']]
                signal, sources = _signal(step, report, episode)
                before = ledger.snapshot()
                ledger.record_signal(signal, sources=[*step.get('codeSources',[]), *sources])
                row.update(status='recorded', signal=signal,
                    beforeContribution=before['contributions'][episode['episodeId']],
                    afterContribution=ledger.contribution(episode['episodeId']))
        except (ValueError, KeyError, TypeError, ValidationError):
            row['reason']='INVALID_LINK_OR_INPUT'
        rows.append(row); processed[key]=row
        if audit_sink: audit_sink(row)
    return {'schemaVersion':'1.0.0','simulatedMaterials':bundle['simulatedMaterials'],
            'episodeInputs':episode_inputs,'steps':rows,'ledger':ledger.snapshot()}
