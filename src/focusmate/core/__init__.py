"""Frontend-independent Goal-Driven orchestration boundary."""

from .goal_template import GoalTemplateError, build_generic_algorithm_goal_contract

__all__ = ["GoalTemplateError", "build_generic_algorithm_goal_contract"]
