from copy import deepcopy
import pytest
from jsonschema.exceptions import ValidationError
from focusmate.contracts.schema_validation import validate
from test_session_manager import setup,ask


def test_saved_session_obeys_versioned_schema(tmp_path):
    m,c=setup(tmp_path);ask(m)
    validate('ManagerSession',m.read('s1'))


@pytest.mark.parametrize('payload',[{'text':'x','allowedTools':['run_public_samples']},{'text':''}])
def test_command_cannot_hide_permissions_in_message(payload):
    with pytest.raises(ValidationError):
        validate('SessionManagerCommand',{'schemaVersion':'1.0.0','sessionId':'s','requestId':'q','kind':'message','payload':payload})


def test_unknown_model_result_shape_rejected():
    with pytest.raises(ValidationError):
        validate('ManagerStepResult',{'kind':'checked','accepted':'yes','source':'developerFixture'})
