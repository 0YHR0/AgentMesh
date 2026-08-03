from hashlib import sha256

from agentmesh.packs.music_studio.providers.deterministic import (
    DeterministicAudioAnalyzer,
    DeterministicMusicProvider,
)


def test_deterministic_music_provider_generates_reproducible_analyzable_wav():
    provider = DeterministicMusicProvider()
    first = provider.generate(operation_key="project-1-round-1", seed="summer city pop")
    second = provider.generate(operation_key="project-1-round-1", seed="summer city pop")

    assert first.content == second.content
    assert first.content[:4] == b"RIFF"
    assert first.content[8:12] == b"WAVE"
    assert first.content_sha256 == sha256(first.content).hexdigest()
    assert first.provider == "deterministic-demo"

    evidence = DeterministicAudioAnalyzer().analyze(first.content)
    assert evidence.duration_seconds == 2
    assert evidence.sample_rate == 8_000
    assert evidence.channels == 1
    assert 0 < evidence.rms_amplitude < evidence.peak_amplitude < 1
    assert evidence.clipping_samples == 0


def test_different_music_seeds_produce_different_audio():
    provider = DeterministicMusicProvider()

    first = provider.generate(operation_key="one", seed="warm pop")
    second = provider.generate(operation_key="two", seed="dark ambient")

    assert first.content_sha256 != second.content_sha256
