import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (
            os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN")
            and os.environ.get("GOOGLE_ADS_TEST_CUSTOMER_ID")
        ),
        reason="needs GOOGLE_ADS_DEVELOPER_TOKEN and GOOGLE_ADS_TEST_CUSTOMER_ID",
    ),
]

from google_ads_mcp import api, server  # noqa: E402

CID = os.environ.get("GOOGLE_ADS_TEST_CUSTOMER_ID", "")
_real_mutate = api.mutate


def _tool(name):
    tool = getattr(server, name)
    return getattr(tool, "fn", tool)


@pytest.fixture(autouse=True)
def validate_only_guard(monkeypatch):
    def guarded(customer_id, operations, validate_only):
        assert validate_only is True, "integration tests must never apply changes"
        return _real_mutate(customer_id, operations, validate_only)

    monkeypatch.setattr(api, "mutate", guarded)


def _first(fields, resource, conditions):
    rows = _tool("search")(CID, fields, resource, conditions, limit=1)
    if not rows:
        pytest.skip(f"no {resource} matching {conditions} in account {CID}")
    return rows[0]


@pytest.fixture(scope="module")
def campaign():
    return _first(
        ["campaign.id", "campaign.name"],
        "campaign",
        [
            "campaign.status = 'ENABLED'",
            "campaign.advertising_channel_type = 'SEARCH'",
            "campaign.bidding_strategy IS NULL",
        ],
    )


@pytest.fixture(scope="module")
def keyword():
    return _first(
        ["ad_group.id", "ad_group_criterion.criterion_id", "ad_group_criterion.resource_name"],
        "ad_group_criterion",
        [
            "ad_group_criterion.type = 'KEYWORD'",
            "ad_group_criterion.status = 'ENABLED'",
            "ad_group_criterion.negative = FALSE",
            "campaign.status = 'ENABLED'",
        ],
    )


@pytest.fixture(scope="module")
def ad():
    return _first(
        ["ad_group.id", "ad_group_ad.ad.id"],
        "ad_group_ad",
        ["ad_group_ad.status = 'ENABLED'", "campaign.status = 'ENABLED'"],
    )


@pytest.fixture(scope="module")
def negative_list():
    return _first(
        ["shared_set.id"],
        "shared_set",
        ["shared_set.type = 'NEGATIVE_KEYWORDS'", "shared_set.status = 'ENABLED'"],
    )


def _assert_preview(result):
    assert result["applied"] is False
    assert "resource_names" not in result


def test_list_accessible_customers():
    customers = _tool("list_accessible_customers")()
    assert customers
    assert all(customer.isdigit() for customer in customers)


def test_search_returns_requested_fields(campaign):
    assert set(campaign) == {"campaign.id", "campaign.name"}
    assert campaign["campaign.id"].isdigit()


def test_search_with_metrics_and_dates():
    rows = _tool("search")(
        CID,
        ["campaign.id", "metrics.clicks", "metrics.cost_micros"],
        "campaign",
        ["segments.date DURING LAST_7_DAYS"],
        limit=5,
    )
    assert all(set(row) == {"campaign.id", "metrics.clicks", "metrics.cost_micros"} for row in rows)


def test_get_resource_metadata():
    metadata = _tool("get_resource_metadata")("campaign")
    assert "campaign.id" in metadata["selectable"]
    assert "metrics.clicks" in metadata["selectable"]
    assert "campaign.status" in metadata["filterable"]


def test_bad_query_reports_request_id():
    with pytest.raises(api.GoogleAdsApiError, match="Request ID"):
        _tool("search")(CID, ["campaign.nope"], "campaign")


def test_set_status_campaign(campaign):
    _assert_preview(_tool("set_status")(CID, "campaign", campaign["campaign.id"], "PAUSED"))


def test_set_status_ad_group(keyword):
    _assert_preview(_tool("set_status")(CID, "ad_group", keyword["ad_group.id"], "PAUSED"))


def test_set_status_ad(ad):
    _assert_preview(
        _tool("set_status")(
            CID, "ad", ad["ad_group_ad.ad.id"], "PAUSED", ad_group_id=ad["ad_group.id"]
        )
    )


def test_set_status_keyword(keyword):
    _assert_preview(
        _tool("set_status")(
            CID,
            "keyword",
            keyword["ad_group_criterion.criterion_id"],
            "PAUSED",
            ad_group_id=keyword["ad_group.id"],
        )
    )


def test_set_campaign_budget(campaign):
    try:
        result = _tool("set_campaign_budget")(CID, campaign["campaign.id"], 1.23)
    except ValueError as exc:
        pytest.skip(str(exc))
    _assert_preview(result)
    assert result["after"]["daily_amount"] == 1.23
    assert result["currency"]


def test_set_bid_ad_group(keyword):
    _assert_preview(_tool("set_bid")(CID, keyword["ad_group.id"], 1.5))


def test_set_bid_keyword(keyword):
    _assert_preview(
        _tool("set_bid")(
            CID,
            keyword["ad_group.id"],
            1.5,
            keyword_id=keyword["ad_group_criterion.criterion_id"],
        )
    )


@pytest.mark.parametrize(
    "strategy, targets",
    [
        ("MANUAL_CPC", {}),
        ("MAXIMIZE_CLICKS", {}),
        ("MAXIMIZE_CONVERSIONS", {"target_cpa": 12.5}),
        ("MAXIMIZE_CONVERSION_VALUE", {"target_roas": 4.0}),
    ],
)
def test_set_bidding_strategy(campaign, strategy, targets):
    result = _tool("set_bidding_strategy")(CID, campaign["campaign.id"], strategy, **targets)
    _assert_preview(result)
    assert result["after"]["strategy"] == strategy


def test_add_keywords_to_ad_group(keyword):
    result = _tool("add_keywords")(
        CID,
        [{"text": "mcp validate only test", "match_type": "PHRASE"}],
        ad_group_id=keyword["ad_group.id"],
    )
    _assert_preview(result)


def test_add_negative_keywords_to_campaign(campaign):
    result = _tool("add_keywords")(
        CID,
        [{"text": "mcp validate only test", "match_type": "EXACT"}],
        campaign_id=campaign["campaign.id"],
    )
    _assert_preview(result)


def test_add_keywords_to_list(negative_list):
    result = _tool("add_keywords")(
        CID,
        [{"text": "mcp validate only test", "match_type": "BROAD"}],
        list_id=negative_list["shared_set.id"],
    )
    _assert_preview(result)


def test_remove_keyword(keyword):
    result = _tool("remove_keyword")(CID, keyword["ad_group_criterion.resource_name"])
    _assert_preview(result)
    assert result["before"]["text"]


def test_create_negative_keyword_list():
    result = _tool("create_negative_keyword_list")(
        CID,
        "mcp validate only test",
        [
            {"text": "gratis", "match_type": "BROAD"},
            {"text": "vagas de emprego", "match_type": "PHRASE"},
        ],
    )
    _assert_preview(result)


def test_link_and_unlink_negative_keyword_list(negative_list, campaign):
    link = _tool("link_negative_keyword_list")
    for unlink in (False, True):
        try:
            result = link(
                CID, negative_list["shared_set.id"], [campaign["campaign.id"]], unlink=unlink
            )
        except ValueError as exc:
            assert "already" in str(exc)
            continue
        _assert_preview(result)
