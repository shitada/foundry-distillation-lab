from copy import deepcopy
from pathlib import Path
import shutil
import sys
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from foundry_distillation_lab.io import read_json, write_json
from foundry_distillation_lab.training import deployment


class Arm:
    def __init__(self, plan, existing=False):
        self.plan, self.existing = plan, existing
        self.calls = []
        self.body = {"id": plan["resource_id"], **deepcopy(plan["body"]),
                     "systemData": {"createdAt": "2026-01-01T00:00:00Z"}}

    def request(self, method, resource_id, body=None, headers=None):
        self.calls.append((method, resource_id, body, headers))
        if method == "GET":
            return {"http_status": 200 if self.existing else 404,
                    "body": deepcopy(self.body) if self.existing else {}, "etag": '"one"'}
        if method == "PUT":
            self.existing = True
            return {"http_status": 201, "body": deepcopy(self.body), "etag": '"one"'}
        self.existing = False
        return {"http_status": 202, "body": {}, "etag": None}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / "runs" / ("test-deployment-" + uuid.uuid4().hex)
        self.work.mkdir(parents=True)
        self.plan = deployment.build_plan(read_json(ROOT / "configs" / "examples" /
                                                   "cloud-deployment.json"))
        self.path = self.work / "deployment.json"
        write_json(self.path, self.plan)

    def tearDown(self):
        for retry in range(6):
            try:
                shutil.rmtree(self.work)
                return
            except PermissionError:
                if retry == 5:
                    raise
                time.sleep(0.1 * 2 ** retry)

    def test_offline_payload_has_unique_owner_and_explicit_model(self):
        self.assertIn(self.plan["owner"][:12], self.plan["resource_id"])
        self.assertEqual(self.plan["body"]["properties"]["versionUpgradeOption"], "NoAutoUpgrade")
        self.assertEqual(self.plan["api_version"], "2024-10-01")

    def test_invalid_plan_does_not_open_transport(self):
        arm = Arm(self.plan)
        invalid = deepcopy(self.plan)
        invalid["resource_id"] = "/subscriptions/wrong"
        path = self.work / "invalid.json"
        write_json(path, invalid)
        with self.assertRaises(ValueError):
            deployment.deploy(path, run_dir=self.work, transport=arm)
        self.assertFalse(arm.calls)

    def test_existing_resource_never_overwritten(self):
        arm = Arm(self.plan, existing=True)
        with self.assertRaises(ValueError):
            deployment.deploy(self.path, run_dir=self.work, transport=arm)
        self.assertEqual([c[0] for c in arm.calls], ["GET"])

    def test_create_cleanup_with_ownership(self):
        arm = Arm(self.plan)
        deployment.deploy(self.path, run_dir=self.work, transport=arm)
        ownership = self.work / "ownership.json"
        deployment.cleanup(ownership, run_dir=self.work / "cleanup", transport=arm)
        self.assertEqual([c[0] for c in arm.calls], ["GET", "PUT", "GET", "DELETE"])
        self.assertEqual(arm.calls[1][3], {"If-None-Match": "*"})
        self.assertEqual(arm.calls[-1][3], {"If-Match": '"one"'})
        self.assertEqual(read_json(self.work / "cleanup" / "cleanup-receipt.json")["state"],
                         "deletion_requested_not_verified")

    def test_replaced_resource_not_deleted(self):
        arm = Arm(self.plan)
        deployment.deploy(self.path, run_dir=self.work, transport=arm)
        ownership = self.work / "ownership.json"
        arm.body["systemData"]["createdAt"] = "2026-01-02T00:00:00Z"
        with self.assertRaises(ValueError):
            deployment.cleanup(ownership, run_dir=self.work / "cleanup", transport=arm)
        self.assertNotIn("DELETE", [c[0] for c in arm.calls])

    def test_status_only_get(self):
        arm = Arm(self.plan)
        for _ in range(3):
            deployment.status(self.path, run_dir=self.work, transport=arm)
        self.assertEqual([c[0] for c in arm.calls], ["GET", "GET", "GET"])
        self.assertEqual(len(list((self.work / "attempts").glob("*.result.json"))), 3)

    def test_create_and_cleanup_are_not_replayed(self):
        arm = Arm(self.plan)
        deployment.deploy(self.path, run_dir=self.work, transport=arm)
        with self.assertRaises(FileExistsError):
            deployment.deploy(self.path, run_dir=self.work, transport=arm)
        self.assertEqual([c[0] for c in arm.calls], ["GET", "PUT"])
        ownership = self.work / "ownership.json"
        deployment.cleanup(ownership, run_dir=self.work / "cleanup", transport=arm)
        with self.assertRaises(FileExistsError):
            deployment.cleanup(ownership, run_dir=self.work / "cleanup", transport=arm)
        self.assertEqual([c[0] for c in arm.calls], ["GET", "PUT", "GET", "DELETE"])

    def test_uncertain_creation_is_not_replayed(self):
        arm = Arm(self.plan)
        request = arm.request

        def uncertain(method, *args):
            response = request(method, *args)
            if method == "PUT":
                raise TimeoutError("Response was lost")
            return response

        arm.request = uncertain
        with self.assertRaises(TimeoutError):
            deployment.deploy(self.path, run_dir=self.work, transport=arm)
        with self.assertRaises(FileExistsError):
            deployment.deploy(self.path, run_dir=self.work, transport=arm)
        self.assertEqual([c[0] for c in arm.calls], ["GET", "PUT"])
        results = [read_json(path) for path in (self.work / "attempts").glob("*.result.json")]
        self.assertIn("outcome_unknown", [result["status"] for result in results])
        self.assertFalse((self.work / "ownership.json").exists())

    def test_tampered_target_rejected(self):
        self.plan["resource_id"] = "/subscriptions/not-a-resource"
        with self.assertRaises(ValueError):
            deployment.validate_plan(self.plan)


if __name__ == "__main__":
    unittest.main()
