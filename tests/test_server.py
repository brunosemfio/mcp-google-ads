import asyncio

import pytest
from fastmcp import Client

from google_ads_mcp import api, server

CID = "1234567890"


def _tool(name):
    tool = getattr(server, name)
    return getattr(tool, "fn", tool)


@pytest.fixture
def ads(monkeypatch):
    state = {"rows": {}, "queries": [], "mutations": []}

    def fake_search(customer_id, query, login_customer_id=None):
        state["queries"].append(query)
        for marker, rows in state["rows"].items():
            if marker in query:
                return rows
        return []

    def fake_mutate(customer_id, operations, validate_only):
        state["mutations"].append(
            {"customer_id": customer_id, "operations": operations, "validate_only": validate_only}
        )
        return [] if validate_only else [f"customers/{customer_id}/x/{i}" for i in range(3)]

    monkeypatch.setattr(api, "search_stream", fake_search)
    monkeypatch.setattr(api, "mutate", fake_mutate)
    return state


class FakeResponse:
    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.content = b"x" if payload is not None else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _failure(message="Bad thing", code=None, request_id="req-1"):
    return {
        "error": {
            "code": 400,
            "message": "Request contains an invalid argument.",
            "details": [
                {
                    "@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure",
                    "errors": [{"errorCode": code or {"queryError": "BAD"}, "message": message}],
                    "requestId": request_id,
                }
            ],
        }
    }


def test_error_parses_google_ads_failure():
    error = api._error(FakeResponse(400, _failure()))
    assert str(error) == "Request ID: req-1\nGoogle Ads API Error: Bad thing (queryError=BAD)"


def test_error_parses_list_wrapped_body():
    error = api._error(FakeResponse(400, [_failure(request_id="req-2")]))
    assert "Request ID: req-2" in str(error)


def test_error_falls_back_to_header_and_text():
    error = api._error(FakeResponse(500, None, text="boom", headers={"request-id": "h-1"}))
    assert str(error) == "HTTP 500: boom"
    error = api._error(
        FakeResponse(403, {"error": {"message": "denied"}}, headers={"request-id": "h-2"})
    )
    assert str(error) == "Request ID: h-2\nHTTP 403: denied"


def test_headers(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "tok")
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "999-888-7777")
    assert api._headers() == {"developer-token": "tok", "login-customer-id": "9998887777"}
    assert api._headers("111-222-3333")["login-customer-id"] == "1112223333"
    monkeypatch.delenv("GOOGLE_ADS_DEVELOPER_TOKEN")
    with pytest.raises(api.GoogleAdsApiError, match="DEVELOPER_TOKEN"):
        api._headers()


def test_api_helpers(monkeypatch):
    calls = []

    def fake_post(path, body, login_customer_id=None):
        calls.append((path, body))
        if path.endswith("searchStream"):
            return [{"results": [{"a": 1}]}, {"results": [{"a": 2}]}, {"fieldMask": "a"}]
        if path == "googleAdsFields:search":
            return {
                "results": [{"name": "x"}],
                "nextPageToken": "" if "pageToken" in body else "p2",
            }
        return {"mutateOperationResponses": [{"campaignResult": {"resourceName": "rn/1"}}]}

    monkeypatch.setattr(api, "post", fake_post)
    assert api.search_stream(CID, "q") == [{"a": 1}, {"a": 2}]
    assert api.search_fields("q") == [{"name": "x"}, {"name": "x"}]
    assert calls[-1] == ("googleAdsFields:search", {"query": "q", "pageToken": "p2"})
    assert api.mutate(CID, [{"op": 1}], validate_only=True) == ["rn/1"]
    assert calls[-1] == (
        f"customers/{CID}/googleAds:mutate",
        {"mutateOperations": [{"op": 1}], "validateOnly": True},
    )


def test_list_accessible_customers(monkeypatch):
    monkeypatch.setattr(api, "get", lambda path: {"resourceNames": ["customers/1", "customers/2"]})
    assert _tool("list_accessible_customers")() == ["1", "2"]


def test_search_builds_query_and_flattens(ads):
    ads["rows"]["FROM ad_group_criterion"] = [
        {"adGroupCriterion": {"keyword": {"matchType": "EXACT"}}, "metrics": {"clicks": "3"}}
    ]
    rows = _tool("search")(
        "123-456-7890",
        ["ad_group_criterion.keyword.match_type", "metrics.clicks", "metrics.cost_micros"],
        "ad_group_criterion",
        conditions=["metrics.clicks > 0", "campaign.status = 'ENABLED'"],
        orderings=["metrics.clicks DESC"],
        limit=10,
    )
    assert ads["queries"] == [
        "SELECT ad_group_criterion.keyword.match_type,metrics.clicks,metrics.cost_micros "
        "FROM ad_group_criterion WHERE metrics.clicks > 0 AND campaign.status = 'ENABLED' "
        "ORDER BY metrics.clicks DESC LIMIT 10 PARAMETERS omit_unselected_resource_names=true"
    ]
    assert rows == [
        {
            "ad_group_criterion.keyword.match_type": "EXACT",
            "metrics.clicks": "3",
            "metrics.cost_micros": None,
        }
    ]


