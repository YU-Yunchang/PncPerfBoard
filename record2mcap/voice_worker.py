"""Runs only inside the optional MLX environment, never in the HTTP process."""
import json
import os
import sys
from pathlib import Path


def main():
    audio, directory, ffmpeg = sys.argv[1:]
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
    Path(directory, "raw.json").write_text(json.dumps({"segments": segments, "language": "zh"}, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
