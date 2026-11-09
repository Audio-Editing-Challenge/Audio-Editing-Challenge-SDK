import io
import json
import wave

import pytest

from aec_upload.validation import UploadPolicy


@pytest.fixture
def policy():
    return UploadPolicy("aec_single_model", frozenset(f"sample-{i:03}" for i in range(500)))


def wav_bytes(*, rate=16000, channels=1, width=2, frames=160):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(bytes(frames * width * channels))
    return output.getvalue()


@pytest.fixture
def submission(tmp_path, policy):
    directory = tmp_path / "submission"
    (directory / "audio").mkdir(parents=True)
    rows = []
    for sample_id in sorted(policy.expected_ids):
        relative = f"audio/{sample_id}.wav"
        (directory / relative).write_bytes(wav_bytes())
        rows.append({"id": sample_id, "audio_path": relative})
    (directory / "submission.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
    )
    return directory
