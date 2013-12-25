# Copyright 2014 Cisco Systems, Inc.  All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# @author: Bob Melander, Cisco Systems, Inc.

from oslo.config import cfg

from neutron.common import constants
from neutron.db import agentschedulers_db
from neutron.manager import NeutronManager
from neutron.openstack.common import importutils
from neutron.plugins.cisco.common import cisco_constants as c_const
from neutron.plugins.cisco.extensions import (ciscohostingdevicemanager as
                                              ciscodevmgr)
from neutron.plugins.common import constants as svc_constants
from neutron.tests.unit.cisco.device_manager import device_manager_test_support
from neutron.tests.unit.cisco.device_manager import test_db_device_manager
from neutron.tests.unit.cisco.l3 import l3_router_test_support
from neutron.tests.unit.cisco.l3 import test_db_routertype
from neutron.tests.unit.cisco.l3 import test_l3_routertype_aware_schedulers
from neutron.tests.unit.openvswitch import test_agent_scheduler


L3_HOSTA = test_agent_scheduler.L3_HOSTA
L3_HOSTB = test_agent_scheduler.L3_HOSTB

CORE_PLUGIN_KLASS = ('neutron.tests.unit.cisco.l3.test_agent_scheduler.'
                     'TestAgentSchedCorePlugin')
L3_PLUGIN_KLASS = test_l3_routertype_aware_schedulers.L3_PLUGIN_KLASS


# A core plugin supporting Cisco device manager functionality
class TestAgentSchedCorePlugin(device_manager_test_support.TestCorePlugin,
                               agentschedulers_db.DhcpAgentSchedulerDbMixin):

    supported_extension_aliases = ["external-net", "agent",
                                   constants.DHCP_AGENT_SCHEDULER_EXT_ALIAS,
                                   ciscodevmgr.HOSTING_DEVICE_MANAGER_ALIAS]

    def __init__(self):
        self.network_scheduler = importutils.import_object(
            cfg.CONF.network_scheduler_driver)
        super(TestAgentSchedCorePlugin, self).__init__()


class L3RouterApplianceAgentSchedulerTestCase(
        test_agent_scheduler.OvsAgentSchedulerTestCase,
        test_db_routertype.RoutertypeTestCaseMixin,
        test_db_device_manager.DeviceManagerTestCaseMixin,
        l3_router_test_support.L3RouterTestSupportMixin,
        device_manager_test_support.DeviceManagerTestSupportMixin):

    resource_prefix_map = (test_db_device_manager.TestDeviceManagerDBPlugin
                           .resource_prefix_map)

    def setUp(self, core_plugin=None, l3_plugin=None, dm_plugin=None,
              ext_mgr=None):
        self.plugin_str = (CORE_PLUGIN_KLASS if core_plugin is None
                           else core_plugin)
        self.l3_plugin = (L3_PLUGIN_KLASS if l3_plugin is None
                          else l3_plugin)

        cfg.CONF.set_override('api_extensions_path',
                              l3_router_test_support.extensions_path)
        cfg.CONF.set_override('default_router_type',
                              c_const.NAMESPACE_ROUTER_TYPE)

        super(L3RouterApplianceAgentSchedulerTestCase, self).setUp()

        self._mock_l3_admin_tenant()
        templates = self._test_create_hosting_device_templates()
        self._test_create_routertypes(templates.values())

    def tearDown(self):
        self._test_remove_routertypes()
        self._test_remove_hosting_device_templates()
        plugin = NeutronManager.get_service_plugins().get(
            svc_constants.L3_ROUTER_NAT)
        if plugin:
            plugin.reset_all()
        super(L3RouterApplianceAgentSchedulerTestCase, self).tearDown()


class L3RouterApplianceL3AgentNotifierTestCase(
        test_agent_scheduler.OvsL3AgentNotifierTestCase,
        test_db_routertype.RoutertypeTestCaseMixin,
        test_db_device_manager.DeviceManagerTestCaseMixin,
        l3_router_test_support.L3RouterTestSupportMixin,
        device_manager_test_support.DeviceManagerTestSupportMixin):

    resource_prefix_map = (test_db_device_manager.TestDeviceManagerDBPlugin
                           .resource_prefix_map)

    def setUp(self, core_plugin=None, l3_plugin=None, dm_plugin=None,
              ext_mgr=None):
        self.plugin_str = (CORE_PLUGIN_KLASS if core_plugin is None
                           else core_plugin)
        self.l3_plugin = (L3_PLUGIN_KLASS if l3_plugin is None
                          else l3_plugin)

        cfg.CONF.set_override('api_extensions_path',
                              l3_router_test_support.extensions_path)
        cfg.CONF.set_override('default_router_type',
                              c_const.NAMESPACE_ROUTER_TYPE)

        super(L3RouterApplianceL3AgentNotifierTestCase, self).setUp()

        self._mock_l3_admin_tenant()
        templates = self._test_create_hosting_device_templates()
        self._test_create_routertypes(templates.values())

    def tearDown(self):
        self._test_remove_routertypes()
        self._test_remove_hosting_device_templates()
        plugin = NeutronManager.get_service_plugins().get(
            svc_constants.L3_ROUTER_NAT)
        if plugin:
            plugin.reset_all()
        super(L3RouterApplianceL3AgentNotifierTestCase, self).tearDown()
