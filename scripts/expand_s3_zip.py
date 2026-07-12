from __future__ import annotations

import argparse
import mimetypes
from pathlib import PurePosixPath
import tempfile
import zipfile

from boto3.s3.transfer import TransferConfig

from titles_api.integrations.config import S3Settings
from titles_api.storage.s3_client import create_source_s3_client


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("key")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--endpoint-url", required=True)
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--credential-prefix", default="MEGA")
    args = parser.parse_args()
    parent = str(PurePosixPath(args.key).parent)
    settings = S3Settings.for_source(endpoint_url=args.endpoint_url, bucket=args.bucket, allowed_prefixes=(parent + "/",), region=args.region, addressing_style="auto", credential_env_prefix=args.credential_prefix)
    client = create_source_s3_client(settings)
    head = client.head_object(Bucket=args.bucket, Key=args.key)
    etag = str(head.get("ETag", "")).strip('"')
    with tempfile.NamedTemporaryFile(suffix=".zip") as archive_file:
        client.download_fileobj(args.bucket, args.key, archive_file)
        archive_file.flush()
        archive_file.seek(0)
        with zipfile.ZipFile(archive_file) as archive:
            uploaded = 0
            for member in archive.infolist():
                path = PurePosixPath(member.filename)
                if member.is_dir() or path.is_absolute() or ".." in path.parts:
                    continue
                destination = f"{parent}/expanded/{path}"
                try:
                    existing = client.head_object(Bucket=args.bucket, Key=destination)
                    if int(existing.get("ContentLength", -1)) == member.file_size and existing.get("Metadata", {}).get("titles-archive-etag") == etag:
                        continue
                except Exception:
                    pass
                extra = {"Metadata": {"titles-archive-key": args.key, "titles-archive-etag": etag}}
                content_type = mimetypes.guess_type(str(path))[0]
                if content_type:
                    extra["ContentType"] = content_type
                with archive.open(member) as body:
                    # Mega S4 rejects a retried multipart part as InvalidPart.
                    # Dataset images are small enough for one atomic PutObject.
                    client.upload_fileobj(
                        body,
                        args.bucket,
                        destination,
                        ExtraArgs=extra,
                        Config=TransferConfig(use_threads=False, multipart_threshold=5 * 1024**3),
                    )
                uploaded += 1
                print({"uploaded": uploaded, "member": member.filename}, flush=True)


if __name__ == "__main__":
    main()
