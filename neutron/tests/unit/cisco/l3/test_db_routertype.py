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

from neutron.plugins.cisco.common import cisco_constants as c_constants
from neutron.plugins.cisco.extensions import (ciscohostingdevicemanager as
                                              ciscodevmgr)
from neutron.plugins.common import constants
from neutron.tests.unit.cisco.device_manager import device_manager_test_support
from neutron.tests.unit.cisco.device_manager.test_db_device_manager import (
    DeviceManagerTestCaseMixin)
from neutron.tests.unit.cisco.l3 import l3_router_test_support
from neutron.tests.unit import test_db_plugin


CORE_PLUGIN_KLASS = device_manager_test_support.CORE_PLUGIN_KLASS
L3_PLUGIN_KLASS = l3_router_test_support.L3_PLUGIN_KLASS

NS_ROUTERTYPE_NAME = c_constants.NAMESPACE_ROUTER_TYPE
VM_ROUTERTYPE_NAME = c_constants.CSR1KV_ROUTER_TYPE
HW_ROUTERTYPE_NAME = "HW_router"

NOOP_SCHEDULER = ('neutron.plugins.cisco.l3.scheduler.'
                  'noop_l3_router_hosting_device_scheduler.'
                  'NoopL3RouterHostingDeviceScheduler')
NOOP_AGT_DRV = NOOP_SCHEDULER

TEST_SLOT_NEED = 2

RT_SETTINGS = {
    NS_ROUTERTYPE_NAME: {
        'slot_need': 0,
        'scheduler': NOOP_SCHEDULER,
        'cfg_agent_driver': NOOP_AGT_DRV},
    VM_ROUTERTYPE_NAME: {
        'slot_need': TEST_SLOT_NEED,
        'scheduler': 'neutron.plugins.cisco.l3.scheduler.'
                     'l3_router_hosting_device_scheduler.'
                     'L3RouterHostingDeviceScheduler',
        'cfg_agent_driver': NOOP_AGT_DRV},
    HW_ROUTERTYPE_NAME: {
        'slot_need': 200,
        'scheduler': 'neutron.plugins.cisco.l3.scheduler.'
                     'l3_router_hosting_device_scheduler.'
                     'L3RouterHostingDeviceScheduler',
        'cfg_agent_driver': NOOP_AGT_DRV}}


class RoutertypeTestCaseMixin(object):

    def _create_routertype(self, fmt, template_id, name, slot_need,
                           expected_res_status=None, **kwargs):
        data = self._get_test_routertype_attr(template_id=template_id,
                                              name=name, slot_need=slot_need,
                                              **kwargs)
        data.update({'tenant_id': kwargs.get('tenant_id', self._tenant_id)})
        data = {'routertype': data}
        hd_req = self.new_create_request('routertypes', data, fmt)
        hd_res = hd_req.get_response(self.ext_api)
        if expected_res_status:
            self.assertEqual(hd_res.status_int, expected_res_status)
        return hd_res

    @contextlib.contextmanager
    def routertype(self, template_id, name='router type 1',
                   slot_need=TEST_SLOT_NEED, fmt=None, no_delete=False,
                   **kwargs):
        if not fmt:
            fmt = self.fmt
        res = self._create_routertype(fmt, template_id, name, slot_need,
                                      **kwargs)
        if res.status_int >= 400:
            raise webob.exc.HTTPClientError(code=res.status_int)
        routertype = self.deserialize(fmt or self.fmt, res)
        yield routertype
        if not no_delete:
            self._delete('routertypes', routertype['routertype']['id'])

    def _get_test_routertype_attr(self, template_id, name='router type 1',
                                  slot_need=TEST_SLOT_NEED, **kwargs):
        data = {
            'name': name,
            'description': kwargs.get('description'),
            'template_id': template_id,
            'slot_need': slot_need,
            'scheduler': kwargs.get('scheduler', NOOP_SCHEDULER),
            'cfg_agent_driver': kwargs.get('cfg_agent_driver', NOOP_AGT_DRV)}
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

    def _test_create_routertypes(self, mappings=None):
        if mappings is None:
            mappings = {}
        self._routertypes = {}
        for mapping in mappings:
            template = mapping['template']
            if template is None:
                self._routertypes[mapping['router_type']] = None
            else:
                routertype_name = mapping['router_type']
                rt = self._create_routertype(
                    self.fmt, template['hosting_device_template']['id'],
                    routertype_name,
                    RT_SETTINGS[routertype_name]['slot_need'])
                self._routertypes[routertype_name] = self.deserialize(self.fmt,
                                                                      rt)
        return self._routertypes

    def _test_remove_routertypes(self):
        try:
            for name, rt in self._routertypes.items():
                if rt is not None:
                    self._delete('routertypes', rt['routertype']['id'])
        except AttributeError:
            pass


class L3TestRoutertypeExtensionManager(
    l3_router_test_support.TestL3RouterBaseExtensionManager):

    def get_resources(self):
        res = super(L3TestRoutertypeExtensionManager, self).get_resources()
        ext_mgr = (device_manager_test_support.
                   TestDeviceManagerExtensionManager())
        for item in ext_mgr.get_resources():
            res.append(item)
        return res


class TestRoutertypeDBPlugin(test_db_plugin.NeutronDbPluginV2TestCase,
                             RoutertypeTestCaseMixin,
                             DeviceManagerTestCaseMixin):
    resource_prefix_map = dict(
        (k, constants.COMMON_PREFIXES[constants.DEVICE_MANAGER])
        for k in ciscodevmgr.RESOURCE_ATTRIBUTE_MAP.keys())

    def setUp(self, core_plugin=None, l3_plugin=None,
              dm_plugin=None, ext_mgr=None):
        if not core_plugin:
            core_plugin = CORE_PLUGIN_KLASS
        if l3_plugin is None:
            l3_plugin = L3_PLUGIN_KLASS
        service_plugins = {'l3_plugin_name': l3_plugin}
        if dm_plugin is not None:
            service_plugins['dm_plugin_name'] = dm_plugin
        cfg.CONF.set_override('api_extensions_path',
                              l3_router_test_support.extensions_path)
        if not ext_mgr:
            ext_mgr = L3TestRoutertypeExtensionManager()
        super(TestRoutertypeDBPlugin, self).setUp(
            plugin=core_plugin, service_plugins=service_plugins,
            ext_mgr=ext_mgr)

    def test_create_routertype(self):
        with self.hosting_device_template() as hdt:
            attrs = self._get_test_routertype_attr(
                hdt['hosting_device_template']['id'])
            with self.routertype(hdt['hosting_device_template']['id']) as rt:
                for k, v in attrs.iteritems():
                    self.assertEqual(rt['routertype'][k], v)

    def _test_show_routertype(self):
        #TODO
        pass

    def _test_list_routertypes(self):
        #TODO
        pass

    def _test_update_routertype(self):
        #TODO
        pass

    def _test_delete_routertype(self):
        #TODO
        pass

    def _test_delete_routertype_in_use(self):
        #TODO
        pass


class TestRoutertypeDBPluginXML(TestRoutertypeDBPlugin):
    fmt = 'xml'
