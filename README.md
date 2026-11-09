# Audio Editing Challenge SDK

Validate and upload your inference results for the Audio Editing Challenge. Requires Python 3.11+.

## Install

```bash
git clone https://github.com/Audio-Editing-Challenge/Audio-Editing-Challenge-SDK.git
cd Audio-Editing-Challenge-SDK
pip install -e .
```

## Prepare your results

```text
my_submission/
├── submission.jsonl
└── audio/
    ├── sample-001.wav
    ├── sample-002.wav
    └── ...
```

Each nonempty line in the UTF-8 `submission.jsonl` must contain exactly two string fields:

```json
{"id": "<official sample ID>", "audio_path": "audio/sample-001.wav"}
```

Replace the placeholder with a real sample ID and include all 500 IDs from your track. Audio paths are relative to `my_submission/`, use `/`, and must be unique for each sample.

| Requirement | Value |
| --- | --- |
| Track 1 | `aec_single_model` |
| Track 2 | `aec_agent` |
| Samples | All 500 official IDs, each exactly once |
| Audio | 16 kHz, mono, 16-bit integer PCM WAV; nonempty |
| Duration | At most 300 seconds per audio file |
| Total upload size | At most 2 GiB, including tar headers and padding |

The SDK fetches the current requirements from the server before validating and uploading. Prepare audio in the required format; the SDK does not convert it. Use regular files, without symlinks or paths outside the results directory.

## Upload

Get your evaluation URL and track-specific Secret Key from the organizers, then run:

```bash
python examples/upload.py ./my_submission \
  --competition aec_single_model \
  --archive ./result.tar \
  --name baseline
```

The script prompts for the URL and Secret Key with hidden input. It validates your results, creates an uncompressed tar, uploads it, and prints the **Upload ID**. For automation, set `AEC_BASE_URL` and `AEC_SECRET_KEY` in the environment instead. Use the base URL without an `/upload` suffix.

After uploading, open the evaluation website, confirm the same Secret Key, select the **Ready Upload**, and click **Start evaluation**. Uploading alone does not start evaluation; upload and submission quotas are separate.

## Python usage

```python
import os

from aec_upload import UploadClient

with UploadClient(
    base_url=os.environ["AEC_BASE_URL"],
    secret_key=os.environ["AEC_SECRET_KEY"],
    competition_id="aec_single_model",
) as client:
    tar_path = client.pack("./my_submission", "./result.tar")
    result = client.upload(tar_path, name="baseline")
    print(result["upload"]["id"])
```

You can also use `client.validate(directory)` to check your results, `client.policy()` to get the expected IDs and limits, or `client.list_uploads()` to see your Ready Uploads and upload quota.

Validation failures raise `aec_upload.ValidationError`; server rejections raise `aec_upload.UploadError`. If a network request fails, check Ready Uploads before retrying: the server may already have accepted the upload.

Keep your evaluation URL and Secret Key private, including in logs and public issues.

## License

[MIT](LICENSE).
