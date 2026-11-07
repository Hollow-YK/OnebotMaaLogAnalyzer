import unittest
from unittest.mock import AsyncMock, patch

from maa_analyzer.core.llm import AnthropicClient, ChatMessage, LLMClient
from maa_analyzer.core.models import ConfigLLM, LLMDefaults, ModelSpec


class OptionalSamplingTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_omit_sampling_parameters(self):
        for client_type, method in ((LLMClient, '_post_with_fallbacks'), (AnthropicClient, '_post_json')):
            with self.subTest(client=client_type.__name__):
                client = client_type(model='example', base_url='https://example.test')
                with patch.object(client, method, new_callable=AsyncMock, return_value={}) as post, \
                     patch.object(client, '_extract_message', return_value=ChatMessage(content='ok')):
                    await client.chat_messages([{'role': 'user', 'content': 'hello'}], model='example')
                payload = post.call_args.args[0]
                self.assertNotIn('temperature', payload)
                self.assertNotIn('top_p', payload)

    def test_legacy_sampling_settings_are_ignored(self):
        for model in (LLMDefaults, ConfigLLM, ModelSpec):
            with self.subTest(model=model):
                settings = model.model_validate({'temperature': 0.2, 'top_p': 0.9})
                self.assertNotIn('temperature', settings.model_dump())
                self.assertNotIn('top_p', settings.model_dump())
