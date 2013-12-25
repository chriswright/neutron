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

from neutron.common import constants
from neutron.common import utils
from neutron import context as neutron_context
from neutron.extensions import portbindings
from neutron import manager
from neutron.openstack.common import jsonutils
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants as svc_constants

LOG = logging.getLogger(__name__)


class L3RouterCfgRpcCallbackMixin(object):
    """Mixin for Cisco cfg agent rpc support in L3 routing service plugin."""

    def cfg_sync_routers(self, context, **kwargs):
        """Sync routers according to filters to a specific Cisco cfg agent.

        @param context: contains user information
        @param kwargs: host, or router_ids
        @return: a list of routers
                 with their hosting devices, interfaces and floating_ips
        """
        router_ids = kwargs.get('router_ids')
        host = kwargs.get('host')
        hd_ids = kwargs.get('hosting_device_ids', [])
        context = neutron_context.get_admin_context()
        l3plugin = manager.NeutronManager.get_service_plugins().get(
            svc_constants.L3_ROUTER_NAT)
        if l3plugin is None:
            routers = {}
            LOG.error(_('No L3 router service plugin registered! '
                        'Will return empty router dictionary to Cisco '
                        'cfg agent@%s.'), host)
        elif utils.is_extension_supported(
                l3plugin, constants.L3_AGENT_SCHEDULER_EXT_ALIAS):
            try:
                routers = l3plugin.list_active_sync_routers_on_hosting_devices(
                    context, host, router_ids, hd_ids)
            except AttributeError:
                routers = {}
        else:
            routers = {}
        plugin = manager.NeutronManager.get_plugin()
        if utils.is_extension_supported(
                plugin, constants.PORT_BINDING_EXT_ALIAS):
            self._ensure_host_set_on_ports(context, plugin, host, routers)
        LOG.debug(_("Routers returned to Cisco cfg agent@%(agt)s:\n "
                    "%(routers)%s"),
                  {'agt': host, 'routers': jsonutils.dumps(routers, indent=5)})
        return routers

    def _ensure_host_set_on_ports(self, context, plugin, host, routers):
        for router in routers:
            LOG.debug(_("Checking router: %(id)s for host: %(host)s"),
                      {'id': router['id'], 'host': host})
            self._ensure_host_set_on_port(context, plugin, host,
                                          router.get('gw_port'))
            for interface in router.get(constants.INTERFACE_KEY, []):
                self._ensure_host_set_on_port(context, plugin, host,
                                              interface)

    def _ensure_host_set_on_port(self, context, plugin, host, port):
        if (port and
            (port.get(portbindings.HOST_ID) != host or
             port.get(portbindings.VIF_TYPE) ==
             portbindings.VIF_TYPE_BINDING_FAILED)):
            plugin.update_port(context, port['id'],
                               {'port': {portbindings.HOST_ID: host}})

    def get_external_network_id(self, context, **kwargs):
        """Get one external network id for cfg agent.

        cfg agent expects only on external network when it performs
        this query.
        """
        context = neutron_context.get_admin_context()
        plugin = manager.NeutronManager.get_plugin()
        net_id = plugin.get_external_network_id(context)
        LOG.debug(_("External network ID returned to cfg agent: %s"),
                  net_id)
        return net_id
