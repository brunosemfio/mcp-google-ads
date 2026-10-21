from __future__ import annotations

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
def remove_keyword(
    customer_id: str | int,
    resource_name: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Permanently remove a keyword from an ad group, a campaign (negative) or a negative
    keyword list.

    resource_name is the keyword's resource name as returned by `search`, e.g.
    customers/123/adGroupCriteria/456~789 (ad_group_criterion.resource_name),
    customers/123/campaignCriteria/456~789 (campaign_criterion.resource_name) or
    customers/123/sharedCriteria/456~789 (shared_criterion.resource_name). With confirm=false
    (default) the removal is only validated and a preview is returned. Call again with
    confirm=true only after the user explicitly approves the preview.
    """
    cid = _cid(customer_id)
    match = re.fullmatch(
        r"customers/(\d+)/(adGroupCriteria|campaignCriteria|sharedCriteria)/\d+~\d+", resource_name
    )
    if not match or match.group(1) != cid:
        raise ValueError(f"Invalid keyword resource_name {resource_name!r} for account {cid}.")
    resource, operation = REMOVABLE[match.group(2)]
    row = _query_one(
        cid,
        f"SELECT {resource}.keyword.text, {resource}.keyword.match_type FROM {resource} "
        f"WHERE {resource}.resource_name = '{resource_name}' AND {resource}.type = 'KEYWORD'",
        "keyword",
    )
    return _write(
        cid,
        [{operation: {"remove": resource_name}}],
        confirm,
        before={
            "text": _field(row, f"{resource}.keyword.text"),
            "match_type": _field(row, f"{resource}.keyword.match_type"),
        },
        after=None,
        warnings=["The keyword will be permanently removed."],
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
