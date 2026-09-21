# sf_connection.py
from simple_salesforce import Salesforce
from dotenv import load_dotenv
import os
from pathlib import Path

load_dotenv(Path(__file__).with_name(".env"))

def get_sf_connection():
    """
    Creates and returns an authenticated Salesforce connection.
    Credentials are pulled from environment variables — never hardcode them.
    """
    sf = Salesforce(
        username=os.environ["SF_USERNAME"],
        password=os.environ["SF_PASSWORD"],
        security_token=os.environ["SF_SECURITY_TOKEN"],
        domain=os.environ.get("SF_DOMAIN", "login")  # use "test" for sandbox
    )
    return sf

if __name__ == "__main__":
    sf = get_sf_connection()
    print("Connected successfully.")
    print("Org ID:", sf.sf_instance)