import asyncio
import os

import pytest
from fastmcp import Client

from google_ads_mcp import api, auth, server

CID = "1234567890"


@pytest.fixture(autouse=True)
def isolate_env_files(monkeypatch, tmp_path):
    monkeypatch.setenv(auth.ENV_FILE_ENV, str(tmp_path / "absent.env"))
    monkeypatch.setattr(auth, "_env_files_loaded", False)


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


def test_env_file_fills_missing_variables(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_bytes(
        b"\xef\xbb\xbf# credenciais\r\n"
        b"export GOOGLE_ADS_DEVELOPER_TOKEN=from_file\r\n"
        b'GOOGLE_ADS_LOGIN_CUSTOMER_ID="999-888-7777"\r\n'
    )
    monkeypatch.setenv(auth.ENV_FILE_ENV, str(env_file))
    monkeypatch.delenv("GOOGLE_ADS_DEVELOPER_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", raising=False)

    assert api._headers() == {"developer-token": "from_file", "login-customer-id": "9998887777"}


def test_real_environment_wins_over_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_ADS_DEVELOPER_TOKEN=from_file\n")
    monkeypatch.setenv(auth.ENV_FILE_ENV, str(env_file))
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "from_env")
    monkeypatch.delenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", raising=False)

    assert api._headers() == {"developer-token": "from_env"}


