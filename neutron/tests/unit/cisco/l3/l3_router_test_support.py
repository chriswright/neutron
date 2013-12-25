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

from neutron.api.v2 import attributes
from neutron.common import exceptions as n_exc
from neutron.extensions import l3
from neutron.openstack.common import log as logging
from neutron.openstack.common import uuidutils
import neutron.plugins
from neutron.plugins.cisco.db.l3 import l3_router_appliance_db
from neutron.plugins.cisco.db.l3 import routertype_db
from neutron.plugins.cisco.extensions import routertype
from neutron.plugins.cisco.l3.rpc import l3_rpc_agent_api_noop
from neutron.tests.unit import test_l3_plugin

LOG = logging.getLogger(__name__)


L3_PLUGIN_KLASS = (
    "neutron.tests.unit.cisco.l3.l3_router_test_support."
    "TestL3RouterServicePlugin")
extensions_path = neutron.plugins.__path__[0] + '/cisco/extensions'


class L3RouterTestSupportMixin:

    # Functions to mock service VM creation.
    def _dispatch_service_vm_mock(self, context, instance_name, vm_image,
                                  vm_flavor, hosting_device_drv, mgmt_port,
                                  ports=None):
        vm_id = uuidutils.generate_uuid()

        try:
            # Assumption for now is that this does not need to be
            # plugin dependent, only hosting device type dependent.
            hosting_device_drv.create_configdrive_files(context, mgmt_port)
        except IOError:
            return None

        if mgmt_port is not None:
            p_dict = {'port': {'device_id': vm_id,
                               'device_owner': 'nova'}}
            self._core_plugin.update_port(context, mgmt_port['id'], p_dict)

        for port in ports:
            p_dict = {'port': {'device_id': vm_id,
                               'device_owner': 'nova'}}
            self._core_plugin.update_port(context, port['id'], p_dict)

        myserver = {'server': {'adminPass': "MVk5HPrazHcG",
                    'id': vm_id,
                    'links': [{'href': "http://openstack.example.com/v2/"
                                       "openstack/servers/" + vm_id,
                               'rel': "self"},
                              {'href': "http://openstack.example.com/"
                                       "openstack/servers/" + vm_id,
                               'rel': "bookmark"}]}}

        return myserver['server']

    def _delete_service_vm_mock(self, context, vm_id, hosting_device_drv,
                                mgmt_nw_id):
            result = True
            # Get ports on management network (should be only one)
            ports = self._core_plugin.get_ports(
                context, filters={'device_id': [vm_id],
                                  'network_id': [mgmt_nw_id]})
            if ports:
                hosting_device_drv.delete_configdrive_files(context, ports[0])

            try:
                ports = self._core_plugin.get_ports(
                    context, filters={'device_id': [vm_id]})
                for port in ports:
                    self._core_plugin.delete_port(context, port['id'])
            except n_exc.NeutronException as e:
                LOG.error(_('Failed to delete service VM %(id)s due to '
                            '%(err)s'), {'id': vm_id, 'err': e})
                result = False
            return result

    def _mock_svc_vm_create_delete(self):
        # Mock creation/deletion of service VMs
        self.dispatch_svc_vm_fcn_p = mock.patch(
            'neutron.plugins.cisco.device_manager.service_vm_lib'
            '.ServiceVMManager.dispatch_service_vm',
            self._dispatch_service_vm_mock)
        self.dispatch_svc_vm_fcn_p.start()

        self.delete_svc_vm_fcn_p = mock.patch(
            'neutron.plugins.cisco.device_manager.service_vm_lib'
            '.ServiceVMManager.delete_service_vm',
            self._delete_service_vm_mock)
        self.delete_svc_vm_fcn_p.start()

    def _mock_get_routertype_scheduler_always_none(self):
        self.get_routertype_scheduler_fcn_p = mock.patch(
            'neutron.plugins.cisco.db.l3.l3_router_appliance_db.'
            'L3RouterApplianceDBMixin._get_router_type_scheduler',
            mock.Mock(return_value=None))
        self.get_routertype_scheduler_fcn_p.start()


class TestL3RouterBaseExtensionManager(object):

    def get_resources(self):
        res = l3.L3.get_resources()
        for item in routertype.Routertype.get_resources():
            res.append(item)
        # Add the resources to the global attribute map
        # This is done here as the setup process won't
        # initialize the main API router which extends
        # the global attribute map
        attributes.RESOURCE_ATTRIBUTE_MAP.update(
            l3.RESOURCE_ATTRIBUTE_MAP)
        attributes.RESOURCE_ATTRIBUTE_MAP.update(
            routertype.RESOURCE_ATTRIBUTE_MAP)
        return res

    def get_actions(self):
        return []

    def get_request_extensions(self):
        return []


# A routertype capable L3 routing service plugin class
class TestL3RouterServicePlugin(
    test_l3_plugin.TestL3NatServicePlugin,
    l3_router_appliance_db.L3RouterApplianceDBMixin,
        routertype_db.RoutertypeDbMixin):

    supported_extension_aliases = ["router", routertype.ROUTERTYPE_ALIAS]
    # Disable notifications from l3 base class to l3 agents
    l3_rpc_notifier = l3_rpc_agent_api_noop.L3AgentNotifyNoOp
