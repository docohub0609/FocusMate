from dataclasses import replace
import pytest
from services.verifier_builder.candidates import CandidateFailure, MutantCandidate
from services.verifier_builder.cross_check import CrossCheckFailure, VerifierCrossChecker
from tests.python.test_verifier_cross_check import setup, FakeRunner, result

class Mutations:
    def __init__(self, batches, decision='legal'):
        self.batches, self.calls, self.decision = batches, [], decision
        self.last_witness_reviews = ()
    def generate(self, record, features, deadline, feedback=None):
        self.calls.append(feedback)
        batch = self.batches[min(len(self.calls)-1, len(self.batches)-1)]
        if isinstance(batch, Exception): raise batch
        return batch
    def assess_witnesses(self, record, features, mutants, deadline):
        return {m.mutant_key:self.decision for m in mutants}

def mutant(source='# SECRET-mutant', witness='100\n'):
    return MutantCandidate('m1','offByOne',source,witness,'wrong value','within bounds')

def execute(tmp_path, setup, provider, runner=None):
    checker = VerifierCrossChecker(runner or FakeRunner(setup[0]),tmp_path/'work',monotonic=lambda:10,mutation_provider=provider)
    material = checker.precheck(*setup,100)
    return checker, material

def test_repair_uses_public_feedback_once_and_retains_valid_base(tmp_path,setup):
    provider=Mutations([(mutant('# SECRET-bad'),),(mutant(),)])
    def change(role,req,runner):
        if role=='bad' and req.operation=='execute': return result(b'bad')
    checker, material=execute(tmp_path,setup,provider,FakeRunner(setup[0],change=change))
    assert material.assurance=='full' and material.mutation_repair_count==1
    assert len(provider.calls)==2
    assert provider.calls[1]['publicFailures'][0]['status']=='publicMismatch'
    assert '100\\n' not in str(provider.calls[1]['publicFailures'])
    assert checker.latest_mutants==provider.batches[1]
    assert len(checker.mutation_audit)==2

def test_no_qualified_challenge_after_one_repair_is_limited(tmp_path,setup):
    provider=Mutations([()])
    _, material=execute(tmp_path,setup,provider)
    assert len(provider.calls)==2
    assert material.assurance=='limited'
    assert material.warning_codes==('MUTATION_CHALLENGE_UNAVAILABLE',)
    assert material.mutation_eligible==material.mutation_killed==0
    assert material.mutation_repair_count==1
    assert material.agreement_count==8

def test_known_wrong_survivor_never_downgrades(tmp_path,setup):
    provider=Mutations([(mutant(witness='777\n'),)])
    def change(role,req,runner):
        if role=='mutant' and req.operation=='execute' and req.stdin!=b'777\n' and req.stdin not in runner.public: return result(req.stdin)
    with pytest.raises(CrossCheckFailure) as exc: execute(tmp_path,setup,provider,FakeRunner(setup[0],change=change))
    assert exc.value.code=='MUTATION_TEST_GAP'
    assert len(provider.calls)==1

def test_unknown_witness_never_counts_as_verified_error(tmp_path,setup):
    _,material=execute(tmp_path,setup,Mutations([(mutant(),)],decision='unknown'))
    assert material.assurance=='limited'

def test_provider_failure_is_not_limited_success(tmp_path,setup):
    with pytest.raises(CrossCheckFailure) as exc: execute(tmp_path,setup,Mutations([CandidateFailure('LLM_TEMPORARILY_UNAVAILABLE','unavailable',True)]))
    assert exc.value.code=='LLM_TEMPORARILY_UNAVAILABLE'


def test_mutation_schema_failure_retains_base_failure_category(tmp_path,setup):
    with pytest.raises(CrossCheckFailure) as exc:
        execute(tmp_path,setup,Mutations([CandidateFailure("CANDIDATE_SCHEMA_INVALID","invalid",True)]))
    assert exc.value.code == "MUTATION_RESPONSE_INVALID"
