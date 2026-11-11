import asyncio
import importlib
import json
import os
import re
import unittest
from unittest.mock import patch

from test_config_and_safety import ROOT, fake_runtime_modules, load_script_module


SDK_CALL = re.compile(r"\bdashboard\.(\w+)\.(\w+)\b")


def sdk_methods(section: str) -> set[str]:
    """Public method names of the installed Meraki SDK's class for a section."""
    module = importlib.import_module(f"meraki.api.{section}")
    methods: set[str] = set()
    for value in vars(module).values():
        if isinstance(value, type) and value.__module__ == module.__name__:
            methods |= {name for name in vars(value) if not name.startswith("_")}
    return methods


class SdkMethodTests(unittest.TestCase):
    def test_every_dashboard_call_exists_in_the_installed_sdk(self):
        # A wrapper around a method the SDK doesn't have fails only when someone
        # calls the tool. Check every call statically against the real SDK.
        source = (ROOT / "meraki-mcp.py").read_text()
        missing = sorted(
            f"{section}.{method}"
            for section, method in set(SDK_CALL.findall(source))
            if method not in sdk_methods(section)
        )
        self.assertEqual([], missing)


class FixedToolTests(unittest.TestCase):
    def load(self, name):
        env = {"MERAKI_API_KEY": "dummy", "MERAKI_ORG_ID": "org1"}
        with patch.dict(os.environ, env, clear=True), fake_runtime_modules():
            return load_script_module("meraki-mcp.py", name)

    def test_device_status_reads_the_org_statuses_for_one_serial(self):
        module = self.load("test_device_status")
        module.dashboard.organizations.getOrganizationDevicesStatuses = (
            lambda org_id, **kwargs: [{"serial": kwargs["serials"][0], "status": "online", "org": org_id}]
        )
        self.assertEqual(
            {"serial": "Q2XX", "status": "online", "org": "org1"},
            json.loads(module.get_device_status("Q2XX")),
        )
        module.dashboard.organizations.getOrganizationDevicesStatuses = lambda org_id, **kwargs: []
        self.assertIn("No device", json.loads(module.get_device_status("Q2XX"))["error"])

    def test_device_uplink_reads_the_org_uplink_addresses_for_one_serial(self):
        module = self.load("test_device_uplink")
        module.dashboard.organizations.getOrganizationDevicesUplinksAddressesByDevice = (
            lambda org_id, **kwargs: [{"serial": kwargs["serials"][0], "uplinks": [{"interface": "man1"}]}]
        )
        self.assertEqual("man1", json.loads(module.get_device_uplink("Q2XX"))["uplinks"][0]["interface"])

    def test_switch_vlans_counts_port_usage_and_adds_layer3_names(self):
        module = self.load("test_switch_vlans")
        switch = module.dashboard.switch
        switch.getOrganizationSwitchPortsBySwitch = lambda org_id, **kwargs: [
            {
                "serial": "Q2SW-1",
                "name": "SW1",
                "ports": [
                    {"portId": "1", "type": "access", "vlan": 6, "voiceVlan": 8},
                    {"portId": "2", "type": "access", "vlan": 6},
                    {"portId": "3", "type": "trunk", "vlan": 14, "allowedVlans": "all"},
                    {"portId": "4", "type": "trunk", "vlan": 1, "allowedVlans": "6,8-9"},
                ],
            },
            {
                "serial": "Q2SW-2",
                "name": "SW2",
                "ports": [{"portId": "1", "type": "access", "vlan": 9}],
            },
        ]

        def routing_interfaces(serial):
            if serial == "Q2SW-2":
                raise RuntimeError("layer 2 switch")
            return [{"vlanId": 6, "name": "Corp", "subnet": "10.0.6.0/24", "interfaceIp": "10.0.6.1"}]

        switch.getDeviceSwitchRoutingInterfaces = routing_interfaces
        switch.getNetworkSwitchStacks = lambda network_id: []

        result = json.loads(module.get_switch_vlans("N_1"))
        vlans = {v["vlanId"]: v for v in result["vlans"]}

        self.assertEqual(2, result["switchCount"])
        self.assertEqual(1, result["trunkPortsAllowingAllVlans"])
        self.assertEqual([1, 6, 8, 9, 14], sorted(vlans))
        self.assertEqual(2, vlans[6]["accessPorts"])
        self.assertEqual(1, vlans[6]["trunkAllowedPorts"])
        self.assertEqual("Corp", vlans[6]["name"])
        self.assertEqual("10.0.6.0/24", vlans[6]["subnet"])
        self.assertEqual(1, vlans[8]["voicePorts"])
        # SW1 only allows VLAN 9 on a trunk; "switches" lists where it's actually used.
        self.assertEqual(["SW2"], vlans[9]["switches"])
        self.assertEqual(1, vlans[14]["trunkNativePorts"])
        self.assertNotIn("name", vlans[9])

    def _clients_module(self, name, pages, clock_step=0.0):
        """Load the module with getNetworkClients serving `pages` in order.

        Each entry is a list of clients or an Exception to raise. The fake
        clock advances clock_step seconds per API call.
        """
        module = self.load(name)
        calls = []
        clock = {"t": 0.0}
        module._now = lambda: clock["t"]
        served = iter(pages)

        def get_network_clients(network_id, **kwargs):
            calls.append(kwargs)
            clock["t"] += clock_step
            page = next(served)
            if isinstance(page, Exception):
                raise page
            return page

        module.clients_dashboard.networks.getNetworkClients = get_network_clients
        return module, calls

    def _get_clients(self, module, **kwargs):
        return json.loads(asyncio.run(module.get_clients("N_1", **kwargs)))

    def test_get_clients_follows_the_cursor_until_a_short_page(self):
        module, calls = self._clients_module(
            "test_clients_paging",
            [[{"id": "k1"}, {"id": "k2"}], [{"id": "k3"}, {"id": "k4"}], [{"id": "k5"}]],
        )
        result = self._get_clients(module, per_page=2, ip="10.48.20.31")

        self.assertTrue(result["complete"])
        self.assertEqual(["k1", "k2", "k3", "k4", "k5"], [c["id"] for c in result["clients"]])
        self.assertEqual(3, result["pages"])
        self.assertEqual([None, "k2", "k4"], [c.get("startingAfter") for c in calls])
        self.assertTrue(all(c["total_pages"] == 1 and c["ip"] == "10.48.20.31" for c in calls))

    def test_get_clients_stops_at_the_time_budget_with_a_resume_token(self):
        # 50s per page: pages 1 and 2 fit the 120s budget, page 3 can't start.
        module, calls = self._clients_module(
            "test_clients_budget",
            [[{"id": "k1"}, {"id": "k2"}], [{"id": "k3"}, {"id": "k4"}], [{"id": "k5"}]],
            clock_step=50.0,
        )
        result = self._get_clients(module, per_page=2)

        self.assertFalse(result["complete"])
        self.assertEqual(2, len(calls))
        self.assertEqual(4, result["count"])
        self.assertEqual("k4", result["next_starting_after"])
        self.assertIn("time budget", result["note"])

        resumed, resumed_calls = self._clients_module("test_clients_resume", [[{"id": "k5"}]])
        result = self._get_clients(resumed, per_page=2, starting_after="k4")
        self.assertTrue(result["complete"])
        self.assertEqual("k4", resumed_calls[0]["startingAfter"])

    def test_get_clients_retries_a_failed_page_once(self):
        module, calls = self._clients_module(
            "test_clients_retry",
            [[{"id": "k1"}, {"id": "k2"}], RuntimeError("503 read timed out"), [{"id": "k3"}]],
        )
        result = self._get_clients(module, per_page=2)
        self.assertTrue(result["complete"])
        self.assertEqual(3, result["count"])

    def test_get_clients_returns_partial_results_when_a_later_page_keeps_failing(self):
        module, _ = self._clients_module(
            "test_clients_partial",
            [[{"id": "k1"}, {"id": "k2"}], RuntimeError("boom"), RuntimeError("boom again")],
        )
        result = self._get_clients(module, per_page=2)
        self.assertFalse(result["complete"])
        self.assertEqual("k2", result["next_starting_after"])
        self.assertIn("boom again", result["note"])

    def test_get_clients_raises_when_the_first_page_fails(self):
        module, _ = self._clients_module(
            "test_clients_first_fail", [RuntimeError("boom"), RuntimeError("boom again")],
        )
        with self.assertRaises(RuntimeError):
            asyncio.run(module.get_clients("N_1"))

    def test_get_clients_honors_total_pages(self):
        module, calls = self._clients_module(
            "test_clients_max_pages",
            [[{"id": "k1"}, {"id": "k2"}], [{"id": "k3"}, {"id": "k4"}]],
        )
        result = self._get_clients(module, per_page=2, total_pages="1")
        self.assertFalse(result["complete"])
        self.assertEqual(1, len(calls))
        self.assertIn("max_pages", result["note"])

    def test_vlan_list_parsing(self):
        module = self.load("test_vlan_parse")
        self.assertEqual([1, 3, 5, 6, 7], module._parse_vlan_list("1,3,5-7"))
        self.assertEqual([], module._parse_vlan_list("all"))
        self.assertEqual([], module._parse_vlan_list(None))


if __name__ == "__main__":
    unittest.main()
