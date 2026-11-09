"""Validate, package and upload inference results without creating an evaluation."""

import argparse
import getpass
import os
from pathlib import Path

import httpx

from aec_upload import UploadClient, UploadError, ValidationError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Directory containing submission.jsonl and WAVs")
    parser.add_argument("--competition", required=True, choices=["aec_single_model", "aec_agent"])
    parser.add_argument("--archive", type=Path, default=Path("result.tar"))
    parser.add_argument("--name", required=True, help="Name shown in Ready Uploads")
    args = parser.parse_args()
    base_url = os.environ.get("AEC_BASE_URL") or getpass.getpass("Platform base URL (hidden): ")
    secret_key = os.environ.get("AEC_SECRET_KEY") or getpass.getpass("Secret Key: ")
    if not base_url.strip() or not secret_key.strip():
        parser.error("A platform URL and Secret Key are required")
    try:
        with UploadClient(base_url.strip(), secret_key.strip(), args.competition) as client:
            print("Validating inference results and packaging tar…")
            path = client.pack(args.directory, args.archive)
            print("Validating tar and uploading…")
            result = client.upload(path, name=args.name)
            print(f"Ready Upload ID: {result['upload']['id']}")
            print("Select this Upload on the platform to start evaluation.")
    except (UploadError, ValidationError, OSError) as exc:
        print(f"Failed: {exc}")
        return 1
    except httpx.HTTPError:
        # Network exception text may contain the private platform URL.
        print("Network request failed. Check connectivity; check Ready Uploads before retrying.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
