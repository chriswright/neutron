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

import mock
from oslo.config import cfg

from neutron.api.v2 import attributes
from neutron.db import agents_db
from neutron.openstack.common import log as logging
import neutron.plugins
from neutron.plugins.cisco.db.device_manager import hosting_device_manager_db
from neutron.plugins.cisco.extensions import (ciscohostingdevicemanager as
                                              ciscodevmgr)
from neutron.tests.unit import test_l3_plugin

LOG = logging.getLogger(__name__)


CORE_PLUGIN_KLASS = (
    'neutron.tests.unit.cisco.device_manager.device_manager_test_support.'
    'TestCorePlugin')
extensions_path = ':' + neutron.plugins.__path__[0] + '/cisco/extensions'


class DeviceManagerTestSupportMixin:

    def _mock_l3_admin_tenant(self):
        # Mock l3 admin tenant
        self.tenant_id_fcn_p = mock.patch(
            'neutron.plugins.cisco.db.device_manager.'
            'hosting_device_manager_db.HostingDeviceManagerMixin.l3_tenant_id')
        self.tenant_id_fcn = self.tenant_id_fcn_p.start()
        self.tenant_id_fcn.return_value = "L3AdminTenantId"

    def _create_mgmt_nw_for_tests(self, fmt):
        self._mgmt_nw = self._make_network(fmt, cfg.CONF.management_network,
                                           True, tenant_id="L3AdminTenantId",
                                           shared=False)
        self._mgmt_subnet = self._make_subnet(fmt, self._mgmt_nw,
                                              "10.0.100.1", "10.0.100.0/24",
                                              ip_version=4)

    def _remove_mgmt_nw_for_tests(self):
        q_p = "network_id=%s" % self._mgmt_nw['network']['id']
        subnets = self._list('subnets', query_params=q_p)
        if subnets:
            for p in self._list('ports', query_params=q_p).get('ports'):
                self._delete('ports', p['id'])
            self._delete('subnets', self._mgmt_subnet['subnet']['id'])
            self._delete('networks', self._mgmt_nw['network']['id'])


class TestDeviceManagerExtensionManager(object):

    def get_resources(self):
        res = ciscodevmgr.Ciscohostingdevicemanager.get_resources()
        # Add the resources to the global attribute map
        # This is done here as the setup process won't
        # initialize the main API router which extends
        # the global attribute map
        attributes.RESOURCE_ATTRIBUTE_MAP.update(
            ciscodevmgr.RESOURCE_ATTRIBUTE_MAP)
        return res

    def get_actions(self):
        return []

    def get_request_extensions(self):
        return []


# A core plugin supporting Cisco device manager functionality
class TestCorePlugin(test_l3_plugin.TestNoL3NatPlugin, agents_db.AgentDbMixin,
                     hosting_device_manager_db.HostingDeviceManagerMixin):

    supported_extension_aliases = ["external-net",
                                   ciscodevmgr.HOSTING_DEVICE_MANAGER_ALIAS]
