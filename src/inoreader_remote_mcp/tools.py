"""The ten MCP tools.

**One Inoreader call per tool call**, with one exception said below: the daily quota is
about a hundred requests per zone, and a tool that paginates or enriches behind Claude's
back would spend it without anyone seeing. Pagination is handed to Claude instead —
`continuation` goes out with a page and comes back for the next one.

Two exceptions. `add_subscription`: `quickadd` takes no folder and no title, so setting
them costs a second write (`subscription/edit`), only when asked for.
`get_folders_and_tags`: the sub-folders of Dynamic Folders are feeds, not labels, so their
names and counts cost `subscription/list` and `unread-count`, only when that folder exists.

Descriptions are in French, short, and say what comes back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import formatting as fmt
from .inoreader import InoreaderClient, InoreaderError

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

Limit = Annotated[int, Field(ge=1, le=100, description="Nombre d'articles (1 à 100).")]
Continuation = Annotated[
    str | None,
    Field(description="Valeur `continuation` d'une réponse précédente, pour la page suivante."),
]
ArticleIds = Annotated[
    list[str],
    Field(min_length=1, description="Identifiants d'articles, tels que renvoyés par les outils."),
]


def register(server: MCPServer, client: InoreaderClient) -> None:
    async def get_user_info() -> dict[str, Any]:
        info = await client.get("user-info")
        return {
            "id": info.get("userId"),
            "name": info.get("userName"),
            "email": info.get("userEmail"),
            "signed_up": fmt.iso(info.get("signupTimeSec")),
        }

    async def get_unread_counts() -> dict[str, Any]:
        payload = await client.get("unread-count")
        maximum = payload.get("max")
        total, folders, feeds = None, [], []
        for entry in payload.get("unreadcounts", []):
            stream_id, count = entry.get("id", ""), int(entry.get("count", 0))
            if stream_id.endswith("/state/com.google/reading-list"):
                total = count
            elif count == 0:
                continue
            elif name := fmt.label_name(stream_id):
                folders.append({"id": stream_id, "name": name, "unread": count})
            elif stream_id.startswith("feed/"):
                feeds.append({"id": stream_id, "unread": count})
        return {
            "total": total,
            # A count equal to `max` is capped: read it as « max or more ».
            "max": maximum,
            "folders": folders,
            "feeds": feeds,
        }

    async def get_subscriptions() -> dict[str, Any]:
        payload = await client.get("subscription/list")
        return {
            "subscriptions": [
                {
                    "id": sub.get("id"),
                    "title": sub.get("title"),
                    "url": sub.get("url"),
                    "site_url": sub.get("htmlUrl"),
                    "folders": [c.get("label") for c in sub.get("categories", [])],
                }
                for sub in payload.get("subscriptions", [])
            ]
        }

    async def get_folders_and_tags() -> dict[str, Any]:
        payload = await client.get("tag/list", {"types": 1, "counts": 1})
        folders, tags = [], []
        dynamic: dict[str, Any] | None = None
        for tag in payload.get("tags", []):
            stream_id = tag.get("id", "")
            if fmt.is_state(stream_id):
                continue
            entry = {
                "id": stream_id,
                "name": fmt.label_name(stream_id) or stream_id,
                "unread": tag.get("unread_count"),
            }
            if tag.get("type") == "folder":
                folders.append(entry)
                if entry["name"] == fmt.DYNAMIC_FOLDERS:
                    dynamic = entry
            elif tag.get("type") == "tag":
                tags.append(entry)
        if dynamic is not None:
            dynamic["subfolders"] = await _dynamic_subfolders(client)
        return {"folders": folders, "tags": tags}

    async def get_articles(
        stream_id: Annotated[
            str | None,
            Field(
                description="Flux (`feed/…`) ou dossier (`user/-/label/…`). "
                "Absent : tout le compte."
            ),
        ] = None,
        unread_only: Annotated[bool, Field(description="Seulement les non-lus.")] = True,
        limit: Limit = 20,
        since: Annotated[
            str | None,
            Field(
                description="Date ISO 8601 : seulement les articles collectés depuis. "
                "Sans fuseau, UTC."
            ),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description="Mots à trouver dans le titre ou le résumé (tous requis, sans "
                "tenir compte des majuscules ni des accents). Filtre la page récupérée."
            ),
        ] = None,
        continuation: Continuation = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"n": limit}
        if unread_only:
            params["xt"] = fmt.READ
        if since:
            params["ot"] = _epoch(since)
        if continuation:
            params["c"] = continuation
        payload = await client.get(_stream_path(stream_id or fmt.READING_LIST), params)
        return fmt.articles_page(payload, query)

    async def get_starred_articles(
        limit: Limit = 20, continuation: Continuation = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"n": limit}
        if continuation:
            params["c"] = continuation
        payload = await client.get(_stream_path(fmt.STARRED), params)
        return fmt.articles_page(payload)

    async def add_subscription(
        url: Annotated[str, Field(description="Adresse du flux RSS/Atom ou du site.")],
        folder: Annotated[
            str | None, Field(description="Nom du dossier où ranger le flux.")
        ] = None,
        title: Annotated[str | None, Field(description="Titre à donner au flux.")] = None,
    ) -> dict[str, Any]:
        response = await client.post("subscription/quickadd", {"quickadd": fmt.feed_stream_id(url)})
        added = response.json()
        stream_id = added.get("streamId")
        if not added.get("numResults") or not stream_id:
            raise InoreaderError(f"Inoreader n'a pas trouvé de flux à l'adresse {url}.")
        if folder or title:
            edit: dict[str, Any] = {"ac": "edit", "s": stream_id}
            if folder:
                edit["a"] = f"user/-/label/{folder}"
            if title:
                edit["t"] = title
            _expect_ok(await client.post("subscription/edit", edit), "subscription/edit")
        return {
            "id": stream_id,
            "title": title or added.get("streamName"),
            "folder": folder,
        }

    async def remove_subscription(
        subscription_id: Annotated[
            str, Field(description="Identifiant du flux (`feed/…`) ou son adresse.")
        ],
    ) -> dict[str, Any]:
        stream_id = fmt.feed_stream_id(subscription_id)
        response = await client.post("subscription/edit", {"ac": "unsubscribe", "s": stream_id})
        _expect_ok(response, "subscription/edit")
        return {"removed": stream_id}

    async def mark_as_read(article_ids: ArticleIds) -> dict[str, Any]:
        response = await client.post("edit-tag", {"a": fmt.READ, "i": article_ids})
        _expect_ok(response, "edit-tag")
        return {"marked_read": len(article_ids)}

    async def mark_as_unread(article_ids: ArticleIds) -> dict[str, Any]:
        response = await client.post("edit-tag", {"r": fmt.READ, "i": article_ids})
        _expect_ok(response, "edit-tag")
        return {"marked_unread": len(article_ids)}

    tools = [
        (
            get_user_info,
            "Utilisateur",
            "Renvoie l'utilisateur Inoreader connecté : identifiant, nom, e-mail.",
            READ_ONLY,
        ),
        (
            get_unread_counts,
            "Non-lus",
            "Renvoie le nombre d'articles non lus : total, par dossier et par flux "
            "(flux et dossiers sans non-lus omis). "
            "Un nombre égal à `max` signifie « max ou plus ».",
            READ_ONLY,
        ),
        (
            get_subscriptions,
            "Abonnements",
            "Renvoie la liste des abonnements : identifiant, titre, adresse du flux, "
            "adresse du site, dossiers.",
            READ_ONLY,
        ),
        (
            get_folders_and_tags,
            "Dossiers et tags",
            "Renvoie les dossiers et les tags, avec leur nombre de non-lus. "
            "Le dossier Dynamic Folders porte en plus ses sous-dossiers (`subfolders`) : "
            "identifiant du flux, nom, non-lus.",
            READ_ONLY,
        ),
        (
            get_articles,
            "Articles",
            "Renvoie les articles d'un flux, d'un dossier ou de tout le compte, du plus récent "
            "au plus ancien : identifiant, titre, adresse, flux, date de publication, "
            "date de collecte (crawlTimeMsec), résumé en texte brut (500 caractères). "
            "Renvoie aussi `continuation` s'il reste des articles. "
            "Avec `query`, ne garde que les articles contenant tous les mots, et indique "
            "`scanned` (articles examinés) et `matched` : l'API n'a pas de recherche, "
            "le filtre porte sur la page récupérée. Pour chercher un sujet, prends "
            "`limit=100`, borne avec `since`, et suis `continuation` si besoin.",
            READ_ONLY,
        ),
        (
            get_starred_articles,
            "Favoris",
            "Renvoie les articles mis en favoris, au même format que get_articles, "
            "avec `continuation` s'il en reste.",
            READ_ONLY,
        ),
        (
            add_subscription,
            "Ajouter un abonnement",
            "Abonne le compte à un flux, éventuellement rangé dans un dossier et renommé. "
            "Renvoie l'identifiant et le titre du flux.",
            WRITE,
        ),
        (
            remove_subscription,
            "Supprimer un abonnement",
            "Désabonne le compte d'un flux. Demande toujours confirmation à l'utilisateur, "
            "en nommant le flux, avant d'appeler cet outil. Renvoie l'identifiant supprimé.",
            DESTRUCTIVE,
        ),
        (
            mark_as_read,
            "Marquer comme lu",
            "Marque des articles comme lus. Renvoie le nombre d'articles marqués.",
            IDEMPOTENT_WRITE,
        ),
        (
            mark_as_unread,
            "Marquer comme non lu",
            "Marque des articles comme non lus. Renvoie le nombre d'articles marqués.",
            IDEMPOTENT_WRITE,
        ),
    ]
    for fn, title, description, hints in tools:
        server.add_tool(fn, title=title, description=description, annotations=hints)


async def _dynamic_subfolders(client: InoreaderClient) -> list[dict[str, Any]]:
    subscriptions = await client.get("subscription/list")
    unread = {
        entry.get("id"): int(entry.get("count", 0))
        for entry in (await client.get("unread-count")).get("unreadcounts", [])
    }
    return [
        {"id": sub.get("id"), "name": sub.get("title"), "unread": unread.get(sub.get("id"), 0)}
        for sub in subscriptions.get("subscriptions", [])
        if fmt.DYNAMIC_FOLDERS in (c.get("label") for c in sub.get("categories", []))
    ]


def _stream_path(stream_id: str) -> str:
    # Fully escaped: a feed id embeds a URL, and its `?` or `#` would otherwise end the path.
    return f"stream/contents/{quote(stream_id, safe='')}"


def _epoch(value: str) -> int:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InoreaderError(f"Date `since` illisible : {value!r} (attendu : ISO 8601).") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp())


def _expect_ok(response: Any, path: str) -> None:
    if response.text.strip() != "OK":
        raise InoreaderError(f"Inoreader n'a pas confirmé l'opération ({path}).")
