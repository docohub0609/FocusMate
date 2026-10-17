import json
from copy import deepcopy
import pytest
from apps.api.problem_features import _parse_candidates, extract_problem_features, FeatureExtractionFailure
from tests.python.test_problem_features import FakeProvider,base_features,problem_request

def test_fixed_missing_labels_are_completed_without_changing_values():
 f=base_features(); f[0]['value']={'value':'n','maximum':100};f[0]['presentation']={'title':'规模','labels':{'/value':'规模'}}
 r=_parse_candidates(json.dumps({'features':f}))
 assert r['features'][0]['presentation']['labels']['/value/maximum']=='最大值'
 assert r['features'][0]['value']==f[0]['value']

def test_repair_receives_previous_candidate_and_specific_constraint():
 bad={'features':base_features()};bad['features'][0]['featureKey']='size.a_i'
 p=FakeProvider([bad,{'features':base_features()}])
 extract_problem_features(problem_request(),p)
 assert 'size.a_i' in p.calls[1]
 assert '^[a-z]' in p.calls[1]

def test_arbitrary_missing_label_is_not_guessed():
 f=base_features();f[0]['value']={'mystery':100};f[0]['presentation']={'title':'规模','labels':{'/value':'规模'}}
 with pytest.raises(FeatureExtractionFailure):_parse_candidates(json.dumps({'features':f}))


def test_failure_details_survive_restart(tmp_path):
 from tests.python.test_t080_jobs import setup_job
 from apps.api.feature_jobs import FeatureJobs
 repo,record,jobs,request=setup_job(tmp_path)
 job=jobs.enqueue(request,'detail-test','owner');claim=jobs.claim('owner')
 details=[{'field':'features.0.presentation','message':'中文名称不完整'}]
 jobs.fail(claim,'LLM_OUTPUT_INVALID',details)
 assert FeatureJobs(repo).get(job['jobId'])['failureDetails']==details
 assert FeatureJobs(repo).get(job['jobId'])['status']=='failed'

def test_model_instructions_include_schema_and_full_vocabulary():
 from apps.api.deepseek_provider import _user_prompt
 from apps.api.feature_vocabulary import ALLOWED_DOMAIN_TAGS
 text=_user_prompt(problem_request()['problemPackage'],[],None)
 assert '^[a-z][A-Za-z0-9.]*$' in text
 assert all(tag in text for tag in ALLOWED_DOMAIN_TAGS)
