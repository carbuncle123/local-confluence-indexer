from __future__ import annotations

from markdown_converter import convert_page_to_markdown, sha256_text, slugify


def make_page() -> dict:
    return {
        "id": "123",
        "space_key": "PROJECT_A",
        "spaceId": "10",
        "title": "認証/API 仕様",
        "status": "current",
        "parentId": "1",
        "authorId": "u1",
        "ownerId": "u1",
        "createdAt": "2026-05-05T00:00:00.000Z",
        "version": {
            "number": 2,
            "createdAt": "2026-05-05T01:00:00.000Z",
            "message": "updated",
            "minorEdit": False,
            "authorId": "u1",
        },
        "body": {
            "storage": {
                "value": (
                    "<h1>見出し</h1><p>Hello</p>"
                    "<ac:structured-macro ac:name='status'></ac:structured-macro>"
                    "<p><ri:user ri:account-id='abc123'></ri:user></p>"
                )
            }
        },
        "labels": {"results": [{"name": "official"}, {"name": "auth"}]},
        "_links": {
            "base": "https://example.atlassian.net",
            "webui": "/wiki/spaces/PROJECT_A/pages/123",
        },
    }


def test_slugify_handles_invalid_chars_and_empty_titles() -> None:
    assert slugify("認証/API 仕様") == "認証-API-仕様"
    assert slugify("   ") == "untitled"


def test_sha256_text_has_expected_prefix() -> None:
    assert sha256_text("abc").startswith("sha256:")


def test_convert_page_to_markdown_renders_frontmatter_and_special_elements() -> None:
    converted = convert_page_to_markdown(make_page(), "2026-05-05T02:00:00+00:00")

    assert converted.content_hash.startswith("sha256:")
    assert "Confluence macro: status" in converted.markdown
    assert "@accountId:abc123" in converted.markdown
    assert "space_key: PROJECT_A" in converted.markdown
    assert "# 認証/API 仕様" in converted.markdown
