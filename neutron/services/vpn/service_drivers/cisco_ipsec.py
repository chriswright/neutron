# vim: tabstop=10 shiftwidth=4 softtabstop=4
#
# Copyright 2013, Paul Michali, Cisco Systems, Inc.
# All Rights Reserved.
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
import netaddr

from neutron.common import rpc as n_rpc
from neutron import manager
from neutron.openstack.common import log as logging
from neutron.openstack.common import rpc
from neutron.openstack.common.rpc import proxy
from neutron.services.vpn.common import topics
from neutron.services.vpn import service_drivers


LOG = logging.getLogger(__name__)

IPSEC = 'ipsec'
BASE_IPSEC_VERSION = '1.0'


class CiscoCsrIPsecVpnDriverCallBack(object):
    """Handler for agent to plugin RPC messaging."""

    # history
    #   1.0 Initial version

    RPC_API_VERSION = BASE_IPSEC_VERSION

    def __init__(self, driver):
        self.driver = driver

    def create_rpc_dispatcher(self):
        return n_rpc.PluginRpcDispatcher([self])

    def get_vpn_services_on_host(self, context, host=None):
        """Retuns the vpnservices on the host."""
        plugin = self.driver.service_plugin
        vpnservices = plugin._get_agent_hosting_vpn_services(
            context, host)
        return [self.driver._make_vpnservice_dict(vpnservice)
                for vpnservice in vpnservices]

    def update_status(self, context, status):
        """Update status of vpnservices."""
        plugin = self.driver.service_plugin
        plugin.update_status_by_agent(context, status)


class CiscoCsrIPsecVpnAgentApi(proxy.RpcProxy):
    """API and handler for plugin to agent RPC messaging."""

    RPC_API_VERSION = BASE_IPSEC_VERSION

    def _agent_notification(self, context, method, router_id,
                            version=None, **kwargs):
        """Notify update for the agent.

        This method will find where is the router, and
        dispatch notification for the agent.
        """
        adminContext = context.is_admin and context or context.elevated()
        plugin = manager.NeutronManager.get_plugin()
        if not version:
            version = self.RPC_API_VERSION
        l3_agents = plugin.get_l3_agents_hosting_routers(
            adminContext, [router_id],
            admin_state_up=True,
            active=True)
        for l3_agent in l3_agents:
            LOG.debug(_('Notify agent at %(topic)s.%(host)s the message '
                        '%(method)s'),
                      {'topic': topics.CISCO_IPSEC_AGENT_TOPIC,
                       'host': l3_agent.host,
                       'method': method,
                       'args': kwargs})
            self.cast(
                context, self.make_msg(method, **kwargs),
                version=version,
                topic='%s.%s' % (topics.CISCO_IPSEC_AGENT_TOPIC,
                                 l3_agent.host))

    def vpnservice_updated(self, context, router_id):
        """Send update event of vpnservices."""
        method = 'vpnservice_updated'
        self._agent_notification(context, method, router_id)

    def create_ipsec_site_connection(self, context, router_id, conn_id):
        """Send device driver create IPSec site-to-site connection request."""
        LOG.debug("PCM: IPSec connection create with %(router)s %(conn)s",
                  {'router': router_id, 'conn': conn_id})
        self._agent_notification(context, 'create_ipsec_site_connection',
                                 router_id, conn_id=conn_id)
        

class CiscoCsrIPsecVPNDriver(service_drivers.VPNDriver):
    """Cisco CSR VPN Service Driver class for IPsec."""

    def __init__(self, service_plugin):
        self.callbacks = CiscoCsrIPsecVpnDriverCallBack(self)
        self.service_plugin = service_plugin
        self.conn = rpc.create_connection(new=True)
        self.conn.create_consumer(
            topics.CISCO_IPSEC_DRIVER_TOPIC,
            self.callbacks.create_rpc_dispatcher(),
            fanout=False)
        self.conn.consume_in_thread()
        self.agent_rpc = CiscoCsrIPsecVpnAgentApi(
            topics.CISCO_IPSEC_AGENT_TOPIC, BASE_IPSEC_VERSION)

    @property
    def service_type(self):
        return IPSEC

    def create_ipsec_site_connection(self, context, ipsec_site_connection):
        router_id = self.service_plugin._get_vpnservice(
            context, ipsec_site_connection['vpnservice_id'])['router_id']
        self.agent_rpc.create_ipsec_site_connection(
            context, router_id, conn_id=ipsec_site_connection['id'])
        
    def create_ipsec_site_connection_old(self, context, ipsec_site_connection):
        vpnservice = self.service_plugin._get_vpnservice(
            context, ipsec_site_connection['vpnservice_id'])
        self.agent_rpc.vpnservice_updated(context, vpnservice['router_id'])

    def update_ipsec_site_connection(
        self, context, old_ipsec_site_connection, ipsec_site_connection):
        vpnservice = self.service_plugin._get_vpnservice(
            context, ipsec_site_connection['vpnservice_id'])
        self.agent_rpc.vpnservice_updated(context, vpnservice['router_id'])

    def delete_ipsec_site_connection(self, context, ipsec_site_connection):
        vpnservice = self.service_plugin._get_vpnservice(
            context, ipsec_site_connection['vpnservice_id'])
        self.agent_rpc.vpnservice_updated(context, vpnservice['router_id'])

    def create_ikepolicy(self, context, ikepolicy):
        pass

    def delete_ikepolicy(self, context, ikepolicy):
        pass

    def update_ikepolicy(self, context, old_ikepolicy, ikepolicy):
        pass

    def create_ipsecpolicy(self, context, ipsecpolicy):
        pass

    def delete_ipsecpolicy(self, context, ipsecpolicy):
        pass

    def update_ipsecpolicy(self, context, old_ipsec_policy, ipsecpolicy):
        pass

    def create_vpnservice(self, context, vpnservice):
        pass

    def update_vpnservice(self, context, old_vpnservice, vpnservice):
        self.agent_rpc.vpnservice_updated(context, vpnservice['router_id'])

    def delete_vpnservice(self, context, vpnservice):
        self.agent_rpc.vpnservice_updated(context, vpnservice['router_id'])

    def _make_vpnservice_dict(self, vpnservice):
        """Convert vpnservice information for vpn agent.

        also converting parameter name for vpn agent driver
        """
        vpnservice_dict = dict(vpnservice)
        vpnservice_dict['ipsec_site_connections'] = []
        vpnservice_dict['subnet'] = dict(
            vpnservice.subnet)
        vpnservice_dict['external_ip'] = vpnservice.router.gw_port[
            'fixed_ips'][0]['ip_address']
        for ipsec_site_connection in vpnservice.ipsec_site_connections:
            ipsec_site_connection_dict = dict(ipsec_site_connection)
            try:
                netaddr.IPAddress(ipsec_site_connection['peer_id'])
            except netaddr.core.AddrFormatError:
                ipsec_site_connection['peer_id'] = (
                    '@' + ipsec_site_connection['peer_id'])
            ipsec_site_connection_dict['ikepolicy'] = dict(
                ipsec_site_connection.ikepolicy)
            ipsec_site_connection_dict['ipsecpolicy'] = dict(
                ipsec_site_connection.ipsecpolicy)
            vpnservice_dict['ipsec_site_connections'].append(
                ipsec_site_connection_dict)
            peer_cidrs = [
                peer_cidr.cidr
                for peer_cidr in ipsec_site_connection.peer_cidrs]
            ipsec_site_connection_dict['peer_cidrs'] = peer_cidrs
        return vpnservice_dict