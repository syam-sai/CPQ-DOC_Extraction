# config.py
#
# Central place for environment-driven settings.
# Locally these come from a .env file (loaded via python-dotenv).
# In AWS (ECS Fargate), the same variable names are set as task
# environment variables / injected secrets — no code change needed.

import os
from dotenv import load_dotenv

load_dotenv()  # no-op in AWS if there's no .env file present

# "local" or "aws" — controls whether we show manual credential entry
# and/or the "load from Secrets Manager" panel.
APP_ENV = os.environ.get("APP_ENV", "local")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# If set, every extraction is also written to this S3 bucket in addition
# to being offered as a browser download. Leave blank to skip S3 entirely
# (e.g. for pure local testing).
S3_BUCKET = os.environ.get("S3_BUCKET", "")

# Secrets in AWS Secrets Manager are expected to be named "{prefix}{instance_name}"
# e.g. "cpq-extractor/prod", "cpq-extractor/sandbox1"
SECRETS_PREFIX = os.environ.get("SECRETS_PREFIX", "cpq-extractor/")
