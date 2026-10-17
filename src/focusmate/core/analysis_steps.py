"""A single structured model request shared by independent and Manager drivers."""
import time
from focusmate.contracts.skill_execution import ProviderResponse, ProviderError


def request_analysis_step(provider, *, system_prompt, user_prompt, deadline, clock=time.monotonic):
    if clock()>=deadline:raise ProviderError('DEADLINE_EXCEEDED')
    response=provider.complete(system_prompt=system_prompt,user_prompt=user_prompt,deadline=deadline)
    if not isinstance(response,ProviderResponse) or not isinstance(response.content,str) or not isinstance(response.model,str) or not response.model.strip():
        raise ProviderError('INVALID_PROVIDER_RESPONSE')
    return response
