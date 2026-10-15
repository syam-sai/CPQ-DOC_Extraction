# sf_auth.py
#
# Supports both Salesforce auth methods behind one connect() call:
#
#   auth_method = "password"           -> username + password + security token
#                                          (works today, but Salesforce is
#                                          actively retiring this flow — use
#                                          only until Client Credentials is
#                                          available to you)
#
#   auth_method = "client_credentials" -> Consumer Key + Consumer Secret
#                                          (recommended long-term — no user
#                                          password ever touches this app)
#
# Both return a ready-to-use simple_salesforce.Salesforce object, so nothing
# else in the codebase needs to know which method was used.

import requests
from simple_salesforce import Salesforce
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


AUTH_TIMEOUT_SECONDS = 30


class SalesforceAuthSession(requests.Session):
    """Use bounded requests and retry transient Salesforce gateway failures."""

    def __init__(self):
        super().__init__()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"POST"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.mount("https://", adapter)
        self.mount("http://", adapter)

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", AUTH_TIMEOUT_SECONDS)
        return super().request(method, url, **kwargs)


def connect_client_credentials(client_id, client_secret, domain="login"):
    """OAuth 2.0 Client Credentials Flow — server-to-server, no user password."""
    url = f"https://{domain}.salesforce.com/services/oauth2/token"
    session = SalesforceAuthSession()
    response = session.post(url, data={
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    })
    response.raise_for_status()
    data = response.json()
    return Salesforce(instance_url=data["instance_url"], session_id=data["access_token"])


def connect_username_password(username, password, security_token="", domain="login"):
    """Legacy Username-Password Flow — being retired by Salesforce, use as a fallback only."""
    return Salesforce(
        username=username,
        password=password,
        security_token=security_token,
        domain=domain,
        session=SalesforceAuthSession(),
    )


def connect(instance):
    """
    instance: dict shaped one of two ways —

    Client credentials:
        {"auth_method": "client_credentials", "client_id": "...",
         "client_secret": "...", "domain": "login"}

    Username/password:
        {"auth_method": "password", "username": "...", "password": "...",
         "security_token": "...", "domain": "login"}
    """
    method = instance.get("auth_method", "password")
    domain = instance.get("domain", "login")

    if method == "client_credentials":
        return connect_client_credentials(
            instance["client_id"], instance["client_secret"], domain
        )
    elif method == "password":
        return connect_username_password(
            instance["username"],
            instance["password"],
            instance.get("security_token", ""),
            domain,
        )
    else:
        raise ValueError(f"Unknown auth_method: {method}")
