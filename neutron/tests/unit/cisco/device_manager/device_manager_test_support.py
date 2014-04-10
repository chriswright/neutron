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
from neutron import context as n_context
from neutron.db import agents_db
from neutron.manager import NeutronManager
from neutron.openstack.common import log as logging
from neutron.openstack.common import uuidutils
import neutron.plugins
from neutron.plugins.cisco.db.device_manager import hosting_device_manager_db
from neutron.plugins.cisco.extensions import (ciscohostingdevicemanager as
                                              ciscodevmgr)
from neutron.plugins.common import constants
from neutron.tests.unit import test_l3_plugin

LOG = logging.getLogger(__name__)


_uuid = uuidutils.generate_uuid

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

    def _mock_dispatch_pool_maintenance(self):
        # Mock creation/deletion of service VMs
        self.dispatch_pool_maintenance_job_fcn_p = mock.patch(
            'neutron.plugins.cisco.device_manager.hosting_device_manager_db'
            '.HostingDeviceManagerMixin._dispatch_pool_maintenance_job')#,
#            self._dispatch_service_vm_mock)
        self.dispatch_pool_maintenance_job_fcn_p .start()

    def _test_remove_all_hosting_devices(self):
        """Removes all hosting devices created during a test."""
        devmgr = NeutronManager.get_service_plugins()[
            constants.DEVICE_MANAGER]
        context = n_context.get_admin_context()
        devmgr.delete_all_hosting_devices(context, True)

    def _get_fake_resource(self, tenant_id=None, id=None):
        return {'id': id or _uuid(),
                'tenant_id': tenant_id or _uuid()}


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
