import json
import time
from urllib.request import Request, urlopen

from tests.python.test_problem_lifecycle_api import running_api, request
from tests.python.test_problem_features import INTAKE


def test_named_problem_delete_and_restore_endpoints(tmp_path):
    with running_api(tmp_path) as base:
        status, created = request(base, 'POST', '/api/v1/problems', {**INTAKE, 'title': '两数之和'})
        assert status == 201
        pid = created['problemPackage']['problemId']
        _, listing = request(base, 'GET', '/api/v1/problems')
        assert listing['items'][0]['title'] == '两数之和'
        for _ in range(2):
            status, result = request(base, 'DELETE', f'/api/v1/problems/{pid}')
            assert status == 200 and result['deleted'] is True
        assert request(base, 'GET', '/api/v1/problems')[1]['items'] == []
        assert request(base, 'GET', '/api/v1/problems?deleted=true')[1]['items'][0]['problemId'] == pid
        assert request(base, 'POST', f'/api/v1/problems/{pid}/restore', {})[0] == 200
        assert request(base, 'GET', '/api/v1/problems')[1]['items'][0]['problemId'] == pid
        assert request(base, 'GET', '/api/v1/problems?deleted=bad')[0] == 422
        assert request(base, 'DELETE', '/api/v1/problems/missing')[0] == 404
        assert request(base, 'POST', '/api/v1/problems', {**INTAKE, 'title': 'x' * 121})[0] == 422


def test_legacy_and_browser_share_one_active_extraction(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from apps.api.__main__ import create_server
    from apps.api.problem_storage import ProblemRepository
    from tests.python.test_problem_lifecycle_api import LifecycleProvider
    entered, release = threading.Event(), threading.Event()
    calls = []
    class Provider(LifecycleProvider):
        def extract(self, *args, **kwargs):
            calls.append(1); entered.set(); release.wait(3)
            return super().extract(*args, **kwargs)
    server = create_server(port=0, feature_provider_factory=Provider, problem_repository=ProblemRepository(tmp_path/'shared.db'))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        _, created = request(base,'POST','/api/v1/problems',INTAKE)
        pid = created['problemPackage']['problemId']
        payload = {'problemId':pid,'problemVersion':1,'expectedDraftRevision':1,'contentHash':created['lifecycle']['contentHash']}
        call = Request(base+'/api/v1/feature-extraction-jobs', data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Idempotency-Key':'browser'})
        with urlopen(call) as response: job=json.load(response)
        assert entered.wait(2)
        with ThreadPoolExecutor(1) as pool:
            old = pool.submit(request,base,'POST','/api/v1/problem-feature-extractions',{'problemRef':{'problemId':pid,'problemVersion':1}})
            time.sleep(.2); release.set()
            status, report = old.result()
        assert status == 201
        assert len(calls) == 1
        _, saved = request(base,'GET','/api/v1/feature-extraction-jobs/'+job['jobId'])
        assert saved['featureSetId'] == report['featureSetId']
    finally:
        release.set(); server.shutdown(); server.server_close(); thread.join(2)


def test_browser_endpoints_create_retry_parse_and_reload(tmp_path):
    with running_api(tmp_path) as base:
        def save():
            call = Request(base + '/api/v1/problems', data=json.dumps(INTAKE).encode(),
                           headers={'Content-Type': 'application/json', 'Idempotency-Key': 'browser-save'}, method='POST')
            with urlopen(call) as response:
                return json.load(response)
        created = save()
        assert save() == created
        status, listing = request(base, 'GET', '/api/v1/problems?limit=20')
        assert status == 200 and len(listing['items']) == 1
        problem_id = created['problemPackage']['problemId']
        prefix = f'/api/v1/problems/{problem_id}/versions/1'
        status, catalog = request(base, 'GET', prefix + '/evidence')
        assert status == 200 and any(item['evidenceId'] == 'constraints:0' for item in catalog['items'])
        body = {'problemId': problem_id, 'problemVersion': 1, 'expectedDraftRevision': 1,
                'contentHash': created['lifecycle']['contentHash']}
        call = Request(base + '/api/v1/feature-extraction-jobs', data=json.dumps(body).encode(),
                       headers={'Content-Type': 'application/json', 'Idempotency-Key': 'browser-parse'}, method='POST')
        with urlopen(call) as response:
            assert response.status == 202
            job = json.load(response)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            _, current = request(base, 'GET', prefix + '/feature-extraction')
            if current['status'] not in ('queued', 'running'):
                break
            time.sleep(.05)
        assert current['jobId'] == job['jobId'] and current['status'] == 'succeeded'
        status, view = request(base, 'GET', prefix + '/features/display')
        assert status == 200 and view['displayComplete'] is True
        assert view['report']['features'][0]['value']['maximum'] == 100000
