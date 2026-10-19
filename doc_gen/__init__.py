# doc_gen package initialization
"""Client document generation package for Salesforce CPQ extraction."""

from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root before any submodule reads os.environ.
# Submodules capture provider/model/concurrency settings at import time, so
# this must run first. No-op in AWS where these are real environment variables.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

__version__ = "1.0.0"