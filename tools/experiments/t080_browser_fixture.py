"""Fake model server for browser failure/recovery checks, never real acceptance."""
import json
import os
import time
from apps.api.__main__ import create_server
from apps.api.deepseek_provider import ProviderResponse
from apps.api.feature_presentation import with_legacy_labels
from tests.python.test_problem_features import FakeProvider, base_features


class BrowserProvider:
    review = FakeProvider.review

    def extract(self, problem, *, evidence_catalog, repair_reason=None):
        time.sleep(0.6)
        features = [with_legacy_labels(f) for f in base_features()]
        for f in features:
            f['reason'] = '题目未说明这一限制。' if f['status'] == 'unknown' else '请对照所选原文核对这一条件。'
        result = {'features': features}
        if os.environ.get('REGRESSION_CASE') == 'io-proposal' and problem['inputSpec'] == '输入一个数组。':
            result['ioProposal'] = {
                'reason': '数组的长度和分隔方式需要明确。',
                'inputSpec': '第一行 n，第二行 n 个整数，以空格分隔。',
                'outputSpec': '输出两个整数，以单个空格分隔，不含文字前缀。',
                'examples': [{'input': '3\n1 2 3\n', 'output': '1 2\n'}],
                'evidenceRefs': ['inputSpec:0', 'outputSpec:0'],
            }
        return ProviderResponse('browser-fake', 'fake-only', json.dumps(result))


if __name__ == '__main__':
    server = create_server(port=int(os.environ['TEST_API_PORT']), feature_provider_factory=BrowserProvider)
    try:
        server.serve_forever()
    finally:
        server.server_close()
