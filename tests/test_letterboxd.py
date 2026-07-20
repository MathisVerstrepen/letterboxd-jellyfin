from unittest.mock import ANY, Mock, call

import pytest

import src.letterboxd as letterboxd
from src.results import LetterboxdDetailResult


def frame_page(*links: str, next_href: str | None = None) -> bytes:
    frames = "".join(
        f'<div data-component-class="LazyPoster" data-target-link="/{link}"></div>'
        for link in links
    )
    next_link = f'<a class="next" href="{next_href}"></a>' if next_href else ""
    return (frames + next_link).encode()


def detail_page(tmdb_id: str, kind: str = "movie") -> bytes:
    return f'<a data-track-action="TMDB" href="/{kind}/{tmdb_id}/"></a>'.encode()


def test_letterboxd_request_retries_with_rotated_proxies(monkeypatch):
    manager = Mock(allow_fallback=True)
    manager.get_proxy.side_effect = ["one", "two", "three"]
    request = Mock(side_effect=RuntimeError("no response"))
    monkeypatch.setattr(letterboxd, "make_request", request)
    assert letterboxd.make_letterboxd_request("user/watchlist/", manager) is None
    assert request.call_args_list == [
        call("https://letterboxd.com/user/watchlist/", proxy, allow_fallback=True)
        for proxy in ("one", "two", "three")
    ]


@pytest.mark.parametrize(
    ("html", "outcome", "media_type", "tmdb_id"),
    [
        (detail_page("123"), "resolved", "movie", "123"),
        (detail_page("45", "tv"), "resolved", "series", "45"),
        (b"<p>missing</p>", "retry", None, None),
        (b'<a data-track-action="TMDB" href=""></a>', "retry", None, None),
    ],
)
def test_extract_tmdb_detail(
    html, outcome, media_type, tmdb_id, response_factory, monkeypatch
):
    monkeypatch.setattr(
        letterboxd,
        "make_letterboxd_request",
        Mock(return_value=response_factory(content=html)),
    )
    result = letterboxd.extract_tmdb_id_from_endpoint("film/example/", Mock())
    assert (result.outcome, result.media_type, result.tmdb_id) == (
        outcome,
        media_type,
        tmdb_id,
    )


@pytest.mark.parametrize(
    "args",
    [
        ("movie", "movie", "1"),
        ("resolved", None, "1"),
        ("resolved", "movie", None),
        ("retry", "series", "1"),
    ],
)
def test_detail_result_rejects_inconsistent_identity(args):
    with pytest.raises(ValueError):
        LetterboxdDetailResult(*args)


def test_watchlist_initial_failure_is_not_no_change(monkeypatch):
    monkeypatch.setattr(letterboxd, "make_letterboxd_request", Mock(return_value=None))
    result = letterboxd.get_new_watchlist_entries("alice", Mock(), 1, None)
    assert result.entries == []
    assert (result.outcome, result.scan_complete, result.failed_items) == (
        "failed",
        False,
        1,
    )


def test_successful_empty_watchlist_is_complete(response_factory, monkeypatch):
    monkeypatch.setattr(
        letterboxd,
        "make_letterboxd_request",
        Mock(return_value=response_factory(content=b"")),
    )
    result = letterboxd.get_new_watchlist_entries("alice", Mock(), 1, None)
    assert result.entries == []
    assert (result.outcome, result.scan_complete, result.cursor_uri) == (
        "success",
        True,
        None,
    )


def test_endpoint_cursor_stops_before_boundary_detail(response_factory, monkeypatch):
    responses = {
        "alice/watchlist/": response_factory(content=frame_page("film/a/", "film/b/")),
        "film/a/": response_factory(content=detail_page("3")),
    }
    request = Mock(side_effect=lambda endpoint, manager: responses[endpoint])
    monkeypatch.setattr(letterboxd, "make_letterboxd_request", request)
    result = letterboxd.get_new_watchlist_entries(
        "alice", Mock(), 1, {"kind": "letterboxd", "value": "film/b/"}
    )
    assert [(entry.endpoint, entry.detail.tmdb_id) for entry in result.entries] == [
        ("film/a/", "3")
    ]
    assert (result.cursor_uri, result.boundary_uri, result.scan_complete) == (
        "film/a/",
        "film/b/",
        True,
    )
    assert call("film/b/", ANY) not in request.call_args_list


