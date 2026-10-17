"""Three controlled T072 variants using the same durable runtime and limits."""
from focusmate.core.action_execution import ActionExecutor
from action_revision_prompts import ACTOR_PROMPT,REVIEW_PROMPT


def configurations(old_actor,old_reviewer):
    return [type(name,(ActionExecutor,),{'actor_prompt':actor,'review_prompt':review,
                'track_review_issues':track})
            for name,actor,review,track in (
                ('baseline',old_actor,old_reviewer,False),
                ('promptOnly',ACTOR_PROMPT,REVIEW_PROMPT,False),
                ('promptAndIssues',ACTOR_PROMPT,REVIEW_PROMPT,True))]
