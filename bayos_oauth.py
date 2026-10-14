"""Single-user OAuth 2.1 authorization server for MCP clients.

ChatGPT and Claude custom connectors do not send a static API key. They use
the authorization-code flow with PKCE and dynamic client registration. This
module issues opaque tokens after the operator types the BayOS gateway key on
the consent page. Only hashes of codes and tokens are stored.
"""

import base64
import hashlib
import hmac
import html
import os
import secrets
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlsplit

SCOPE = "bayos"
ACCESS_TTL_SECONDS = 900
REFRESH_TTL_SECONDS = 30 * 24 * 3600
CODE_TTL_SECONDS = 60
_CHALLENGE_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
# Hosted Claude apps. claude.ai is the fixed URI in Anthropic's connector docs.
# claude.com is the second exact callback used by custom connectors.
_CLAUDE_HOSTED_REDIRECTS = {
    ("claude.ai", "/api/mcp/auth_callback"),
    ("claude.com", "/api/mcp/auth_callback"),
}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pkce(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def resource_for(origin: str) -> str:
    return origin.rstrip("/") + "/mcp"


def metadata_url(origin: str) -> str:
    return origin.rstrip("/") + "/.well-known/oauth-protected-resource/mcp"


def authorization_server_metadata(origin: str) -> Dict[str, Any]:
    base = origin.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE],
        "authorization_response_iss_parameter_supported": True,
    }


def protected_resource_metadata(origin: str) -> Dict[str, Any]:
    base = origin.rstrip("/")
    return {
        "resource": resource_for(base),
        "authorization_servers": [base],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
    }


def www_authenticate(origin: str) -> str:
    quoted = metadata_url(origin).replace('"', "")
    return f'Bearer resource_metadata="{quoted}", scope="{SCOPE}", error="invalid_token", error_description="Authorization is required."'


def _hosted_claude_redirect(parsed) -> bool:
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.query:
        return False
    return (host, parsed.path) in _CLAUDE_HOSTED_REDIRECTS


def _claude_code_loopback(parsed) -> bool:
    """Claude Code's documented loopback: http://localhost[:port]/callback or 127.0.0.1."""
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "http" or host not in {"localhost", "127.0.0.1"}:
        return False
    if parsed.path != "/callback" or parsed.query:
        return False
    return True


def redirect_allowed(uri: str) -> bool:
    parsed = urlsplit(uri)
    if parsed.username or parsed.password or parsed.fragment:
        return False
    host = (parsed.hostname or "").lower()
    if _hosted_claude_redirect(parsed) or _claude_code_loopback(parsed):
        return True
    if parsed.scheme == "https" and host == "chatgpt.com":
        if parsed.path == "/connector_platform_oauth_redirect":
            return True
        prefix = "/connector/oauth/"
        if parsed.path.startswith(prefix) and len(parsed.path) > len(prefix):
            return True
        return False
    if os.environ.get("BAYOS_OAUTH_ALLOW_LOCAL", "") == "1" and parsed.scheme == "http" and host in {"127.0.0.1", "localhost"}:
        return True
    return False


def _challenge_ok(value: str) -> bool:
    if not value or len(value) < 43 or len(value) > 128:
        return False
    return all(ch in _CHALLENGE_CHARS for ch in value)