def test_get_resource_metadata(monkeypatch):
    queries = []

    def fake_fields(query, login_customer_id=None):
        queries.append(query)
        if "ATTRIBUTE" in query:
            return [{"name": "campaign.id", "selectable": True, "filterable": True}]
        return [
            {"name": "metrics.clicks", "selectable": True, "sortable": True},
            {"name": "campaign.id", "selectable": True},
        ]

    monkeypatch.setattr(api, "search_fields", fake_fields)
    assert _tool("get_resource_metadata")("campaign") == {
        "resource": "campaign",
        "selectable": ["campaign.id", "metrics.clicks"],
        "filterable": ["campaign.id"],
        "sortable": ["metrics.clicks"],
    }
    assert len(queries) == 2
    with pytest.raises(ValueError):
        _tool("get_resource_metadata")("campaign' OR 1=1")


def test_micros():
    assert server._micros(49.9) == "49900000"
    assert server._micros("0.01") == "10000"
    for bad in (0, -1, 1.234, "abc"):
        with pytest.raises(ValueError):
            server._micros(bad)


def test_ids_must_be_digits(ads):
    with pytest.raises(ValueError, match="only digits"):
        _tool("set_status")(CID, "campaign", "1 OR 1=1", "PAUSED")
    with pytest.raises(ValueError, match="customer_id"):
        _tool("set_status")("abc", "campaign", "1", "PAUSED")


@pytest.mark.parametrize(
    "resource_type, key, operation, resource_name",
    [
        ("campaign", "campaign", "campaignOperation", "rn/c"),
        ("ad_group", "adGroup", "adGroupOperation", "rn/ag"),
        ("ad", "adGroupAd", "adGroupAdOperation", "rn/5~7"),
        ("keyword", "adGroupCriterion", "adGroupCriterionOperation", "rn/5~9"),
    ],
)
def test_set_status(ads, resource_type, key, operation, resource_name):
    ads["rows"]["FROM"] = [{key: {"resourceName": resource_name, "status": "ENABLED"}}]
    result = _tool("set_status")(CID, resource_type, "7", "PAUSED", ad_group_id="5")
    mutation = ads["mutations"][0]
    assert mutation["validate_only"] is True
    assert mutation["operations"] == [
        {
            operation: {
                "update": {"resourceName": resource_name, "status": "PAUSED"},
                "updateMask": "status",
            }
        }
    ]
    assert result["before"]["status"] == "ENABLED"
    assert result["after"]["status"] == "PAUSED"
    assert result["applied"] is False
    assert "resource_names" not in result


def test_set_status_rules(ads):
    with pytest.raises(ValueError, match="ad_group_id is required"):
        _tool("set_status")(CID, "keyword", "7", "PAUSED")
    with pytest.raises(ValueError, match="not found"):
        _tool("set_status")(CID, "campaign", "7", "PAUSED")
    ads["rows"]["FROM campaign"] = [{"campaign": {"resourceName": "rn", "status": "REMOVED"}}]
    with pytest.raises(ValueError, match="removed"):
        _tool("set_status")(CID, "campaign", "7", "PAUSED")
    ads["rows"]["FROM campaign"] = [{"campaign": {"resourceName": "rn", "status": "PAUSED"}}]
    assert _tool("set_status")(CID, "campaign", "7", "PAUSED")["warnings"]


def test_confirm_applies_and_returns_resource_names(ads):
    ads["rows"]["FROM campaign"] = [{"campaign": {"resourceName": "rn", "status": "ENABLED"}}]
    result = _tool("set_status")(CID, "campaign", "7", "PAUSED", confirm=True)
    assert ads["mutations"][0]["validate_only"] is False
    assert result["applied"] is True
    assert result["resource_names"]


def _budget_row(**budget):
    return {
        "customer": {"currencyCode": "USD"},
        "campaign": {"name": "C"},
        "campaignBudget": {
            "resourceName": "rn/b",
            "amountMicros": "160000000",
            "explicitlyShared": False,
            "referenceCount": "1",
            "period": "DAILY",
            **budget,
        },
    }


