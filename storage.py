# storage.py
#
# Storage/secrets backend with pluggable storage adapters:
#   - LocalStorage (default): keeps everything on the local filesystem
#   - S3Storage: stores to AWS S3 (lazy boto3 import; does not require AWS credentials to run locally)
#
# Backend is selected via STORAGE_BACKEND environment variable ("local" or "s3").

import os
import io
import json
import shutil
from abc import ABC, abstractmethod


class StorageAdapter(ABC):
    """Abstract base storage adapter interface."""

    @abstractmethod
    def put_file(self, local_path, dest_key):
        """Uploads/copies a local file to dest_key."""
        pass

    @abstractmethod
    def open(self, path_or_key, mode="r", encoding=None):
        """Opens and returns a file-like object."""
        pass

    @abstractmethod
    def list(self, prefix=""):
        """Lists items with the given prefix."""
        pass

    @abstractmethod
    def exists(self, path_or_key):
        """Checks if the item exists."""
        pass

    @abstractmethod
    def get_download_link(self, path_or_key):
        """Returns a download link or local file path."""
        pass


class LocalStorage(StorageAdapter):
    """Local filesystem storage adapter."""

    def __init__(self, base_dir="data/runs"):
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)

    def _resolve(self, path_or_key):
        if os.path.isabs(path_or_key):
            return path_or_key
        return os.path.normpath(os.path.join(self.base_dir, path_or_key))

    def put_file(self, local_path, dest_key):
        dest_path = self._resolve(dest_key)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        if os.path.abspath(local_path) != os.path.abspath(dest_path):
            shutil.copy2(local_path, dest_path)
        return dest_path

    def open(self, path_or_key, mode="r", encoding=None):
        resolved = self._resolve(path_or_key)
        if "b" in mode:
            return open(resolved, mode=mode)
        return open(resolved, mode=mode, encoding=encoding or "utf-8")

    def list(self, prefix=""):
        resolved_prefix = self._resolve(prefix)
        search_dir = resolved_prefix if os.path.isdir(resolved_prefix) else os.path.dirname(resolved_prefix)
        if not os.path.exists(search_dir):
            return []
        results = []
        for root, dirs, files in os.walk(search_dir):
            for f in files:
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, self.base_dir)
                if rel_path.startswith(prefix.replace("\\", "/")):
                    results.append(rel_path.replace("\\", "/"))
        return results

    def exists(self, path_or_key):
        return os.path.exists(self._resolve(path_or_key))

    def get_download_link(self, path_or_key):
        return self._resolve(path_or_key)


class S3Storage(StorageAdapter):
    """S3 storage adapter with lazy boto3 import."""

    def __init__(self, bucket=None, region="us-east-1"):
        self.bucket = bucket or os.environ.get("S3_BUCKET", "")
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._s3 = None

    def _get_client(self):
        if self._s3 is None:
            import boto3
            self._s3 = boto3.client("s3", region_name=self.region)
        return self._s3

    def put_file(self, local_path, dest_key):
        if not self.bucket:
            raise ValueError("S3_BUCKET is not configured.")
        client = self._get_client()
        content_type = "application/octet-stream"
        if dest_key.endswith(".json"):
            content_type = "application/json"
        elif dest_key.endswith(".ndjson"):
            content_type = "application/x-ndjson"
        elif dest_key.endswith(".xlsx"):
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif dest_key.endswith(".csv"):
            content_type = "text/csv"

        with open(local_path, "rb") as f:
            client.put_object(
                Bucket=self.bucket,
                Key=dest_key.replace("\\", "/"),
                Body=f,
                ContentType=content_type,
            )
        return f"s3://{self.bucket}/{dest_key.replace('\\', '/')}"

    def open(self, path_or_key, mode="r", encoding=None):
        if not self.bucket:
            raise ValueError("S3_BUCKET is not configured.")
        client = self._get_client()
        key = path_or_key.replace(f"s3://{self.bucket}/", "").replace("\\", "/")
        response = client.get_object(Bucket=self.bucket, Key=key)
        body_stream = response["Body"]
        if "b" in mode:
            return body_stream
        return io.TextIOWrapper(body_stream, encoding=encoding or "utf-8")

    def list(self, prefix=""):
        if not self.bucket:
            return []
        client = self._get_client()
        paginator = client.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix.replace("\\", "/")):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys

    def exists(self, path_or_key):
        if not self.bucket:
            return False
        client = self._get_client()
        key = path_or_key.replace(f"s3://{self.bucket}/", "").replace("\\", "/")
        try:
            client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def get_download_link(self, path_or_key):
        if not self.bucket:
            return path_or_key
        client = self._get_client()
        key = path_or_key.replace(f"s3://{self.bucket}/", "").replace("\\", "/")
        try:
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=3600,
            )
        except Exception:
            return f"s3://{self.bucket}/{key}"


def get_storage_adapter(backend=None):
    """Factory returning LocalStorage (default) or S3Storage based on STORAGE_BACKEND."""
    backend = backend or os.environ.get("STORAGE_BACKEND", "local").lower()
    if backend == "s3":
        return S3Storage()
    return LocalStorage()


# ---------------------------------------------------------------------------
# Backward compatibility functions
# ---------------------------------------------------------------------------
def save_to_s3(data, bucket, key, region="us-east-1"):
    import boto3
    s3 = boto3.client("s3", region_name=region)
    body = json.dumps(data, indent=2).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    return f"s3://{bucket}/{key}"


def list_secret_instances(prefix, region="us-east-1"):
    """Returns secret names under the given prefix, e.g. 'cpq-extractor/prod'."""
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    names = []
    paginator = client.get_paginator("list_secrets")
    for page in paginator.paginate():
        for s in page.get("SecretList", []):
            if s["Name"].startswith(prefix):
                names.append(s["Name"])
    return names


def get_secret_instance(secret_name, region="us-east-1"):
    """Fetches a secret and returns an instance dict shaped for sf_auth.connect()."""
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_name)
    secret = json.loads(response["SecretString"])

    if "client_id" in secret and "client_secret" in secret:
        secret["auth_method"] = "client_credentials"
    elif "username" in secret and "password" in secret:
        secret["auth_method"] = "password"
    else:
        raise ValueError(f"Secret '{secret_name}' doesn't match a known credential shape.")

    secret.setdefault("domain", "login")
    return secret
