"""Shared, bounded-memory validation for the SDK and streaming Upload service."""

from __future__ import annotations

import io
import json
import math
import os
import struct
import tarfile
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import BinaryIO

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
DEFAULT_MAX_AUDIO_SECONDS = 300
MANIFEST_NAME = "submission.jsonl"
MANIFEST_LIMIT = 1024 * 1024
CHUNK_SIZE = 64 * 1024


class ValidationError(ValueError):
    """A submission does not satisfy the public upload contract."""


@dataclass(frozen=True)
class UploadPolicy:
    competition_id: str
    expected_ids: frozenset[str]
    max_audio_seconds: int = DEFAULT_MAX_AUDIO_SECONDS
    max_archive_bytes: int = 2048 * 1024 * 1024

    def __post_init__(self) -> None:
        if len(self.expected_ids) != 500 or any(
            not isinstance(value, str) or not value for value in self.expected_ids
        ):
            raise ValidationError("Upload policy must contain exactly 500 unique sample IDs")
        if (
            type(self.max_audio_seconds) is not int
            or not 1 <= self.max_audio_seconds <= 3600
            or type(self.max_archive_bytes) is not int
            or self.max_archive_bytes <= 0
        ):
            raise ValidationError("Invalid upload size/duration limits")

    def as_dict(self) -> dict:
        return {
            "competition_id": self.competition_id,
            "expected_ids": sorted(self.expected_ids),
            "sample_count": len(self.expected_ids),
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "sample_width_bits": SAMPLE_WIDTH * 8,
            "encoding": "PCM_16",
            "max_audio_seconds": self.max_audio_seconds,
            "max_archive_bytes": self.max_archive_bytes,
            "manifest": MANIFEST_NAME,
            "archive_format": "ustar",
            "manifest_first": True,
        }

    @classmethod
    def from_dict(cls, value: dict) -> UploadPolicy:
        if any(value.get(key) != expected for key, expected in {
            "sample_count": 500, "sample_rate": SAMPLE_RATE, "channels": CHANNELS,
            "sample_width_bits": 16, "encoding": "PCM_16", "archive_format": "ustar",
            "manifest": MANIFEST_NAME, "manifest_first": True,
        }.items()):
            raise ValidationError("Server upload format is not supported by this SDK")
        ids = value["expected_ids"]
        if not isinstance(ids, list) or len(ids) != 500:
            raise ValidationError("Server policy must contain 500 sample IDs")
        return cls(
            competition_id=value["competition_id"], expected_ids=frozenset(ids),
            max_audio_seconds=value["max_audio_seconds"],
            max_archive_bytes=value["max_archive_bytes"],
        )


@lru_cache
def expected_ids(competition_id: str) -> frozenset[str]:
    if competition_id not in {"aec_single_model", "aec_agent"}:
        raise ValidationError("No sample ID list is configured for this competition")
    return frozenset(json.loads(files("aec_upload").joinpath(
        "data", f"{competition_id}.json"
    ).read_text(encoding="utf-8")))


def policy_for_competition(
    competition_id: str, *, max_audio_seconds: int = DEFAULT_MAX_AUDIO_SECONDS,
    max_archive_bytes: int = 2048 * 1024 * 1024,
) -> UploadPolicy:
    return UploadPolicy(competition_id, expected_ids(competition_id), max_audio_seconds,
                        max_archive_bytes)


def safe_relative_path(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name or "\\" in name or "\x00" in name or ":" in name
        or path.is_absolute() or ".." in path.parts or str(path) != name or name == "."
        or any(ord(char) < 32 for char in name)
    ):
        raise ValidationError(f"Unsafe or non-canonical relative path: {name!r}")
    return name


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def parse_manifest(data: bytes, policy: UploadPolicy) -> dict[str, str]:
    if len(data) > MANIFEST_LIMIT:
        raise ValidationError("submission.jsonl exceeds 1 MiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("submission.jsonl must be UTF-8") from exc
    mapping: dict[str, str] = {}
    paths: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=_object)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValidationError(f"Manifest line {lineno}: invalid JSON ({exc})") from exc
        if not isinstance(row, dict) or set(row) != {"id", "audio_path"} or not all(
            isinstance(row[key], str) for key in ("id", "audio_path")
        ):
            raise ValidationError(f"Manifest line {lineno}: expected only string id and audio_path")
        sample_id, path = row["id"], safe_relative_path(row["audio_path"])
        if sample_id not in policy.expected_ids:
            raise ValidationError(f"Manifest line {lineno}: unknown ID {sample_id!r}")
        if sample_id in mapping:
            raise ValidationError(f"Manifest line {lineno}: duplicate ID {sample_id!r}")
        if path in paths:
            raise ValidationError(f"Manifest line {lineno}: audio_path reused by multiple IDs")
        if not path.lower().endswith(".wav"):
            raise ValidationError(f"Manifest line {lineno}: audio_path must end in .wav")
        mapping[sample_id] = path
        paths.add(path)
    missing = policy.expected_ids - mapping.keys()
    if missing:
        raise ValidationError(f"Manifest is missing {len(missing)} of 500 required IDs")
    return mapping


