"""Tests for the Surveillance Station Transactions (POS) parsers."""

from surveillance.services.transactions import parse_devices, parse_transactions


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
    assert receipt_lines(text, 2).splitlines() == ["#1 05:00 Lyn", "1x item0"]


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