def test_set_campaign_budget(ads):
    ads["rows"]["FROM campaign"] = [_budget_row()]
    result = _tool("set_campaign_budget")(CID, "7", 49.9)
    assert ads["mutations"][0]["operations"] == [
        {
            "campaignBudgetOperation": {
                "update": {"resourceName": "rn/b", "amountMicros": "49900000"},
                "updateMask": "amount_micros",
            }
        }
    ]
    assert result["before"]["daily_amount"] == 160.0
    assert result["after"]["daily_amount"] == 49.9
    assert result["currency"] == "USD"


@pytest.mark.parametrize(
    "budget, message",
    [
        ({"explicitlyShared": True}, "shared"),
        ({"referenceCount": "2"}, "shared"),
        ({"period": "CUSTOM_PERIOD"}, "daily"),
    ],
)
def test_set_campaign_budget_refusals(ads, budget, message):
    ads["rows"]["FROM campaign"] = [_budget_row(**budget)]
    with pytest.raises(ValueError, match=message):
        _tool("set_campaign_budget")(CID, "7", 10)
    assert not ads["mutations"]


def test_set_bid_ad_group_and_keyword(ads):
    ads["rows"]["FROM ad_group_criterion"] = [
        {
            "customer": {"currencyCode": "BRL"},
            "campaign": {"biddingStrategyType": "MANUAL_CPC"},
            "adGroupCriterion": {"resourceName": "rn/5~9", "cpcBidMicros": "1000000"},
        }
    ]
    ads["rows"]["FROM ad_group"] = [
        {
            "customer": {"currencyCode": "BRL"},
            "campaign": {"biddingStrategyType": "MAXIMIZE_CONVERSIONS"},
            "adGroup": {"resourceName": "rn/5", "name": "G"},
        }
    ]
    keyword = _tool("set_bid")(CID, "5", 2.5, keyword_id="9")
    group = _tool("set_bid")(CID, "5", 2.5)
    assert ads["mutations"][0]["operations"][0]["adGroupCriterionOperation"]["update"] == {
        "resourceName": "rn/5~9",
        "cpcBidMicros": "2500000",
    }
    assert ads["mutations"][1]["operations"][0]["adGroupOperation"]["updateMask"] == (
        "cpc_bid_micros"
    )
    assert keyword["before"]["cpc_bid"] == 1.0
    assert keyword["warnings"] == []
    assert group["before"]["cpc_bid"] is None
    assert "MAXIMIZE_CONVERSIONS" in group["warnings"][0]


def _campaign_row(**campaign):
    return {
        "customer": {"currencyCode": "BRL"},
        "campaign": {
            "resourceName": "rn/c",
            "name": "C",
            "biddingStrategyType": "TARGET_SPEND",
            **campaign,
        },
    }


@pytest.mark.parametrize(
    "strategy, targets, field, scheme, mask",
    [
        ("MANUAL_CPC", {}, "manualCpc", {}, "manual_cpc.enhanced_cpc_enabled"),
        ("MAXIMIZE_CLICKS", {}, "targetSpend", {}, "target_spend.cpc_bid_ceiling_micros"),
        (
            "MAXIMIZE_CONVERSIONS",
            {"target_cpa": 12.5},
            "maximizeConversions",
            {"targetCpaMicros": "12500000"},
            "maximize_conversions.target_cpa_micros",
        ),
        (
            "MAXIMIZE_CONVERSION_VALUE",
            {"target_roas": 4.0},
            "maximizeConversionValue",
            {"targetRoas": 4.0},
            "maximize_conversion_value.target_roas",
        ),
    ],
)
def test_set_bidding_strategy(ads, strategy, targets, field, scheme, mask):
    ads["rows"]["FROM campaign"] = [_campaign_row()]
    result = _tool("set_bidding_strategy")(CID, "7", strategy, **targets)
    assert ads["mutations"][0]["operations"] == [
        {
            "campaignOperation": {
                "update": {"resourceName": "rn/c", field: scheme},
                "updateMask": mask,
            }
        }
    ]
    assert result["before"]["strategy"] == "MAXIMIZE_CLICKS"
    assert result["after"]["strategy"] == strategy
    assert bool(result["warnings"]) == (strategy != "MAXIMIZE_CLICKS")