def test_legacy_boundary_converts_to_endpoint(response_factory, monkeypatch):
    responses = {
        "alice/watchlist/": response_factory(content=frame_page("film/a/", "film/b/")),
        "film/a/": response_factory(content=detail_page("3")),
        "film/b/": response_factory(content=detail_page("2")),
    }
    monkeypatch.setattr(
        letterboxd,
        "make_letterboxd_request",
        Mock(side_effect=lambda endpoint, manager: responses[endpoint]),
    )
    result = letterboxd.get_new_watchlist_entries(
        "alice", Mock(), 1, {"kind": "legacy_tmdb", "value": "2"}
    )
    assert [entry.endpoint for entry in result.entries] == ["film/a/"]
    assert (result.cursor_uri, result.boundary_uri, result.scan_complete) == (
        "film/a/",
        "film/b/",
        True,
    )


def test_known_detail_failure_is_retryable_but_scan_complete(response_factory, monkeypatch):
    monkeypatch.setattr(
        letterboxd,
        "make_letterboxd_request",
        Mock(return_value=response_factory(content=frame_page("film/a/"))),
    )
    monkeypatch.setattr(
        letterboxd,
        "extract_tmdb_id_from_endpoint",
        Mock(return_value=LetterboxdDetailResult("retry")),
    )
    result = letterboxd.get_new_watchlist_entries("alice", Mock(), 1, None)
    assert result.entries[0].endpoint == "film/a/"
    assert (result.outcome, result.scan_complete, result.cursor_uri) == (
        "partial",
        True,
        "film/a/",
    )


def test_unresolved_legacy_candidate_stops_incomplete(response_factory, monkeypatch):
    monkeypatch.setattr(
        letterboxd,
        "make_letterboxd_request",
        Mock(return_value=response_factory(content=frame_page("film/a/", "film/b/"))),
    )
    detail = Mock(return_value=LetterboxdDetailResult("retry"))
    monkeypatch.setattr(letterboxd, "extract_tmdb_id_from_endpoint", detail)
    result = letterboxd.get_new_watchlist_entries(
        "alice", Mock(), 1, {"kind": "legacy_tmdb", "value": "2"}
    )
    assert [entry.endpoint for entry in result.entries] == ["film/a/"]
    assert not result.scan_complete


def test_malformed_frame_stops_before_later_entries(response_factory, monkeypatch):
    html = (
        b'<div data-component-class="LazyPoster"></div>'
        + frame_page("film/a/", next_href="alice/watchlist/page/2/")
    )
    request = Mock(return_value=response_factory(content=html))
    monkeypatch.setattr(letterboxd, "make_letterboxd_request", request)
    result = letterboxd.get_new_watchlist_entries("alice", Mock(), 1, None)
    assert result.failed_items == 1
    assert result.outcome == "partial"
    assert not result.scan_complete
    assert result.entries == []


def test_failed_pagination_returns_known_entries_without_advancing(
    response_factory, monkeypatch
):
    request = Mock(
        side_effect=[
            response_factory(
                content=frame_page("film/a/", next_href="alice/watchlist/page/2/")
            ),
            response_factory(content=detail_page("1")),
            None,
        ]
    )
    monkeypatch.setattr(letterboxd, "make_letterboxd_request", request)
    result = letterboxd.get_new_watchlist_entries("alice", Mock(), 1, None)
    assert [entry.endpoint for entry in result.entries] == ["film/a/"]
    assert result.failed_items == 1
    assert result.outcome == "partial"
    assert not result.scan_complete


def test_default_filter_skips_series_but_series_only_keeps_it(
    response_factory, monkeypatch
):
    monkeypatch.setattr(
        letterboxd,
        "make_letterboxd_request",
        Mock(return_value=response_factory(content=frame_page("show/a/"))),
    )
    monkeypatch.setattr(
        letterboxd,
        "extract_tmdb_id_from_endpoint",
        Mock(return_value=LetterboxdDetailResult("resolved", "series", "20")),
    )
    default = letterboxd.get_new_watchlist_entries("alice", Mock(), 1, None)
    series = letterboxd.get_new_watchlist_entries(
        "alice", Mock(), 1, None, include_series=True, include_movies=False
    )
    assert default.entries == []
    assert default.skipped_items == 1
    assert [(entry.endpoint, entry.detail.tmdb_id) for entry in series.entries] == [
        ("show/a/", "20")
    ]
