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

import contextlib

from oslo.config import cfg
import webob.exc

from neutron.api import extensions as api_ext
from neutron.common import config
from neutron import context as n_context
from neutron.manager import NeutronManager
from neutron.openstack.common import importutils
from neutron.plugins.cisco.common import cisco_constants as c_constants
from neutron.plugins.cisco.db.device_manager import (hosting_device_manager_db
                                                     as hdm_db)
from neutron.plugins.cisco.extensions import ciscohostingdevicemanager
from neutron.plugins.common import constants
from neutron.tests.unit.cisco.device_manager import device_manager_test_support
from neutron.tests.unit import test_db_plugin


DB_DM_PLUGIN_KLASS = (
    'neutron.plugins.cisco.db.device_manager.hosting_device_manager_db.'
    'HostingDeviceManagerMixin')

NN_CATEGORY = ciscohostingdevicemanager.NETWORK_NODE_CATEGORY
NN_TEMPLATE_NAME = c_constants.NETWORK_NODE_TEMPLATE
NS_ROUTERTYPE_NAME = c_constants.NAMESPACE_ROUTER_TYPE
VM_CATEGORY = ciscohostingdevicemanager.VM_CATEGORY
VM_TEMPLATE_NAME = "CSR1kv_template"
VM_BOOTING_TIME = 420
VM_SLOT_CAPACITY = 3
VM_DESIRED_SLOTS_FREE = 3
VM_ROUTERTYPE_NAME = c_constants.CSR1KV_ROUTER_TYPE
HW_CATEGORY = ciscohostingdevicemanager.HARDWARE_CATEGORY
HW_TEMPLATE_NAME = "HW_template"
HW_ROUTERTYPE_NAME = "HW_router"

DEFAULT_SERVICE_TYPES = "router"
NETWORK_NODE_SERVICE_TYPES = "router:fwaas:vpn"

NOOP_DEVICE_DRIVER = ('neutron.plugins.cisco.device_manager.'
                      'hosting_device_drivers.noop_hd_driver.'
                      'NoopHostingDeviceDriver')
NOOP_PLUGGING_DRIVER = ('neutron.plugins.cisco.device_manager.'
                        'plugging_drivers.noop_plugging_driver.'
                        'NoopPluggingDriver')
TEST_DEVICE_DRIVER = ('neutron.plugins.cisco.test.device_manager.'
                      'hd_test_driver.TestHostingDeviceDriver')
TEST_PLUGGING_DRIVER = ('neutron.plugins.cisco.test.device_manager.'
                        'plugging_test_driver.TestTrunkingPlugDriver')

DESCRIPTION = "default description"
SHARED = True
ACTION = "allow"
ENABLED = True
ADMIN_STATE_UP = True


