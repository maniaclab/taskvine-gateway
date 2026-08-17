from fastapi import Header, HTTPException
from jupyterhub.services.auth import HubAuth

# HubAuth reads JUPYTERHUB_API_TOKEN/JUPYTERHUB_API_URL from the environment -
# that's this service's *own* token, registered as a JupyterHub service
# (mirrors hub.services.dask-gateway.apiToken), used to call back to the Hub
# and validate the *caller's* token on each request.
hub_auth = HubAuth()


def get_username(authorization: str = Header(...)) -> str:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "token" or not token:
        raise HTTPException(status_code=401, detail="Expected 'Authorization: token <JUPYTERHUB_API_TOKEN>'")

    user = hub_auth.user_for_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired JupyterHub token")

    return user["name"]