class OAuthStore:
    """Firestore when the inbox uses Firestore. Memory otherwise, including tests."""

    def __init__(self) -> None:
        self._clients: Dict[str, Dict[str, Any]] = {}
        self._codes: Dict[str, Dict[str, Any]] = {}
        self._tokens: Dict[str, Dict[str, Any]] = {}
        self._client: Any = None

    def reset_for_tests(self) -> None:
        self._clients.clear()
        self._codes.clear()
        self._tokens.clear()
        self._client = None

    def _firestore(self) -> bool:
        forced = os.environ.get("BAYOS_INBOX_STORAGE", "").strip().lower()
        if forced == "memory":
            return False
        if os.environ.get("BAYOS_OAUTH_STORAGE", "").strip().lower() == "memory":
            return False
        return bool(os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip())

    def _fs(self) -> Any:
        if self._client is None:
            from google.cloud import firestore

            project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
            self._client = firestore.Client(project=project)
        return self._client

    def _put(self, collection: str, doc_id: str, payload: Dict[str, Any], memory: Dict[str, Dict[str, Any]]) -> None:
        if self._firestore():
            self._fs().collection(collection).document(doc_id).set(payload, timeout=8)
            return
        memory[doc_id] = dict(payload)

    def _get(self, collection: str, doc_id: str, memory: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if self._firestore():
            snapshot = self._fs().collection(collection).document(doc_id).get(timeout=8)
            if not getattr(snapshot, "exists", False):
                return None
            return snapshot.to_dict() or {}
        found = memory.get(doc_id)
        return dict(found) if found else None

    def _delete(self, collection: str, doc_id: str, memory: Dict[str, Dict[str, Any]]) -> None:
        if self._firestore():
            self._fs().collection(collection).document(doc_id).delete(timeout=8)
            return
        memory.pop(doc_id, None)

    def save_client(self, client_id: str, record: Dict[str, Any]) -> None:
        self._put("oauth_clients", client_id, record, self._clients)

    def get_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        if not client_id or len(client_id) > 128:
            return None
        return self._get("oauth_clients", client_id, self._clients)

    def save_code(self, code: str, record: Dict[str, Any]) -> None:
        self._put("oauth_codes", _hash(code), record, self._codes)

    def take_code(self, code: str) -> Optional[Dict[str, Any]]:
        key = _hash(code)
        record = self._get("oauth_codes", key, self._codes)
        if record is None:
            return None
        self._delete("oauth_codes", key, self._codes)
        if int(record.get("expires_at", 0)) < int(time.time()):
            return None
        return record

    def save_token(self, token: str, record: Dict[str, Any]) -> None:
        self._put("oauth_tokens", _hash(token), record, self._tokens)

    def get_token(self, token: str, kind: str) -> Optional[Dict[str, Any]]:
        if not token or len(token) > 512:
            return None
        record = self._get("oauth_tokens", _hash(token), self._tokens)
        if not record or record.get("kind") != kind:
            return None
        if int(record.get("expires_at", 0)) < int(time.time()):
            self._delete("oauth_tokens", _hash(token), self._tokens)
            return None
        return record

    def delete_token(self, token: str) -> None:
        if token:
            self._delete("oauth_tokens", _hash(token), self._tokens)


store = OAuthStore()


def register_client(body: Dict[str, Any]) -> Dict[str, Any]:
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > 5:
        raise ValueError("redirect_uris")
    cleaned: List[str] = []
    for uri in uris:
        if not isinstance(uri, str) or not redirect_allowed(uri):
            raise ValueError("redirect_uri")
        if uri not in cleaned:
            cleaned.append(uri)
    method = body.get("token_endpoint_auth_method") or "none"
    if method != "none":
        raise ValueError("token_endpoint_auth_method")
    name = body.get("client_name") or "ChatGPT"
    if not isinstance(name, str):
        raise ValueError("client_name")
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n")[:80] or "ChatGPT"
    client_id = secrets.token_urlsafe(24)
    store.save_client(
        client_id,
        {
            "redirect_uris": cleaned,
            "token_endpoint_auth_method": "none",
            "client_name": name,
            "created_at": int(time.time()),
        },
    )
    return {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": cleaned,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": name,
        "scope": SCOPE,
    }


def _scope_ok(raw: Optional[str]) -> bool:
    if raw is None or raw == "":
        return True
    return raw == SCOPE


def validate_authorize(origin: str, params: Dict[str, str]) -> Optional[str]:
    """Return an OAuth error code, or None when the request can show consent."""
    client = store.get_client(params.get("client_id", ""))
    redirect = params.get("redirect_uri", "")
    if client is None or redirect not in client.get("redirect_uris", []) or not redirect_allowed(redirect):
        return "invalid_request"
    if params.get("response_type") != "code":
        return "unsupported_response_type"
    if not _scope_ok(params.get("scope")):
        return "invalid_scope"
    if params.get("code_challenge_method") != "S256" or not _challenge_ok(params.get("code_challenge", "")):
        return "invalid_request"
    if params.get("resource") != resource_for(origin):
        return "invalid_target"
    state = params.get("state", "")
    if not state or len(state) > 512 or any(ord(ch) < 32 for ch in state):
        return "invalid_request"
    return None


def issue_code(origin: str, params: Dict[str, str]) -> str:
    code = secrets.token_urlsafe(32)
    store.save_code(
        code,
        {
            "client_id": params["client_id"],
            "redirect_uri": params["redirect_uri"],
            "challenge": params["code_challenge"],
            "resource": resource_for(origin),
            "scope": SCOPE,
            "expires_at": int(time.time()) + CODE_TTL_SECONDS,
        },
    )
    return code


def authorization_redirect(origin: str, params: Dict[str, str], code: str) -> str:
    query = urlencode(
        {
            "code": code,
            "state": params.get("state", ""),
            "iss": origin.rstrip("/"),
        }
    )
    separator = "&" if "?" in params["redirect_uri"] else "?"
    return params["redirect_uri"] + separator + query


def error_redirect(origin: str, params: Dict[str, str], error: str) -> Optional[str]:
    client = store.get_client(params.get("client_id", ""))
    redirect = params.get("redirect_uri", "")
    if client is None or redirect not in client.get("redirect_uris", []) or not redirect_allowed(redirect):
        return None
    query = {"error": error, "iss": origin.rstrip("/")}
    if params.get("state"):
        query["state"] = params["state"]
    separator = "&" if "?" in redirect else "?"
    return redirect + separator + urlencode(query)


def _verifier_matches(verifier: str, challenge: str) -> bool:
    if not _challenge_ok(verifier) or not _challenge_ok(challenge):
        return False
    try:
        computed = _pkce(verifier)
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(computed, challenge)


def _issue_pair(client_id: str, resource: str) -> Dict[str, Any]:
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    now = int(time.time())
    family = secrets.token_urlsafe(12)
    store.save_token(
        access,
        {
            "kind": "access",
            "client_id": client_id,
            "resource": resource,
            "scope": SCOPE,
            "expires_at": now + ACCESS_TTL_SECONDS,
            "family": family,
        },
    )
    store.save_token(
        refresh,
        {
            "kind": "refresh",
            "client_id": client_id,
            "resource": resource,
            "scope": SCOPE,
            "expires_at": now + REFRESH_TTL_SECONDS,
            "family": family,
        },
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_SECONDS,
        "refresh_token": refresh,
        "scope": SCOPE,
    }


def exchange_code(origin: str, form: Dict[str, str]) -> Dict[str, Any]:
    record = store.take_code(form.get("code", ""))
    if record is None:
        raise ValueError("invalid_grant")
    if form.get("client_id") != record.get("client_id"):
        raise ValueError("invalid_grant")
    if form.get("redirect_uri") != record.get("redirect_uri"):
        raise ValueError("invalid_grant")
    if form.get("resource") != resource_for(origin) or record.get("resource") != resource_for(origin):
        raise ValueError("invalid_grant")
    if not _verifier_matches(form.get("code_verifier", ""), str(record.get("challenge", ""))):
        raise ValueError("invalid_grant")
    return _issue_pair(str(record["client_id"]), resource_for(origin))


def exchange_refresh(origin: str, form: Dict[str, str]) -> Dict[str, Any]:
    presented = form.get("refresh_token", "")
    record = store.get_token(presented, "refresh")
    if record is None:
        raise ValueError("invalid_grant")
    if form.get("client_id") != record.get("client_id"):
        raise ValueError("invalid_grant")
    if form.get("resource") != resource_for(origin) or record.get("resource") != resource_for(origin):
        raise ValueError("invalid_grant")
    store.delete_token(presented)
    return _issue_pair(str(record["client_id"]), resource_for(origin))


def access_record(origin: str, token: str) -> Optional[Dict[str, Any]]:
    record = store.get_token(token, "access")
    if record is None:
        return None
    if record.get("resource") != resource_for(origin) or record.get("scope") != SCOPE:
        return None
    return record


def keys_match(expected: str, presented: str) -> bool:
    if not expected or len(presented) != len(expected):
        return False
    return hmac.compare_digest(presented, expected)


def client_display_name(client_id: str) -> str:
    client = store.get_client(client_id)
    if not client:
        return "This app"
    name = client.get("client_name") or ""
    if not isinstance(name, str) or not name.strip():
        return "This app"
    return name.strip()


def consent_page(params: Dict[str, str], message: str, nonce: str) -> str:
    hidden = []
    for name in (
        "response_type",
        "client_id",
        "redirect_uri",
        "state",
        "code_challenge",
        "code_challenge_method",
        "scope",
        "resource",
    ):
        value = html.escape(params.get(name, ""), quote=True)
        hidden.append(f'<input type="hidden" name="{name}" value="{value}">')
    notice = f'<p class="error">{html.escape(message)}</p>' if message else ""
    label = html.escape(client_display_name(params.get("client_id", "")), quote=True)
    redirect = urlsplit(params.get("redirect_uri", ""))
    host = html.escape(redirect.hostname or "unknown", quote=True)
    loopback = (redirect.hostname or "").lower() in {"localhost", "127.0.0.1"}
    local_note = (
        "<p>This sign-in redirects to this computer. Allow it only if you started it.</p>"
        if loopback
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BayOS consent</title>
<style nonce="{html.escape(nonce, quote=True)}">
  body {{ margin: 0; font-family: system-ui, sans-serif; background: #0f1419; color: #e7ecf1; }}
  main {{ max-width: 28rem; margin: 0 auto; padding: 24px 16px; }}
  input, button {{ font: inherit; width: 100%; padding: 10px 12px; border-radius: 10px; }}
  input {{ background: #1a242f; color: inherit; border: 1px solid #3d5166; }}
  button {{ margin-top: 12px; background: #1f6f6a; color: inherit; border: 0; }}
  .error {{ color: #ffb4b4; }}
</style>
</head>
<body>
<main>
  <h1>Connect BayOS</h1>
  <p>{label} is asking to read and write this gateway's inbox. Enter the BayOS gateway key to allow it. The key is checked on this server and is not sent to {label}.</p>
  <p>Redirect host: {host}.</p>
  {local_note}
  {notice}
  <form method="post" action="/oauth/authorize">
    {''.join(hidden)}
    <label>Gateway key</label>
    <input type="password" name="gateway_key" autocomplete="current-password" required>
    <button type="submit">Allow inbox access</button>
  </form>
</main>
</body>
</html>
"""


def render_consent(params: Dict[str, str], message: str):
    nonce = secrets.token_urlsafe(16)
    return consent_page(params, message, nonce), consent_headers(nonce, params.get("redirect_uri", ""))


def parse_form(raw: bytes) -> Dict[str, str]:
    from urllib.parse import parse_qs

    text = raw.decode("utf-8")
    parsed = parse_qs(text, keep_blank_values=True, max_num_fields=20)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def consent_headers(nonce: str, redirect_uri: str = "") -> Dict[str, str]:
    # Chrome applies form-action to the post-submit redirect, so the
    # client's redirect origin must be allowed or the button silently fails.
    from urllib.parse import urlsplit
    extra = ""
    if redirect_uri and redirect_allowed(redirect_uri):
        parts = urlsplit(redirect_uri)
        extra = f" {parts.scheme}://{parts.netloc}"
    policy = (
        "default-src 'none'; "
        f"style-src 'nonce-{nonce}'; "
        f"base-uri 'none'; form-action 'self'{extra}; frame-ancestors 'none'"
    )
    return {
        "Content-Security-Policy": policy,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }
