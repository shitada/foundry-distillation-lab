"""Offline pricing tests: synthetic meters exercise selection, not live naming claims."""

import copy
import json
import subprocess
import unittest
import urllib.error
import urllib.parse
from unittest.mock import Mock, patch

from foundry_distillation_lab.reporting.pricing import API_URL, AzurePricing, PricingError


IDENTITY = {"model": "gpt-4.1-nano", "version": "2025-04-14",
            "region": "eastus2", "sku": {"name": "GlobalStandard", "capacity": 10}}


def meter(kind="Input", **overrides):
    result = {
        "serviceName": "Foundry Models", "productName": "Azure OpenAI",
        "skuName": "GPT 4.1 nano Global", "meterName": f"GPT 4.1 nano Global {kind}",
        "meterId": kind, "currencyCode": "USD", "type": "Consumption",
        "armRegionName": "eastus2", "unitOfMeasure": "1K", "retailPrice": 0.001,
        "effectiveStartDate": "2025-01-01T00:00:00Z", "tierMinimumUnits": 0,
    }
    result.update(overrides)
    return result


def client(rows):
    pricing = AzurePricing()
    pricing._fetch = Mock(return_value={"Items": rows, "NextPageLink": None})
    return pricing


class DeploymentTests(unittest.TestCase):
    def test_windows_cli_launcher_is_resolved_and_arguments_remain_explicit(self):
        launcher = r"C:\Program Files\Azure\az.CMD"
        with patch("foundry_distillation_lab.reporting.pricing.shutil.which", return_value=launcher), \
                patch("foundry_distillation_lab.reporting.pricing.subprocess.run",
                      return_value=subprocess.CompletedProcess([], 0, '{"id":"sub"}')) as run:
            self.assertEqual(AzurePricing()._az(["account", "show"]), {"id": "sub"})
            self.assertEqual(run.call_args.args[0][0], launcher)
            self.assertFalse(run.call_args.kwargs["shell"])
            with self.assertRaises(PricingError):
                AzurePricing()._az(["account", "show", "--subscription", "sub&command"])
            self.assertEqual(run.call_count, 1)

    def fixture(self):
        account = {"name": "account", "resourceGroup": "rg", "location": "eastus2",
                   "id": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/account",
                   "properties": {"endpoint": "https://custom.openai.azure.com/",
                                  "endpoints": {"AI": "https://account.services.ai.azure.com/"}}}
        deployment = {"id": account["id"] + "/deployments/student",
                      "systemData": {"createdAt": "2025-06-01T00:00:00Z"},
                      "sku": {"name": "GlobalStandard", "capacity": 50},
                      "properties": {"provisioningState": "Succeeded",
                                     "model": {"name": "gpt-4.1-nano", "version": "2025-04-14"}}}
        return account, deployment

    def test_selected_subscription_exact_endpoint_project_and_full_sku(self):
        account, deployment = self.fixture()
        responses = [{"id": "sub"}, [account], deployment]
        with patch("foundry_distillation_lab.reporting.pricing.subprocess.run") as run:
            run.side_effect = [subprocess.CompletedProcess([], 0, json.dumps(x)) for x in responses]
            pricing = AzurePricing(timeout=7)
            value = pricing.deployment("https://account.services.ai.azure.com/api/projects/project", "student")
            self.assertEqual(value["sku"], deployment["sku"])
            self.assertEqual(value["region"], "eastus2")
            self.assertEqual(value["version"], "2025-04-14")
            self.assertEqual(value["created_at"], "2025-06-01T00:00:00Z")
            self.assertEqual(value["provisioning_state"], "Succeeded")
            self.assertEqual(run.call_count, 3)
            for call in run.call_args_list:
                self.assertFalse(call.kwargs["shell"])
                self.assertEqual(call.kwargs["timeout"], 7)
                self.assertIsInstance(call.args[0], list)
            for call in run.call_args_list[1:]:
                args = call.args[0]
                self.assertEqual(args[args.index("--subscription") + 1], "sub")
            value["sku"]["name"] = "changed"
            self.assertEqual(pricing.deployment("https://account.services.ai.azure.com", "student")["sku"]["name"],
                             "GlobalStandard")
            self.assertEqual(run.call_count, 3)

    def test_training_account_region_is_independent_and_cached(self):
        account, deployment = self.fixture()
        training = {**account, "name": "training", "location": "westus",
                    "id": account["id"].replace("/account", "/training"), "properties": {}}
        pricing = AzurePricing()
        pricing._az = Mock(side_effect=[{"id": "sub"}, [account, training], deployment])
        identity = pricing.account("https://training.services.ai.azure.com/api/projects/test")
        self.assertEqual(identity["region"], "westus")
        self.assertEqual(pricing._az.call_count, 2)
        identity["region"] = "changed"
        self.assertEqual(pricing.account("https://training.services.ai.azure.com")["region"], "westus")
        self.assertEqual(pricing.deployment("https://account.openai.azure.com", "student")["region"], "eastus2")
        self.assertEqual(pricing._az.call_count, 3)

    def test_missing_invalid_future_creation_time_stays_unknown(self):
        account, deployment = self.fixture()
        for value in (None, "invalid", "2999-01-01T00:00:00Z"):
            deployment["systemData"]["createdAt"] = value
            pricing = AzurePricing()
            pricing._az = Mock(side_effect=[{"id": "sub"}, [account], deployment])
            self.assertIsNone(pricing.deployment("https://account.openai.azure.com", "student")["created_at"])

    def test_custom_exact_hostname_and_account_name_domains(self):
        account, deployment = self.fixture()
        for host in ("custom.openai.azure.com", "account.openai.azure.com"):
            pricing = AzurePricing()
            pricing._az = Mock(side_effect=[{"id": "sub"}, [account], deployment])
            self.assertEqual(pricing.deployment("https://" + host, "student")["model"], "gpt-4.1-nano")

    def test_ambiguous_missing_untrusted_hosts_and_absent_region(self):
        account, deployment = self.fixture()
        for accounts in ([account, account], [], [{**account, "location": None}]):
            pricing = AzurePricing()
            pricing._az = Mock(side_effect=[{"id": "sub"}, accounts, deployment])
            with self.assertRaises(PricingError):
                pricing.deployment("https://account.openai.azure.com", "student")
            self.assertEqual(pricing._az.call_count, 2)
        for endpoint in ("https://account.openai.azure.com.evil.test", "http://account.openai.azure.com",
                         "https://user@account.openai.azure.com", "https://other.example.com",
                         "https://account.openai.azure.com:8443"):
            with self.assertRaises(PricingError):
                AzurePricing().deployment(endpoint, "student")

    def test_cli_failures_are_clear_and_not_cached(self):
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired("az", 1),
                        OSError("unavailable")):
            with patch("foundry_distillation_lab.reporting.pricing.subprocess.run", side_effect=failure):
                with self.assertRaises(PricingError):
                    AzurePricing().deployment("https://account.openai.azure.com", "student")
        for output in (subprocess.CompletedProcess([], 1, ""),
                       subprocess.CompletedProcess([], 0, "not json")):
            with patch("foundry_distillation_lab.reporting.pricing.subprocess.run", return_value=output):
                with self.assertRaises(PricingError):
                    AzurePricing().deployment("https://account.openai.azure.com", "student")


