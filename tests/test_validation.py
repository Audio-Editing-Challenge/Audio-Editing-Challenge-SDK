import io
import json
import struct
import tarfile
from dataclasses import replace

import pytest

from aec_upload.validation import (
    ValidationError,
    build_submission_tar,
    expected_ids,
    parse_manifest,
    validate_archive,
    validate_submission,
    validate_tar_stream,
    validate_wav,
)

from .conftest import wav_bytes


def test_exported_id_lists_are_complete_and_distinct():
    single, agent = expected_ids("aec_single_model"), expected_ids("aec_agent")
    assert len(single) == len(agent) == 500
    assert single != agent


def test_pack_then_validate_all_500_files(submission, tmp_path, policy):
    report = validate_submission(submission, policy)
    path = build_submission_tar(submission, tmp_path / "result.tar", policy)
    assert validate_archive(path, policy) == report
    assert report["sample_count"] == 500
    with tarfile.open(path) as archive:
        assert archive.getnames()[0] == "submission.jsonl"
        assert len(archive.getmembers()) == 501


@pytest.mark.parametrize("problem", ["missing", "duplicate", "unknown", "reused_path", "traversal"])
def test_manifest_rejects_incomplete_or_ambiguous_inputs(submission, policy, problem):
    rows = [json.loads(line) for line in (submission / "submission.jsonl").read_text().splitlines()]
    if problem == "missing":
        rows.pop()
    elif problem == "duplicate":
        rows[-1] = rows[0]
    elif problem == "unknown":
        rows[0]["id"] = "not-an-official-id"
    elif problem == "reused_path":
        rows[-1]["audio_path"] = rows[0]["audio_path"]
    else:
        rows[0]["audio_path"] = "../outside.wav"
    with pytest.raises(ValidationError):
        parse_manifest("\n".join(json.dumps(row) for row in rows).encode(), policy)


@pytest.mark.parametrize("kwargs", [
    {"rate": 48000}, {"channels": 2}, {"width": 3}, {"width": 1}, {"frames": 0},
])
def test_audio_format_is_strict(policy, kwargs):
    data = wav_bytes(**kwargs)
    with pytest.raises(ValidationError):
        validate_wav(io.BytesIO(data), len(data), policy, "audio.wav")


def test_duration_boundary_and_truncated_pcm(policy):
    data = wav_bytes(frames=300 * 16000)
    assert validate_wav(io.BytesIO(data), len(data), policy, "audio.wav") == 300
    excessive = wav_bytes(frames=300 * 16000 + 1)
    with pytest.raises(ValidationError, match="exceeds 300"):
        validate_wav(io.BytesIO(excessive), len(excessive), policy, "audio.wav")
    with pytest.raises(ValidationError, match="Truncated"):
        validate_wav(io.BytesIO(data[:-2]), len(data), policy, "audio.wav")


def test_forged_wav_and_float_audio_rejected(policy):
    data = bytearray(wav_bytes())
    struct.pack_into("<H", data, 20, 3)
    with pytest.raises(ValidationError, match="16-bit PCM"):
        validate_wav(io.BytesIO(data), len(data), policy, "audio.wav")
    data = bytearray(wav_bytes())
    struct.pack_into("<I", data, 40, 2)
    with pytest.raises(ValidationError):
        validate_wav(io.BytesIO(data), len(data), policy, "audio.wav")


def test_pack_failure_does_not_publish_tar(submission, tmp_path, policy):
    (submission / "audio/sample-499.wav").write_bytes(wav_bytes(rate=44100))
    destination = tmp_path / "submission.tar"
    with pytest.raises(ValidationError):
        build_submission_tar(submission, destination, policy)
    assert not destination.exists()
    assert not list(tmp_path.glob(".submission-*.tar"))


def test_symlink_source_rejected(submission, policy, tmp_path):
    audio = submission / "audio/sample-000.wav"
    audio.unlink()
    target = tmp_path / "outside.wav"
    target.write_bytes(wav_bytes())
    audio.symlink_to(target)
    with pytest.raises(ValidationError, match="Symlinks"):
        validate_submission(submission, policy)


def tar_bytes(members):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


@pytest.mark.parametrize("member", [
    ("../outside", b"", tarfile.REGTYPE),
    ("audio/sample-000.wav", b"", tarfile.SYMTYPE),
    ("audio/sample-000.wav", b"", tarfile.LNKTYPE),
    ("pax", b"", tarfile.XHDTYPE),
    ("device", b"", tarfile.CHRTYPE),
    ("extra.txt", b"hello", tarfile.REGTYPE),
    ("submission.jsonl", b"", tarfile.REGTYPE),
])
def test_unsafe_or_unexpected_tar_members_rejected(submission, policy, member):
    manifest = (submission / "submission.jsonl").read_bytes()
    data = tar_bytes([("submission.jsonl", manifest, tarfile.REGTYPE), member])
    with pytest.raises(ValidationError):
        validate_tar_stream(io.BytesIO(data), policy)


def test_missing_audio_and_manifest_order_rejected(submission, policy):
    manifest = (submission / "submission.jsonl").read_bytes()
    for members in [
        [("submission.jsonl", manifest, tarfile.REGTYPE)],
        [("audio/sample-000.wav", wav_bytes(), tarfile.REGTYPE)],
    ]:
        with pytest.raises(ValidationError):
            validate_tar_stream(io.BytesIO(tar_bytes(members)), policy)


def test_tar_truncation_trailer_and_size_limit(submission, policy, tmp_path):
    path = build_submission_tar(submission, tmp_path / "valid.tar", policy)
    data = path.read_bytes()
    with pytest.raises(ValidationError, match="Truncated"):
        validate_tar_stream(io.BytesIO(data[:2000]), policy)
    with pytest.raises(ValidationError, match="after the tar terminator"):
        validate_tar_stream(io.BytesIO(data + b"another archive"), policy)
    with pytest.raises(ValidationError, match="size limit"):
        validate_tar_stream(io.BytesIO(data), replace(policy, max_archive_bytes=1024))


def test_stream_reader_handles_small_network_chunks(submission, policy, tmp_path):
    path = build_submission_tar(submission, tmp_path / "valid.tar", policy)

    class ShortReads(io.BytesIO):
        def read(self, size):
            return super().read(min(size, 37))

    assert validate_tar_stream(ShortReads(path.read_bytes()), policy)["sample_count"] == 500
