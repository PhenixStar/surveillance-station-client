"""Tests for the Surveillance Station Transactions (POS) parsers."""

from surveillance.services.transactions import (
    Transaction,
    fresh_receipts,
    parse_devices,
    parse_server_time,
    parse_transactions,
)


def test_parse_devices_keeps_only_paired() -> None:
    data = {
        "data": [
            {"id": 1, "name": "SD-XL-POS", "pairedcam_id": 12, "pairedcam_enable": True},
            {"id": 2, "name": "unpaired", "pairedcam_id": 29, "pairedcam_enable": False},
            {"id": 3, "name": "no camera", "pairedcam_id": 0, "pairedcam_enable": True},
            {"name": "broken row"},
        ],
        "total": 4,
    }
    assert parse_devices(data) == {1: 12}


def test_parse_transactions_joins_lines_and_sorts_oldest_first() -> None:
    data = {
        "data": [
            {
                "id": "1_4737",
                "pos_id": 1,
                "begin_tmstmp": 1792011709,
                "end_tmstmp": 1792011760,
                "camera_id": 0,
                "content": [
                    {"line_id": 2, "content": "TOTAL P100 Cash"},
                    {"line_id": 1, "content": "#1 05:01:40 Lyn\n2x BILLIARD P100"},
                ],
            },
            {
                "id": "3_1784",
                "pos_id": 3,
                "begin_tmstmp": 1792011568,
                "content": [{"line_id": 1, "content": "#036471 04:59:25 Jewel"}],
            },
            {"id": "1_1", "pos_id": 1, "begin_tmstmp": 1, "content": []},
            {"pos_id": 1, "content": [{"content": "no id"}]},
        ],
        "total": 4,
    }
    txs = parse_transactions(data)
    assert [t.id for t in txs] == ["3_1784", "1_4737"]
    assert txs[1].pos_id == 1
    assert txs[1].begin == 1792011709
    assert txs[1].end == 1792011760
    assert txs[0].end == 0
    assert txs[1].text == "#1 05:01:40 Lyn\n2x BILLIARD P100\nTOTAL P100 Cash"


def test_parse_handles_empty_and_list_payloads() -> None:
    assert parse_devices(None) == {}
    assert parse_transactions([]) == []
    assert (
        parse_transactions(
            [{"id": "5_9", "pos_id": 5, "begin_tmstmp": 7, "content": [{"content": "x"}]}]
        )[0].text
        == "x"
    )


def test_poll_interval_config() -> None:
    from surveillance.config import MIN_POLL_INTERVAL, _config_from_data

    assert _config_from_data({"general": {}}).poll_interval_transactions == 5
    cfg = _config_from_data({"general": {"poll_interval_transactions": 0}})
    assert cfg.poll_interval_transactions == MIN_POLL_INTERVAL


def test_receipt_lines_keeps_header_and_total() -> None:
    from surveillance.ui.liveview import receipt_lines

    text = "\n".join(["#1 05:00 Lyn", *[f"1x item{i}" for i in range(10)], "TOTAL P1 Cash"])
    assert receipt_lines(text, 20) == text
    capped = receipt_lines(text, 5).splitlines()
    assert len(capped) == 5
    assert capped[0] == "#1 05:00 Lyn"
    assert capped[-1] == "TOTAL P1 Cash"
    assert "more lines" in capped[2]
    assert receipt_lines(text, 3).splitlines() == [
        "#1 05:00 Lyn",
        "… 10 more lines",
        "TOTAL P1 Cash",
    ]


def test_receipt_settings_are_registered_and_clamped() -> None:
    from surveillance import settings_registry
    from surveillance.ui import liveview

    section = next(s for s in settings_registry.SECTIONS if s.title == "POS receipt overlay")
    assert {s.key for s in section.settings} == {
        "receipt_scale_percent",
        "receipt_max_lines",
        "receipt_seconds",
    }
    assert [b.key for b in section.bool_settings] == ["receipt_enabled"]
    old = liveview._RECEIPT_SCALE_PERCENT
    try:
        liveview.set_receipt_scale_percent(5)
        assert liveview._RECEIPT_SCALE_PERCENT == 25.0
        liveview.set_receipt_scale_percent(70)
        assert liveview._RECEIPT_SCALE_PERCENT == 70.0
    finally:
        liveview.set_receipt_scale_percent(old)


def test_fresh_receipts_counts_a_sale_from_its_end() -> None:
    cutoff = 1000.0
    long_sale = Transaction(id="1_1", pos_id=1, begin=900, end=1010, text="a")
    old_sale = Transaction(id="1_2", pos_id=1, begin=900, end=950, text="b")
    open_sale = Transaction(id="1_3", pos_id=1, begin=1005, end=0, text="c")
    seen: dict[str, str] = {}
    assert fresh_receipts([long_sale, old_sale, open_sale], seen, cutoff) == [
        long_sale,
        open_sale,
    ]
    assert seen == {"1_1": "a", "1_2": "b", "1_3": "c"}
    assert fresh_receipts([long_sale, old_sale, open_sale], seen, cutoff) == []


def test_fresh_receipts_redraws_a_receipt_whose_text_grew() -> None:
    seen: dict[str, str] = {}
    first = Transaction(id="1_1", pos_id=1, begin=900, end=0, text="#1 Lyn")
    assert fresh_receipts([first], seen, 1000.0) == []
    grown = Transaction(id="1_1", pos_id=1, begin=900, end=0, text="#1 Lyn\n2x BEER")
    assert fresh_receipts([grown], seen, 1000.0) == [grown]
    assert seen == {"1_1": "#1 Lyn\n2x BEER"}


def test_parse_server_time() -> None:
    assert parse_server_time({"data": [], "timestamp": "1505355467"}) == 1505355467
    assert parse_server_time({"data": []}) == 0
    assert parse_server_time([]) == 0


def test_only_slots_on_screen_count() -> None:
    """A hidden slot keeps its camera across layout switches, and the
    Live page keeps every camera while another page is shown: neither is
    on screen, so neither draws a receipt or keeps the poll going."""
    from types import SimpleNamespace

    from surveillance.ui.liveview import LiveView

    shown: list[int] = []

    def slot(index: int, camera_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            camera=SimpleNamespace(id=camera_id),
            show_receipt=lambda text: shown.append(index),
        )

    page = SimpleNamespace(
        _slots=[slot(0, 7), slot(1, 7), slot(2, 8)],
        _active=[0],
        _streams_paused=False,
    )
    page._shown_slots = lambda: LiveView._shown_slots(page)  # type: ignore[arg-type]

    assert LiveView.displayed_camera_ids(page) == {7}  # type: ignore[arg-type]
    LiveView.show_transaction(page, 7, "receipt")  # type: ignore[arg-type]
    assert shown == [0]

    page._streams_paused = True
    assert LiveView.displayed_camera_ids(page) == set()  # type: ignore[arg-type]
    LiveView.show_transaction(page, 7, "receipt")  # type: ignore[arg-type]
    assert shown == [0]
