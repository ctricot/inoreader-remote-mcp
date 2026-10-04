from inoreader_remote_mcp import formatting as fmt


def test_html_to_text_strips_tags_scripts_and_entities():
    html = (
        "<p>Hello&nbsp;<b>world</b> &amp; co</p><script>alert(1)</script>"
        "<style>p{}</style><div>next</div>line<br>break"
    )
    assert fmt.html_to_text(html) == "Hello world & co next line break"


def test_truncate():
    assert fmt.truncate("short") == "short"
    long = "word " * 200
    cut = fmt.truncate(long)
    assert len(cut) <= 500 and cut.endswith("…")


def test_label_name_and_state():
    assert fmt.label_name("user/1005921515/label/Tech news") == "Tech news"
    assert fmt.label_name("user/-/label/Tech") == "Tech"
    assert fmt.label_name("feed/http://x") is None
    assert fmt.is_state("user/1005921515/state/com.google/starred")
    assert not fmt.is_state("user/-/label/Tech")


def test_feed_stream_id():
    assert fmt.feed_stream_id("https://x.org/rss") == "feed/https://x.org/rss"
    assert fmt.feed_stream_id("feed/https://x.org/rss") == "feed/https://x.org/rss"


def test_article():
    item = {
        "id": "tag:google.com,2005:reader/item/00000000148b9369",
        "title": "Caf&eacute; <em>news</em>",
        "published": 1_700_000_000,
        "crawlTimeMsec": "1700000123456",
        "canonical": [{"href": "https://x.org/a"}],
        "alternate": [{"href": "https://x.org/alt"}],
        "summary": {"content": "<p>" + "x" * 800 + "</p>"},
        "origin": {"streamId": "feed/https://x.org/rss", "title": "X"},
    }
    out = fmt.article(item)
    assert out == {
        "id": "tag:google.com,2005:reader/item/00000000148b9369",
        "title": "Café news",
        "url": "https://x.org/a",
        "feed": "X",
        "published": "2023-11-14T22:13:20Z",
        "crawlTimeMsec": 1700000123456,
        "summary": "x" * 499 + "…",
    }


def test_article_tolerates_missing_fields():
    out = fmt.article({"id": "1", "alternate": [{"href": "https://x.org/alt"}]})
    assert out["url"] == "https://x.org/alt"
    assert out["title"] == "" and out["summary"] == ""
    assert out["published"] is None and out["crawlTimeMsec"] is None
