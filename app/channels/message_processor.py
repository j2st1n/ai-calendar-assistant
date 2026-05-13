class MessageProcessor:
    async def process(self, text: str) -> str:
        return "未识别到日程信息，请补充时间和事件内容。"