def test_session_loads_env_file_before_resolving_credentials(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_APPLICATION_CREDENTIALS=/keys/ads.json\n")
    monkeypatch.setenv(auth.ENV_FILE_ENV, str(env_file))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(auth, "_session", None)
    seen = {}

    def fake_default(scopes):
        seen["path"] = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        return object(), None

    monkeypatch.setattr(auth.google.auth, "default", fake_default)
    monkeypatch.setattr(auth, "AuthorizedSession", lambda credentials: credentials)
    auth.get_session()

    assert seen["path"] == "/keys/ads.json"


def test_credentials_variable_wins_over_inherited_application_credentials(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{auth.CREDENTIALS_ENV}=/keys/ads.json\n")
    monkeypatch.setenv(auth.ENV_FILE_ENV, str(env_file))
    monkeypatch.delenv(auth.CREDENTIALS_ENV, raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/keys/personal.json")
    monkeypatch.setattr(auth, "_session", None)
    seen = {}

    def fake_load(path, scopes):
        seen["path"], seen["scopes"] = path, scopes
        return object(), None

    monkeypatch.setattr(auth.google.auth, "load_credentials_from_file", fake_load)
    monkeypatch.setattr(auth, "AuthorizedSession", lambda credentials: credentials)
    auth.get_session()

    assert seen == {"path": "/keys/ads.json", "scopes": auth.SCOPES}


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
def test_remove_keywords(ads, kind, resource, operation):
    name = f"customers/{CID}/{kind}/5~9"
    keyword = {"text": "k", "matchType": "EXACT"}
    ads["rows"]["FROM"] = [{resource: {"resourceName": name, "keyword": keyword}}]
    result = _tool("remove_keywords")(CID, [name, name])
    assert ads["mutations"][0]["operations"] == [{operation: {"remove": name}}]
    assert f"resource_name IN ('{name}')" in ads["queries"][0]
    assert result["before"] == [{"resource_name": name, "text": "k", "match_type": "EXACT"}]
    assert result["after"] is None


def test_remove_keywords_mixes_kinds_in_one_call(ads):
    group = f"customers/{CID}/adGroupCriteria/5~9"
    shared = f"customers/{CID}/sharedCriteria/3~4"
    keyword = {"text": "k", "matchType": "EXACT"}
    ads["rows"]["FROM ad_group_criterion"] = [
        {"adGroupCriterion": {"resourceName": group, "keyword": keyword}}
    ]
    ads["rows"]["FROM shared_criterion"] = [
        {"sharedCriterion": {"resourceName": shared, "keyword": keyword}}
    ]
    _tool("remove_keywords")(CID, [group, shared])
    assert ads["mutations"] == [
        {
            "customer_id": CID,
            "operations": [
                {"adGroupCriterionOperation": {"remove": group}},
                {"sharedCriterionOperation": {"remove": shared}},
            ],
            "validate_only": True,
        }
    ]


def test_remove_keywords_rules(ads):
    for name in (
        "customers/999/adGroupCriteria/5~9",
        f"customers/{CID}/campaigns/5",
        f"customers/{CID}/adGroupCriteria/5~9' OR '1'='1",
    ):
        with pytest.raises(ValueError, match="Invalid keyword resource_name"):
            _tool("remove_keywords")(CID, [name])
    with pytest.raises(ValueError, match="empty"):
        _tool("remove_keywords")(CID, [])
    with pytest.raises(ValueError, match="not found"):
        _tool("remove_keywords")(CID, [f"customers/{CID}/adGroupCriteria/5~9"])
    assert not ads["mutations"]


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
    assert len(tools) == 17
    for name, annotations in tools.items():
        if name in read:
            assert annotations.readOnlyHint is True
        else:
            assert annotations.readOnlyHint is False
            assert annotations.destructiveHint is True


def _clone_rows(ads, **campaign):
    ads["rows"]["FROM campaign WHERE"] = [
        {
            "customer": {"currencyCode": "USD"},
            "campaign": {
                "name": "LA - Rent",
                "advertisingChannelType": "SEARCH",
                "biddingStrategyType": "MAXIMIZE_CONVERSIONS",
                "maximizeConversions": {"targetCpaMicros": "53000000"},
                "networkSettings": {"targetGoogleSearch": True, "targetSearchNetwork": False},
                "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE_OR_INTEREST"},
                **campaign,
            },
            "campaignBudget": {"amountMicros": "160000000", "deliveryMethod": "STANDARD"},
        }
    ]
    ads["rows"]["FROM campaign_criterion"] = [
        {
            "campaignCriterion": {
                "type": "LANGUAGE",
                "negative": False,
                "language": {"languageConstant": "languageConstants/1000"},
            }
        },
        {
            "campaignCriterion": {
                "type": "PROXIMITY",
                "negative": False,
                "proximity": {"geoPoint": {"latitudeInMicroDegrees": 1}, "radius": 20},
            }
        },
    ]
    ads["rows"]["FROM campaign_asset"] = [
        {"campaignAsset": {"asset": "customers/1/assets/9", "fieldType": "AD_IMAGE"}}
    ]
    ads["rows"]["FROM campaign_shared_set"] = [
        {"campaignSharedSet": {"sharedSet": "customers/1/sharedSets/3"}}
    ]
    ads["rows"]["FROM ad_group WHERE"] = [
        {
            "adGroup": {
                "id": "5",
                "name": "LA - Rent",
                "status": "ENABLED",
                "type": "SEARCH_STANDARD",
            }
        }
    ]
    ads["rows"]["FROM ad_group_criterion"] = [
        {
            "adGroup": {"id": "5"},
            "adGroupCriterion": {
                "status": "ENABLED",
                "negative": False,
                "keyword": {"text": "photo booth", "matchType": "BROAD"},
            },
        }
    ]
    headline = {"text": "H", "pinnedField": "HEADLINE_1", "assetPerformanceLabel": "GOOD"}
    ads["rows"]["FROM ad_group_ad"] = [
        {
            "adGroup": {"id": "5"},
            "adGroupAd": {
                "status": "ENABLED",
                "ad": {
                    "type": "RESPONSIVE_SEARCH_AD",
                    "finalUrls": ["https://x.com/la"],
                    "responsiveSearchAd": {
                        "headlines": [headline],
                        "descriptions": [{"text": "D", "policySummaryInfo": {}}],
                        "path1": "Photo",
                    },
                },
            },
        },
        {"adGroup": {"id": "5"}, "adGroupAd": {"status": "ENABLED", "ad": {"type": "IMAGE_AD"}}},
    ]


def test_clone_campaign(ads):
    _clone_rows(ads)
    result = _tool("clone_campaign")(CID, "7", " HOU - Rent ")
    budget = f"customers/{CID}/campaignBudgets/-1"
    campaign = f"customers/{CID}/campaigns/-2"
    group = f"customers/{CID}/adGroups/-3"
    assert ads["mutations"][0]["operations"] == [
        {
            "campaignBudgetOperation": {
                "create": {
                    "resourceName": budget,
                    "name": "HOU - Rent",
                    "explicitlyShared": False,
                    "amountMicros": "160000000",
                    "deliveryMethod": "STANDARD",
                }
            }
        },
        {
            "campaignOperation": {
                "create": {
                    "resourceName": campaign,
                    "name": "HOU - Rent",
                    "status": "PAUSED",
                    "advertisingChannelType": "SEARCH",
                    "campaignBudget": budget,
                    "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
                    "maximizeConversions": {"targetCpaMicros": "53000000"},
                    "networkSettings": {"targetGoogleSearch": True, "targetSearchNetwork": False},
                    "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE_OR_INTEREST"},
                }
            }
        },
        {
            "campaignCriterionOperation": {
                "create": {
                    "campaign": campaign,
                    "language": {"languageConstant": "languageConstants/1000"},
                    "negative": False,
                }
            }
        },
        {
            "campaignCriterionOperation": {
                "create": {
                    "campaign": campaign,
                    "proximity": {"geoPoint": {"latitudeInMicroDegrees": 1}, "radius": 20},
                    "negative": False,
                }
            }
        },
        {
            "campaignAssetOperation": {
                "create": {
                    "campaign": campaign,
                    "asset": "customers/1/assets/9",
                    "fieldType": "AD_IMAGE",
                }
            }
        },
        {
            "campaignSharedSetOperation": {
                "create": {"campaign": campaign, "sharedSet": "customers/1/sharedSets/3"}
            }
        },
        {
            "adGroupOperation": {
                "create": {
                    "resourceName": group,
                    "campaign": campaign,
                    "name": "HOU - Rent",
                    "status": "ENABLED",
                    "type": "SEARCH_STANDARD",
                }
            }
        },
        {
            "adGroupCriterionOperation": {
                "create": {
                    "adGroup": group,
                    "status": "ENABLED",
                    "negative": False,
                    "keyword": {"text": "photo booth", "matchType": "BROAD"},
                }
            }
        },
        {
            "adGroupAdOperation": {
                "create": {
                    "adGroup": group,
                    "status": "ENABLED",
                    "ad": {
                        "finalUrls": ["https://x.com/la"],
                        "responsiveSearchAd": {
                            "headlines": [{"text": "H", "pinnedField": "HEADLINE_1"}],
                            "descriptions": [{"text": "D"}],
                            "path1": "Photo",
                        },
                    },
                }
            }
        },
    ]
    assert result["after"] == {
        "source": "LA - Rent",
        "name": "HOU - Rent",
        "status": "PAUSED",
        "daily_budget": 160.0,
        "strategy": "MAXIMIZE_CONVERSIONS",
        "target_cpa": 53.0,
        "target_roas": None,
        "criteria": {"LANGUAGE": 1, "PROXIMITY": 1},
        "campaign_assets": 1,
        "negative_keyword_lists": 1,
        "ad_groups": [{"name": "HOU - Rent", "keywords": 1, "ads": 1}],
    }
    assert result["currency"] == "USD"
    assert result["warnings"] == ["Skipped IMAGE_AD ad in ad group 'LA - Rent'."]


@pytest.mark.parametrize(
    "campaign, message",
    [
        ({"advertisingChannelType": "PERFORMANCE_MAX"}, "search campaigns"),
        ({"biddingStrategy": "customers/1/biddingStrategies/2"}, "portfolio"),
        ({"biddingStrategyType": "TARGET_IMPRESSION_SHARE"}, "not supported"),
    ],
)
def test_clone_campaign_refusals(ads, campaign, message):
    _clone_rows(ads, **campaign)
    with pytest.raises(ValueError, match=message):
        _tool("clone_campaign")(CID, "7", "HOU - Rent")
    with pytest.raises(ValueError, match="name"):
        _tool("clone_campaign")(CID, "7", " ")
    assert not ads["mutations"]


def test_set_locations(ads):
    ads["rows"]["FROM campaign WHERE"] = [{"campaign": {"name": "C"}}]
    ads["rows"]["FROM campaign_criterion"] = [
        {
            "campaignCriterion": {
                "resourceName": "rn/1",
                "proximity": {
                    "geoPoint": {"latitudeInMicroDegrees": 34054908, "longitudeInMicroDegrees": -1},
                    "radius": 20,
                    "radiusUnits": "KILOMETERS",
                },
            }
        },
        {"campaignCriterion": {"resourceName": "rn/2", "location": {"geoTargetConstant": "g/1"}}},
    ]
    result = _tool("set_locations")(
        CID,
        "7",
        points=[{"latitude": 29.7604, "longitude": -95.3698, "radius": 20}],
        location_ids=["1026339"],
    )
    campaign = f"customers/{CID}/campaigns/7"
    assert ads["mutations"][0]["operations"] == [
        {"campaignCriterionOperation": {"remove": "rn/1"}},
        {"campaignCriterionOperation": {"remove": "rn/2"}},
        {
            "campaignCriterionOperation": {
                "create": {
                    "campaign": campaign,
                    "proximity": {
                        "geoPoint": {
                            "latitudeInMicroDegrees": 29760400,
                            "longitudeInMicroDegrees": -95369800,
                        },
                        "radius": 20.0,
                        "radiusUnits": "KILOMETERS",
                    },
                }
            }
        },
        {
            "campaignCriterionOperation": {
                "create": {
                    "campaign": campaign,
                    "location": {"geoTargetConstant": "geoTargetConstants/1026339"},
                }
            }
        },
    ]
    assert "negative = FALSE" in ads["queries"][1]
    assert result["before"]["locations"] == [
        {"latitude": 34.054908, "longitude": -0.000001, "radius": 20, "units": "KILOMETERS"},
        {"location": "g/1"},
    ]
    assert result["after"]["locations"] == [
        {"latitude": 29.7604, "longitude": -95.3698, "radius": 20.0, "units": "KILOMETERS"},
        {"location": "geoTargetConstants/1026339"},
    ]


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({}, "points and/or location_ids"),
        ({"points": [{"latitude": 100, "longitude": 0, "radius": 5}]}, "Invalid point"),
        ({"points": [{"latitude": 1, "longitude": 1, "radius": 0}]}, "Invalid point"),
        ({"points": [{"latitude": 1, "radius": 5}]}, "Invalid point"),
        ({"points": [{"latitude": 1, "longitude": 1, "radius": 5, "units": "FEET"}]}, "units"),
        ({"location_ids": ["Houston"]}, "location_id"),
    ],
)
def test_set_locations_rules(ads, kwargs, message):
    ads["rows"]["FROM campaign WHERE"] = [{"campaign": {"name": "C"}}]
    with pytest.raises(ValueError, match=message):
        _tool("set_locations")(CID, "7", **kwargs)
    assert not ads["mutations"]


