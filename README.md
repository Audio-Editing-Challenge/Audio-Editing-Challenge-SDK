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

## Import from Hugging Face Storage Buckets

You can upload your results to Hugging Face and import them into the evaluation website. We recommend a **private Bucket with a temporary download link** to keep your results private.

### 1. Create a Bucket and credentials

Visit [Create a Storage Bucket](https://huggingface.co/new-bucket), enter a Bucket name, and choose **Private** so other participants cannot view your files. Public Buckets are also supported. Hugging Face includes 100 GB of private storage with a free account.

Visit [Access Tokens](https://huggingface.co/settings/tokens) and create a **Write** token with read and write access to your Bucket. In the token's three-dot menu on the right, select **Generate S3 credentials** and save the **Access Key ID** and **Secret Access Key** for step 4. These are Hugging Face credentials, separate from your competition Secret Key.

Install the tools and sign in with your Hugging Face token:

```bash
pip install -U huggingface_hub boto3
hf auth login
```

### 2. Validate and pack your results

Use the evaluation URL and competition Secret Key from the organizers:

```python
import os

from aec_upload import UploadClient

with UploadClient(
    base_url=os.environ["AEC_BASE_URL"],
    secret_key=os.environ["AEC_SECRET_KEY"],
    competition_id="aec_single_model",  # Use aec_agent for Track 2.
) as client:
    client.pack("./my_submission", "./result.tar")
```

This creates `result.tar` only after validation passes.

### 3. Upload the tar to Hugging Face

Replace `username` and `my-results` with your Hugging Face username and Bucket name:

```bash
hf buckets cp ./result.tar hf://buckets/username/my-results/result.tar
```

### 4. Create a temporary link and import

Use the same username, Bucket name, and file path below. When prompted, enter the two S3 credentials from step 1.

```python
import os
from getpass import getpass

import boto3
from botocore.config import Config
from aec_upload import UploadClient

s3 = boto3.client(
    "s3",
    endpoint_url="https://username.s3.hf.co",
    aws_access_key_id=getpass("HF S3 Access Key ID: "),
    aws_secret_access_key=getpass("HF S3 Secret Access Key: "),
    config=Config(
        region_name="us-east-1",
        signature_version="s3v4",
        s3={"addressing_style": "path"},
    ),
)
signed_url = s3.generate_presigned_url(
    "get_object",
    Params={"Bucket": "my-results", "Key": "result.tar"},
    ExpiresIn=3600,
)

with UploadClient(
    base_url=os.environ["AEC_BASE_URL"],
    secret_key=os.environ["AEC_SECRET_KEY"],
    competition_id="aec_single_model",  # Use aec_agent for Track 2.
) as client:
    result = client.upload_from_hf(
        signed_url,
        name="my-results",
        source_type="presigned",
    )
    print(result["upload"]["id"])
```

Keep the generated URL unchanged and use it promptly; this example makes it valid for one hour. Keep your credentials and temporary link private, and leave the file in place until the import finishes. If the link expires, generate a new one.

The call waits until your results are ready and prints an **Upload ID**. Open the evaluation website, confirm your competition Secret Key, select the **Ready Upload**, and click **Start evaluation**. Imports use your normal upload quota. If a network error occurs, check Ready Uploads before retrying.

### Alternative: import from a public Bucket

For a public Bucket, a temporary link is optional. Inside the `with UploadClient(...) as client:` block above, replace the import call with:

```python
result = client.upload_from_hf(
    "hf://buckets/username/my-results/result.tar",
    name="my-results",
)
print(result["upload"]["id"])
```

## License

[MIT](LICENSE).