def test_set_bidding_strategy_rules(ads):
    with pytest.raises(ValueError, match="target_cpa"):
        _tool("set_bidding_strategy")(CID, "7", "MANUAL_CPC", target_cpa=10)
    with pytest.raises(ValueError, match="target_roas"):
        _tool("set_bidding_strategy")(CID, "7", "MAXIMIZE_CONVERSIONS", target_roas=2)
    ads["rows"]["FROM campaign"] = [
        _campaign_row(biddingStrategy="customers/1/biddingStrategies/2")
    ]
    with pytest.raises(ValueError, match="portfolio"):
        _tool("set_bidding_strategy")(CID, "7", "MANUAL_CPC")
    ads["rows"]["FROM campaign"] = [
        _campaign_row(
            biddingStrategyType="MAXIMIZE_CONVERSIONS",
            maximizeConversions={"targetCpaMicros": "12500000"},
        )
    ]
    result = _tool("set_bidding_strategy")(CID, "7", "MAXIMIZE_CONVERSIONS", target_cpa=12.5)
    assert result["before"] == result["after"]
    assert result["warnings"] == []


def test_add_keywords_to_ad_group_skips_existing(ads):
    ads["rows"]["SELECT ad_group.name"] = [{"adGroup": {"name": "G"}}]
    ads["rows"]["FROM ad_group_criterion"] = [
        {"adGroupCriterion": {"keyword": {"text": "motel", "matchType": "EXACT"}}}
    ]
    result = _tool("add_keywords")(
        CID,
        [
            {"text": "Motel", "match_type": "exact"},
            {"text": " motel barato ", "match_type": "PHRASE"},
        ],
        ad_group_id="5",
    )
    assert "ad_group_criterion.negative = FALSE" in ads["queries"][1]
    assert ads["mutations"][0]["operations"] == [
        {
            "adGroupCriterionOperation": {
                "create": {
                    "adGroup": f"customers/{CID}/adGroups/5",
                    "negative": False,
                    "keyword": {"text": "motel barato", "matchType": "PHRASE"},
                }
            }
        }
    ]
    assert result["after"]["added"] == [{"text": "motel barato", "match_type": "PHRASE"}]
    assert "motel" in result["warnings"][0].lower()


def test_add_keywords_to_campaign_is_always_negative(ads):
    ads["rows"]["SELECT campaign.name"] = [{"campaign": {"name": "C"}}]
    _tool("add_keywords")(CID, [{"text": "gratis", "match_type": "BROAD"}], campaign_id="7")
    create = ads["mutations"][0]["operations"][0]["campaignCriterionOperation"]["create"]
    assert create["campaign"] == f"customers/{CID}/campaigns/7"
    assert create["negative"] is True


def test_add_keywords_to_list(ads):
    ads["rows"]["FROM shared_set"] = [
        {"sharedSet": {"name": "L", "type": "NEGATIVE_KEYWORDS", "status": "ENABLED"}}
    ]
    _tool("add_keywords")(CID, [{"text": "gratis", "match_type": "BROAD"}], list_id="3")
    assert ads["mutations"][0]["operations"] == [
        {
            "sharedCriterionOperation": {
                "create": {
                    "sharedSet": f"customers/{CID}/sharedSets/3",
                    "keyword": {"text": "gratis", "matchType": "BROAD"},
                }
            }
        }
    ]
    ads["rows"]["FROM shared_set"] = [{"sharedSet": {"name": "L", "type": "NEGATIVE_PLACEMENTS"}}]
    with pytest.raises(ValueError, match="not a negative keyword list"):
        _tool("add_keywords")(CID, [{"text": "gratis", "match_type": "BROAD"}], list_id="3")


def test_add_keywords_rules(ads):
    keywords = [{"text": "a", "match_type": "BROAD"}]
    with pytest.raises(ValueError, match="exactly one"):
        _tool("add_keywords")(CID, keywords)
    with pytest.raises(ValueError, match="exactly one"):
        _tool("add_keywords")(CID, keywords, ad_group_id="1", campaign_id="2")
    with pytest.raises(ValueError, match="match_type"):
        _tool("add_keywords")(CID, [{"text": "a", "match_type": "FUZZY"}], ad_group_id="1")
    with pytest.raises(ValueError, match="empty"):
        _tool("add_keywords")(CID, [], ad_group_id="1")
    ads["rows"]["SELECT ad_group.name"] = [{"adGroup": {"name": "G"}}]
    ads["rows"]["FROM ad_group_criterion"] = [
        {"adGroupCriterion": {"keyword": {"text": "a", "matchType": "BROAD"}}}
    ]
    with pytest.raises(ValueError, match="already exist"):
        _tool("add_keywords")(CID, keywords, ad_group_id="1")
    assert not ads["mutations"]


