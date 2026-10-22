from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import api

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

mcp = FastMCP(
    name="google-ads",
    instructions=(
        "Read and edit Google Ads accounts. Use list_accessible_customers and search (GAQL) to "
        "find accounts, campaigns, ad groups, ads and keywords, and get_resource_metadata to "
        "discover valid fields. Write tools default to confirm=false: the change is only "
        "validated by Google (validateOnly) and a before/after preview is returned. Show the "
        "preview to the user and call the tool again with confirm=true ONLY after the user "
        "explicitly approves that specific change in the conversation. Money amounts are in the "
        "account currency (returned as `currency`), never in micros."
    ),
)

MATCH_TYPES = ("EXACT", "PHRASE", "BROAD")

STRATEGIES = {
    "MANUAL_CPC": ("manualCpc", "manual_cpc.enhanced_cpc_enabled"),
    "MAXIMIZE_CLICKS": ("targetSpend", "target_spend.cpc_bid_ceiling_micros"),
    "MAXIMIZE_CONVERSIONS": ("maximizeConversions", "maximize_conversions.target_cpa_micros"),
    "MAXIMIZE_CONVERSION_VALUE": (
        "maximizeConversionValue",
        "maximize_conversion_value.target_roas",
    ),
}

STATUS_TARGETS = {
    "campaign": ("campaign", "campaignOperation", "campaign.name", "campaign.id = {id}"),
    "ad_group": ("ad_group", "adGroupOperation", "ad_group.name", "ad_group.id = {id}"),
    "ad": (
        "ad_group_ad",
        "adGroupAdOperation",
        "ad_group_ad.ad.type",
        "ad_group.id = {ad_group_id} AND ad_group_ad.ad.id = {id}",
    ),
    "keyword": (
        "ad_group_criterion",
        "adGroupCriterionOperation",
        "ad_group_criterion.keyword.text",
        "ad_group.id = {ad_group_id} AND ad_group_criterion.criterion_id = {id} "
        "AND ad_group_criterion.type = 'KEYWORD'",
    ),
}

REMOVABLE = {
    "adGroupCriteria": ("ad_group_criterion", "adGroupCriterionOperation"),
    "campaignCriteria": ("campaign_criterion", "campaignCriterionOperation"),
    "sharedCriteria": ("shared_criterion", "sharedCriterionOperation"),
}

SCHEMES = {
    "MANUAL_CPC": "manualCpc",
    "TARGET_SPEND": "targetSpend",
    "MAXIMIZE_CONVERSIONS": "maximizeConversions",
    "MAXIMIZE_CONVERSION_VALUE": "maximizeConversionValue",
}

CLONED_CRITERIA = {
    "LANGUAGE": "language",
    "LOCATION": "location",
    "PROXIMITY": "proximity",
    "AD_SCHEDULE": "adSchedule",
    "KEYWORD": "keyword",
}

DAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")

ASSET_TYPES = {
    "SITELINK": ("sitelinkAsset", ("link_text", "final_url")),
    "CALLOUT": ("calloutAsset", ("text",)),
    "STRUCTURED_SNIPPET": ("structuredSnippetAsset", ("header", "values")),
    "CALL": ("callAsset", ("phone_number", "country_code")),
}

ASSET_COLUMNS = (
    "asset.final_urls, asset.sitelink_asset.link_text, asset.sitelink_asset.description1, "
    "asset.sitelink_asset.description2, asset.callout_asset.callout_text, "
    "asset.structured_snippet_asset.header, asset.structured_snippet_asset.values, "
    "asset.call_asset.country_code, asset.call_asset.phone_number"
)

ASSET_LINKS = {
    "customerAssets": ("customer_asset", "customerAssetOperation"),
    "campaignAssets": ("campaign_asset", "campaignAssetOperation"),
    "adGroupAssets": ("ad_group_asset", "adGroupAssetOperation"),
}


def _cid(customer_id: str | int) -> str:
    cid = api.digits(customer_id)
    if not cid:
        raise ValueError(f"Invalid customer_id {customer_id!r}.")
    return cid


def _id(value: str | int | None, name: str) -> str:
    if not re.fullmatch(r"\d+", str(value)):
        raise ValueError(f"Invalid {name} {value!r}: must contain only digits.")
    return str(value)


def _micros(amount: float | str) -> str:
    try:
        value = Decimal(str(amount))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount {amount!r}.") from exc
    if value <= 0 or value != value.quantize(Decimal("0.01")):
        raise ValueError(f"Invalid amount {amount!r}: must be positive with at most 2 decimals.")
    return str(int(value * 1_000_000))


def _money(micros: str | int | None) -> float | None:
    return None if micros is None else int(micros) / 1_000_000


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _field(row: dict[str, Any], path: str) -> Any:
    value: Any = row
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(_camel(part))
    return value


def _query_one(customer_id: str, query: str, what: str) -> dict[str, Any]:
    rows = api.search_stream(customer_id, query)
    if not rows:
        raise ValueError(f"{what} not found in account {customer_id}.")
    return rows[0]


def _keywords(keywords: list[dict[str, str]]) -> list[dict[str, str]]:
    if not keywords:
        raise ValueError("keywords must not be empty.")
    result = []
    for keyword in keywords:
        text = str(keyword.get("text", "")).strip()
        match_type = str(keyword.get("match_type", "")).upper()
        if not text or match_type not in MATCH_TYPES:
            raise ValueError(
                f"Invalid keyword {keyword!r}: needs text and match_type in {MATCH_TYPES}."
            )
        result.append({"text": text, "match_type": match_type})
    return result


