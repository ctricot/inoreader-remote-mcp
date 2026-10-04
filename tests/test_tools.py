from urllib.parse import parse_qs

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from inoreader_remote_mcp.inoreader import API_BASE
from inoreader_remote_mcp.server import build

STREAM = f"{API_BASE}stream/contents/"
READ = "user/-/state/com.google/read"


@pytest.fixture
def server(settings, client, authorised):
    return build(settings, client)


async def call(server, name, **arguments):
    result = await server.call_tool(name, arguments)
    return result.structured_content


async def call_error(server, name, **arguments) -> str:
    """The text Claude receives: the protocol handler turns a `ToolError` into an
    `is_error` result carrying this very message."""
    with pytest.raises(ToolError) as caught:
        await server.call_tool(name, arguments)
    return str(caught.value)


def form(route) -> dict[str, list[str]]:
    return parse_qs(route.calls.last.request.content.decode())


def stream_route(**params):
    return respx.get(url__startswith=STREAM, params=params)


ITEM = {
    "id": "tag:google.com,2005:reader/item/0000000000000001",
    "title": "T",
    "published": 1_700_000_000,
    "crawlTimeMsec": "1700000000000",
    "canonical": [{"href": "https://x.org/a"}],
    "summary": {"content": "<p>Body</p>"},
    "origin": {"title": "X"},
}


async def test_ten_tools_with_french_descriptions(server):
    tools = {t.name: t for t in await server.list_tools()}
    assert sorted(tools) == sorted(
        [
            "get_user_info",
            "get_unread_counts",
            "get_subscriptions",
            "get_folders_and_tags",
            "get_articles",
            "get_starred_articles",
            "add_subscription",
            "remove_subscription",
            "mark_as_read",
            "mark_as_unread",
        ]
    )
    assert "confirmation" in tools["remove_subscription"].description
    assert tools["remove_subscription"].annotations.destructive_hint
    assert tools["get_articles"].annotations.read_only_hint


@respx.mock
async def test_get_user_info(server):
    respx.get(f"{API_BASE}user-info").respond(
        json={"userId": "1", "userName": "me", "userEmail": "m@x.org", "signupTimeSec": 0}
    )
    assert await call(server, "get_user_info") == {
        "id": "1",
        "name": "me",
        "email": "m@x.org",
        "signed_up": None,
    }


@respx.mock
async def test_get_unread_counts(server):
    respx.get(f"{API_BASE}unread-count").respond(
        json={
            "max": 1000,
            "unreadcounts": [
                {"id": "user/1/state/com.google/reading-list", "count": 42},
                {"id": "user/1/state/com.google/starred", "count": 3},
                {"id": "user/1/label/Tech", "count": 40},
                {"id": "user/1/label/Empty", "count": 0},
                {"id": "feed/https://x.org/rss", "count": 2},
                {"id": "feed/https://y.org/rss", "count": 0},
            ],
        }
    )
    assert await call(server, "get_unread_counts") == {
        "total": 42,
        "max": 1000,
        "folders": [{"id": "user/1/label/Tech", "name": "Tech", "unread": 40}],
        "feeds": [{"id": "feed/https://x.org/rss", "unread": 2}],
    }


@respx.mock
async def test_get_subscriptions(server):
    respx.get(f"{API_BASE}subscription/list").respond(
        json={
            "subscriptions": [
                {
                    "id": "feed/https://x.org/rss",
                    "title": "X",
                    "url": "https://x.org/rss",
                    "htmlUrl": "https://x.org",
                    "categories": [{"id": "user/1/label/Tech", "label": "Tech"}],
                    "iconUrl": "ignored",
                }
            ]
        }
    )
    assert await call(server, "get_subscriptions") == {
        "subscriptions": [
            {
                "id": "feed/https://x.org/rss",
                "title": "X",
                "url": "https://x.org/rss",
                "site_url": "https://x.org",
                "folders": ["Tech"],
            }
        ]
    }


@respx.mock
async def test_get_folders_and_tags(server):
    route = respx.get(f"{API_BASE}tag/list").respond(
        json={
            "tags": [
                {"id": "user/1/state/com.google/starred"},
                {"id": "user/1/label/Tech", "type": "folder", "unread_count": 4},
                {"id": "user/1/label/later", "type": "tag", "unread_count": 1},
                {"id": "user/1/label/search", "type": "active_search"},
            ]
        }
    )
    assert await call(server, "get_folders_and_tags") == {
        "folders": [{"id": "user/1/label/Tech", "name": "Tech", "unread": 4}],
        "tags": [{"id": "user/1/label/later", "name": "later", "unread": 1}],
    }
    params = route.calls.last.request.url.params
    assert params["types"] == "1" and params["counts"] == "1"


@respx.mock
async def test_get_articles_defaults_to_unread_reading_list(server):
    route = stream_route().respond(json={"items": [ITEM], "continuation": "next"})
    out = await call(server, "get_articles")
    request = route.calls.last.request
    assert request.url.raw_path.startswith(
        b"/reader/api/0/stream/contents/user%2F-%2Fstate%2Fcom.google%2Freading-list?"
    )
    assert request.url.params["n"] == "20"
    assert request.url.params["xt"] == READ
    assert "ot" not in request.url.params and "c" not in request.url.params
    assert out["continuation"] == "next"
    assert out["articles"][0]["summary"] == "Body"


