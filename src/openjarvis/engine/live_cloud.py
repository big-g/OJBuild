"""Resolve cloud credentials per request without changing shared engine wrappers."""

from openjarvis.engine._base import InferenceEngine


class LiveCloudEngine(InferenceEngine):
    engine_id = "cloud"
    is_cloud = True

    @staticmethod
    def _engine(model=None):
        from openjarvis.core.cloud_providers import resolve_keys
        from openjarvis.engine.cloud import CloudEngine

        keys = resolve_keys()
        if model is not None:
            from openjarvis.engine.cloud import (
                _is_anthropic_model,
                _is_codex_model,
                _is_deepseek_model,
                _is_google_model,
                _is_minimax_model,
                _is_openrouter_model,
            )

            predicates = (
                (_is_codex_model, "OPENAI_CODEX_API_KEY"),
                (_is_openrouter_model, "OPENROUTER_API_KEY"),
                (_is_minimax_model, "MINIMAX_API_KEY"),
                (_is_deepseek_model, "DEEPSEEK_API_KEY"),
                (_is_anthropic_model, "ANTHROPIC_API_KEY"),
                (_is_google_model, "GEMINI_API_KEY"),
            )
            name = next(
                (key for matches, key in predicates if matches(model)), "OPENAI_API_KEY"
            )
            keys = {name: keys.get(name, "")}
        return CloudEngine(keys=keys)

    def generate(self, messages, *, model, **kwargs):
        engine = self._engine(model)
        try:
            return engine.generate(messages, model=model, **kwargs)
        finally:
            engine.close()

    async def stream(self, messages, *, model, **kwargs):
        engine = self._engine(model)
        try:
            async for item in engine.stream(messages, model=model, **kwargs):
                yield item
        finally:
            engine.close()

    async def stream_full(self, messages, *, model, **kwargs):
        engine = self._engine(model)
        try:
            async for item in engine.stream_full(messages, model=model, **kwargs):
                yield item
        finally:
            engine.close()

    def list_models(self):
        from openjarvis.core.cloud_providers import resolve_keys
        from openjarvis.engine import cloud

        keys = resolve_keys()
        catalogs = (
            ("OPENAI_API_KEY", cloud._OPENAI_MODELS),
            ("ANTHROPIC_API_KEY", cloud._ANTHROPIC_MODELS),
            ("GEMINI_API_KEY", cloud._GOOGLE_MODELS),
            ("OPENROUTER_API_KEY", cloud._OPENROUTER_POPULAR),
            ("MINIMAX_API_KEY", cloud._MINIMAX_MODELS),
            ("DEEPSEEK_API_KEY", cloud._DEEPSEEK_MODELS),
            ("OPENAI_CODEX_API_KEY", cloud._CODEX_MODELS),
        )
        return [model for key, models in catalogs if keys.get(key) for model in models]

    def health(self):
        return bool(self.list_models())

    def can_serve(self, model):
        engine = self._engine(model)
        try:
            return engine.can_serve(model)
        finally:
            engine.close()
