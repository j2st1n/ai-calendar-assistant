from app.ai.schemas import ExtractionResult, Intent


class EventExtractor:
    async def extract(self, text: str) -> ExtractionResult:
        return ExtractionResult(intent=Intent.no_event, missing_fields=["implementation_pending"], confidence=0.0)
