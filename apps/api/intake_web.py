"""Read-only presentation endpoints used by the problem intake UI."""
import re
from urllib.parse import parse_qs, urlsplit
from .feature_presentation import public_report
from .problem_evidence import build_evidence_catalog
from .problem_storage import StorageFailure


def get_operation(handler):
    url = urlsplit(handler.path)
    if url.path == '/api/v1/problems':
        def listing():
            query = parse_qs(url.query)
            try:
                if set(query) - {'limit', 'cursor', 'deleted'} or any(len(v) != 1 for v in query.values()):
                    raise ValueError()
                if query.get('deleted', ['false'])[0] not in {'true', 'false'}:
                    raise ValueError()
                limit = int(query.get('limit', ['20'])[0])
            except ValueError:
                raise StorageFailure('INVALID_LIST_REQUEST', '题目列表参数无效。', 422, []) from None
            return handler.problem_repository.list_problems(limit=limit, cursor=query.get('cursor', [None])[0],
                                                           deleted=query.get('deleted', ['false'])[0] == 'true')
        return listing
    match = re.fullmatch(r'/api/v1/feature-extraction-jobs/([^/]+)', url.path)
    if match:
        return lambda: handler.feature_jobs.get(match[1])
    match = re.fullmatch(r'/api/v1/problems/([^/]+)/versions/(\d+)/(evidence|features/confirmed|features/display|feature-extraction)', url.path)
    if not match:
        return None
    problem_id, version, action = match[1], int(match[2]), match[3]
    def read():
        repo = handler.problem_repository
        record = repo.get(problem_id, version)
        if action == 'evidence':
            return {'problemRef': {'problemId': problem_id, 'problemVersion': version},
                    'contentHash': record['lifecycle']['contentHash'], 'draftRevision': record['lifecycle']['draftRevision'],
                    'items': build_evidence_catalog(record['problemPackage'])}
        if action == 'feature-extraction':
            return handler.feature_jobs.current(problem_id, version)
        if action == 'features/confirmed':
            return repo.confirmed_feature_set(problem_id, version)
        report = (repo.confirmed_feature_set(problem_id, version) if record['lifecycle']['status'] == 'frozen'
                  else repo.current_feature_set(problem_id, version))
        return public_report(report)
    return read
