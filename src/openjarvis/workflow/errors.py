"""Safe actionable generation failures suitable for job history and chat."""


class GenerationError(ValueError):
    def __init__(self, message, category, recovery):
        super().__init__(message)
        self.category, self.recovery = category, recovery

    def public_details(self):
        return {"category": self.category, "recovery": self.recovery}
