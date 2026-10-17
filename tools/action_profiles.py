"""Explicit developer choice; experimental quality does not replace the default."""
from focusmate.core.action_execution import ActionExecutor
from focusmate.core.action_quality import FocusedActionExecutor, VERSION
from focusmate.adapters.action_review import DeepSeekActionReviewProvider


def action_profile(name):
    if name=='baseline':
        return ActionExecutor,None,{'profile':name,'experimental':False,'review':'sameProviderAsActor'}
    if name!='quality-candidate':raise ValueError('UNKNOWN_ACTION_PROFILE')
    reviewer=DeepSeekActionReviewProvider.from_environment(thinking=True)
    return FocusedActionExecutor,reviewer,{'profile':name,'experimental':True,
        'promptVersion':VERSION,'review':reviewer.public_config}