@pytest.mark.parametrize(
    "kind, resource, operation",
    [
        ("adGroupCriteria", "adGroupCriterion", "adGroupCriterionOperation"),
        ("campaignCriteria", "campaignCriterion", "campaignCriterionOperation"),
        ("sharedCriteria", "sharedCriterion", "sharedCriterionOperation"),
    ],
)
def test_remove_keyword(ads, kind, resource, operation):
    name = f"customers/{CID}/{kind}/5~9"
    ads["rows"]["FROM"] = [{resource: {"keyword": {"text": "k", "matchType": "EXACT"}}}]
    result = _tool("remove_keyword")(CID, name)
    assert ads["mutations"][0]["operations"] == [{operation: {"remove": name}}]
    assert f"resource_name = '{name}'" in ads["queries"][0]
    assert result["before"] == {"text": "k", "match_type": "EXACT"}
    assert result["after"] is None


def test_remove_keyword_rules(ads):
    for name in (
        "customers/999/adGroupCriteria/5~9",
        f"customers/{CID}/campaigns/5",
        f"customers/{CID}/adGroupCriteria/5~9' OR '1'='1",
    ):
        with pytest.raises(ValueError, match="Invalid keyword resource_name"):
            _tool("remove_keyword")(CID, name)
    with pytest.raises(ValueError, match="not found"):
        _tool("remove_keyword")(CID, f"customers/{CID}/adGroupCriteria/5~9")


def test_create_negative_keyword_list(ads):
    result = _tool("create_negative_keyword_list")(
        CID, " Geral ", [{"text": "gratis", "match_type": "BROAD"}]
    )
    temp = f"customers/{CID}/sharedSets/-1"
    assert ads["mutations"][0]["operations"] == [
        {
            "sharedSetOperation": {
                "create": {"resourceName": temp, "name": "Geral", "type": "NEGATIVE_KEYWORDS"}
            }
        },
        {
            "sharedCriterionOperation": {
                "create": {"sharedSet": temp, "keyword": {"text": "gratis", "matchType": "BROAD"}}
            }
        },
    ]
    assert result["after"]["name"] == "Geral"
    with pytest.raises(ValueError, match="name"):
        _tool("create_negative_keyword_list")(CID, " ", [{"text": "a", "match_type": "BROAD"}])


def _link_rows(ads, linked):
    ads["rows"]["FROM shared_set"] = [{"sharedSet": {"name": "L", "type": "NEGATIVE_KEYWORDS"}}]
    ads["rows"]["FROM campaign_shared_set"] = [{"campaign": {"id": c}} for c in linked]
    ads["rows"]["FROM campaign WHERE"] = [
        {"campaign": {"id": "7", "name": "A"}},
        {"campaign": {"id": "8", "name": "B"}},
    ]


def test_link_negative_keyword_list(ads):
    _link_rows(ads, linked=["7"])
    result = _tool("link_negative_keyword_list")(CID, "3", ["7", "8"])
    assert ads["mutations"][0]["operations"] == [
        {
            "campaignSharedSetOperation": {
                "create": {
                    "campaign": f"customers/{CID}/campaigns/8",
                    "sharedSet": f"customers/{CID}/sharedSets/3",
                }
            }
        }
    ]
    assert result["before"]["linked"] == ["A"]
    assert result["after"]["linked"] == ["A", "B"]
    assert "'A' is already linked" in result["warnings"][0]


def test_unlink_negative_keyword_list(ads):
    _link_rows(ads, linked=["7"])
    result = _tool("link_negative_keyword_list")(CID, "3", ["7", "8"], unlink=True)
    assert ads["mutations"][0]["operations"] == [
        {"campaignSharedSetOperation": {"remove": f"customers/{CID}/campaignSharedSets/7~3"}}
    ]
    assert result["after"]["linked"] == []


def test_link_negative_keyword_list_rules(ads):
    _link_rows(ads, linked=["7", "8"])
    with pytest.raises(ValueError, match="already linked"):
        _tool("link_negative_keyword_list")(CID, "3", ["7", "8"])
    with pytest.raises(ValueError, match="not found"):
        _tool("link_negative_keyword_list")(CID, "3", ["9"])
    with pytest.raises(ValueError, match="empty"):
        _tool("link_negative_keyword_list")(CID, "3", [])


def test_tool_annotations():
    async def list_tools():
        async with Client(server.mcp) as client:
            return await client.list_tools()

    tools = {tool.name: tool.annotations for tool in asyncio.run(list_tools())}
    read = {"list_accessible_customers", "search", "get_resource_metadata"}
    assert len(tools) == 11
    for name, annotations in tools.items():
        if name in read:
            assert annotations.readOnlyHint is True
        else:
            assert annotations.readOnlyHint is False
            assert annotations.destructiveHint is True