def read_exact(stream: BinaryIO, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            raise ValidationError("Truncated archive or WAV file")
        data.extend(part)
    return bytes(data)


def _discard(stream: BinaryIO, size: int) -> None:
    while size:
        count = min(CHUNK_SIZE, size)
        read_exact(stream, count)
        size -= count


def validate_wav(stream: BinaryIO, size: int, policy: UploadPolicy, name: str) -> float:
    """Validate RIFF framing and actual PCM bytes, not only the advertised duration."""
    maximum_data = policy.max_audio_seconds * SAMPLE_RATE * SAMPLE_WIDTH
    if not 44 <= size <= maximum_data + 65536:
        raise ValidationError(f"{name}: WAV size exceeds the duration/metadata limit or is too small")
    header = read_exact(stream, 12)
    if header[:4] != b"RIFF" or header[8:] != b"WAVE":
        raise ValidationError(f"{name}: expected a little-endian RIFF/WAVE file")
    if struct.unpack("<I", header[4:8])[0] + 8 != size:
        raise ValidationError(f"{name}: RIFF size does not match the actual file size")
    remaining, fmt_seen, data_seen, data_size = size - 12, False, False, 0
    while remaining:
        if remaining < 8:
            raise ValidationError(f"{name}: truncated WAV chunk header")
        chunk_id, chunk_size = struct.unpack("<4sI", read_exact(stream, 8))
        remaining -= 8
        padded = chunk_size + chunk_size % 2
        if padded > remaining:
            raise ValidationError(f"{name}: WAV chunk extends beyond the file")
        if chunk_id == b"fmt ":
            if fmt_seen or data_seen or not 16 <= chunk_size <= 4096:
                raise ValidationError(f"{name}: invalid or duplicate WAV format chunk")
            fmt = read_exact(stream, chunk_size)
            tag, channels, rate, byte_rate, alignment, bits = struct.unpack("<HHIIHH", fmt[:16])
            pcm = tag == 1 and (
                len(fmt) == 16
                or (len(fmt) >= 18 and struct.unpack("<H", fmt[16:18])[0] <= len(fmt) - 18)
            )
            if tag == 0xFFFE and len(fmt) >= 40:
                extra_size, valid_bits = struct.unpack("<HH", fmt[16:20])
                pcm = (
                    22 <= extra_size <= len(fmt) - 18 and valid_bits == 16
                    and fmt[24:40] == bytes.fromhex("0100000000001000800000aa00389b71")
                )
            if not pcm or (channels, rate, byte_rate, alignment, bits) != (1, 16000, 32000, 2, 16):
                raise ValidationError(f"{name}: audio must be 16000 Hz, mono, 16-bit PCM WAV")
            fmt_seen = True
        elif chunk_id == b"data":
            if not fmt_seen or data_seen or chunk_size == 0 or chunk_size % SAMPLE_WIDTH:
                raise ValidationError(f"{name}: empty, duplicate, or invalid PCM data chunk")
            if chunk_size > maximum_data:
                raise ValidationError(f"{name}: audio exceeds {policy.max_audio_seconds} seconds")
            data_seen, data_size = True, chunk_size
            _discard(stream, chunk_size)
        else:
            _discard(stream, chunk_size)
        if chunk_size % 2:
            read_exact(stream, 1)
        remaining -= padded
    if not fmt_seen or not data_seen:
        raise ValidationError(f"{name}: missing WAV format or PCM data")
    return data_size / (SAMPLE_RATE * SAMPLE_WIDTH)


class LimitedReader:
    def __init__(self, stream: BinaryIO, limit: int):
        self.stream, self.limit, self.total = stream, limit, 0

    def read(self, size: int) -> bytes:
        chunk = self.stream.read(min(size, self.limit - self.total + 1))
        self.total += len(chunk)
        if self.total > self.limit:
            raise ValidationError("Archive exceeds the configured upload size limit")
        return chunk


def validate_tar_stream(stream: BinaryIO, policy: UploadPolicy) -> dict:
    """Read uncompressed USTAR once; reject metadata extensions before their payloads."""
    reader = LimitedReader(stream, policy.max_archive_bytes)
    manifest = None
    seen: set[str] = set()
    audio_seen: set[str] = set()
    audio_paths: set[str] = set()
    directories: set[str] = set()
    durations: list[float] = []
    while True:
        header = read_exact(reader, 512)
        if header == bytes(512):
            if read_exact(reader, 512) != bytes(512):
                raise ValidationError("Tar must end with two zero blocks")
            while padding := reader.read(CHUNK_SIZE):
                if any(padding):
                    raise ValidationError("Non-zero data or another archive after the tar terminator")
            break
        try:
            member = tarfile.TarInfo.frombuf(header, encoding="utf-8", errors="strict")
        except (tarfile.HeaderError, UnicodeError, ValueError) as exc:
            raise ValidationError("Invalid USTAR header (compressed tar is not supported)") from exc
        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE):
            raise ValidationError("Only regular files/directories are allowed; no links or PAX/GNU extensions")
        name = safe_relative_path(member.name.rstrip("/") if member.isdir() else member.name)
        if name in seen or member.size < 0:
            raise ValidationError(f"Duplicate tar member or invalid size: {name}")
        seen.add(name)
        if manifest is None:
            if name != MANIFEST_NAME or not member.isfile() or member.size > MANIFEST_LIMIT:
                raise ValidationError("The first tar member must be submission.jsonl (at most 1 MiB)")
            manifest = parse_manifest(read_exact(reader, member.size), policy)
            audio_paths = set(manifest.values())
            directories = {
                str(parent) for path in audio_paths for parent in PurePosixPath(path).parents
                if str(parent) != "."
            }
        elif member.isdir():
            if member.size or name not in directories:
                raise ValidationError(f"Unexpected directory or non-empty directory member: {name}")
        else:
            if name not in audio_paths:
                raise ValidationError(f"Unexpected file not referenced by the manifest: {name}")
            durations.append(validate_wav(reader, member.size, policy, name))
            audio_seen.add(name)
        padding_size = (-member.size) % 512
        if padding_size and any(read_exact(reader, padding_size)):
            raise ValidationError("Non-zero tar member padding")
    if manifest is None or audio_seen != audio_paths:
        raise ValidationError(f"Archive is missing {len(audio_paths - audio_seen)} required WAV files")
    return {
        "sample_count": len(audio_seen), "max_audio_seconds": max(durations),
        "total_audio_seconds": math.fsum(durations), "archive_bytes": reader.total,
    }


