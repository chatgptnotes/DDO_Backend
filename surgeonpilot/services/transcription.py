"""
Speech-to-text for in-person doctor/patient consultations.

The doctor records the live, in-room conversation in the browser; that audio is
uploaded to the transcribe endpoint and turned into text here. Hindi (`hi-IN`)
is the default language since most consultations are in Hindi.

Design note — the transcription *engine* is deliberately isolated behind the
single `transcribe_audio()` function and selected via `settings.TRANSCRIPTION_ENGINE`.
Today the only implemented engine is the free Google Web Speech backend of the
`SpeechRecognition` library; `whisper` and `gemini` are reserved for later and
raise until implemented. Swapping engines must never require changes to the view
or the frontend — they only ever see `TranscriptionResult`.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger("surgeonpilot")

DEFAULT_LANGUAGE = "hi-IN"


@dataclass(frozen=True)
class TranscriptionResult:
    """What every engine returns. `transcript` is empty when no speech was
    recognised (e.g. silence) — that is a valid 200, not an error."""

    transcript: str
    language: str
    engine: str


class TranscriptionError(Exception):
    """Raised when the engine itself fails (network/quota/decoding), as opposed
    to simply not recognising any words. The view maps this to HTTP 502."""


def transcribe_audio(
    *,
    audio_bytes: bytes,
    filename: str = "",
    language: str = DEFAULT_LANGUAGE,
) -> TranscriptionResult:
    """Transcribe a recorded audio blob to text.

    `audio_bytes` is the raw uploaded file (browser MediaRecorder produces
    webm/opus by default). `language` is a BCP-47 tag such as `hi-IN`/`en-IN`.
    """
    engine = getattr(settings, "TRANSCRIPTION_ENGINE", "google")

    if engine == "google":
        return _transcribe_google(audio_bytes=audio_bytes, language=language)
    if engine in {"whisper", "gemini"}:
        raise NotImplementedError(
            f"TRANSCRIPTION_ENGINE='{engine}' is not implemented yet; use 'google'."
        )
    raise TranscriptionError(f"Unknown TRANSCRIPTION_ENGINE: {engine!r}")


def _transcribe_google(*, audio_bytes: bytes, language: str) -> TranscriptionResult:
    """Free Google Web Speech backend via the SpeechRecognition library.

    SpeechRecognition only reads PCM WAV/AIFF/FLAC, so the incoming webm/opus is
    first decoded to mono 16-bit WAV with pydub (which shells out to ffmpeg).
    """
    # Imported lazily so the module (and the rest of the app) still imports on
    # machines without the audio stack installed; the cost lands only on a real
    # transcription request.
    import speech_recognition as sr
    from pydub import AudioSegment

    try:
        segment = AudioSegment.from_file(io.BytesIO(audio_bytes))
    except Exception as exc:  # noqa: BLE001 — pydub raises bare Exceptions
        logger.warning("Audio decode failed (ffmpeg missing or bad file): %s", exc)
        raise TranscriptionError(
            "Could not decode the audio. Ensure ffmpeg is installed on the server."
        ) from exc

    wav_buffer = io.BytesIO()
    segment.set_channels(1).export(wav_buffer, format="wav")
    wav_buffer.seek(0)

    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_buffer) as source:
        audio_data = recognizer.record(source)

    try:
        text = recognizer.recognize_google(audio_data, language=language)
    except sr.UnknownValueError:
        # No intelligible speech — return an empty transcript, not an error.
        logger.info("Google STT found no recognisable speech (language=%s)", language)
        return TranscriptionResult(transcript="", language=language, engine="google")
    except sr.RequestError as exc:
        logger.error("Google STT request failed: %s", exc)
        raise TranscriptionError(
            "The speech recognition service is unavailable. Please try again."
        ) from exc

    return TranscriptionResult(transcript=text, language=language, engine="google")
