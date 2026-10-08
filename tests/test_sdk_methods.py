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

    def test_vlan_list_parsing(self):
        module = self.load("test_vlan_parse")
        self.assertEqual([1, 3, 5, 6, 7], module._parse_vlan_list("1,3,5-7"))
        self.assertEqual([], module._parse_vlan_list("all"))
        self.assertEqual([], module._parse_vlan_list(None))


if __name__ == "__main__":
    unittest.main()
