"""THE provider swap point (SDD §2.4, C-2).

The only module that imports provider SDK / service classes. Each factory
branches on the configured provider name and returns a Pipecat service.
Swapping a provider = add a branch here + set an env var.
"""

from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.google.llm import GoogleLLMService

from app.config import Settings


def make_stt(settings: Settings):
    if settings.stt_provider == "deepgram_flux":
        return DeepgramFluxSTTService(
            api_key=settings.deepgram_api_key,
            # Higher eot_threshold = Flux waits for more certainty the user is
            # done, sitting through brief pauses before ending the turn (softer
            # turn-taking). Confidence-based, not a fixed timer. Tune by ear.
            settings=DeepgramFluxSTTService.Settings(
                eot_threshold=settings.flux_eot_threshold
            ),
        )
    raise ValueError(f"Unknown STT_PROVIDER: {settings.stt_provider}")


def make_llm(settings: Settings):
    if settings.llm_provider == "google":
        return GoogleLLMService(
            api_key=settings.google_api_key,
            settings=GoogleLLMService.Settings(
                model=settings.llm_model,
                # Thinking OFF: with it on, Flash TTFB blows the §5 latency
                # budget. Pipecat defaults 2.5 Flash to budget 0 already; set
                # it explicitly so an upstream default change can't silently
                # re-enable it.
                thinking=GoogleLLMService.ThinkingConfig(thinking_budget=0),
            ),
        )
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")


def make_tts(settings: Settings, text_filters: list):
    if settings.tts_provider == "cartesia":
        return CartesiaTTSService(
            api_key=settings.cartesia_api_key,
            settings=CartesiaTTSService.Settings(
                model="sonic-3",
                voice=settings.cartesia_voice_id,
                # Sonic-3 speech rate; 1.0 = normal, lower = slower.
                generation_config=GenerationConfig(speed=settings.cartesia_speed),
            ),
            text_filters=text_filters,
        )
    raise ValueError(f"Unknown TTS_PROVIDER: {settings.tts_provider}")
