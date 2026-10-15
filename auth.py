"""Identity providers.

Both providers return the same claim shape an Entra ID token carries:
    {"oid": ..., "preferred_username": ..., "name": ..., "groups": [group object ids]}
so the rest of the app doesn't care which one is in use.
"""
import json
from pathlib import Path
from urllib.parse import quote

from werkzeug.security import check_password_hash, generate_password_hash


class AuthError(Exception):
    pass


class MockEntra:
    """Simulated Entra ID backed by sample_data/mock_entra_directory.json."""

    interactive = False  # login form is rendered by this app

    def __init__(self, directory_path):
        data = json.loads(Path(directory_path).read_text(encoding="utf-8"))
        self.demo_password = data["demo_password"]
        pw_hash = generate_password_hash(self.demo_password)
        self.users = {u["upn"].lower(): {**u, "password_hash": pw_hash} for u in data["users"]}

    def authenticate(self, upn, password):
        user = self.users.get((upn or "").strip().lower())
        if not user or not check_password_hash(user["password_hash"], password or ""):
            raise AuthError("Unknown user or wrong password.")
        return self._claims(user)

    def current_groups(self, oid):
        """Live group lookup, used to re-check membership at sign-off time."""
        for user in self.users.values():
            if user["oid"] == oid:
                return list(user["groups"])
        return []

    def demo_accounts(self):
        return [{"upn": u["upn"], "name": u["name"], "scenario": u["scenario"]} for u in self.users.values()]

    @staticmethod
    def _claims(user):
        return {
            "oid": user["oid"],
            "preferred_username": user["upn"],
            "name": user["name"],
            "groups": list(user["groups"]),
        }


class EntraAuth:
    """Real Microsoft Entra ID sign-in (OIDC auth code flow via MSAL).

    App registration setup:
      * Web redirect URI: https://<host>/auth/callback
      * Token configuration -> Add groups claim -> "Groups assigned to the application"
        (security groups, emitted as object IDs). Assign the SG-CAPREVIEW-* groups to the
        enterprise app. This keeps tokens small and avoids the >200 groups "overage" case.
    """

    interactive = True  # login happens on Microsoft's page

    def __init__(self, tenant_id, client_id, client_secret):
        import msal  # only needed in Entra mode

        self.authority = f"https://login.microsoftonline.com/{tenant_id}"
        self.app = msal.ConfidentialClientApplication(
            client_id, authority=self.authority, client_credential=client_secret
        )
        self.scopes = ["User.Read"]

    def begin(self, redirect_uri):
        """Returns the MSAL flow dict; store it in the session and redirect to flow['auth_uri']."""
        return self.app.initiate_auth_code_flow(self.scopes, redirect_uri=redirect_uri)

    def complete(self, flow, query_args):
        try:
            result = self.app.acquire_token_by_auth_code_flow(flow, dict(query_args))
        except ValueError as exc:  # state mismatch, replayed callback, etc.
            raise AuthError(str(exc)) from exc
        if "error" in result:
            raise AuthError(result.get("error_description") or result["error"])

        claims = result["id_token_claims"]
        if "groups" not in claims and "_claim_names" in claims:
            raise AuthError(
                "Too many groups for the token (overage). Configure the app registration to emit "
                "only 'Groups assigned to the application'."
            )
        return {
            "oid": claims["oid"],
            "preferred_username": claims.get("preferred_username", ""),
            "name": claims.get("name", claims.get("preferred_username", "")),
            "groups": claims.get("groups", []),
        }

    def current_groups(self, oid):
        # Without a Graph token we rely on the groups in the ID token for the session lifetime.
        # To re-check live, request GroupMember.Read.All and call /me/checkMemberGroups here.
        return None

    def logout_url(self, post_logout_redirect):
        return f"{self.authority}/oauth2/v2.0/logout?post_logout_redirect_uri={quote(post_logout_redirect, safe='')}"
