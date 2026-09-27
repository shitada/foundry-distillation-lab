"""Bounded, read-only Azure deployment discovery and public USD retail rates.

No billing APIs, resource mutations, inference calls, or disk caches are used.
Meter selection is deliberately conservative: unknown naming is not a price.
The public API may throttle (HTTP 429); failures are explicit and never retried.
Requests use its default USD currency, and every selected row must confirm USD.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request


API_URL = "https://prices.azure.com/api/retail/prices"
RATE_KEYS = (
    "input_per_million", "output_per_million", "cached_input_per_million",
    "hosting_per_hour", "training_per_million",
)
_MODELS = ("gpt-5.5", "gpt-4.1-nano", "gpt-4.1")
_SCOPES = {"Standard": "regional", "GlobalStandard": "global",
           "DataZoneStandard": "data zone"}


class PricingError(RuntimeError):
    """Discovery, authentication, or public-price retrieval could not complete."""


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed


def _text(value):
    return re.sub(r"[^a-z0-9.<>]+", " ", str(value).lower()).strip()


def _endpoint_host(endpoint):
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        host = parsed.hostname or ""
        valid = (parsed.scheme == "https" and not parsed.username
                 and not parsed.password and parsed.port in (None, 443)
                 and re.fullmatch(
                     r"[a-z0-9-]+\.(?:openai\.azure\.com|services\.ai\.azure\.com)",
                     host))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise PricingError("Endpoint must be an HTTPS Azure OpenAI or Foundry account endpoint.")
    return host


def _model_identity(identity):
    model = identity.get("model")
    base = identity.get("base_model")
    if isinstance(base, dict):
        base = base.get("name")
    if not isinstance(model, str):
        return None
    fine_tuned = bool(re.search(r"(?:[.:/-]ft(?:[.:/-]|$)|fine[-_ ]?tuned)", model, re.I))
    if base and base != model:
        fine_tuned = True
    if fine_tuned:
        if base:
            model = base
        elif re.fullmatch(r"gpt-4\.1-nano-2025-04-14\.ft[.:-].+", model):
            model = "gpt-4.1-nano"
        else:
            return None
    if not isinstance(model, str):
        return None
    for supported in _MODELS:
        if re.fullmatch(re.escape(supported) + r"(?:-\d{4}-\d{2}-\d{2})?", model):
            return supported, fine_tuned
    return None


class AzurePricing:
    """Resolve selected-subscription identities and conservatively match retail meters.

    ``sku`` in deployment identities is the complete ARM SKU dictionary. Callers
    may also supply its exact name as a string to ``rates``. Optional
    ``context_tier="short"`` or ``"long"`` selects explicitly labeled GPT-5.5
    context meters; omitted context stays unknown. ``training_type`` accepts
    ``GlobalStandard``, ``Standard``, or ``Developer``. Observed retail naming
    maps these to global, regional, and developer-global training respectively.
    For training-only identities, use the training account's region and set
    ``sku`` to the training type string; no inference scope is inferred.
    Transport failures raise PricingError, while missing/ambiguous rates are
    null with machine-readable rate names in ``missing`` and details in
    ``missing_reasons``. Returned values are copies of in-memory caches.
    """

    def __init__(self, *, timeout=20, max_pages=8):
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise ValueError("timeout must be between 0 and 120 seconds")
        if not isinstance(max_pages, int) or not 1 <= max_pages <= 20:
            raise ValueError("max_pages must be between 1 and 20")
        self.timeout = timeout
        self.max_pages = max_pages
        self._accounts = None
        self._account_identities = {}
        self._deployments = {}
        self._records = {}
        self._rates = {}

    def _az(self, arguments):
        if any(not isinstance(arg, str) or re.search(r'[\r\n"&|<>^%!]', arg) for arg in arguments):
            raise PricingError("Unsupported characters in Azure CLI discovery arguments.")
        try:
            result = subprocess.run(
                [shutil.which("az") or "az", *arguments, "--output", "json", "--only-show-errors"],
                capture_output=True, text=True, check=False, timeout=self.timeout,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise PricingError("Azure CLI is unavailable; install/sign in before deployment discovery.") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PricingError("Azure CLI deployment discovery failed or timed out.") from exc
        if result.returncode:
            raise PricingError(
                "Azure CLI read failed; check sign-in, selected subscription, and account/deployment read permission."
            )
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise PricingError("Azure CLI returned invalid JSON.") from exc

    def account(self, endpoint):
        """Resolve an account or project endpoint without reading deployments."""
        host = _endpoint_host(endpoint)
        if host in self._account_identities:
            return copy.deepcopy(self._account_identities[host])
        if self._accounts is None:
            subscription = self._az(["account", "show"])
            subscription_id = subscription.get("id") if isinstance(subscription, dict) else None
            if not subscription_id:
                raise PricingError("Azure CLI has no selected subscription.")
            accounts = self._az([
                "cognitiveservices", "account", "list", "--subscription", subscription_id,
            ])
            if not isinstance(accounts, list) or not all(isinstance(a, dict) for a in accounts):
                raise PricingError("Azure CLI returned an invalid account list.")
            self._accounts = subscription_id, accounts
        subscription_id, accounts = self._accounts
        matches = []
        for account in accounts:
            props = account.get("properties") or {}
            endpoints = props.get("endpoints") or {}
            urls = [props.get("endpoint")]
            urls.extend(endpoints.values() if isinstance(endpoints, dict) else [])
            hosts = set()
            for url in urls:
                if isinstance(url, str):
                    try:
                        hosts.add(_endpoint_host(url))
                    except PricingError:
                        pass
            name = account.get("name", "")
            if isinstance(name, str) and re.fullmatch(r"[a-zA-Z0-9-]+", name):
                hosts.update({name.lower() + ".openai.azure.com",
                              name.lower() + ".services.ai.azure.com"})
            if host in hosts:
                matches.append(account)
        if len(matches) != 1:
            raise PricingError(
                "Endpoint account is ambiguous in the selected subscription."
                if matches else "Endpoint account was not found in the selected subscription."
            )
        account = matches[0]
        resource_id = account.get("id")
        region = account.get("location")
        group = account.get("resourceGroup")
        if not group and isinstance(resource_id, str):
            match = re.search(r"/resourceGroups/([^/]+)/", resource_id, re.I)
            group = match.group(1) if match else None
        if not all(isinstance(v, str) and v for v in (resource_id, region, group, account.get("name"))):
            raise PricingError("Account metadata lacks its resource ID, region, name, or resource group.")
        identity = {
            "region": region, "resource_id": resource_id, "subscription_id": subscription_id,
            "name": account["name"], "resource_group": group,
            "source": {"type": "azure_cli_arm_read", "resource_id": resource_id,
                       "retrieved_at": _now().isoformat(), "endpoint_host": host},
        }
        self._account_identities[host] = identity
        return copy.deepcopy(identity)

    def deployment(self, endpoint, deployment_name):
        """Read a deployment snapshot; missing ARM creation time stays unknown."""
        host = _endpoint_host(endpoint)
        if not isinstance(deployment_name, str) or not deployment_name or deployment_name.startswith("-"):
            raise PricingError("A deployment name is required.")
        key = host, deployment_name
        if key in self._deployments:
            return copy.deepcopy(self._deployments[key])
        account = self.account(endpoint)
        resource_id = account["resource_id"]
        subscription_id = account["subscription_id"]
        result = self._az([
            "cognitiveservices", "account", "deployment", "show",
            "--subscription", subscription_id, "--name", account["name"],
            "--resource-group", account["resource_group"], "--deployment-name", deployment_name,
        ])
        if not isinstance(result, dict):
            raise PricingError("Azure CLI returned invalid deployment metadata.")
        props = result.get("properties") or {}
        model = props.get("model") or {}
        sku = result.get("sku")
        if not isinstance(model, dict) or not model.get("name") or not isinstance(sku, dict) or not sku.get("name"):
            raise PricingError("Deployment metadata lacks an exact model name or SKU.")
        system_data = result.get("systemData") or {}
        created_at = system_data.get("createdAt") or props.get("createdAt")
        try:
            if _timestamp(created_at) > _now():
                created_at = None
        except (ValueError, TypeError):
            created_at = None
        identity = {
            "model": model["name"], "version": model.get("version"), "region": account["region"],
            "sku": sku, "resource_id": resource_id, "subscription_id": subscription_id,
            "deployment_name": deployment_name, "deployment_id": result.get("id"),
            "created_at": created_at, "provisioning_state": props.get("provisioningState"),
            "base_model": model.get("baseModel") or props.get("baseModel"),
            "source": {
                "type": "azure_cli_arm_read", "resource_id": resource_id,
                "deployment_id": result.get("id"), "retrieved_at": _now().isoformat(),
                "endpoint_host": host,
            },
        }
        self._deployments[key] = identity
        return copy.deepcopy(identity)

    def _fetch(self, url):
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        # Reject redirects, including cross-host redirects, before making another request.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            opener = urllib.request.build_opener(NoRedirect())
            with opener.open(request, timeout=self.timeout) as response:
                payload = response.read(8 * 1024 * 1024 + 1)
                if len(payload) > 8 * 1024 * 1024:
                    raise PricingError("Public retail price response exceeds the size limit.")
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            raise PricingError(f"Public retail prices HTTP {exc.code}; no automatic retry was attempted.") from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise PricingError("Public retail price request failed or returned invalid JSON.") from exc

    @staticmethod
    def _page_url(url):
        if not isinstance(url, str):
            raise PricingError("Invalid retail-price pagination URL.")
        try:
            parsed = urllib.parse.urlsplit(url)
            valid = (parsed.scheme == "https" and parsed.hostname == "prices.azure.com"
                     and parsed.port in (None, 443) and not parsed.username
                     and not parsed.password and not parsed.fragment
                     and parsed.path.rstrip("/") == "/api/retail/prices")
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise PricingError("Untrusted retail-price pagination URL.")
        return url

    def _retail_records(self, model, region):
        family = {"gpt-4.1": "4.1", "gpt-4.1-nano": "4.1", "gpt-5.5": "5.5"}[model]
        key = family, region
        if key in self._records:
            return self._records[key]
        query = f"contains(skuName, '{family}') and armRegionName eq '{region}'"
        url = API_URL + "?" + urllib.parse.urlencode({"$filter": query})
        rows, visited = [], set()
        captured = _now().isoformat()
        for _ in range(self.max_pages):
            self._page_url(url)
            if url in visited:
                raise PricingError("Retail-price pagination loop.")
            visited.add(url)
            page = self._fetch(url)
            if not isinstance(page, dict) or not isinstance(page.get("Items"), list):
                raise PricingError("Public retail price response lacks Items.")
            for row in page["Items"]:
                if not isinstance(row, dict):
                    raise PricingError("Public retail price response contains an invalid item.")
                rows.append((row, {"url": url, "retrieved_at": captured, "filter": query}))
            following = page.get("NextPageLink")
            if following is None or following == "":
                self._records[key] = rows
                return rows
            url = self._page_url(following)
        raise PricingError("Public retail price pagination exceeds the configured page limit.")

    @staticmethod
    def _select(row, model, region, scope, fine_tuned, training_type, context_tier, version, now):
        product = "Azure OpenAI GPT5" if model == "gpt-5.5" else "Azure OpenAI"
        if (row.get("currencyCode") != "USD" or row.get("type") != "Consumption"
                or str(row.get("armRegionName", "")).lower() != region
                or row.get("serviceName") != "Foundry Models"
                or row.get("productName") != product or not row.get("meterId")):
            return None
        try:
            if _timestamp(row.get("effectiveStartDate")) > now:
                return None
            if row.get("effectiveEndDate") and _timestamp(row["effectiveEndDate"]) <= now:
                return None
            price = float(row["retailPrice"])
            if isinstance(row["retailPrice"], bool) or not math.isfinite(price) or price < 0:
                return None
            if float(row.get("tierMinimumUnits", 0)) != 0:
                return None
        except (ValueError, TypeError, KeyError, OverflowError):
            return None
        raw_text = " ".join(str(row.get(k, "")) for k in ("skuName", "meterName", "productName"))
        dates = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", raw_text))
        if dates and (not version or dates != {version}):
            return None
        text = _text(raw_text)
        if re.search(r"\b(reserv\w*|devtest|dev test|batch|provision\w*|ptu|priority|premium|pp|flex)\b", text):
            return None
        model_text = "5.5" if model == "gpt-5.5" else _text(model)
        if not re.search(r"(?<![a-z0-9.])" + re.escape(model_text) + r"(?![a-z0-9.])", text):
            return None
        # A GPT-4.1 request must never inherit nano/mini pricing.
        if model == "gpt-4.1" and re.search(r"\b(nano|mini)\b", text):
            return None
        if model == "gpt-4.1-nano" and re.search(r"\bmini\b", text):
            return None
        scopes = {label for label, pattern in (
            ("data zone", r"\b(?:data ?zone|dz)\b"), ("global", r"\b(?:global|glbl|gl)\b"),
            ("regional", r"\b(?:regional|regnl)\b"),
        ) if re.search(pattern, text)}
        is_training = bool(re.search(r"\btrain(?:ing)?\b", text))
        is_developer = bool(re.search(r"\b(?:dev|developer)\b", text))
        if is_training:
            training_scope = {"globalstandard": "global", "standard": "regional",
                              "developer": "global"}.get(training_type)
            if scopes != {training_scope}:
                return None
        elif scope == "training_only":
            return None
        else:
            if is_developer or scopes != {scope}:
                return None
        if model == "gpt-5.5":
            tiers = {tier for tier, pattern in (
                ("short", r"\b(?:shortco|short context)\b"),
                ("long", r"\b(?:longco|long context)\b"),
            ) if re.search(pattern, text)}
            if context_tier not in ("short", "long") or tiers != {context_tier} or re.search(r"[<>]\s*\d", text):
                return None
        elif re.search(r"\b(context|long|longco|shortco)\b|[<>]\s*\d", text):
            return None
        is_fine_tuned = bool(re.search(r"\b(?:fine tun(?:e|ed|ing)|finetun(?:e|ed|ing)|ft)\b", text))
        if is_training:
            if (not training_type or is_developer != (training_type == "developer")
                    or re.search(r"\b(output|outp|opt|cached?|caching|cd|host(?:ing)?)\b", text)
                    or (training_type == "developer" and re.search(r"\bstandard\b", text))):
                return None
            kind = "training_per_million"
        else:
            if is_fine_tuned != fine_tuned:
                return None
            if re.search(r"\bhost(?:ing)?\b", text):
                if re.search(r"\b(input|inp|output|outp|opt|cached?|caching|cd)\b", text):
                    return None
                kind = "hosting_per_hour"
            elif re.search(r"\b(output|outp|opt)\b", text):
                if re.search(r"\b(input|inp|cached?|caching|cd)\b", text):
                    return None
                kind = "output_per_million"
            elif re.search(r"\b(input|inp)\b", text):
                kind = ("cached_input_per_million" if re.search(r"\b(cached?|caching|cd)\b", text)
                        else "input_per_million")
            else:
                return None
        unit = _text(row.get("unitOfMeasure", ""))
        if kind == "hosting_per_hour":
            return (kind, price) if unit in ("1 hour", "1hour") else None
        factors = {"1k": 1000, "1k tokens": 1000, "1m": 1, "1m tokens": 1}
        if unit not in factors:
            return None
        converted = price * factors[unit]
        return (kind, converted) if math.isfinite(converted) else None

    def rates(self, identity, *, training_type=None):
        """Return nullable public USD rates; never guess missing model/region/SKU."""
        if not isinstance(identity, dict):
            raise ValueError("identity must be a dictionary")
        if training_type is not None:
            training_type = str(training_type).lower()
            if training_type not in ("globalstandard", "standard", "developer"):
                raise ValueError("training_type must be GlobalStandard, Standard, Developer, or None")
        key = json.dumps([identity, training_type], sort_keys=True)
        if key in self._rates:
            return copy.deepcopy(self._rates[key])
        result = {name: None for name in RATE_KEYS}
        result.update(sources={}, missing=[], missing_reasons={}, currency="USD",
                      retrieved_at=_now().isoformat())
        required = ["training_per_million"] if training_type else list(RATE_KEYS[:-1])
        resolved = _model_identity(identity)
        region = identity.get("region")
        sku = identity.get("sku")
        training_only = (training_type is not None and isinstance(sku, str)
                         and sku.lower() == training_type)
        sku = sku.get("name") if isinstance(sku, dict) else sku
        scope = ("training_only" if training_only else
                 _SCOPES.get(sku) if isinstance(sku, str) else None)
        if not resolved or not isinstance(region, str) or not re.fullmatch(r"[a-zA-Z0-9]+", region) or not scope:
            result["missing"] = required
            result["missing_reasons"] = {name: "unsupported_or_incomplete_identity" for name in required}
            return result
        model, fine_tuned = resolved
        base = identity.get("base_model") or identity.get("model")
        version = (base.get("version") if isinstance(base, dict) else None) if fine_tuned else identity.get("version")
        if not version:
            base = base.get("name") if isinstance(base, dict) else base
            match = re.match(re.escape(model) + r"-(\d{4}-\d{2}-\d{2})(?:[.]ft|$)", base) if isinstance(base, str) else None
            version = match.group(1) if match else None
        region = region.lower()
        rows = self._retail_records(model, region)
        candidates = {name: [] for name in RATE_KEYS}
        now = _now()
        for row, provenance in rows:
            selected = self._select(row, model, region, scope, fine_tuned, training_type,
                                    identity.get("context_tier"), version, now)
            if not selected:
                continue
            kind, value = selected
            source = {**provenance, **{field: row.get(field) for field in (
                "meterId", "meterName", "skuName", "productName", "unitOfMeasure",
                "retailPrice", "effectiveStartDate", "effectiveEndDate", "currencyCode",
                "armRegionName", "skuId", "productId",
            )}, "identity": copy.deepcopy(identity), "training_type": training_type}
            candidates[kind].append((value, source))
        for kind, items in candidates.items():
            # Exact duplicate rows are harmless; distinct meters/dates remain ambiguous.
            unique = {}
            for value, source in items:
                signature = json.dumps({k: v for k, v in source.items()
                                        if k not in ("url", "retrieved_at")}, sort_keys=True)
                unique[signature] = value, source
            if len(unique) == 1:
                result[kind], result["sources"][kind] = next(iter(unique.values()))
            elif len(unique) > 1:
                result["missing_reasons"][kind] = "ambiguous_meters"
        if (not fine_tuned and scope in ("regional", "global")
                and result["input_per_million"] is not None
                and result["output_per_million"] is not None
                and not candidates["hosting_per_hour"]):
            result["hosting_per_hour"] = 0.0
            result["sources"]["hosting_per_hour"] = {
                "type": "token_billing_policy",
                "reason": "Base Standard/GlobalStandard PAYG tokens have no deployment hosting charge.",
                "identity": copy.deepcopy(identity), "retrieved_at": result["retrieved_at"],
                "token_meter_sources": [result["sources"][name] for name in
                                        ("input_per_million", "output_per_million")],
            }
        result["missing"] = [name for name in required if result[name] is None]
        for name in result["missing"]:
            result["missing_reasons"].setdefault(name, "no_verified_matching_meter")
        self._rates[key] = result
        return copy.deepcopy(result)
