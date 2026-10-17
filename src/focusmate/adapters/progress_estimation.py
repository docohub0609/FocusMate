"""Transport only; the Core controls evidence and request count."""
from focusmate.adapters.stage_recognition import DeepSeekStageRecognitionProvider


class DeepSeekProgressEstimationProvider(DeepSeekStageRecognitionProvider):
    pass