def _write(
    customer_id: str,
    operations: list[dict[str, Any]],
    confirm: bool,
    before: Any,
    after: Any,
    currency: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    resource_names = api.mutate(customer_id, operations, validate_only=not confirm)
    result = {
        "applied": confirm,
        "before": before,
        "after": after,
        "currency": currency,
        "warnings": warnings or [],
    }
    if confirm:
        result["resource_names"] = resource_names
    return result


@mcp.tool(annotations=READ_ONLY)
def list_accessible_customers() -> list[str]:
    """Return the ids of customers directly accessible by the authenticated user.

    Use this first to discover customer ids. A manager (MCC) account appears here; query its
    `customer_client` resource with `search` to list the client accounts under it.
    """
    response = api.get("customers:listAccessibleCustomers")
    return [name.removeprefix("customers/") for name in response.get("resourceNames", [])]


@mcp.tool(annotations=READ_ONLY)
def search(
    customer_id: str | int,
    fields: list[str],
    resource: str,
    conditions: list[str] | None = None,
    orderings: list[str] | None = None,
    limit: int | None = None,
    login_customer_id: str | int | None = None,
) -> list[dict[str, Any]]:
    """Fetch data from the Google Ads API with a GAQL query (searchStream).

    Args:
        customer_id: The customer id, digits only (123-456-7890 becomes 1234567890).
        fields: Full field names to select, e.g. ['campaign.id', 'metrics.clicks'].
        resource: The resource in the FROM clause, e.g. 'campaign', 'keyword_view'.
        conditions: WHERE conditions, combined with AND.
        orderings: ORDER BY clauses.
        limit: Maximum number of rows.
        login_customer_id: Optional manager id for the login-customer-id header.

    Hints:
        Grammar: https://developers.google.com/google-ads/api/docs/query/grammar
        Resources: https://developers.google.com/google-ads/api/fields/latest/overview
        Use get_resource_metadata to find the fields you can select, filter and sort for a
        resource. Do not guess fields and always use full names ('campaign.id', not 'id').
        Dates are YYYY-MM-DD, e.g. "segments.date BETWEEN '2026-09-01' AND '2026-09-30'".
        Money fields ending in _micros are in the account currency times 1,000,000.
        int64 values (ids, micros, clicks) are returned as strings; unset fields as null.
        change_event queries must use a LIMIT of at most 10000.
    """
    query = f"SELECT {','.join(fields)} FROM {resource}"
    if conditions:
        query += f" WHERE {' AND '.join(conditions)}"
    if orderings:
        query += f" ORDER BY {','.join(orderings)}"
    if limit:
        query += f" LIMIT {limit}"
    query += " PARAMETERS omit_unselected_resource_names=true"
    rows = api.search_stream(_cid(customer_id), query, login_customer_id)
    return [{field: _field(row, field) for field in fields} for row in rows]


@mcp.tool(annotations=READ_ONLY)
def get_resource_metadata(
    resource_name: str, login_customer_id: str | int | None = None
) -> dict[str, Any]:
    """Return the selectable, filterable and sortable fields of a Google Ads resource,
    including the metrics ('metrics.*') and segments ('segments.*') compatible with it.

    Use it before building a `search` query; do not guess fields. The result rarely changes,
    so reuse it within the conversation.

    Args:
        resource_name: Resource name, e.g. 'campaign', 'ad_group', 'keyword_view'.
        login_customer_id: Optional manager id for the login-customer-id header.
    """
    if not re.fullmatch(r"[a-z_]+", resource_name):
        raise ValueError(f"Invalid resource_name {resource_name!r}.")
    columns = "SELECT name, selectable, filterable, sortable"
    fields = api.search_fields(
        f"{columns} WHERE name LIKE '{resource_name}.%' AND category = 'ATTRIBUTE'",
        login_customer_id,
    ) + api.search_fields(
        f"{columns} WHERE selectable_with CONTAINS ANY('{resource_name}')", login_customer_id
    )
    return {
        "resource": resource_name,
        **{
            key: sorted({field["name"] for field in fields if field.get(key)})
            for key in ("selectable", "filterable", "sortable")
        },
    }


@mcp.tool(annotations=WRITE)
def set_status(
    customer_id: str | int,
    resource_type: Literal["campaign", "ad_group", "ad", "keyword"],
    resource_id: str | int,
    status: Literal["ENABLED", "PAUSED"],
    ad_group_id: str | int | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Pause or enable a campaign, ad group, ad or keyword.

    With confirm=false (default) the change is only validated and a preview is returned.
    Call again with confirm=true only after the user explicitly approves the preview.

    Args:
        customer_id: The account id.
        resource_type: campaign, ad_group, ad or keyword.
        resource_id: Campaign id, ad group id, ad id or keyword criterion id.
        status: ENABLED or PAUSED.
        ad_group_id: Required for ad and keyword.
        confirm: Apply the change for real.
    """
    cid = _cid(customer_id)
    resource, operation, label, condition = STATUS_TARGETS[resource_type]
    if resource_type in ("ad", "keyword") and ad_group_id is None:
        raise ValueError(f"ad_group_id is required for resource_type={resource_type}.")
    where = condition.format(
        id=_id(resource_id, "resource_id"),
        ad_group_id="" if ad_group_id is None else _id(ad_group_id, "ad_group_id"),
    )
    row = _query_one(
        cid,
        f"SELECT {resource}.resource_name, {resource}.status, {label} "
        f"FROM {resource} WHERE {where}",
        resource_type,
    )
    current = _field(row, f"{resource}.status")
    if current == "REMOVED":
        raise ValueError(f"{resource_type} {resource_id} is removed and cannot change status.")
    warnings = [f"{resource_type} is already {status}."] if current == status else []
    update = {"resourceName": _field(row, f"{resource}.resource_name"), "status": status}
    return _write(
        cid,
        [{operation: {"update": update, "updateMask": "status"}}],
        confirm,
        before={label: _field(row, label), "status": current},
        after={label: _field(row, label), "status": status},
        warnings=warnings,
    )


@mcp.tool(annotations=WRITE)
def set_campaign_budget(
    customer_id: str | int,
    campaign_id: str | int,
    daily_amount: float,
    confirm: bool = False,
) -> dict[str, Any]:
    """Change a campaign's daily budget.

    The amount is in the account currency (e.g. 50.00 means R$ 50,00 or US$ 50.00), never in
    micros. Shared budgets are refused because the change would hit every campaign sharing
    them. With confirm=false (default) the change is only validated and a preview is returned.
    Call again with confirm=true only after the user explicitly approves the preview.
    """
    cid = _cid(customer_id)
    row = _query_one(
        cid,
        "SELECT campaign.name, campaign_budget.resource_name, campaign_budget.amount_micros, "
        "campaign_budget.explicitly_shared, campaign_budget.reference_count, "
        "campaign_budget.period, customer.currency_code FROM campaign "
        f"WHERE campaign.id = {_id(campaign_id, 'campaign_id')}",
        "campaign",
    )
    shared = _field(row, "campaign_budget.explicitly_shared")
    if shared or int(_field(row, "campaign_budget.reference_count") or 0) > 1:
        raise ValueError(
            "The campaign uses a shared budget; changing it would affect every campaign "
            "that shares it."
        )
    if _field(row, "campaign_budget.period") != "DAILY":
        raise ValueError("The campaign budget is not a daily budget.")
    micros = _micros(daily_amount)
    name = _field(row, "campaign.name")
    update = {"resourceName": _field(row, "campaign_budget.resource_name"), "amountMicros": micros}
    return _write(
        cid,
        [{"campaignBudgetOperation": {"update": update, "updateMask": "amount_micros"}}],
        confirm,
        before={
            "campaign": name,
            "daily_amount": _money(_field(row, "campaign_budget.amount_micros")),
        },
        after={"campaign": name, "daily_amount": _money(micros)},
        currency=_field(row, "customer.currency_code"),
    )


@mcp.tool(annotations=WRITE)
def set_bid(
    customer_id: str | int,
    ad_group_id: str | int,
    amount: float,
    keyword_id: str | int | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Change the max CPC bid of an ad group, or of one keyword when keyword_id is given.

    The amount is in the account currency, never in micros. The bid only takes effect under
    the MANUAL_CPC strategy; a warning is returned otherwise. With confirm=false (default)
    the change is only validated and a preview is returned. Call again with confirm=true only
    after the user explicitly approves the preview.
    """
    cid = _cid(customer_id)
    ad_group = _id(ad_group_id, "ad_group_id")
    if keyword_id is None:
        resource, operation, label = "ad_group", "adGroupOperation", "ad_group.name"
        where = f"ad_group.id = {ad_group}"
    else:
        resource, operation = "ad_group_criterion", "adGroupCriterionOperation"
        label = "ad_group_criterion.keyword.text"
        where = (
            f"ad_group.id = {ad_group} AND ad_group_criterion.criterion_id = "
            f"{_id(keyword_id, 'keyword_id')} AND ad_group_criterion.type = 'KEYWORD'"
        )
    row = _query_one(
        cid,
        f"SELECT {resource}.resource_name, {resource}.cpc_bid_micros, {label}, "
        f"campaign.bidding_strategy_type, customer.currency_code FROM {resource} WHERE {where}",
        "keyword" if keyword_id is not None else "ad_group",
    )
    strategy = _field(row, "campaign.bidding_strategy_type")
    warnings = []
    if strategy != "MANUAL_CPC":
        warnings.append(f"The campaign strategy is {strategy}, so this bid is likely ignored.")
    micros = _micros(amount)
    update = {"resourceName": _field(row, f"{resource}.resource_name"), "cpcBidMicros": micros}
    return _write(
        cid,
        [{operation: {"update": update, "updateMask": "cpc_bid_micros"}}],
        confirm,
        before={
            label: _field(row, label),
            "cpc_bid": _money(_field(row, f"{resource}.cpc_bid_micros")),
        },
        after={label: _field(row, label), "cpc_bid": _money(micros)},
        currency=_field(row, "customer.currency_code"),
        warnings=warnings,
    )


@mcp.tool(annotations=WRITE)
def set_bidding_strategy(
    customer_id: str | int,
    campaign_id: str | int,
    strategy: Literal[
        "MANUAL_CPC", "MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"
    ],
    target_cpa: float | None = None,
    target_roas: float | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Change a campaign's standard bidding strategy and/or its CPA or ROAS target.

    Repeat the current strategy to change only the target. target_cpa (account currency) is
    only valid with MAXIMIZE_CONVERSIONS; target_roas (a ratio: 4.0 means 400%) only with
    MAXIMIZE_CONVERSION_VALUE. Omitting the target removes the existing one. Portfolio
    strategies are refused. Google rejects strategies the campaign type does not support,
    and the preview shows that error. With confirm=false (default) the change is only
    validated and a preview is returned. Call again with confirm=true only after the user
    explicitly approves the preview.
    """
    if target_cpa is not None and strategy != "MAXIMIZE_CONVERSIONS":
        raise ValueError("target_cpa is only valid with MAXIMIZE_CONVERSIONS.")
    if target_roas is not None and strategy != "MAXIMIZE_CONVERSION_VALUE":
        raise ValueError("target_roas is only valid with MAXIMIZE_CONVERSION_VALUE.")
    if target_roas is not None and target_roas <= 0:
        raise ValueError("target_roas must be positive.")
    cid = _cid(customer_id)
    row = _query_one(
        cid,
        "SELECT campaign.resource_name, campaign.name, campaign.bidding_strategy, "
        "campaign.bidding_strategy_type, campaign.maximize_conversions.target_cpa_micros, "
        "campaign.maximize_conversion_value.target_roas, customer.currency_code FROM campaign "
        f"WHERE campaign.id = {_id(campaign_id, 'campaign_id')}",
        "campaign",
    )
    if _field(row, "campaign.bidding_strategy"):
        raise ValueError("The campaign uses a portfolio bidding strategy, which is not supported.")
    current_type = _field(row, "campaign.bidding_strategy_type")
    name = _field(row, "campaign.name")
    before = {
        "campaign": name,
        "strategy": "MAXIMIZE_CLICKS" if current_type == "TARGET_SPEND" else current_type,
        "target_cpa": _money(_field(row, "campaign.maximize_conversions.target_cpa_micros")),
        "target_roas": _field(row, "campaign.maximize_conversion_value.target_roas"),
    }
    field, mask = STRATEGIES[strategy]
    scheme: dict[str, Any] = {}
    if target_cpa is not None:
        scheme["targetCpaMicros"] = _micros(target_cpa)
    if target_roas is not None:
        scheme["targetRoas"] = target_roas
    after = {
        "campaign": name,
        "strategy": strategy,
        "target_cpa": _money(scheme.get("targetCpaMicros")),
        "target_roas": target_roas,
    }
    warnings = []
    if before != after:
        warnings.append("Changing the strategy or its target restarts the learning period.")
    update = {"resourceName": _field(row, "campaign.resource_name"), field: scheme}
    return _write(
        cid,
        [{"campaignOperation": {"update": update, "updateMask": mask}}],
        confirm,
        before=before,
        after=after,
        currency=_field(row, "customer.currency_code"),
        warnings=warnings,
    )


@mcp.tool(annotations=WRITE)
def add_keywords(
    customer_id: str | int,
    keywords: list[dict[str, str]],
    ad_group_id: str | int | None = None,
    campaign_id: str | int | None = None,
    list_id: str | int | None = None,
    negative: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Add keywords to an ad group, negative keywords to a campaign, or keywords to a
    negative keyword list.

    Pass exactly one target: ad_group_id (positive, or negative with negative=true),
    campaign_id (always negative) or list_id (a negative keyword list, always negative).
    Each keyword is {"text": "...", "match_type": "EXACT" | "PHRASE" | "BROAD"}. Keywords that
    already exist in the target are skipped with a warning. The batch is all or nothing. With
    confirm=false (default) the change is only validated and a preview is returned. Call again
    with confirm=true only after the user explicitly approves the preview.
    """
    if sum(target is not None for target in (ad_group_id, campaign_id, list_id)) != 1:
        raise ValueError("Pass exactly one of ad_group_id, campaign_id or list_id.")
    cid = _cid(customer_id)
    wanted = _keywords(keywords)
    if ad_group_id is not None:
        ad_group = _id(ad_group_id, "ad_group_id")
        row = _query_one(
            cid, f"SELECT ad_group.name FROM ad_group WHERE ad_group.id = {ad_group}", "ad_group"
        )
        target = {"ad_group": _field(row, "ad_group.name"), "negative": negative}
        prefix, operation = "ad_group_criterion", "adGroupCriterionOperation"
        scope = (
            f"ad_group.id = {ad_group} AND ad_group_criterion.negative = {str(negative).upper()} "
            "AND ad_group_criterion.status != 'REMOVED'"
        )
        base: dict[str, Any] = {
            "adGroup": f"customers/{cid}/adGroups/{ad_group}",
            "negative": negative,
        }
    elif campaign_id is not None:
        campaign = _id(campaign_id, "campaign_id")
        row = _query_one(
            cid, f"SELECT campaign.name FROM campaign WHERE campaign.id = {campaign}", "campaign"
        )
        target = {"campaign": _field(row, "campaign.name"), "negative": True}
        prefix, operation = "campaign_criterion", "campaignCriterionOperation"
        scope = (
            f"campaign.id = {campaign} AND campaign_criterion.negative = TRUE "
            "AND campaign_criterion.status != 'REMOVED'"
        )
        base = {"campaign": f"customers/{cid}/campaigns/{campaign}", "negative": True}
    else:
        shared_set = _id(list_id, "list_id")
        row = _query_one(
            cid,
            "SELECT shared_set.name, shared_set.type, shared_set.status FROM shared_set "
            f"WHERE shared_set.id = {shared_set}",
            "negative keyword list",
        )
        if _field(row, "shared_set.type") != "NEGATIVE_KEYWORDS":
            raise ValueError(f"Shared set {shared_set} is not a negative keyword list.")
        if _field(row, "shared_set.status") != "ENABLED":
            raise ValueError(f"Negative keyword list {shared_set} is not enabled.")
        target = {"list": _field(row, "shared_set.name"), "negative": True}
        prefix, operation = "shared_criterion", "sharedCriterionOperation"
        scope = f"shared_set.id = {shared_set}"
        base = {"sharedSet": f"customers/{cid}/sharedSets/{shared_set}"}
    existing = api.search_stream(
        cid,
        f"SELECT {prefix}.keyword.text, {prefix}.keyword.match_type FROM {prefix} "
        f"WHERE {scope} AND {prefix}.type = 'KEYWORD'",
    )
    seen = {
        (
            str(_field(r, f"{prefix}.keyword.text")).lower(),
            _field(r, f"{prefix}.keyword.match_type"),
        )
        for r in existing
    }
    added, warnings = [], []
    for keyword in wanted:
        key = (keyword["text"].lower(), keyword["match_type"])
        if key in seen:
            warnings.append(
                f"Skipped existing keyword {keyword['text']!r} ({keyword['match_type']})."
            )
            continue
        seen.add(key)
        added.append(keyword)
    if not added:
        raise ValueError("All keywords already exist in the target.")
    operations = [
        {
            operation: {
                "create": {
                    **base,
                    "keyword": {"text": keyword["text"], "matchType": keyword["match_type"]},
                }
            }
        }
        for keyword in added
    ]
    return _write(
        cid,
        operations,
        confirm,
        before={**target, "keyword_count": len(existing)},
        after={**target, "keyword_count": len(existing) + len(added), "added": added},
        warnings=warnings,
    )


@mcp.tool(annotations=WRITE)
def remove_keywords(
    customer_id: str | int,
    resource_names: list[str],
    confirm: bool = False,
) -> dict[str, Any]:
    """Permanently remove keywords from ad groups, campaigns (negatives) or negative keyword
    lists, in one all-or-nothing call.

    resource_names are the keywords' resource names as returned by `search`, e.g.
    customers/123/adGroupCriteria/456~789 (ad_group_criterion.resource_name),
    customers/123/campaignCriteria/456~789 (campaign_criterion.resource_name) or
    customers/123/sharedCriteria/456~789 (shared_criterion.resource_name). With confirm=false
    (default) the removal is only validated and a preview is returned. Call again with
    confirm=true only after the user explicitly approves the preview.
    """
    cid = _cid(customer_id)
    if not resource_names:
        raise ValueError("resource_names must not be empty.")
    by_kind: dict[str, list[str]] = {}
    for name in dict.fromkeys(resource_names):
        match = re.fullmatch(
            r"customers/(\d+)/(adGroupCriteria|campaignCriteria|sharedCriteria)/\d+~\d+", name
        )
        if not match or match.group(1) != cid:
            raise ValueError(f"Invalid keyword resource_name {name!r} for account {cid}.")
        by_kind.setdefault(match.group(2), []).append(name)
    removed, operations = [], []
    for kind, names in by_kind.items():
        resource, operation = REMOVABLE[kind]
        quoted = ", ".join(f"'{name}'" for name in names)
        rows = {
            _field(row, f"{resource}.resource_name"): row
            for row in api.search_stream(
                cid,
                f"SELECT {resource}.resource_name, {resource}.keyword.text, "
                f"{resource}.keyword.match_type FROM {resource} "
                f"WHERE {resource}.resource_name IN ({quoted}) AND {resource}.type = 'KEYWORD'",
            )
        }
        missing = [name for name in names if name not in rows]
        if missing:
            raise ValueError(f"Keywords not found in account {cid}: {', '.join(missing)}.")
        for name in names:
            removed.append(
                {
                    "resource_name": name,
                    "text": _field(rows[name], f"{resource}.keyword.text"),
                    "match_type": _field(rows[name], f"{resource}.keyword.match_type"),
                }
            )
            operations.append({operation: {"remove": name}})
    return _write(
        cid,
        operations,
        confirm,
        before=removed,
        after=None,
        warnings=[f"{len(removed)} keyword(s) will be permanently removed."],
    )


@mcp.tool(annotations=WRITE)
def create_negative_keyword_list(
    customer_id: str | int,
    name: str,
    keywords: list[dict[str, str]],
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a negative keyword list (shared set) with its initial keywords, in one atomic
    call. Link it to campaigns with link_negative_keyword_list.

    Each keyword is {"text": "...", "match_type": "EXACT" | "PHRASE" | "BROAD"}. With
    confirm=true the first resource name returned is the new list
    (customers/<id>/sharedSets/<list_id>). With confirm=false (default) the change is only
    validated and a preview is returned. Call again with confirm=true only after the user
    explicitly approves the preview.
    """
    cid = _cid(customer_id)
    if not name.strip():
        raise ValueError("name must not be empty.")
    wanted = _keywords(keywords)
    temp = f"customers/{cid}/sharedSets/-1"
    operations: list[dict[str, Any]] = [
        {
            "sharedSetOperation": {
                "create": {"resourceName": temp, "name": name.strip(), "type": "NEGATIVE_KEYWORDS"}
            }
        }
    ]
    operations += [
        {
            "sharedCriterionOperation": {
                "create": {
                    "sharedSet": temp,
                    "keyword": {"text": keyword["text"], "matchType": keyword["match_type"]},
                }
            }
        }
        for keyword in wanted
    ]
    return _write(
        cid,
        operations,
        confirm,
        before=None,
        after={"name": name.strip(), "keywords": wanted},
    )


@mcp.tool(annotations=WRITE)
def link_negative_keyword_list(
    customer_id: str | int,
    list_id: str | int,
    campaign_ids: list[str | int],
    unlink: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Link a negative keyword list to campaigns, or unlink it with unlink=true.

    Campaigns already in the requested state are skipped with a warning. With confirm=false
    (default) the change is only validated and a preview is returned. Call again with
    confirm=true only after the user explicitly approves the preview.
    """
    cid = _cid(customer_id)
    shared_set = _id(list_id, "list_id")
    ids = [_id(campaign_id, "campaign_id") for campaign_id in campaign_ids]
    if not ids:
        raise ValueError("campaign_ids must not be empty.")
    row = _query_one(
        cid,
        "SELECT shared_set.name, shared_set.type FROM shared_set "
        f"WHERE shared_set.id = {shared_set}",
        "negative keyword list",
    )
    if _field(row, "shared_set.type") != "NEGATIVE_KEYWORDS":
        raise ValueError(f"Shared set {shared_set} is not a negative keyword list.")
    names = {
        _field(r, "campaign.id"): _field(r, "campaign.name")
        for r in api.search_stream(
            cid,
            "SELECT campaign.id, campaign.name FROM campaign "
            f"WHERE campaign.id IN ({', '.join(ids)})",
        )
    }
    missing = [campaign_id for campaign_id in ids if campaign_id not in names]
    if missing:
        raise ValueError(f"Campaigns not found in account {cid}: {', '.join(missing)}.")
    linked = {
        _field(r, "campaign.id")
        for r in api.search_stream(
            cid,
            "SELECT campaign.id FROM campaign_shared_set WHERE "
            f"shared_set.id = {shared_set} AND campaign_shared_set.status = 'ENABLED'",
        )
    }
    todo = [campaign_id for campaign_id in ids if (campaign_id in linked) == unlink]
    state = "unlinked" if unlink else "linked"
    warnings = [
        f"Campaign {names[campaign_id]!r} is already {state}."
        for campaign_id in ids
        if campaign_id not in todo
    ]
    if not todo:
        raise ValueError(f"All campaigns are already {state}.")
    set_name = f"customers/{cid}/sharedSets/{shared_set}"
    operations: list[dict[str, Any]]
    if unlink:
        operations = [
            {
                "campaignSharedSetOperation": {
                    "remove": f"customers/{cid}/campaignSharedSets/{c}~{shared_set}"
                }
            }
            for c in todo
        ]
    else:
        operations = [
            {
                "campaignSharedSetOperation": {
                    "create": {"campaign": f"customers/{cid}/campaigns/{c}", "sharedSet": set_name}
                }
            }
            for c in todo
        ]
    list_name = _field(row, "shared_set.name")
    return _write(
        cid,
        operations,
        confirm,
        before={"list": list_name, "linked": [names[c] for c in ids if c in linked]},
        after={"list": list_name, "linked": [] if unlink else [names[c] for c in ids]},
        warnings=warnings,
    )


def _pick(source: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: source[key] for key in keys if key in source}


@mcp.tool(annotations=WRITE)
def clone_campaign(
    customer_id: str | int,
    source_campaign_id: str | int,
    name: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a paused copy of a search campaign, in one atomic call.

    Copies the budget (as a new non-shared budget), bidding strategy and target, networks,
    location option, tracking settings, languages, locations, radius targets, ad schedule,
    campaign negative keywords, campaign assets (images, logo, sitelinks...), linked negative
    keyword lists, and every ad group with its keywords and responsive search ads (ad group
    names have the source campaign name replaced by the new name). Not copied:
    device bid adjustments, audiences, ad group assets and other ad types (listed in warnings).
    The new campaign is created PAUSED; adjust it with the other tools and enable it with
    set_status. With confirm=true resource_names[1] is the new campaign. With confirm=false
    (default) the creation is only validated and a preview is returned. Call again with
    confirm=true only after the user explicitly approves the preview.
    """
    cid = _cid(customer_id)
    source = _id(source_campaign_id, "source_campaign_id")
    if not name.strip():
        raise ValueError("name must not be empty.")
    name = name.strip()
    row = _query_one(
        cid,
        "SELECT campaign.name, campaign.advertising_channel_type, campaign.bidding_strategy, "
        "campaign.bidding_strategy_type, campaign.maximize_conversions.target_cpa_micros, "
        "campaign.maximize_conversion_value.target_roas, "
        "campaign.target_spend.cpc_bid_ceiling_micros, "
        "campaign.network_settings.target_google_search, "
        "campaign.network_settings.target_search_network, "
        "campaign.network_settings.target_content_network, "
        "campaign.network_settings.target_partner_search_network, "
        "campaign.geo_target_type_setting.positive_geo_target_type, "
        "campaign.geo_target_type_setting.negative_geo_target_type, "
        "campaign.tracking_url_template, campaign.final_url_suffix, "
        "campaign_budget.amount_micros, campaign_budget.delivery_method, "
        f"customer.currency_code FROM campaign WHERE campaign.id = {source}",
        "campaign",
    )
    campaign = row["campaign"]
    if campaign.get("advertisingChannelType") != "SEARCH":
        raise ValueError("Only search campaigns can be cloned.")
    if campaign.get("biddingStrategy"):
        raise ValueError("The campaign uses a portfolio bidding strategy, which is not supported.")
    strategy = campaign.get("biddingStrategyType")
    if strategy not in SCHEMES:
        raise ValueError(f"Bidding strategy {strategy} is not supported.")
    scheme = SCHEMES[strategy]
    budget_name = f"customers/{cid}/campaignBudgets/-1"
    campaign_name = f"customers/{cid}/campaigns/-2"
    operations: list[dict[str, Any]] = [
        {
            "campaignBudgetOperation": {
                "create": {
                    "resourceName": budget_name,
                    "name": name,
                    "explicitlyShared": False,
                    **_pick(row["campaignBudget"], "amountMicros", "deliveryMethod"),
                }
            }
        },
        {
            "campaignOperation": {
                "create": {
                    "resourceName": campaign_name,
                    "name": name,
                    "status": "PAUSED",
                    "advertisingChannelType": "SEARCH",
                    "campaignBudget": budget_name,
                    "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
                    scheme: campaign.get(scheme, {}),
                    **_pick(
                        campaign,
                        "networkSettings",
                        "geoTargetTypeSetting",
                        "trackingUrlTemplate",
                        "finalUrlSuffix",
                    ),
                }
            }
        },
    ]
    criteria = api.search_stream(
        cid,
        "SELECT campaign_criterion.type, campaign_criterion.negative, "
        "campaign_criterion.bid_modifier, campaign_criterion.language.language_constant, "
        "campaign_criterion.location.geo_target_constant, "
        "campaign_criterion.proximity.geo_point.latitude_in_micro_degrees, "
        "campaign_criterion.proximity.geo_point.longitude_in_micro_degrees, "
        "campaign_criterion.proximity.radius, campaign_criterion.proximity.radius_units, "
        "campaign_criterion.ad_schedule.day_of_week, campaign_criterion.ad_schedule.start_hour, "
        "campaign_criterion.ad_schedule.start_minute, campaign_criterion.ad_schedule.end_hour, "
        "campaign_criterion.ad_schedule.end_minute, campaign_criterion.keyword.text, "
        "campaign_criterion.keyword.match_type FROM campaign_criterion "
        f"WHERE campaign.id = {source} AND campaign_criterion.status != 'REMOVED' "
        f"AND campaign_criterion.type IN ({', '.join(CLONED_CRITERIA)})",
    )
    criteria_count: dict[str, int] = {}
    for criterion in criteria:
        data = criterion["campaignCriterion"]
        field = CLONED_CRITERIA[data["type"]]
        criteria_count[data["type"]] = criteria_count.get(data["type"], 0) + 1
        operations.append(
            {
                "campaignCriterionOperation": {
                    "create": {
                        "campaign": campaign_name,
                        field: data[field],
                        **_pick(data, "negative", "bidModifier"),
                    }
                }
            }
        )
    assets = api.search_stream(
        cid,
        "SELECT campaign.id, campaign_asset.asset, campaign_asset.field_type FROM campaign_asset "
        f"WHERE campaign.id = {source} AND campaign_asset.status = 'ENABLED'",
    )
    operations += [
        {
            "campaignAssetOperation": {
                "create": {
                    "campaign": campaign_name,
                    **_pick(asset["campaignAsset"], "asset", "fieldType"),
                }
            }
        }
        for asset in assets
    ]
    shared_sets = api.search_stream(
        cid,
        "SELECT campaign.id, campaign_shared_set.shared_set FROM campaign_shared_set "
        f"WHERE campaign.id = {source} AND campaign_shared_set.status = 'ENABLED'",
    )
    operations += [
        {
            "campaignSharedSetOperation": {
                "create": {
                    "campaign": campaign_name,
                    "sharedSet": shared_set["campaignSharedSet"]["sharedSet"],
                }
            }
        }
        for shared_set in shared_sets
    ]
    ad_groups = api.search_stream(
        cid,
        "SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.type, "
        f"ad_group.cpc_bid_micros FROM ad_group WHERE campaign.id = {source} "
        "AND ad_group.status != 'REMOVED'",
    )
    keywords = api.search_stream(
        cid,
        "SELECT ad_group.id, ad_group_criterion.status, ad_group_criterion.negative, "
        "ad_group_criterion.cpc_bid_micros, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type FROM ad_group_criterion "
        f"WHERE campaign.id = {source} AND ad_group.status != 'REMOVED' "
        "AND ad_group_criterion.status != 'REMOVED' AND ad_group_criterion.type = 'KEYWORD'",
    )
    ads = api.search_stream(
        cid,
        "SELECT ad_group.id, ad_group_ad.status, ad_group_ad.ad.type, ad_group_ad.ad.final_urls, "
        "ad_group_ad.ad.responsive_search_ad.headlines, "
        "ad_group_ad.ad.responsive_search_ad.descriptions, "
        "ad_group_ad.ad.responsive_search_ad.path1, ad_group_ad.ad.responsive_search_ad.path2 "
        f"FROM ad_group_ad WHERE campaign.id = {source} AND ad_group.status != 'REMOVED' "
        "AND ad_group_ad.status != 'REMOVED'",
    )
    warnings = []
    summary = []
    for index, ad_group in enumerate(ad_groups):
        source_group = ad_group["adGroup"]
        group_name = f"customers/{cid}/adGroups/{-3 - index}"
        new_group_name = source_group["name"].replace(campaign["name"], name)
        operations.append(
            {
                "adGroupOperation": {
                    "create": {
                        "resourceName": group_name,
                        "campaign": campaign_name,
                        "name": new_group_name,
                        **_pick(source_group, "status", "type", "cpcBidMicros"),
                    }
                }
            }
        )
        group_keywords = [k for k in keywords if k["adGroup"]["id"] == source_group["id"]]
        operations += [
            {
                "adGroupCriterionOperation": {
                    "create": {
                        "adGroup": group_name,
                        **_pick(
                            keyword["adGroupCriterion"],
                            "status",
                            "negative",
                            "cpcBidMicros",
                            "keyword",
                        ),
                    }
                }
            }
            for keyword in group_keywords
        ]
        group_ads = [a for a in ads if a["adGroup"]["id"] == source_group["id"]]
        copied_ads = 0
        for ad_row in group_ads:
            ad = ad_row["adGroupAd"]["ad"]
            if ad.get("type") != "RESPONSIVE_SEARCH_AD":
                warnings.append(
                    f"Skipped {ad.get('type')} ad in ad group {source_group['name']!r}."
                )
                continue
            rsa = ad["responsiveSearchAd"]
            operations.append(
                {
                    "adGroupAdOperation": {
                        "create": {
                            "adGroup": group_name,
                            "status": ad_row["adGroupAd"]["status"],
                            "ad": {
                                "finalUrls": ad["finalUrls"],
                                "responsiveSearchAd": {
                                    "headlines": [
                                        _pick(h, "text", "pinnedField") for h in rsa["headlines"]
                                    ],
                                    "descriptions": [
                                        _pick(d, "text", "pinnedField") for d in rsa["descriptions"]
                                    ],
                                    **_pick(rsa, "path1", "path2"),
                                },
                            },
                        }
                    }
                }
            )
            copied_ads += 1
        summary.append({"name": new_group_name, "keywords": len(group_keywords), "ads": copied_ads})
    targets = campaign.get(scheme, {})
    return _write(
        cid,
        operations,
        confirm,
        before=None,
        after={
            "source": campaign["name"],
            "name": name,
            "status": "PAUSED",
            "daily_budget": _money(row["campaignBudget"].get("amountMicros")),
            "strategy": "MAXIMIZE_CLICKS" if strategy == "TARGET_SPEND" else strategy,
            "target_cpa": _money(targets.get("targetCpaMicros")),
            "target_roas": targets.get("targetRoas"),
            "criteria": criteria_count,
            "campaign_assets": len(assets),
            "negative_keyword_lists": len(shared_sets),
            "ad_groups": summary,
        },
        currency=row["customer"]["currencyCode"],
        warnings=warnings,
    )


def _point(point: dict[str, Any]) -> dict[str, Any]:
    try:
        latitude = Decimal(str(point["latitude"]))
        longitude = Decimal(str(point["longitude"]))
        radius = float(point["radius"])
    except (KeyError, InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid point {point!r}: needs latitude, longitude and radius.") from exc
    units = str(point.get("units", "KILOMETERS")).upper()
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and radius > 0):
        raise ValueError(f"Invalid point {point!r}.")
    if units not in ("KILOMETERS", "MILES"):
        raise ValueError(f"Invalid units {units!r}: use KILOMETERS or MILES.")
    return {
        "geoPoint": {
            "latitudeInMicroDegrees": int(latitude * 1_000_000),
            "longitudeInMicroDegrees": int(longitude * 1_000_000),
        },
        "radius": radius,
        "radiusUnits": units,
    }


def _describe_location(criterion: dict[str, Any]) -> dict[str, Any]:
    if "location" in criterion:
        return {"location": criterion["location"]["geoTargetConstant"]}
    proximity = criterion["proximity"]
    point = proximity.get("geoPoint", {})
    return {
        "latitude": point.get("latitudeInMicroDegrees", 0) / 1_000_000,
        "longitude": point.get("longitudeInMicroDegrees", 0) / 1_000_000,
        "radius": proximity.get("radius"),
        "units": proximity.get("radiusUnits"),
    }


@mcp.tool(annotations=WRITE)
def set_locations(
    customer_id: str | int,
    campaign_id: str | int,
    points: list[dict[str, Any]] | None = None,
    location_ids: list[str | int] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Replace a campaign's targeted locations (negative locations are kept).

    points are radius targets: {"latitude": 29.7604, "longitude": -95.3698, "radius": 20,
    "units": "KILOMETERS" | "MILES"} (units default to KILOMETERS). location_ids are Google
    geo target ids, found with `search` on geo_target_constant (e.g. fields
    geo_target_constant.id, geo_target_constant.canonical_name with condition
    "geo_target_constant.name = 'Houston'"). All current targeted locations and radius targets
    are removed and the new ones added in one call. With confirm=false (default) the change is
    only validated and a preview is returned. Call again with confirm=true only after the user
    explicitly approves the preview.
    """
    if not points and not location_ids:
        raise ValueError("Pass points and/or location_ids.")
    cid = _cid(customer_id)
    campaign = _id(campaign_id, "campaign_id")
    row = _query_one(
        cid, f"SELECT campaign.name FROM campaign WHERE campaign.id = {campaign}", "campaign"
    )
    current = api.search_stream(
        cid,
        "SELECT campaign_criterion.resource_name, campaign_criterion.location.geo_target_constant, "
        "campaign_criterion.proximity.geo_point.latitude_in_micro_degrees, "
        "campaign_criterion.proximity.geo_point.longitude_in_micro_degrees, "
        "campaign_criterion.proximity.radius, campaign_criterion.proximity.radius_units "
        f"FROM campaign_criterion WHERE campaign.id = {campaign} "
        "AND campaign_criterion.type IN (LOCATION, PROXIMITY) "
        "AND campaign_criterion.negative = FALSE AND campaign_criterion.status != 'REMOVED'",
    )
    new = [{"proximity": _point(point)} for point in points or []]
    new += [
        {"location": {"geoTargetConstant": f"geoTargetConstants/{_id(location, 'location_id')}"}}
        for location in location_ids or []
    ]
    operations = [
        {"campaignCriterionOperation": {"remove": c["campaignCriterion"]["resourceName"]}}
        for c in current
    ]
    operations += [
        {
            "campaignCriterionOperation": {
                "create": {"campaign": f"customers/{cid}/campaigns/{campaign}", **criterion}
            }
        }
        for criterion in new
    ]
    name = _field(row, "campaign.name")
    return _write(
        cid,
        operations,
        confirm,
        before={
            "campaign": name,
            "locations": [_describe_location(c["campaignCriterion"]) for c in current],
        },
        after={"campaign": name, "locations": [_describe_location(c) for c in new]},
    )


@mcp.tool(annotations=WRITE)
def set_ad_schedule(
    customer_id: str | int,
    campaign_id: str | int,
    schedule: list[dict[str, Any]],
    confirm: bool = False,
) -> dict[str, Any]:
    """Replace a campaign's ad schedule.

    Each entry is {"day": "MONDAY".."SUNDAY", "start_hour": 5, "end_hour": 23}, with whole
    hours from 0 to 24 (24 means midnight) in the account time zone (returned as time_zone).
    An empty list removes the schedule, so ads run at all hours. Bid adjustments on the
    current schedule are lost. With confirm=false (default) the change is only validated and
    a preview is returned. Call again with confirm=true only after the user explicitly
    approves the preview.
    """
    entries = []
    for entry in schedule:
        day = str(entry.get("day", "")).upper()
        start, end = entry.get("start_hour"), entry.get("end_hour")
        if (
            day not in DAYS
            or not isinstance(start, int)
            or not isinstance(end, int)
            or not 0 <= start < end <= 24
        ):
            raise ValueError(
                f"Invalid schedule entry {entry!r}: needs day in {DAYS} and whole hours "
                "with 0 <= start_hour < end_hour <= 24."
            )
        entries.append({"day": day, "start_hour": start, "end_hour": end})
    cid = _cid(customer_id)
    campaign = _id(campaign_id, "campaign_id")
    row = _query_one(
        cid,
        f"SELECT campaign.name, customer.time_zone FROM campaign WHERE campaign.id = {campaign}",
        "campaign",
    )
    current = api.search_stream(
        cid,
        "SELECT campaign_criterion.resource_name, campaign_criterion.bid_modifier, "
        "campaign_criterion.ad_schedule.day_of_week, campaign_criterion.ad_schedule.start_hour, "
        "campaign_criterion.ad_schedule.end_hour FROM campaign_criterion "
        f"WHERE campaign.id = {campaign} AND campaign_criterion.type = AD_SCHEDULE "
        "AND campaign_criterion.status != 'REMOVED'",
    )
    if not current and not entries:
        raise ValueError("The campaign has no ad schedule to remove.")
    warnings = []
    if any("bidModifier" in c["campaignCriterion"] for c in current):
        warnings.append("Bid adjustments on the current schedule will be lost.")
    operations = [
        {"campaignCriterionOperation": {"remove": c["campaignCriterion"]["resourceName"]}}
        for c in current
    ]
    operations += [
        {
            "campaignCriterionOperation": {
                "create": {
                    "campaign": f"customers/{cid}/campaigns/{campaign}",
                    "adSchedule": {
                        "dayOfWeek": entry["day"],
                        "startHour": entry["start_hour"],
                        "startMinute": "ZERO",
                        "endHour": entry["end_hour"],
                        "endMinute": "ZERO",
                    },
                }
            }
        }
        for entry in entries
    ]
    name = _field(row, "campaign.name")
    time_zone = _field(row, "customer.time_zone")
    before = [
        {
            "day": _field(c, "campaign_criterion.ad_schedule.day_of_week"),
            "start_hour": _field(c, "campaign_criterion.ad_schedule.start_hour"),
            "end_hour": _field(c, "campaign_criterion.ad_schedule.end_hour"),
        }
        for c in current
    ]
    return _write(
        cid,
        operations,
        confirm,
        before={"campaign": name, "time_zone": time_zone, "schedule": before},
        after={"campaign": name, "time_zone": time_zone, "schedule": entries},
        warnings=warnings,
    )


@mcp.tool(annotations=WRITE)
def set_ad_final_url(
    customer_id: str | int,
    ad_id: str | int,
    final_url: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Change the final URL (landing page) of an ad.

    ad_id is ad_group_ad.ad.id. With confirm=false (default) the change is only validated
    and a preview is returned. Call again with confirm=true only after the user explicitly
    approves the preview.
    """
    if not re.fullmatch(r"https?://\S+", final_url):
        raise ValueError(f"Invalid final_url {final_url!r}: must start with http:// or https://.")
    cid = _cid(customer_id)
    ad = _id(ad_id, "ad_id")
    row = _query_one(
        cid,
        "SELECT campaign.name, ad_group.name, ad_group_ad.ad.final_urls FROM ad_group_ad "
        f"WHERE ad_group_ad.ad.id = {ad} AND ad_group_ad.status != 'REMOVED'",
        "ad",
    )
    context = {
        "campaign": _field(row, "campaign.name"),
        "ad_group": _field(row, "ad_group.name"),
    }
    update = {"resourceName": f"customers/{cid}/ads/{ad}", "finalUrls": [final_url]}
    return _write(
        cid,
        [{"adOperation": {"update": update, "updateMask": "final_urls"}}],
        confirm,
        before={**context, "final_urls": _field(row, "ad_group_ad.ad.final_urls")},
        after={**context, "final_urls": [final_url]},
    )


def _asset(spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    kind = str(spec.get("type", "")).upper()
    if kind not in ASSET_TYPES or not all(spec.get(key) for key in ASSET_TYPES[kind][1]):
        raise ValueError(
            f"Invalid asset {spec!r}: needs type in {tuple(ASSET_TYPES)} and its fields "
            "(see the tool description)."
        )
    if kind == "SITELINK":
        descriptions = {key: spec[key] for key in ("description1", "description2") if key in spec}
        return kind, {
            "finalUrls": [spec["final_url"]],
            "sitelinkAsset": {"linkText": spec["link_text"], **descriptions},
        }
    if kind == "CALLOUT":
        return kind, {"calloutAsset": {"calloutText": spec["text"]}}
    if kind == "STRUCTURED_SNIPPET":
        return kind, {
            "structuredSnippetAsset": {"header": spec["header"], "values": spec["values"]}
        }
    return kind, {
        "callAsset": {"countryCode": spec["country_code"], "phoneNumber": spec["phone_number"]}
    }


def _asset_key(kind: str, asset: dict[str, Any]) -> tuple[str, str]:
    content = _pick(asset, "finalUrls", ASSET_TYPES[kind][0])
    return kind, json.dumps(content, sort_keys=True).lower()


@mcp.tool(annotations=WRITE)
def add_assets(
    customer_id: str | int,
    assets: list[dict[str, Any]],
    campaign_id: str | int | None = None,
    ad_group_id: str | int | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create sitelink, callout, structured snippet or call assets and link them to the
    account (default), a campaign (campaign_id) or an ad group (ad_group_id), in one atomic call.

    Each asset is one of:
    {"type": "SITELINK", "link_text": "...", "final_url": "https://...", "description1": "...",
    "description2": "..."} (descriptions are optional, both or none);
    {"type": "CALLOUT", "text": "..."};
    {"type": "STRUCTURED_SNIPPET", "header": "Types", "values": ["...", "...", "..."]} (header
    is one of Google's fixed English headers, 3 to 10 values);
    {"type": "CALL", "phone_number": "(305) 555-0100", "country_code": "US"}.
    Account assets serve in every campaign. Campaign or ad group assets of a type replace,
    there, the ones of that type from higher levels. Assets with the same content already
    linked at that level are skipped with a warning. With confirm=true resource_names
    alternate between each new asset and its link. With confirm=false (default) the change is
    only validated and a preview is returned. Call again with confirm=true only after the user
    explicitly approves the preview.
    """
    if campaign_id is not None and ad_group_id is not None:
        raise ValueError("Pass campaign_id or ad_group_id, not both.")
    if not assets:
        raise ValueError("assets must not be empty.")
    cid = _cid(customer_id)
    wanted = [_asset(spec) for spec in assets]
    link: dict[str, str] = {}
    columns = scope = ""
    if ad_group_id is not None:
        ad_group = _id(ad_group_id, "ad_group_id")
        row = _query_one(
            cid, f"SELECT ad_group.name FROM ad_group WHERE ad_group.id = {ad_group}", "ad_group"
        )
        target = {"ad_group": _field(row, "ad_group.name")}
        resource, operation = "ad_group_asset", "adGroupAssetOperation"
        columns, scope = "ad_group.id, ", f" AND ad_group.id = {ad_group}"
        link = {"adGroup": f"customers/{cid}/adGroups/{ad_group}"}
    elif campaign_id is not None:
        campaign = _id(campaign_id, "campaign_id")
        row = _query_one(
            cid, f"SELECT campaign.name FROM campaign WHERE campaign.id = {campaign}", "campaign"
        )
        target = {"campaign": _field(row, "campaign.name")}
        resource, operation = "campaign_asset", "campaignAssetOperation"
        columns, scope = "campaign.id, ", f" AND campaign.id = {campaign}"
        link = {"campaign": f"customers/{cid}/campaigns/{campaign}"}
    else:
        target = {"account": cid}
        resource, operation = "customer_asset", "customerAssetOperation"
    existing = api.search_stream(
        cid,
        f"SELECT {columns}{resource}.field_type, {ASSET_COLUMNS} FROM {resource} "
        f"WHERE {resource}.status != 'REMOVED' "
        f"AND {resource}.field_type IN ({', '.join(ASSET_TYPES)}){scope}",
    )
    counts: dict[str, int] = {}
    seen = set()
    for r in existing:
        kind = _field(r, f"{resource}.field_type")
        counts[kind] = counts.get(kind, 0) + 1
        seen.add(_asset_key(kind, r.get("asset", {})))
    added: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    warnings = []
    for spec, (kind, body) in zip(assets, wanted, strict=True):
        key = _asset_key(kind, body)
        if key in seen:
            warnings.append(f"Skipped existing {kind} asset {spec!r}.")
            continue
        seen.add(key)
        temp = f"customers/{cid}/assets/-{len(added) + 1}"
        operations += [
            {"assetOperation": {"create": {"resourceName": temp, **body}}},
            {operation: {"create": {**link, "asset": temp, "fieldType": kind}}},
        ]
        added.append({**spec, "type": kind})
    if not added:
        raise ValueError("All assets already exist at that level.")
    after_counts = dict(counts)
    for asset in added:
        after_counts[asset["type"]] = after_counts.get(asset["type"], 0) + 1
    if link:
        level = next(iter(target)).replace("_", " ")
        warnings += [
            f"The {level} had no {kind} assets of its own: these replace the {kind} assets "
            "it inherited from higher levels."
            for kind in dict.fromkeys(asset["type"] for asset in added)
            if not counts.get(kind)
        ]
    return _write(
        cid,
        operations,
        confirm,
        before={**target, "assets": counts},
        after={**target, "assets": after_counts, "added": added},
        warnings=warnings,
    )


@mcp.tool(annotations=WRITE)
def remove_assets(
    customer_id: str | int,
    resource_names: list[str],
    confirm: bool = False,
) -> dict[str, Any]:
    """Unlink assets (sitelinks, callouts, images...) from the account, campaigns or ad groups,
    in one all-or-nothing call. The assets stay in the account library.

    resource_names are the links' resource names as returned by `search`, e.g.
    customers/123/customerAssets/456~SITELINK (customer_asset.resource_name),
    customers/123/campaignAssets/789~456~CALLOUT (campaign_asset.resource_name) or
    customers/123/adGroupAssets/789~456~CALL (ad_group_asset.resource_name). With
    confirm=false (default) the removal is only validated and a preview is returned. Call
    again with confirm=true only after the user explicitly approves the preview.
    """
    cid = _cid(customer_id)
    if not resource_names:
        raise ValueError("resource_names must not be empty.")
    by_kind: dict[str, list[str]] = {}
    for name in dict.fromkeys(resource_names):
        match = re.fullmatch(
            r"customers/(\d+)/(customerAssets|campaignAssets|adGroupAssets)/(?:\d+~)+[A-Z_]+",
            name,
        )
        if not match or match.group(1) != cid:
            raise ValueError(f"Invalid asset link resource_name {name!r} for account {cid}.")
        by_kind.setdefault(match.group(2), []).append(name)
    removed, operations = [], []
    content = ["finalUrls", *(key for key, _ in ASSET_TYPES.values())]
    for kind, names in by_kind.items():
        resource, operation = ASSET_LINKS[kind]
        quoted = ", ".join(f"'{name}'" for name in names)
        rows = {
            _field(row, f"{resource}.resource_name"): row
            for row in api.search_stream(
                cid,
                f"SELECT {resource}.resource_name, {resource}.field_type, {ASSET_COLUMNS} "
                f"FROM {resource} WHERE {resource}.resource_name IN ({quoted}) "
                f"AND {resource}.status != 'REMOVED'",
            )
        }
        missing = [name for name in names if name not in rows]
        if missing:
            raise ValueError(f"Asset links not found in account {cid}: {', '.join(missing)}.")
        for name in names:
            removed.append(
                {
                    "resource_name": name,
                    "field_type": _field(rows[name], f"{resource}.field_type"),
                    **_pick(rows[name].get("asset", {}), *content),
                }
            )
            operations.append({operation: {"remove": name}})
    return _write(
        cid,
        operations,
        confirm,
        before=removed,
        after=None,
        warnings=[f"{len(removed)} asset link(s) will be removed; the assets stay in the account."],
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
