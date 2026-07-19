from unittest.mock import ANY, Mock, call

import pytest

import src.letterboxd as letterboxd


def test_letterboxd_request_uses_selected_proxy(response_factory, monkeypatch):
    response = response_factory(content=b"ok")
    manager = Mock(allow_fallback=False)
    manager.get_proxy.return_value = {"https": "proxy"}
    request = Mock(return_value=response)
    monkeypatch.setattr(letterboxd, "make_request", request)

    assert letterboxd.make_letterboxd_request("film/example/", manager) is response
    request.assert_called_once_with(
        "https://letterboxd.com/film/example/",
        {"https": "proxy"},
        allow_fallback=False,
    )


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
    ("html", "tmdb_id", "failed", "skipped"),
    [
        (b'<a data-track-action="TMDB" href="https://tmdb.org/movie/123/">x</a>', "123", 0, 0),
        (b'<a data-track-action="TMDB" href="https://tmdb.org/tv/45/">x</a>', None, 0, 1),
        (b"<p>missing</p>", None, 1, 0),
        (b'<a data-track-action="TMDB" href=""></a>', None, 1, 0),
    ],
)
def test_extract_tmdb_detail(html, tmdb_id, failed, skipped, response_factory, monkeypatch):
    monkeypatch.setattr(
        letterboxd,
        "make_letterboxd_request",
        Mock(return_value=response_factory(content=html)),
    )

    result = letterboxd.extract_tmdb_id_from_endpoint("film/example/", Mock())

    assert (result.tmdb_id, result.failed_items, result.skipped_items) == (
        tmdb_id,
        failed,
        skipped,
    )


def test_extract_tmdb_detail_counts_request_failure(monkeypatch):
    monkeypatch.setattr(letterboxd, "make_letterboxd_request", Mock(return_value=None))
    result = letterboxd.extract_tmdb_id_from_endpoint("film/example/", Mock())
    assert result.failed_items == 1


def frame_page(*links: str, next_href: str | None = None) -> bytes:
    frames = "".join(
        f'<div data-component-class="LazyPoster" data-target-link="/{link}"></div>'
        for link in links
    )
    next_link = f'<a class="next" href="{next_href}"></a>' if next_href else ""
    return (frames + next_link).encode()


def detail_page(tmdb_id: str) -> bytes:
    return f'<a data-track-action="TMDB" href="/movie/{tmdb_id}/"></a>'.encode()


def test_watchlist_initial_failure(monkeypatch):
    monkeypatch.setattr(letterboxd, "make_letterboxd_request", Mock(return_value=None))
    result = letterboxd.get_new_watchlist_tmdb_ids("alice", Mock(), 1, None)
    assert result.tmdb_ids == []
    assert result.failed_items == 1


def test_watchlist_preserves_order_and_stops_at_saved_boundary(response_factory, monkeypatch):
    responses = {
        "alice/watchlist/": response_factory(content=frame_page("film/a/", "film/b/", "film/c/")),
        "film/a/": response_factory(content=detail_page("3")),
        "film/b/": response_factory(content=detail_page("2")),
        "film/c/": response_factory(content=detail_page("1")),
    }
    request = Mock(side_effect=lambda endpoint, manager: responses[endpoint])
    monkeypatch.setattr(letterboxd, "make_letterboxd_request", request)

    result = letterboxd.get_new_watchlist_tmdb_ids("alice", Mock(), 1, "2")

    assert result.tmdb_ids == ["3"]
    assert result.failed_items == 0


def test_watchlist_follows_pagination_and_counts_detail_outcomes(response_factory, monkeypatch):
    pages = {
        "alice/watchlist/": response_factory(
            content=frame_page("film/a/", "film/tv/", next_href="alice/watchlist/page/2/")
        ),
        "alice/watchlist/page/2/": response_factory(
            content=frame_page("film/b/", "film/missing/")
        ),
        "film/a/": response_factory(content=detail_page("30")),
        "film/tv/": response_factory(
            content=b'<a data-track-action="TMDB" href="/tv/9/"></a>'
        ),
        "film/b/": response_factory(content=detail_page("20")),
        "film/missing/": response_factory(content=b"<p>none</p>"),
    }
    request = Mock(side_effect=lambda endpoint, manager: pages[endpoint])
    monkeypatch.setattr(letterboxd, "make_letterboxd_request", request)

    result = letterboxd.get_new_watchlist_tmdb_ids("alice", Mock(), 1, None)

    assert result.tmdb_ids == ["30", "20"]
    assert (result.failed_items, result.skipped_items) == (1, 1)
    assert call("alice/watchlist/page/2/", ANY) in request.call_args_list


def test_watchlist_counts_malformed_frame_and_future_exception(response_factory, monkeypatch):
    html = (
        b'<div data-component-class="LazyPoster"></div>'
        b'<div data-component-class="LazyPoster" data-target-link="/film/a/"></div>'
    )
    monkeypatch.setattr(
        letterboxd,
        "make_letterboxd_request",
        Mock(return_value=response_factory(content=html)),
    )
    monkeypatch.setattr(
        letterboxd,
        "extract_tmdb_id_from_endpoint",
        Mock(side_effect=RuntimeError("worker failed")),
    )

    result = letterboxd.get_new_watchlist_tmdb_ids("alice", Mock(), 1, None)

    assert result.failed_items == 2


def test_watchlist_counts_failed_next_page(response_factory, monkeypatch):
    request = Mock(
        side_effect=[
            response_factory(content=frame_page(next_href="alice/watchlist/page/2/")),
            None,
        ]
    )
    monkeypatch.setattr(letterboxd, "make_letterboxd_request", request)
    result = letterboxd.get_new_watchlist_tmdb_ids("alice", Mock(), 1, None)
    assert result.failed_items == 1
