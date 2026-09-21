# storage.py
#
# Storage/secrets backend. Designed so local testing NEVER requires AWS
# credentials at all — S3 and Secrets Manager functions are only called
# when the app is configured to use them (S3_BUCKET set, or the user
# clicks "load from Secrets Manager").

import json


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
        for s in page["SecretList"]:
            if s["Name"].startswith(prefix):
                names.append(s["Name"])
    return names


def get_secret_instance(secret_name, region="us-east-1"):
    """
    Fetches a secret and returns an `instance` dict shaped for sf_auth.connect().
    Expects the secret's JSON to contain either:
      {"client_id": "...", "client_secret": "...", "domain": "login"}
    or
      {"username": "...", "password": "...", "security_token": "...", "domain": "login"}
    """
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