def test_set_ad_schedule(ads):
    ads["rows"]["FROM campaign WHERE"] = [
        {"campaign": {"name": "C"}, "customer": {"timeZone": "America/New_York"}}
    ]
    ads["rows"]["FROM campaign_criterion"] = [
        {
            "campaignCriterion": {
                "resourceName": "rn/1",
                "bidModifier": 1.2,
                "adSchedule": {"dayOfWeek": "MONDAY", "startHour": 3, "endHour": 21},
            }
        }
    ]
    result = _tool("set_ad_schedule")(
        CID, "7", [{"day": "monday", "start_hour": 5, "end_hour": 23}]
    )
    assert ads["mutations"][0]["operations"] == [
        {"campaignCriterionOperation": {"remove": "rn/1"}},
        {
            "campaignCriterionOperation": {
                "create": {
                    "campaign": f"customers/{CID}/campaigns/7",
                    "adSchedule": {
                        "dayOfWeek": "MONDAY",
                        "startHour": 5,
                        "startMinute": "ZERO",
                        "endHour": 23,
                        "endMinute": "ZERO",
                    },
                }
            }
        },
    ]
    assert result["before"] == {
        "campaign": "C",
        "time_zone": "America/New_York",
        "schedule": [{"day": "MONDAY", "start_hour": 3, "end_hour": 21}],
    }
    assert result["after"]["schedule"] == [{"day": "MONDAY", "start_hour": 5, "end_hour": 23}]
    assert "Bid adjustments" in result["warnings"][0]