class DeviceManagerTestCaseMixin(object):

    def _create_hosting_device(self, fmt, template_id, management_port_id,
                               admin_state_up, expected_res_status=None,
                               **kwargs):
        data = {'hosting_device': self._get_test_hosting_device_attr(
            template_id=template_id, management_port_id=management_port_id,
            admin_state_up=admin_state_up, **kwargs)}
        hd_req = self.new_create_request('hosting_devices', data, fmt)
        hd_res = hd_req.get_response(self.ext_api)
        if expected_res_status:
            self.assertEqual(hd_res.status_int, expected_res_status)
        return hd_res

    @contextlib.contextmanager
    def hosting_device(self, template_id, management_port_id, fmt=None,
                       admin_state_up=True, no_delete=False, **kwargs):
        if not fmt:
            fmt = self.fmt
        res = self._create_hosting_device(fmt, template_id, management_port_id,
                                          admin_state_up, **kwargs)
        if res.status_int >= 400:
            raise webob.exc.HTTPClientError(code=res.status_int)
        hosting_device = self.deserialize(fmt or self.fmt, res)
        yield hosting_device
        if not no_delete:
            self._delete('hosting_devices',
                         hosting_device['hosting_device']['id'])

    def _create_hosting_device_template(self, fmt, name, enabled,
                                        host_category,
                                        expected_res_status=None, **kwargs):
        data = {'hosting_device_template':
                self._get_test_hosting_device_template_attr(
                    name=name, enabled=enabled, host_category=host_category,
                    **kwargs)}
        hdt_req = self.new_create_request('hosting_device_templates', data,
                                          fmt)

        hdt_res = hdt_req.get_response(self.ext_api)
        if expected_res_status:
            self.assertEqual(hdt_res.status_int, expected_res_status)
        return hdt_res

    @contextlib.contextmanager
    def hosting_device_template(self, fmt=None, name='device_template_1',
                                enabled=True, host_category=VM_CATEGORY,
                                no_delete=False, **kwargs):
        if not fmt:
            fmt = self.fmt
        res = self._create_hosting_device_template(fmt, name, enabled,
                                                   host_category, **kwargs)
        if res.status_int >= 400:
            raise webob.exc.HTTPClientError(code=res.status_int)
        hd_template = self.deserialize(fmt or self.fmt, res)
        yield hd_template
        if not no_delete:
            self._delete('hosting_device_templates',
                         hd_template['hosting_device_template']['id'])

    def _get_test_hosting_device_attr(self, template_id, management_port_id,
                                      admin_state_up=True, **kwargs):
        data = {
            'tenant_id': kwargs.get('tenant_id', self._tenant_id),
            'template_id': template_id,
            'credentials_id': kwargs.get('credentials_id'),
            'device_id': kwargs.get('device_id', 'mfc_device_id'),
            'admin_state_up': admin_state_up,
            'management_port_id': management_port_id,
            'protocol_port': kwargs.get('protocol_port', 22),
            'cfg_agent_id': kwargs.get('cfg_agent_id'),
            'tenant_bound': kwargs.get('tenant_bound'),
            'auto_delete': kwargs.get('auto_delete', False)}
        return data

    def _get_test_hosting_device_template_attr(self, name='device_template_1',
                                               enabled=True,
                                               host_category=VM_CATEGORY,
                                               **kwargs):
        data = {
            'tenant_id': kwargs.get('tenant_id', self._tenant_id),
            'name': name,
            'enabled': enabled,
            'host_category': host_category,
            'service_types': kwargs.get('service_types',
                                        DEFAULT_SERVICE_TYPES),
            'image': kwargs.get('image'),
            'flavor': kwargs.get('flavor'),
            'default_credentials_id': kwargs.get('default_credentials_id'),
            'configuration_mechanism': kwargs.get('configuration_mechanism'),
            'protocol_port': kwargs.get('protocol_port', 22),
            'booting_time': kwargs.get('booting_time', 0),
            'slot_capacity': kwargs.get('slot_capacity', 0),
            'desired_slots_free': kwargs.get('desired_slots_free', 0),
            'tenant_bound': kwargs.get('tenant_bound', []),
            'device_driver': kwargs.get('device_driver', NOOP_DEVICE_DRIVER),
            'plugging_driver': kwargs.get('plugging_driver',
                                          NOOP_PLUGGING_DRIVER)}
        return data

    def _test_list_resources(self, resource, items,
                             neutron_context=None,
                             query_params=None):
        if resource.endswith('y'):
            resource_plural = resource.replace('y', 'ies')
        else:
            resource_plural = resource + 's'

        res = self._list(resource_plural,
                         neutron_context=neutron_context,
                         query_params=query_params)
        resource = resource.replace('-', '_')
        self.assertEqual(sorted([i['id'] for i in res[resource_plural]]),
                         sorted([i[resource]['id'] for i in items]))

    def _replace_hosting_device_status(self, attrs, old_status, new_status):
        if attrs['status'] is old_status:
            attrs['status'] = new_status
        return attrs

    def _test_create_hosting_device_templates(self):
        # template for network nodes.
        nnt = self._create_hosting_device_template(self.fmt, NN_TEMPLATE_NAME,
                                                   True, NN_CATEGORY)
        vmt = self._create_hosting_device_template(
            self.fmt, VM_TEMPLATE_NAME, True, VM_CATEGORY,
            booting_time=VM_BOOTING_TIME,
            slot_capacity=VM_SLOT_CAPACITY,
            desired_slots_free=VM_DESIRED_SLOTS_FREE,
            device_driver=TEST_DEVICE_DRIVER,
            plugging_driver=TEST_PLUGGING_DRIVER)
        nw_node_template = self.deserialize(self.fmt, nnt)
        vm_template = self.deserialize(self.fmt, vmt)
        hw_template = None
        self._templates = {'network_node': {'template': nw_node_template,
                                            'router_type': NS_ROUTERTYPE_NAME},
                           'vm': {'template': vm_template,
                                  'router_type': VM_ROUTERTYPE_NAME},
                           'hw': {'template': hw_template,
                                  'router_type': HW_ROUTERTYPE_NAME}}
        return self._templates

    def _test_remove_hosting_device_templates(self):

        try:
            for name, info in self._templates.items():
                template = info['template']
                if template is not None:
                    self._delete('hosting_device_templates',
                                 template['hosting_device_template']['id'])
        except AttributeError:
            return