@respx.mock
async def test_get_articles_with_every_parameter(server):
    route = stream_route().respond(json={"items": []})
    out = await call(
        server,
        "get_articles",
        stream_id="feed/https://x.org/rss?format=xml",
        unread_only=False,
        limit=100,
        since="2026-10-01T00:00:00+02:00",
        continuation="abc",
    )
    request = route.calls.last.request
    assert b"feed%2Fhttps%3A%2F%2Fx.org%2Frss%3Fformat%3Dxml?" in request.url.raw_path
    params = request.url.params
    assert params["n"] == "100" and params["c"] == "abc"
    assert params["ot"] == "1790805600"  # 2026-09-30T22:00:00Z
    assert "xt" not in params
    assert out == {"articles": [], "continuation": None}


async def test_get_articles_rejects_bad_input(server):
    assert "ISO 8601" in await call_error(server, "get_articles", since="yesterday")
    await call_error(server, "get_articles", limit=101)


@respx.mock
async def test_get_starred_articles(server):
    route = stream_route().respond(json={"items": [ITEM]})
    out = await call(server, "get_starred_articles", limit=5, continuation="c1")
    request = route.calls.last.request
    assert b"user%2F-%2Fstate%2Fcom.google%2Fstarred?" in request.url.raw_path
    assert request.url.params["n"] == "5" and request.url.params["c"] == "c1"
    assert "xt" not in request.url.params
    assert len(out["articles"]) == 1


@respx.mock
async def test_add_subscription_alone_is_one_call(server):
    quickadd = respx.post(f"{API_BASE}subscription/quickadd").respond(
        json={"numResults": 1, "streamId": "feed/https://x.org/rss", "streamName": "X"}
    )
    edit = respx.post(f"{API_BASE}subscription/edit")
    out = await call(server, "add_subscription", url="https://x.org/rss")
    assert form(quickadd)["quickadd"] == ["feed/https://x.org/rss"]
    assert not edit.called
    assert out == {"id": "feed/https://x.org/rss", "title": "X", "folder": None}


@respx.mock
async def test_add_subscription_with_folder_and_title(server):
    respx.post(f"{API_BASE}subscription/quickadd").respond(
        json={"numResults": 1, "streamId": "feed/https://x.org/rss", "streamName": "X"}
    )
    edit = respx.post(f"{API_BASE}subscription/edit").respond(text="OK")
    out = await call(
        server, "add_subscription", url="https://x.org/rss", folder="Tech news", title="Mine"
    )
    assert form(edit) == {
        "ac": ["edit"],
        "s": ["feed/https://x.org/rss"],
        "a": ["user/-/label/Tech news"],
        "t": ["Mine"],
    }
    assert out == {"id": "feed/https://x.org/rss", "title": "Mine", "folder": "Tech news"}


@respx.mock
async def test_add_subscription_not_found(server):
    respx.post(f"{API_BASE}subscription/quickadd").respond(
        json={"numResults": 0, "query": "feed/https://nope"}
    )
    assert "pas trouvé" in await call_error(server, "add_subscription", url="https://nope")


@respx.mock
async def test_remove_subscription(server):
    route = respx.post(f"{API_BASE}subscription/edit").respond(text="OK")
    out = await call(server, "remove_subscription", subscription_id="https://x.org/rss")
    assert form(route) == {"ac": ["unsubscribe"], "s": ["feed/https://x.org/rss"]}
    assert out == {"removed": "feed/https://x.org/rss"}


@respx.mock
async def test_mark_as_read_and_unread_in_one_call(server):
    route = respx.post(f"{API_BASE}edit-tag").respond(text="OK")
    ids = ["tag:google.com,2005:reader/item/1", "tag:google.com,2005:reader/item/2"]

    assert await call(server, "mark_as_read", article_ids=ids) == {"marked_read": 2}
    assert form(route) == {"a": [READ], "i": ids}

    assert await call(server, "mark_as_unread", article_ids=ids) == {"marked_unread": 2}
    assert form(route) == {"r": [READ], "i": ids}
    assert route.call_count == 2


async def test_mark_requires_ids(server):
    await call_error(server, "mark_as_read", article_ids=[])


@respx.mock
async def test_unconfirmed_write_is_an_error(server):
    respx.post(f"{API_BASE}edit-tag").respond(text="Error")
    assert "pas confirmé" in await call_error(server, "mark_as_read", article_ids=["1"])


@respx.mock
async def test_quota_error_reaches_claude_verbatim(server):
    respx.get(f"{API_BASE}user-info").mock(
        return_value=httpx.Response(
            429,
            headers={
                "X-Reader-Zone1-Usage": "100",
                "X-Reader-Zone1-Limit": "100",
                "X-Reader-Limits-Reset-After": "600",
            },
        )
    )
    message = await call_error(server, "get_user_info")
    assert "Quota journalier" in message and "10 min" in message


async def test_not_authorised_error_reaches_claude(settings, client):
    message = await call_error(build(settings, client), "get_user_info")
    assert "/oauth/inoreader/start" in message