def test_set_ad_schedule_empty_removes_schedule(ads):
    ads["rows"]["FROM campaign WHERE"] = [{"campaign": {"name": "C"}}]
    ads["rows"]["FROM campaign_criterion"] = [{"campaignCriterion": {"resourceName": "rn/1"}}]
    result = _tool("set_ad_schedule")(CID, "7", [])
    assert ads["mutations"][0]["operations"] == [{"campaignCriterionOperation": {"remove": "rn/1"}}]
    assert result["after"]["schedule"] == []


def test_set_ad_schedule_rules(ads):
    for entry in (
        {"day": "MONDAYS", "start_hour": 5, "end_hour": 23},
        {"day": "MONDAY", "start_hour": 23, "end_hour": 5},
        {"day": "MONDAY", "start_hour": 5, "end_hour": 25},
        {"day": "MONDAY", "start_hour": 5.5, "end_hour": 23},
    ):
        with pytest.raises(ValueError, match="Invalid schedule entry"):
            _tool("set_ad_schedule")(CID, "7", [entry])
    ads["rows"]["FROM campaign WHERE"] = [{"campaign": {"name": "C"}}]
    with pytest.raises(ValueError, match="no ad schedule"):
        _tool("set_ad_schedule")(CID, "7", [])
    assert not ads["mutations"]


