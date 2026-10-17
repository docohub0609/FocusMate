import json
import pytest
from services.verifier_builder.cross_check import same_generated_cases,CrossCheckFailure
from tests.python.test_verifier_cross_check import setup,FakeRunner,run,result,cases


def test_json_layout_does_not_change_generated_inputs(tmp_path,setup):
    def change(role,request,runner):
        if role=='generator' and request.operation=='execute':
            return result(json.dumps({'cases':cases(True)},indent=len(runner.calls)%3,sort_keys=True).encode())
    material,_=run(tmp_path,setup,FakeRunner(setup[0],change=change))
    assert material.cases


@pytest.mark.parametrize('second',[
    b'{"cases":[{"stdin":" a\\n"}]}',
    b'{"cases":[{"stdin":"b\\n"}]}',
    b'{"cases":[]}',
])
def test_input_whitespace_values_and_count_are_not_normalized(second):
    assert not same_generated_cases(b'{"cases":[{"stdin":"a\\n"}]}',second)


def test_reordered_cases_and_duplicate_json_keys_still_fail():
    assert not same_generated_cases(b'{"cases":[1,2]}',b'{"cases":[2,1]}')
    with pytest.raises(CrossCheckFailure):same_generated_cases(b'{"cases":[],"cases":[]}',b'{"cases":[]}')