class TestDeviceManagerDBPlugin(
    test_db_plugin.NeutronDbPluginV2TestCase,
    DeviceManagerTestCaseMixin,
    device_manager_test_support.DeviceManagerTestSupportMixin):

    resource_prefix_map = dict(
        (k, constants.COMMON_PREFIXES[constants.DEVICE_MANAGER])
        for k in ciscohostingdevicemanager.RESOURCE_ATTRIBUTE_MAP.keys())

    def setUp(self, core_plugin=None, dm_plugin=None, ext_mgr=None):
        if dm_plugin is None:
            dm_plugin = DB_DM_PLUGIN_KLASS
        service_plugins = {'dm_plugin_name': dm_plugin}
        cfg.CONF.set_override('api_extensions_path',
                              device_manager_test_support.extensions_path)
        hdm_db.HostingDeviceManagerMixin.supported_extension_aliases = (
            [ciscohostingdevicemanager.HOSTING_DEVICE_MANAGER_ALIAS])
        super(TestDeviceManagerDBPlugin, self).setUp(
            plugin=core_plugin, service_plugins=service_plugins,
            ext_mgr=ext_mgr)

        if not ext_mgr:
            self.plugin = importutils.import_object(dm_plugin)
            ext_mgr = api_ext.PluginAwareExtensionManager(
                device_manager_test_support.extensions_path,
                {constants.DEVICE_MANAGER: self.plugin})
            app = config.load_paste_app('extensions_test_app')
            self.ext_api = api_ext.ExtensionMiddleware(app, ext_mgr=ext_mgr)

        self._create_mgmt_nw_for_tests(self.fmt)
        self._devmgr = NeutronManager.get_service_plugins()[
            constants.DEVICE_MANAGER]

    def tearDown(self):
        self._test_remove_all_hosting_devices()
        self._remove_mgmt_nw_for_tests()
        super(TestDeviceManagerDBPlugin, self).tearDown()

    def test_create_vm_hosting_device(self):
        with self.hosting_device_template() as hdt:
            with self.port(subnet=self._mgmt_subnet) as mgmt_port:
                attrs = self._get_test_hosting_device_attr(
                    template_id=hdt['hosting_device_template']['id'],
                    management_port_id=mgmt_port['port']['id'],
                    auto_delete=True)
                with self.hosting_device(
                        template_id=hdt['hosting_device_template']['id'],
                        management_port_id=mgmt_port['port']['id'],
                        auto_delete=True) as hd:
                    for k, v in attrs.iteritems():
                        self.assertEqual(hd['hosting_device'][k], v)

    def test_create_hw_hosting_device(self):
        with self.hosting_device_template(host_category=HW_CATEGORY) as hdt:
            with self.port(subnet=self._mgmt_subnet) as mgmt_port:
                attrs = self._get_test_hosting_device_attr(
                    template_id=hdt['hosting_device_template']['id'],
                    management_port_id=mgmt_port['port']['id'])
                with self.hosting_device(
                        template_id=hdt['hosting_device_template']['id'],
                        management_port_id=mgmt_port['port']['id']) as hd:
                    for k, v in attrs.iteritems():
                        self.assertEqual(hd['hosting_device'][k], v)

    def _test_delete_hosting_device_in_use(self):
        pass

    def test_create_vm_hosting_device_template(self):
        attrs = self._get_test_hosting_device_template_attr()

        with self.hosting_device_template() as hdt:
            for k, v in attrs.iteritems():
                self.assertEqual(hdt['hosting_device_template'][k], v)

    def test_create_hw_hosting_device_template(self):
        attrs = self._get_test_hosting_device_template_attr(
            host_category=HW_CATEGORY)

        with self.hosting_device_template(host_category=HW_CATEGORY) as hdt:
            for k, v in attrs.iteritems():
                self.assertEqual(hdt['hosting_device_template'][k], v)

    def test_create_nn_hosting_device_template(self):
        attrs = self._get_test_hosting_device_template_attr(
            host_category=NN_CATEGORY)

        with self.hosting_device_template(host_category=NN_CATEGORY) as hdt:
            for k, v in attrs.iteritems():
                self.assertEqual(hdt['hosting_device_template'][k], v)

    def _test_show_hosting_device_template(self):
        #TODO
        pass

    def _test_list_hosting_device_templates(self):
        #TODO
        pass

    def _test_update_hosting_device_template(self):
        #TODO
        pass

    def _test_delete_hosting_device_template(self):
        #TODO
        pass

    def _test_delete_hosting_device_template_in_use(self):
        #TODO
        pass

    # driver request tests
    def _test_get_driver(self, get_method, id=None, test_for_none=False,
                         is_admin=False):
        with self.hosting_device_template() as hdt:
            context = n_context.Context(
                None, hdt['hosting_device_template']['tenant_id'],
                is_admin=is_admin)
            driver_getter = getattr(self._devmgr, get_method)
            template_id = id or hdt['hosting_device_template']['id']
            driver = driver_getter(context, template_id)
            if test_for_none:
                self.assertIsNone(driver)
            else:
                self.assertIsNotNone(driver)

    def test_get_hosting_device_driver(self):
        self._test_get_driver('get_hosting_device_driver')

    def test_get_non_existent_hosting_device_driver_returns_none(self):
        self._test_get_driver('get_hosting_device_driver', 'bogus_id', True)

    def test_get_plugging_device_driver(self):
        self._test_get_driver('get_hosting_device_plugging_driver')

    def test_get_non_existent_plugging_device_driver_returns_none(self):
        self._test_get_driver('get_hosting_device_plugging_driver', 'bogus_id',
                              True)

    # slot allocation tests, succeeds means returns True,
    # fails means returns False
    def test_acquire_with_slot_surplus_in_owned_hosting_device_succeeds(self):
        #TODO
        pass

    def test_acquire_with_slot_surplus_in_shared_hosting_device_succeeds(self):
        with self.hosting_device_template() as hdt:
            with self.port(subnet=self._mgmt_subnet) as mgmt_port:
                with self.hosting_device(
                        template_id=hdt['hosting_device_template']['id'],
                        management_port_id=mgmt_port['port']['id'],
                        auto_delete=True) as hd:
                    resource = self._get_fake_resource()
                    self._devmgr._get_hosting_device(context, hd['id'])
                    result = self._devmgr.acquire_hosting_device_slots(
                        context, hd, resource, VM_SLOT_CAPACITY - 1)


    def test_acquire_with_slot_surplus_take_hosting_device_ownership1_succeeds(
            self):
        pass

    def test_acquire_with_slot_surplus_take_hosting_device_ownership2_succeeds(
            self):
        pass

    def test_acquire_slots_in_other_owned_hosting_device_fails(self):
        pass

    def test_acquire_slots_take_ownership_of_other_owned_hosting_device_fails(
            self):
        pass

    def test_acquire_slots_take_ownership_of_multi_tenant_hosting_device_fails(
            self):
        pass

    def test_acquire_with_slot_deficit_in_owned_hosting_device_fails(self):
        pass

    def test_acquire_with_slot_deficit_in_shared_hosting_device_fails(self):
        pass

    def test_acquire_with_slot_deficit_in_other_owned_hosting_device_fails(
            self):
        pass

    def test_acquire_slots_success_triggers_pool_maintenance(self):
        pass

    def test_acquire_with_slot_deficit_fail_triggers_pool_maintenance(self):
        pass

    # slot release tests, succeeds means returns True,
    # fails means returns False
    def test_release_allocated_slots_in_owned_hosting_device_succeeds(self):
        pass

    def test_release_allocated_slots_in_shared_hosting_device_succeeds(self):
        pass

    def test_release_all_slots_returns_hosting_device_ownership(self):
        pass

    def test_release_slots_in_other_owned_hosting_device_fails(self):
        pass

    def test_release_too_many_slots_in_hosting_device_fails(self):
        pass

    def test_release_with_slot_deficit_in_shared_hosting_device_fails(self):
        pass

    def test_release_with_slot_deficit_in_other_owned_hosting_device_fails(
            self):
        pass

    def test_release_slots_success_triggers_pool_maintenance(self):
        pass

    def test_release_too_many_slots_fail_triggers_pool_maintenance(self):
        pass

    # hosting device deletion tests
    def test_delete_all_managed_hosting_devices(self):
        pass

    def test_delete_all_hosting_devices(self):
        pass

    def test_delete_all_managed_hosting_devices_by_template(self):
        pass

    def test_delete_all_hosting_devices_by_template(self):
        pass

    # handled failed hosting device tests
    def test_service_plugins_informed_about_failed_hosting_device(self):
        pass

    def test_vm_based_failed_hosting_device_gets_deleted(self):
        pass

    def test_non_vm_based_failed_hosting_device_not_deleted(self):
        pass

    # hosting device pool maintenance tests


class TestDeviceManagerDBPluginXML(TestDeviceManagerDBPlugin):
    fmt = 'xml'
