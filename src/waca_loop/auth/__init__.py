"""Authentication (§5) — Google OAuth + identity resolution to a Viewer."""

from waca_loop.auth.identity import Identity, resolve_viewer
from waca_loop.auth.oauth import OAuthManager, StaticIdentity

__all__ = ["Identity", "resolve_viewer", "OAuthManager", "StaticIdentity"]