def test_set_ad_final_url(ads):
    ads["rows"]["FROM ad_group_ad"] = [
        {
            "campaign": {"name": "C"},
            "adGroup": {"name": "G"},
            "adGroupAd": {"ad": {"finalUrls": ["https://x.com/la"]}},
        }
    ]
    result = _tool("set_ad_final_url")(CID, "9", "https://x.com/houston")
    assert ads["mutations"][0]["operations"] == [
        {
            "adOperation": {
                "update": {
                    "resourceName": f"customers/{CID}/ads/9",
                    "finalUrls": ["https://x.com/houston"],
                },
                "updateMask": "final_urls",
            }
        }
    ]
    assert result["before"]["final_urls"] == ["https://x.com/la"]
    assert result["after"]["final_urls"] == ["https://x.com/houston"]


def test_set_ad_final_url_rules(ads):
    with pytest.raises(ValueError, match="final_url"):
        _tool("set_ad_final_url")(CID, "9", "x.com/houston")
    with pytest.raises(ValueError, match="not found"):
        _tool("set_ad_final_url")(CID, "9", "https://x.com/houston")
    assert not ads["mutations"]


SITELINK = {
    "type": "sitelink",
    "link_text": "Pricing",
    "final_url": "https://x.com/pricing",
    "description1": "A",
    "description2": "B",
}


def test_add_assets_to_account(ads):
    result = _tool("add_assets")(
        CID,
        [
            SITELINK,
            {"type": "CALLOUT", "text": "Free delivery"},
            {"type": "STRUCTURED_SNIPPET", "header": "Types", "values": ["a", "b", "c"]},
            {"type": "CALL", "phone_number": "(305) 555-0100", "country_code": "US"},
        ],
    )
    temps = [f"customers/{CID}/assets/-{i}" for i in range(1, 5)]
    bodies = [
        {
            "finalUrls": ["https://x.com/pricing"],
            "sitelinkAsset": {"linkText": "Pricing", "description1": "A", "description2": "B"},
        },
        {"calloutAsset": {"calloutText": "Free delivery"}},
        {"structuredSnippetAsset": {"header": "Types", "values": ["a", "b", "c"]}},
        {"callAsset": {"countryCode": "US", "phoneNumber": "(305) 555-0100"}},
    ]
    kinds = ["SITELINK", "CALLOUT", "STRUCTURED_SNIPPET", "CALL"]
    expected = []
    for temp, body, kind in zip(temps, bodies, kinds, strict=True):
        expected += [
            {"assetOperation": {"create": {"resourceName": temp, **body}}},
            {"customerAssetOperation": {"create": {"asset": temp, "fieldType": kind}}},
        ]
    assert ads["mutations"][0]["operations"] == expected
    assert "FROM customer_asset WHERE customer_asset.status != 'REMOVED'" in ads["queries"][0]
    assert result["before"] == {"account": CID, "assets": {}}
    assert result["after"]["assets"] == dict.fromkeys(kinds, 1)
    assert result["after"]["added"][0]["type"] == "SITELINK"
    assert result["warnings"] == []


