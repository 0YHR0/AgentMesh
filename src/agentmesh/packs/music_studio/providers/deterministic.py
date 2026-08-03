from __future__ import annotations

import io
import math
import struct
import wave
from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class GeneratedMusic:
    content: bytes
    media_type: str
    provider: str
    operation_key: str
    content_sha256: str


@dataclass(frozen=True)
class AudioEvidence:
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_width_bytes: int
    frame_count: int
    peak_amplitude: float
    rms_amplitude: float
    clipping_samples: int

    def to_dict(self) -> dict[str, int | float]:
        return {
            "duration_seconds": self.duration_seconds,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "sample_width_bytes": self.sample_width_bytes,
            "frame_count": self.frame_count,
            "peak_amplitude": self.peak_amplitude,
            "rms_amplitude": self.rms_amplitude,
            "clipping_samples": self.clipping_samples,
        }


class DeterministicMusicProvider:
    """Generate a small valid WAV fixture without credentials or network access."""

    provider_name = "deterministic-demo"

    def generate(self, *, operation_key: str, seed: str) -> GeneratedMusic:
        digest = sha256(seed.encode("utf-8")).digest()
        sample_rate = 8_000
        duration_seconds = 2
        frequencies = (
            220 + digest[0] % 80,
            330 + digest[1] % 90,
            440 + digest[2] % 100,
        )
        frames = bytearray()
        for index in range(sample_rate * duration_seconds):
            second = index / sample_rate
            envelope = min(1.0, index / 400, (sample_rate * duration_seconds - index) / 400)
            signal = sum(
                math.sin(2 * math.pi * frequency * second) for frequency in frequencies
            ) / len(frequencies)
            sample = int(12_000 * envelope * signal)
            frames.extend(struct.pack("<h", sample))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(bytes(frames))
        content = buffer.getvalue()
        return GeneratedMusic(
            content=content,
            media_type="audio/wav",
            provider=self.provider_name,
            operation_key=operation_key,
            content_sha256=sha256(content).hexdigest(),
        )


class DeterministicAudioAnalyzer:
    """Measure the actual WAV bytes used by the Demo critic."""

    def analyze(self, content: bytes) -> AudioEvidence:
        with wave.open(io.BytesIO(content), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            frames = source.readframes(frame_count)
        if sample_width != 2:
            raise ValueError("The deterministic analyzer requires 16-bit PCM WAV audio")
        sample_count = len(frames) // 2
        samples = struct.unpack(f"<{sample_count}h", frames)
        peak = max((abs(value) for value in samples), default=0)
        square_mean = sum(value * value for value in samples) / max(1, sample_count)
        return AudioEvidence(
            duration_seconds=round(frame_count / sample_rate, 3),
            sample_rate=sample_rate,
            channels=channels,
            sample_width_bytes=sample_width,
            frame_count=frame_count,
            peak_amplitude=round(peak / 32767, 6),
            rms_amplitude=round(math.sqrt(square_mean) / 32767, 6),
            clipping_samples=sum(abs(value) >= 32767 for value in samples),
        )
