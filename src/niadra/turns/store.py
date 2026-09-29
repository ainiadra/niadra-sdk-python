"""Where the recorded values go in `pointer` mode: the company's own storage, never Niadra.

The sender writes each value with the company's credentials, then sends only the pointer and the digest.
A store is any callable `put(key, data) -> pointer`: `key` is a path unique to the turn and the value,
`data` the value's canonical JSON, and `pointer` a URI the company's replay runner can read back
(`s3://bucket/key`). `S3Store` writes to an S3 bucket, or anything that speaks its protocol, with boto3.
"""

from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlsplit


class BlobStore(Protocol):
    def __call__(self, key: str, data: bytes) -> str:
        """Writes `data` under `key` and returns the pointer to it. Raises when it could not."""
        ...


class S3Store:
    """Writes values to `s3://bucket/prefix/<turn_id>/<blob>.json` with boto3.

    `client` is a boto3 S3 client, which carries the company's credentials and region; by default one is
    made from the environment, as boto3 finds it. boto3 is not a dependency of the SDK: install it to use
    this store.
    """

    def __init__(self, bucket: str, *, client: Any = None) -> None:
        parts = urlsplit(bucket if "://" in bucket else f"s3://{bucket}")
        if parts.scheme != "s3" or not parts.netloc:
            raise ValueError("pass the bucket as `s3://bucket` or `s3://bucket/prefix`")
        self.bucket = parts.netloc
        self.prefix = parts.path.strip("/")
        self._client = client

    def __call__(self, key: str, data: bytes) -> str:
        full = f"{self.prefix}/{key}" if self.prefix else key
        self._s3().put_object(Bucket=self.bucket, Key=full, Body=data, ContentType="application/json")
        return f"s3://{self.bucket}/{full}"

    def _s3(self) -> Any:
        if self._client is None:
            import boto3  # only a company that keeps its values in S3 needs it

            self._client = boto3.client("s3")
        return self._client