def test_add_assets_to_campaign_skips_existing(ads):
    ads["rows"]["FROM campaign WHERE"] = [{"campaign": {"name": "C"}}]
    ads["rows"]["FROM campaign_asset"] = [
        {
            "campaignAsset": {"fieldType": "CALLOUT"},
            "asset": {"calloutAsset": {"calloutText": "Free Delivery"}},
        }
    ]
    result = _tool("add_assets")(
        CID, [{"type": "CALLOUT", "text": "free delivery"}, SITELINK], campaign_id="7"
    )
    temp = f"customers/{CID}/assets/-1"
    assert ads["mutations"][0]["operations"][1] == {
        "campaignAssetOperation": {
            "create": {
                "campaign": f"customers/{CID}/campaigns/7",
                "asset": temp,
                "fieldType": "SITELINK",
            }
        }
    }
    assert len(ads["mutations"][0]["operations"]) == 2
    assert "SELECT campaign.id, campaign_asset.field_type" in ads["queries"][1]
    assert "AND campaign.id = 7" in ads["queries"][1]
    assert result["before"] == {"campaign": "C", "assets": {"CALLOUT": 1}}
    assert result["after"]["assets"] == {"CALLOUT": 1, "SITELINK": 1}
    assert "Skipped existing CALLOUT" in result["warnings"][0]
    assert "campaign had no SITELINK" in result["warnings"][1]


def test_add_assets_to_ad_group(ads):
    ads["rows"]["FROM ad_group WHERE"] = [{"adGroup": {"name": "G"}}]
    result = _tool("add_assets")(CID, [{"type": "CALLOUT", "text": "x"}], ad_group_id="5")
    link = ads["mutations"][0]["operations"][1]["adGroupAssetOperation"]["create"]
    assert link["adGroup"] == f"customers/{CID}/adGroups/5"
    assert "AND ad_group.id = 5" in ads["queries"][1]
    assert "ad group had no CALLOUT" in result["warnings"][0]


def test_add_assets_rules(ads):
    callout = [{"type": "CALLOUT", "text": "x"}]
    with pytest.raises(ValueError, match="not both"):
        _tool("add_assets")(CID, callout, campaign_id="7", ad_group_id="5")
    with pytest.raises(ValueError, match="empty"):
        _tool("add_assets")(CID, [])
    for bad in (
        {"type": "PROMOTION", "text": "x"},
        {"type": "SITELINK", "link_text": "x"},
        {"type": "CALLOUT", "text": ""},
    ):
        with pytest.raises(ValueError, match="Invalid asset"):
            _tool("add_assets")(CID, [bad])
    ads["rows"]["FROM customer_asset"] = [
        {"customerAsset": {"fieldType": "CALLOUT"}, "asset": {"calloutAsset": {"calloutText": "x"}}}
    ]
    with pytest.raises(ValueError, match="already exist"):
        _tool("add_assets")(CID, callout)
    assert not ads["mutations"]


@pytest.mark.parametrize(
    "kind, resource, operation, suffix",
    [
        ("customerAssets", "customerAsset", "customerAssetOperation", "9~SITELINK"),
        ("campaignAssets", "campaignAsset", "campaignAssetOperation", "7~9~CALLOUT"),
        ("adGroupAssets", "adGroupAsset", "adGroupAssetOperation", "5~9~STRUCTURED_SNIPPET"),
    ],
)
def test_remove_assets(ads, kind, resource, operation, suffix):
    name = f"customers/{CID}/{kind}/{suffix}"
    ads["rows"]["FROM"] = [
        {
            resource: {"resourceName": name, "fieldType": "CALLOUT"},
            "asset": {"resourceName": "rn/9", "calloutAsset": {"calloutText": "x"}},
        }
    ]
    result = _tool("remove_assets")(CID, [name, name])
    assert ads["mutations"][0]["operations"] == [{operation: {"remove": name}}]
    assert f"resource_name IN ('{name}')" in ads["queries"][0]
    assert result["before"] == [
        {"resource_name": name, "field_type": "CALLOUT", "calloutAsset": {"calloutText": "x"}}
    ]
    assert result["after"] is None


def test_remove_assets_rules(ads):
    for name in (
        "customers/999/customerAssets/9~SITELINK",
        f"customers/{CID}/assets/9",
        f"customers/{CID}/customerAssets/9~SITELINK' OR '1'='1",
    ):
        with pytest.raises(ValueError, match="Invalid asset link"):
            _tool("remove_assets")(CID, [name])
    with pytest.raises(ValueError, match="empty"):
        _tool("remove_assets")(CID, [])
    with pytest.raises(ValueError, match="not found"):
        _tool("remove_assets")(CID, [f"customers/{CID}/customerAssets/9~SITELINK"])
    assert not ads["mutations"]
