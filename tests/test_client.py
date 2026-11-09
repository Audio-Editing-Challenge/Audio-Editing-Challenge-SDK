import hashlib

import httpx
import pytest

from aec_upload import UploadClient, UploadError, ValidationError

from .conftest import wav_bytes


def test_sdk_packs_validates_and_streams_raw_tar(submission, policy, tmp_path):
    received = []

    def handle(request):
        if request.url.path == "/upload/policy":
            return httpx.Response(200, json=policy.as_dict())
        assert request.headers["content-type"] == "application/x-tar"
        assert request.headers["authorization"] == "Bearer test-key"
        assert "secret_key" not in request.url.params
        body = request.read()
        assert len(body) == int(request.headers["content-length"])
        received.append(body)
        return httpx.Response(201, json={"upload": {"sha256": hashlib.sha256(body).hexdigest()}})

    with UploadClient("http://test", "test-key", policy.competition_id) as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(handle), base_url="http://test")
        path = client.pack(submission, tmp_path / "submission.tar")
        result = client.upload(path, name="基线模型")
    assert result["upload"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(received) == 1


def test_sdk_refuses_bad_audio_before_pack_and_bad_tar_before_post(submission, policy, tmp_path):
    def handle(request):
        assert request.url.path == "/upload/policy", "Invalid content must never be uploaded"
        return httpx.Response(200, json=policy.as_dict())

    with UploadClient("http://test", "test-key", policy.competition_id) as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(handle), base_url="http://test")
        (submission / "audio/sample-000.wav").write_bytes(wav_bytes(channels=2))
        output = tmp_path / "invalid.tar"
        with pytest.raises(ValidationError):
            client.pack(submission, output)
        assert not output.exists()
        output.write_bytes(b"invalid tar")
        with pytest.raises(ValidationError):
            client.upload(output, name="bad")


@pytest.mark.parametrize("response", [
    httpx.Response(502, text="Bad gateway"),
    httpx.Response(403, json={"detail": "Invalid key"}),
    httpx.Response(500, json=["unavailable"]),
    httpx.Response(200, json=[]),
    httpx.Response(200, json=None),
    httpx.Response(302, json={"detail": "Use another endpoint"}),
])
def test_service_errors_have_a_consistent_public_exception(response):
    with pytest.raises(UploadError):
        UploadClient._result(response)


def test_policy_mismatch_prevents_packaging(submission, policy, tmp_path):
    wrong_policy = {**policy.as_dict(), "competition_id": "other-track"}
    with UploadClient("http://test", "test-key", policy.competition_id) as client:
        client._client.close()
        client._client = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=wrong_policy)),
            base_url="http://test",
        )
        output = tmp_path / "wrong-track.tar"
        with pytest.raises(ValidationError, match="different competition"):
            client.pack(submission, output)
        assert not output.exists()


def test_ready_upload_listing_and_network_errors_are_not_retried(policy):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == "/upload/uploads"
        assert request.url.params["competition_id"] == policy.competition_id
        assert request.url.params["secret_key"] == "test-key"
        if len(calls) == 1:
            return httpx.Response(200, json={"uploads": [], "quota": {"remaining": 5}})
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    with UploadClient("http://test", "test-key", policy.competition_id) as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(handle), base_url="http://test")
        assert client.list_uploads() == {"uploads": [], "quota": {"remaining": 5}}
        with pytest.raises(httpx.ReadTimeout):
            client.list_uploads()
    assert len(calls) == 2
