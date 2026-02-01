"""
S3-compatible storage backend.

Supports AWS S3 and S3-compatible services like MinIO.
"""

from io import BytesIO
from typing import BinaryIO, Iterator, Optional, Union

import boto3
from botocore.exceptions import ClientError
import structlog

from config.settings import get_settings
from storage.base import StorageBackend

logger = structlog.get_logger(__name__)


class S3Storage(StorageBackend):
    """
    S3-compatible storage backend.

    Works with AWS S3 and S3-compatible services like MinIO.
    """

    def __init__(
        self,
        bucket: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        region: Optional[str] = None,
    ):
        """
        Initialize S3 storage.

        Args:
            bucket: S3 bucket name
            endpoint_url: S3 endpoint URL (for MinIO)
            access_key: AWS access key
            secret_key: AWS secret key
            region: AWS region
        """
        settings = get_settings()

        self.bucket = bucket or settings.s3_bucket
        self.endpoint_url = endpoint_url or settings.s3_endpoint_url
        self.region = region or settings.s3_region

        # Create S3 client
        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=access_key or settings.s3_access_key,
            aws_secret_access_key=secret_key or settings.s3_secret_key,
            region_name=self.region,
        )

        # Ensure bucket exists
        self._ensure_bucket()

        logger.info(
            "s3_storage_initialized",
            bucket=self.bucket,
            endpoint=self.endpoint_url,
        )

    def _ensure_bucket(self):
        """Ensure the bucket exists, create if not."""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "404":
                # Bucket doesn't exist, create it
                try:
                    if self.region and self.region != "us-east-1":
                        self.client.create_bucket(
                            Bucket=self.bucket,
                            CreateBucketConfiguration={
                                "LocationConstraint": self.region
                            },
                        )
                    else:
                        self.client.create_bucket(Bucket=self.bucket)
                    logger.info("created_s3_bucket", bucket=self.bucket)
                except ClientError as create_error:
                    logger.error(
                        "s3_bucket_creation_failed",
                        bucket=self.bucket,
                        error=str(create_error),
                    )
                    raise
            else:
                raise

    def put(
        self,
        key: str,
        data: Union[bytes, BinaryIO, str],
        content_type: Optional[str] = None,
    ) -> str:
        """Store data at the given key."""
        # Normalize key
        key = key.lstrip("/")

        try:
            extra_args = {}
            if content_type:
                extra_args["ContentType"] = content_type

            if isinstance(data, str):
                data = data.encode("utf-8")
                if not content_type:
                    extra_args["ContentType"] = "text/plain; charset=utf-8"

            if isinstance(data, bytes):
                body = BytesIO(data)
            else:
                body = data

            self.client.upload_fileobj(
                body,
                self.bucket,
                key,
                ExtraArgs=extra_args if extra_args else None,
            )

            logger.debug("stored_to_s3", key=key, bucket=self.bucket)
            return f"s3://{self.bucket}/{key}"

        except ClientError as e:
            logger.error("s3_put_error", key=key, error=str(e))
            raise

    def get(self, key: str) -> Optional[bytes]:
        """Retrieve data from the given key."""
        key = key.lstrip("/")

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "NoSuchKey":
                return None
            logger.error("s3_get_error", key=key, error=str(e))
            return None

    def exists(self, key: str) -> bool:
        """Check if a key exists."""
        key = key.lstrip("/")

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def delete(self, key: str) -> bool:
        """Delete data at the given key."""
        key = key.lstrip("/")

        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            logger.debug("deleted_from_s3", key=key)
            return True
        except ClientError as e:
            logger.error("s3_delete_error", key=key, error=str(e))
            return False

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        """List all keys with the given prefix."""
        prefix = prefix.lstrip("/")

        try:
            paginator = self.client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self.bucket, Prefix=prefix)

            for page in pages:
                for obj in page.get("Contents", []):
                    yield obj["Key"]

        except ClientError as e:
            logger.error("s3_list_error", prefix=prefix, error=str(e))

    def get_url(self, key: str) -> str:
        """Get a URL for accessing the stored data."""
        key = key.lstrip("/")

        if self.endpoint_url:
            return f"{self.endpoint_url}/{self.bucket}/{key}"
        return f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{key}"

    def generate_presigned_url(
        self,
        key: str,
        expiration: int = 3600,
    ) -> Optional[str]:
        """
        Generate a presigned URL for temporary access.

        Args:
            key: Storage key
            expiration: URL expiration in seconds

        Returns:
            Presigned URL or None on error
        """
        key = key.lstrip("/")

        try:
            url = self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expiration,
            )
            return url
        except ClientError as e:
            logger.error("presigned_url_error", key=key, error=str(e))
            return None

    def copy(self, src_key: str, dst_key: str) -> bool:
        """Copy data from one key to another."""
        src_key = src_key.lstrip("/")
        dst_key = dst_key.lstrip("/")

        try:
            self.client.copy_object(
                Bucket=self.bucket,
                Key=dst_key,
                CopySource={"Bucket": self.bucket, "Key": src_key},
            )
            return True
        except ClientError as e:
            logger.error("s3_copy_error", src=src_key, dst=dst_key, error=str(e))
            return False
