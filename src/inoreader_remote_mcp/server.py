"""The MCP server and the ASGI application that serves it.

One Starlette application holds everything, at the root: `/mcp` (Streamable HTTP, bearer
token required), the OAuth authorisation server routes the SDK generates from
`claude_oauth.Provider` (`/.well-known/…`, `/register`, `/authorize`, `/token`, `/revoke`),
and the routes added with `custom_route` (`/health`, the consent screen, the Inoreader
authorisation). At the root because the OAuth metadata announces endpoints computed from the
issuer, and a client calls them exactly where announced.
"""

from __future__ import annotations

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, claude_oauth, inoreader_oauth, tools
from .config import Settings, get_settings
from .inoreader import InoreaderClient

INSTRUCTIONS = """\
Ce serveur donne accès au compte Inoreader de son propriétaire : flux, dossiers, articles,
favoris. Il peut aussi marquer des articles comme lus ou non lus, ajouter et supprimer des
abonnements.

L'API Inoreader limite le nombre d'appels par jour : évite les appels inutiles, et préfère
un appel avec `limit` élevé à plusieurs petits appels.

Chercher un sujet dans les articles : Inoreader n'a pas de recherche par API. Quand
l'utilisateur parle de « mes articles » ou de ses flux, la réponse est dans Inoreader, pas
sur le web. Appelle `get_articles` avec `query`, `limit=100` et `since` (la période voulue),
sur le dossier le plus probable (`get_folders_and_tags` les liste). `scanned` dit combien
d'articles ont été examinés : s'il reste une `continuation`, suis-la plutôt que d'élargir
`since` à l'aveugle, et dis à l'utilisateur ce qui a été couvert avant de conclure qu'il n'y
a rien. Les sous-dossiers de Dynamic Folders sont déjà des surveillances par mot-clé.

Marquer comme lu : ne marque que des articles que tu as trouvés et montrés. Pour vider tout
un flux ou dossier, utilise `mark_all_as_read` avec `before`, après confirmation.
"""


def build(
    settings: Settings | None = None,
    client: InoreaderClient | None = None,
    provider: claude_oauth.Provider | None = None,
) -> MCPServer:
    """A fresh server. A factory, not a module singleton: the SDK's session manager can
    only be started once per instance, and the tests start one per client."""
    settings = settings or get_settings()
    client = client or InoreaderClient(settings)
    provider = provider or claude_oauth.Provider(settings)
    server = MCPServer(
        name="inoreader",
        title="Inoreader",
        instructions=INSTRUCTIONS,
        version=__version__,
        log_level=settings.log_level.upper(),
        auth_server_provider=provider,
        auth=AuthSettings(
            # Strings, not `AnyHttpUrl`: the SDK then keeps the path-less form, with no
            # trailing slash. Issuer comparison is exact string comparison.
            issuer_url=settings.public_url.rstrip("/"),
            # Publishes the protected-resource metadata and the `WWW-Authenticate` header
            # that tells Claude where to find the authorisation server. Without it, a
            # remote client gets a mute 401.
            resource_server_url=settings.url("/mcp"),
            required_scopes=[claude_oauth.SCOPE],
            # Mandatory, not optional: Claude is not configured by hand, it declares itself.
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[claude_oauth.SCOPE],
                default_scopes=[claude_oauth.SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
            # This server is the only issuer its tokens are ever valid at.
            validate_token_resource=False,
        ),
    )

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    tools.register(server, client)
    claude_oauth.register_consent(server, settings, provider)
    inoreader_oauth.register(server, settings, client)
    return server


def create_app(
    settings: Settings | None = None,
    client: InoreaderClient | None = None,
    provider: claude_oauth.Provider | None = None,
) -> Starlette:
    settings = settings or get_settings()
    host = settings.public_host
    return build(settings, client, provider).streamable_http_app(
        streamable_http_path="/mcp",
        # Stateless: the container is recreated on every update, and a session held in
        # memory would die with it, mid-conversation. Every tool is request/response.
        stateless_http=True,
        # The SDK only accepts `localhost` by default, which would reject every request
        # coming through the reverse proxy.
        transport_security=TransportSecuritySettings(
            allowed_hosts=[host, f"{host}:*", "127.0.0.1:*", "localhost:*"],
            allowed_origins=[f"https://{host}", f"http://{host}", "http://localhost:*"],
        ),
    )
