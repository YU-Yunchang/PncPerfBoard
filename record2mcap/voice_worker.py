"""Runs only inside the separate transcription environment, never in the HTTP process."""
import json
import os
import sys
from pathlib import Path

WINDOWS_MODEL = "small"
WINDOWS_CACHE = Path(__file__).resolve().parent.parent / ".venv-transcribe" / "models"


def windows_model(local_only):
    from faster_whisper import WhisperModel

    return WhisperModel(WINDOWS_MODEL, device="cpu", compute_type="int8",
                        download_root=str(WINDOWS_CACHE), local_files_only=local_only)


def transcribe_windows(audio):
    model = windows_model(local_only=True)
    segments, info = model.transcribe(
        audio, language=os.environ.get("PERFBOARD_WHISPER_LANGUAGE", "zh"),
        condition_on_previous_text=False, no_speech_threshold=0.35,
    )
    result = []
    for segment in segments:
        result.append({name: getattr(segment, name) for name in (
            "start", "end", "text", "compression_ratio", "avg_logprob", "no_speech_prob"
        )})
        print("PROGRESS", round(segment.end * 1000), flush=True)
    return {"segments": result, "language": info.language,
            "model": "Systran/faster-whisper-small"}


def transcribe_mlx(audio, ffmpeg):
    os.environ["HF_HUB_OFFLINE"] = "1"
    import mlx_whisper
    import numpy as np
    import subprocess
    # Pass a waveform to MLX so no ffmpeg PATH wrapper is necessary.
    decoded = subprocess.run([ffmpeg, "-i", audio, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                             check=True, capture_output=True).stdout
    waveform = np.frombuffer(decoded, np.int16).astype(np.float32) / 32768
    # Independent 30-second windows produce reliable progress; 2-second overlap avoids cutting words.
    segments = []
    step = 28 * 16000
    for offset in range(0, len(waveform), step):
        result = mlx_whisper.transcribe(waveform[offset:offset + 30 * 16000],
            path_or_hf_repo="mlx-community/whisper-large-v3-turbo", language="zh",
            condition_on_previous_text=False, no_speech_threshold=0.35, verbose=False)
        for segment in result["segments"]:
            if offset and segment["end"] <= 2:
                continue
            segment["start"] += offset / 16000
            segment["end"] += offset / 16000
            if segments and segment["start"] < segments[-1]["end"] and segment["text"].strip() == segments[-1]["text"].strip():
                continue
            segments.append(segment)
        print("PROGRESS", min(len(waveform), offset + step) * 1000 // 16000, flush=True)
    return {"segments": segments, "language": "zh", "model": "mlx-community/whisper-large-v3-turbo"}


def main():
    if sys.argv[1:] == ["--prepare"]:
        windows_model(local_only=False)
        from imageio_ffmpeg import get_ffmpeg_exe
        import subprocess

        subprocess.run([get_ffmpeg_exe(), "-version"], check=True, stdout=subprocess.DEVNULL)
        return
    audio, directory, ffmpeg = sys.argv[1:]
    raw = transcribe_windows(audio) if sys.platform == "win32" else transcribe_mlx(audio, ffmpeg)
    Path(directory, "raw.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
