"""Minimal Gemini Interactions API adapter with structured JSON output."""

import json

from .redact import redact
from .schema import ANALYSIS_SCHEMA, validate_analysis


SYSTEM_INSTRUCTION = """You are SMC2026's offline shadow research analyst.
You have NO trading authority. Never issue an order, position instruction, or
real-time action. Analyze only the supplied immutable historical evidence.
Distinguish mainnet market evidence from testnet execution results. Do not treat
integration-test PnL as strategy-valid when economic_result_valid is false.
Identify missing or low-quality data explicitly. Recommendations must be framed
as hypotheses for later replay/backtesting, never as commands to the live bot.
Assess the evidence symmetrically: always report evidence supporting the setup
and evidence contradicting it. Do not default to caution, veto, or smaller size;
do not default to aggression either. Separate observations from hypotheses and
calibrate confidence from data coverage, sample independence, and validity.
Return only JSON matching the supplied schema."""


def _usage_dict(interaction):
    for field in ('usage_metadata', 'usage'):
        usage = getattr(interaction, field, None)
        if usage is None:
            continue
        if hasattr(usage, 'model_dump'):
            return usage.model_dump(mode='json', exclude_none=True)
        if isinstance(usage, dict):
            return dict(usage)
    return {}


class GeminiShadowClient:
    def __init__(self, api_key, config):
        if not api_key:
            raise ValueError('Gemini credential is missing')
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError('google-genai is not installed') from exc
        self.api_key = api_key
        self.config = config
        self._client = genai.Client(
            api_key=api_key,
            http_options={
                'timeout': int(config.request_timeout_seconds * 1000),
                # The worker owns retry/backoff so the SDK must not multiply
                # one quota failure into several hidden HTTP requests.
                # The generated Interactions client treats attempts=1 as one
                # retry despite the public type saying it means no retries.
                # Excluding normal HTTP failures from its retry allowlist
                # leaves all backoff/circuit policy to ShadowWorker.
                'retry_options': {'attempts': 1, 'http_status_codes': [599]},
            },
        )

    async def analyze(self, envelope):
        safe_envelope = redact(envelope, secrets=(self.api_key,))
        interaction = await self._client.aio.interactions.create(
            model=self.config.model,
            input=json.dumps(safe_envelope, ensure_ascii=False, separators=(',', ':')),
            system_instruction=SYSTEM_INSTRUCTION,
            generation_config={'thinking_level': self.config.thinking_level},
            response_format={
                'type': 'text',
                'mime_type': 'application/json',
                'schema': ANALYSIS_SCHEMA,
            },
        )
        try:
            payload = json.loads(interaction.output_text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError('Gemini returned invalid JSON') from exc
        return validate_analysis(payload), _usage_dict(interaction)

    async def close(self):
        await self._client.aio.aclose()