class RateTests(unittest.TestCase):
    def test_units_cache_provenance_and_zero_base_hosting(self):
        pricing = client([meter(), meter("Output", unitOfMeasure="1M", retailPrice=2),
                          meter("Cached Input", retailPrice=0.0005)])
        result = pricing.rates(IDENTITY)
        self.assertEqual(result["input_per_million"], 1)
        self.assertEqual(result["output_per_million"], 2)
        self.assertEqual(result["cached_input_per_million"], 0.5)
        self.assertEqual(result["hosting_per_hour"], 0)
        self.assertIsNone(result["training_per_million"])
        self.assertEqual(result["missing"], [])
        source = result["sources"]["input_per_million"]
        for key in ("url", "retrieved_at", "filter", "identity", "meterId",
                    "unitOfMeasure", "retailPrice", "effectiveStartDate"):
            self.assertIn(key, source)
        result["sources"].clear()
        self.assertTrue(pricing.rates(IDENTITY)["sources"])
        pricing._fetch.assert_called_once()
        self.assertNotIn("currencyCode=", pricing._fetch.call_args.args[0])

    def test_decoys_do_not_contaminate_token_prices(self):
        decoys = [
            meter("Cached Input"), meter("Training Standard"), meter("Output"),
            meter("Input Batch"), meter("Input Provisioned"), meter("Input Long Context"),
            meter("Input Reserved"), meter("Input DevTest"), meter("Input Fine Tuned"),
            meter("Input", skuName="GPT 4.1 mini Global", meterName="GPT 4.1 mini Global Input"),
            meter("Input", skuName="GPT 4.1 nano Regional", meterName="GPT 4.1 nano Regional Input"),
            meter("Input", skuName="GPT 4.1 nano Data Zone", meterName="GPT 4.1 nano Data Zone Input"),
            meter("Input", skuName="GPT 4.1 nano", meterName="GPT 4.1 nano Input"),
            meter("Input Regional"), meter("Hosting Input"),
            meter("Input 2025-04-15"), meter("Input", meterId=None),
        ]
        for decoy in decoys:
            with self.subTest(decoy=decoy):
                result = client([decoy]).rates(IDENTITY)
                self.assertIsNone(result["input_per_million"])

    def test_currency_dates_type_and_invalid_prices(self):
        for changes in (
            {"currencyCode": "JPY"}, {"type": "Reservation"}, {"armRegionName": "westus"},
            {"effectiveStartDate": "2999-01-01T00:00:00Z"}, {"effectiveStartDate": "invalid"},
            {"effectiveEndDate": "2020-01-01T00:00:00Z"}, {"retailPrice": -1},
            {"retailPrice": float("nan")}, {"retailPrice": float("inf")},
            {"retailPrice": True}, {"unitOfMeasure": "100 Hours"}, {"tierMinimumUnits": 1},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(client([meter(**changes)]).rates(IDENTITY)["input_per_million"])

    def test_exact_duplicates_accepted_distinct_meters_and_dates_ambiguous(self):
        row = meter()
        self.assertEqual(client([row, copy.deepcopy(row)]).rates(IDENTITY)["input_per_million"], 1)
        for changes in ({"meterId": "other"}, {"retailPrice": 0.2},
                        {"effectiveStartDate": "2025-02-01T00:00:00Z"}):
            result = client([row, meter(**changes)]).rates(IDENTITY)
            self.assertIsNone(result["input_per_million"])
            self.assertEqual(result["missing_reasons"]["input_per_million"], "ambiguous_meters")

    def test_fine_tuned_base_metadata_training_and_hosting(self):
        identity = {**IDENTITY, "model": "gpt-4.1-nano-2025-04-14.ft-123"}
        pricing = client([meter(), meter("Fine Tuned Input", retailPrice=0.002),
                          meter("Fine Tuned Output", retailPrice=0.003),
                          meter("Training Standard", retailPrice=0.004,
                                skuName="GPT 4.1 nano Regional", meterName="GPT 4.1 nano Regional Training"),
                          meter("Training Developer", retailPrice=0.005)])
        result = pricing.rates(identity, training_type="standard")
        self.assertEqual(result["input_per_million"], 2)
        self.assertEqual(result["training_per_million"], 4)
        self.assertIsNone(result["hosting_per_hour"])
        self.assertEqual(result["missing"], [])
        self.assertIn("hosting_per_hour", pricing.rates(identity)["missing"])
        with_host = client([meter("Fine Tuned Hosting", unitOfMeasure="1 Hour", retailPrice=1.7)])
        self.assertEqual(with_host.rates(identity)["hosting_per_hour"], 1.7)
        explicit = {**IDENTITY, "model": "custom-student", "base_model": "gpt-4.1-nano-2025-04-14"}
        self.assertEqual(pricing.rates(explicit)["input_per_million"], 2)
        self.assertEqual(pricing.rates(IDENTITY, training_type="developer")["training_per_million"], 5)

    def test_unrecognized_student_and_incomplete_identity_make_no_requests(self):
        for changes in ({"model": "student"}, {"model": "gpt-4.1-nano-something.ft-123"},
                        {"region": None}, {"sku": "ProvisionedManaged"},
                        {"model": "gpt-4.1-nano.ft-123"}, {"model": "gpt-4.1-mini"},
                        {"region": "eastus2' or true"}):
            pricing = client([])
            result = pricing.rates({**IDENTITY, **changes})
            self.assertTrue(result["missing"])
            pricing._fetch.assert_not_called()

    def test_grader_not_nano_and_explicit_regional_data_zone_scope(self):
        grader = {**IDENTITY, "model": "gpt-4.1"}
        rows = [meter(), meter(skuName="GPT 4.1 Global", meterName="GPT 4.1 Global Input",
                              retailPrice=0.004)]
        self.assertEqual(client(rows).rates(grader)["input_per_million"], 4)
        for sku, label in (("Standard", "Regional"), ("DataZoneStandard", "Data Zone")):
            row = meter(skuName=f"GPT 4.1 nano {label}", meterName=f"GPT 4.1 nano {label} Input")
            self.assertEqual(client([row]).rates({**IDENTITY, "sku": sku})["input_per_million"], 1)

    def test_gpt55_requires_explicit_short_context_identity_and_meter(self):
        identity = {**IDENTITY, "model": "gpt-5.5"}
        short = meter(skuName="GPT 5.5 Global Short Context",
                      meterName="GPT 5.5 Global Short Context Input", productName="Azure OpenAI GPT5")
        long = meter(skuName="GPT 5.5 Global Long Context", meterName="GPT 5.5 Global Long Context Input",
                     productName="Azure OpenAI GPT5")
        unlabeled = meter(skuName="GPT 5.5 Global", meterName="GPT 5.5 Global Input",
                          productName="Azure OpenAI GPT5")
        self.assertIsNone(client([short]).rates(identity)["input_per_million"])
        identity["context_tier"] = "short"
        self.assertEqual(client([short, long, unlabeled]).rates(identity)["input_per_million"], 1)
        self.assertIsNone(client([long, unlabeled]).rates(identity)["input_per_million"])

    def test_version_labels_and_mixed_training_meters(self):
        self.assertEqual(client([meter("Input 2025-04-14")]).rates(IDENTITY)["input_per_million"], 1)
        for kind in ("Training Standard Cached Input", "Training Standard Output",
                     "Training Standard Hosting", "Training Standard Developer"):
            with self.subTest(kind=kind):
                result = client([meter(kind)]).rates(IDENTITY, training_type="standard")
                self.assertIsNone(result["training_per_million"])

    def test_training_type_identity_does_not_infer_deployment_scope_or_region(self):
        identity = {**IDENTITY, "sku": "standard", "region": "westus"}
        rows = [meter("Training Standard"), meter("Input", armRegionName="westus"),
                meter("Training Standard", armRegionName="westus", retailPrice=0.004,
                      skuName="GPT 4.1 nano Regional", meterName="GPT 4.1 nano Regional Training")]
        result = client(rows).rates(identity, training_type="standard")
        self.assertEqual(result["training_per_million"], 4)
        self.assertIsNone(result["input_per_million"])
        self.assertIsNone(result["hosting_per_hour"])
        self.assertEqual(result["missing"], [])

    def test_observed_gpt55_short_long_global_datazone_and_premium_decoys(self):
        # Name/price/unit/product samples supplied from the 2026-09-27 public read.
        rows = []
        samples = (
            ("ShortCo", "Gl", (5, 30, 0.5)), ("LongCo", "Gl", (10, 45, 1)),
            ("ShortCo", "Dz", (5.5, 33, 0.55)), ("LongCo", "Dz", (11, 49.5, 1.1)),
        )
        for context, scope, prices in samples:
            for kind, price in zip(("inp", "opt", "cd inp"), prices):
                name = f"5.5 {context} {kind} {scope}"
                rows.append(meter(skuName=name, meterName=name, meterId=name,
                                  productName="Azure OpenAI GPT5", unitOfMeasure="1M",
                                  retailPrice=price, effectiveStartDate="2026-05-01T00:00:00Z"))
        for suffix in ("PP", "Batch"):
            for kind, price in zip(("inp", "opt", "cd inp"), (12.5, 75, 1.25)):
                name = f"5.5 ShortCo {kind} Gl {suffix}"
                rows.append(meter(skuName=name, meterName=name, meterId=name,
                                  productName="Azure OpenAI GPT5", unitOfMeasure="1M",
                                  retailPrice=price, effectiveStartDate="2026-05-01T00:00:00Z"))
        pricing = client(rows)
        for context, scope, prices in samples:
            identity = {**IDENTITY, "model": "gpt-5.5", "version": None,
                        "sku": "GlobalStandard" if scope == "Gl" else "DataZoneStandard",
                        "context_tier": "short" if context == "ShortCo" else "long"}
            result = pricing.rates(identity)
            for key, price in zip(("input_per_million", "output_per_million", "cached_input_per_million"), prices):
                self.assertEqual(result[key], price)
        unknown = pricing.rates({**IDENTITY, "model": "gpt-5.5"})
        self.assertIsNone(unknown["input_per_million"])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(pricing._fetch.call_args.args[0]).query)
        self.assertEqual(query["$filter"], ["contains(skuName, '5.5') and armRegionName eq 'eastus2'"])

    def test_family_records_shared_without_sharing_selected_model_rates(self):
        pricing = client([
            meter(),
            meter("Input", skuName="gpt 4.1 Inp glbl", meterName="gpt 4.1 Inp glbl",
                  meterId="grader", retailPrice=0.002),
            meter("Input", serviceName="Other Service", retailPrice=0.03),
            meter("Input", productName="Other Product", retailPrice=0.04),
            meter("Input", type="Reservation", retailPrice=0.05),
        ])
        self.assertEqual(pricing.rates(IDENTITY)["input_per_million"], 1)
        self.assertEqual(pricing.rates({**IDENTITY, "model": "gpt-4.1"})["input_per_million"], 2)
        pricing._fetch.assert_called_once()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(pricing._fetch.call_args.args[0]).query)
        self.assertEqual(query["$filter"], ["contains(skuName, '4.1') and armRegionName eq 'eastus2'"])
        self.assertNotIn("currencyCode", query)

    def test_missing_is_scoped_to_requested_rate_purpose(self):
        pricing = client([])
        self.assertEqual(pricing.rates(IDENTITY, training_type="standard")["missing"],
                         ["training_per_million"])
        self.assertNotIn("training_per_million", pricing.rates(IDENTITY)["missing"])
        unknown = {**IDENTITY, "model": "unknown"}
        self.assertEqual(pricing.rates(unknown, training_type="standard")["missing"],
                         ["training_per_million"])

    def test_fine_tuned_adapter_version_is_not_the_base_model_version(self):
        pricing = client([
            meter("Fine Tuned Input 2025-04-14", retailPrice=0.002),
            meter("Fine Tuned Input 2025-04-15", retailPrice=0.009),
        ])
        for fields in (
            {"model": "gpt-4.1-nano-2025-04-14.ft-123"},
            {"model": "ftid", "base_model": "gpt-4.1-nano-2025-04-14"},
            {"model": "ftid", "base_model": {"name": "gpt-4.1-nano", "version": "2025-04-14"}},
        ):
            with self.subTest(fields=fields):
                identity = {**IDENTITY, **fields, "version": "1"}
                result = pricing.rates(identity)
                self.assertEqual(result["input_per_million"], 2)
                self.assertIsNone(result["hosting_per_hour"])
        unknown = pricing.rates({**IDENTITY, "model": "ftid", "version": "1"})
        self.assertIsNone(unknown["input_per_million"])
        self.assertIsNone(unknown["hosting_per_hour"])

    def test_observed_september_27_2026_retail_names_and_sample_prices(self):
        # Names/prices supplied from a live eastus2 public API read on 2026-09-27.
        # Remaining envelope fields are synthetic; these are not guaranteed prices.
        def observed(name, price, unit="1K"):
            return meter(skuName=name, meterName=name, meterId=name,
                         retailPrice=price, unitOfMeasure=unit)

        rows = [
            observed("gpt 4.1 nano Inp glbl", 0.0001),
            observed("gpt 4.1 nano Outp glbl", 0.0004),
            observed("gpt 4.1 nano cached Inp glbl", 0.000025),
            observed("gpt 4.1 nano Inp regnl", 0.00011),
            observed("gpt 4.1 nano Outp regnl", 0.00044),
            observed("gpt 4.1 nano cached Inp regnl", 0.000028),
            observed("gpt-4.1-nano-ft input global", 0.0001),
            observed("gpt-4.1-nano-ft output global", 0.0004),
            observed("gpt-4.1-nano-ft cached input global", 0.000025),
            observed("gpt-4.1-nano-ft hosting global", 1.7, "1Hour"),
            observed("gpt-4.1-nano-ft input regional", 0.00011),
            observed("gpt-4.1-nano-ft output regional", 0.00044),
            observed("gpt-4.1-nano-ft cached input regional", 0.000028),
            observed("gpt-4.1-nano-ft hosting regional", 1.7, "1 Hour"),
            observed("gpt-4.1-nano FT Training global", 0.0015),
            observed("gpt-4.1-nano FT Training regional", 0.00165),
            observed("gpt 4.1 nano dev ft training glbl", 0.00075),
            observed("gpt 4.1 nano dev ft inp glbl", 0.009),
            observed("gpt 4.1 nano dev ft outp glbl", 0.009),
        ]
        pricing = client(rows)
        for sku, expected in (("GlobalStandard", (0.1, 0.4, 0.025)),
                              ("Standard", (0.11, 0.44, 0.028))):
            identity = {**IDENTITY, "sku": {"name": sku}}
            for fine_tuned in (False, True):
                if fine_tuned:
                    identity = {**identity, "model": "gpt-4.1-nano-2025-04-14.ft-123", "version": "1"}
                result = pricing.rates(identity)
                for key, value in zip(("input_per_million", "output_per_million", "cached_input_per_million"), expected):
                    self.assertAlmostEqual(result[key], value)
                self.assertEqual(result["hosting_per_hour"], 1.7 if fine_tuned else 0)
        for training_type, expected in (("GlobalStandard", 1.5), ("Standard", 1.65), ("Developer", 0.75)):
            identity = {**IDENTITY, "sku": training_type}
            result = pricing.rates(identity, training_type=training_type)
            self.assertAlmostEqual(result["training_per_million"], expected)
            self.assertEqual(result["missing"], [])