def validate_archive(tar_path: str | Path, policy: UploadPolicy) -> dict:
    with Path(tar_path).open("rb") as stream:
        return validate_tar_stream(stream, policy)


def _source_file(root: Path, relative: str) -> Path:
    path = root
    for part in PurePosixPath(safe_relative_path(relative)).parts:
        path /= part
        if path.is_symlink():
            raise ValidationError(f"Symlinks are not allowed: {relative}")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValidationError(f"Missing or unsafe file: {relative}")
    return path


def validate_submission(submission_dir: str | Path, policy: UploadPolicy) -> dict:
    root = Path(submission_dir).resolve(strict=True)
    manifest_path = _source_file(root, MANIFEST_NAME)
    with manifest_path.open("rb") as stream:
        manifest_bytes = stream.read(MANIFEST_LIMIT + 1)
    mapping = parse_manifest(manifest_bytes, policy)
    durations = []
    archive_size = 512 + (len(manifest_bytes) + 511) // 512 * 512 + 1024
    for path in mapping.values():
        source = _source_file(root, path)
        size = source.stat().st_size
        with source.open("rb") as stream:
            durations.append(validate_wav(stream, size, policy, path))
        info = tarfile.TarInfo(path)
        info.size = size
        try:
            info.tobuf(format=tarfile.USTAR_FORMAT, encoding="utf-8")
        except (ValueError, UnicodeError) as exc:
            raise ValidationError(f"Path cannot be represented in USTAR: {path}") from exc
        archive_size += 512 + (size + 511) // 512 * 512
    archive_size = (archive_size + 10239) // 10240 * 10240
    if archive_size > policy.max_archive_bytes:
        raise ValidationError("Packaged submission would exceed the upload size limit")
    return {
        "sample_count": len(mapping), "max_audio_seconds": max(durations),
        "total_audio_seconds": math.fsum(durations), "archive_bytes": archive_size,
    }


def build_submission_tar(
    submission_dir: str | Path, tar_path: str | Path, policy: UploadPolicy,
) -> Path:
    """Validate before packaging, then atomically publish a revalidated archive."""
    root = Path(submission_dir).resolve(strict=True)
    validate_submission(root, policy)
    with _source_file(root, MANIFEST_NAME).open("rb") as source:
        manifest_bytes = source.read(MANIFEST_LIMIT + 1)
    mapping = parse_manifest(manifest_bytes, policy)
    destination = Path(tar_path).absolute()
    protected = {_source_file(root, value) for value in (MANIFEST_NAME, *mapping.values())}
    if destination.resolve() in protected:
        raise ValidationError("Output tar must not overwrite submission inputs")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".submission-", suffix=".tar", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                info = tarfile.TarInfo(MANIFEST_NAME)
                info.size = len(manifest_bytes)
                archive.addfile(info, io.BytesIO(manifest_bytes))
                for name in mapping.values():
                    source = _source_file(root, name)
                    info = tarfile.TarInfo(name)
                    info.size = source.stat().st_size
                    with source.open("rb") as stream:
                        archive.addfile(info, stream)
        validate_archive(temporary, policy)
        os.replace(temporary, destination)
        return destination
    finally:
        Path(temporary).unlink(missing_ok=True)