class PagingTests(unittest.TestCase):
    def test_pages_and_records_cached_only_after_completion(self):
        pricing = AzurePricing()
        following = API_URL + "?$skip=100"
        pricing._fetch = Mock(side_effect=[
            {"Items": [meter()], "NextPageLink": following},
            {"Items": [meter("Output")], "NextPageLink": None},
        ])
        self.assertEqual(pricing.rates(IDENTITY)["output_per_million"], 1)
        self.assertEqual(pricing._fetch.call_count, 2)
        pricing.rates(IDENTITY, training_type="standard")
        self.assertEqual(pricing._fetch.call_count, 2)

    def test_failed_second_page_never_caches_partial_results(self):
        pricing = AzurePricing()
        first = {"Items": [meter()], "NextPageLink": API_URL + "?$skip=1"}
        pricing._fetch = Mock(side_effect=[first, PricingError("HTTP 429")])
        with self.assertRaisesRegex(PricingError, "429"):
            pricing.rates(IDENTITY)
        self.assertEqual(pricing._records, {})
        self.assertEqual(pricing._rates, {})
        pricing._fetch = Mock(return_value={"Items": []})
        self.assertIsNone(pricing.rates(IDENTITY)["input_per_million"])

    def test_page_limit_loops_invalid_and_untrusted_urls_fail_closed(self):
        urls = ["http://prices.azure.com/api/retail/prices",
                "https://prices.azure.com.evil.test/api/retail/prices",
                "https://user@prices.azure.com/api/retail/prices",
                "https://prices.azure.com:444/api/retail/prices",
                "https://prices.azure.com/other", "/api/retail/prices", {}, 1]
        for following in urls:
            pricing = AzurePricing()
            pricing._fetch = Mock(return_value={"Items": [meter()], "NextPageLink": following})
            with self.assertRaises(PricingError):
                pricing.rates(IDENTITY)
            pricing._fetch.assert_called_once()
            self.assertEqual(pricing._records, {})
        pricing = AzurePricing(max_pages=1)
        pricing._fetch = Mock(return_value={"Items": [meter()], "NextPageLink": API_URL + "?next=1"})
        with self.assertRaisesRegex(PricingError, "page limit"):
            pricing.rates(IDENTITY)
        pricing = AzurePricing()
        pricing._fetch = Mock(return_value={"Items": [], "NextPageLink": API_URL + "?next=1"})
        with self.assertRaisesRegex(PricingError, "loop"):
            pricing.rates(IDENTITY)
        self.assertEqual(pricing._fetch.call_count, 2)
        for payload in ({}, {"Items": [None]}, []):
            pricing = AzurePricing()
            pricing._fetch = Mock(return_value=payload)
            with self.assertRaises(PricingError):
                pricing.rates(IDENTITY)

    def test_http429_is_explicit_no_retry(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(API_URL, 429, "Too Many Requests", {}, None)
        with patch("foundry_distillation_lab.reporting.pricing.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(PricingError, "HTTP 429"):
                AzurePricing().rates(IDENTITY)
        opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
