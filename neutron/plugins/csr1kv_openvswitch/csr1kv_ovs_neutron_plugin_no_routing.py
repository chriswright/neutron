# Copyright 2011 VMware, Inc.
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
# @author: Somik Behera, Nicira Networks, Inc.
# @author: Brad Hall, Nicira Networks, Inc.
# @author: Dan Wendlandt, Nicira Networks, Inc.
# @author: Dave Lapsley, Nicira Networks, Inc.
# @author: Aaron Rosen, Nicira Networks, Inc.
# @author: Bob Kukura, Red Hat, Inc.
# @author: Seetharama Ayyadevara, Freescale Semiconductor, Inc.
# @author: Bob Melander, Cisco Systems, Inc.

import sys

from oslo.config import cfg
import sqlalchemy as sa
from sqlalchemy.orm import exc

from neutron.agent import securitygroups_rpc as sg_rpc
from neutron.api.rpc.agentnotifiers import dhcp_rpc_agent_api
from neutron.api.v2 import attributes
from neutron.common import constants as q_const
from neutron.common import exceptions as n_exc
from neutron.common import rpc as q_rpc
from neutron.common import topics
from neutron.common import utils
from neutron.db import agents_db
from neutron.db import agentschedulers_db
from neutron.db import allowedaddresspairs_db as addr_pair_db
from neutron.db import db_base_plugin_v2
from neutron.db import dhcp_rpc_base
from neutron.db import external_net_db
from neutron.db import extradhcpopt_db
from neutron.db import model_base
from neutron.db import models_v2
from neutron.db import portbindings_db
from neutron.db import quota_db  # noqa
from neutron.db import securitygroups_rpc_base as sg_db_rpc
from neutron.extensions import allowedaddresspairs as addr_pair
from neutron.extensions import extra_dhcp_opt as edo_ext
from neutron.extensions import portbindings
from neutron.extensions import providernet as provider
from neutron import manager
from neutron.openstack.common import importutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import rpc
from neutron.openstack.common.rpc import proxy
import neutron.plugins
from neutron.plugins.common import constants as svc_constants
from neutron.plugins.common import utils as plugin_utils
from neutron.plugins.csr1kv_openvswitch.common import config  # noqa
from neutron.plugins.csr1kv_openvswitch.common import constants
from neutron.plugins.csr1kv_openvswitch.extensions import trunkport
from neutron.plugins.openvswitch import ovs_db_v2


LOG = logging.getLogger(__name__)


class CSR1kv_OVSRpcCallbacks(dhcp_rpc_base.DhcpRpcCallbackMixin,
                             sg_db_rpc.SecurityGroupServerRpcCallbackMixin):

    # history
    #   1.0 Initial version
    #   1.1 Support Security Group RPC

    RPC_API_VERSION = '1.1'

    def __init__(self, notifier, tunnel_type, plugin):
        self.notifier = notifier
        self.tunnel_type = tunnel_type
        # Bob - Patch to handle trunk ports.
        self.plugin = plugin
        # Bob - End of patch

    def create_rpc_dispatcher(self):
        '''Get the rpc dispatcher for this manager.

        If a manager would like to set an rpc API version, or support more than
        one class as the target of rpc messages, override this method.
        '''
        return q_rpc.PluginRpcDispatcher([self,
                                          agents_db.AgentExtRpcCallback()])

    @classmethod
    def get_port_from_device(cls, device):
        port = ovs_db_v2.get_port_from_device(device)
        if port:
            port['device'] = device
        return port

    def get_device_details(self, rpc_context, **kwargs):
        """Agent requests device details."""
        agent_id = kwargs.get('agent_id')
        device = kwargs.get('device')
        LOG.debug(_("Device %(device)s details requested from %(agent_id)s"),
                  {'device': device, 'agent_id': agent_id})
        port = ovs_db_v2.get_port(device)
        if port:
            binding = ovs_db_v2.get_network_binding(None, port['network_id'])
            entry = {'device': device,
                     'network_id': port['network_id'],
                     'port_id': port['id'],
                     'admin_state_up': port['admin_state_up'],
                     'network_type': binding.network_type,
                     'segmentation_id': binding.segmentation_id,
                     'physical_network': binding.physical_network}
            # Bob - Patch to handle trunk ports.
            self.plugin.extend_resource_dict_trunks(rpc_context, entry,
                                                    entry['network_id'],
                                                    extended_info=True)
            # Bob - End of patch
            new_status = (q_const.PORT_STATUS_ACTIVE if port['admin_state_up']
                          else q_const.PORT_STATUS_DOWN)
            if port['status'] != new_status:
                ovs_db_v2.set_port_status(port['id'], new_status)
        else:
            entry = {'device': device}
            LOG.debug(_("%s can not be found in database"), device)
        return entry

    def update_device_down(self, rpc_context, **kwargs):
        """Device no longer exists on agent."""
        agent_id = kwargs.get('agent_id')
        device = kwargs.get('device')
        host = kwargs.get('host')
        port = ovs_db_v2.get_port(device)
        LOG.debug(_("Device %(device)s no longer exists on %(agent_id)s"),
                  {'device': device, 'agent_id': agent_id})
        if port:
            entry = {'device': device,
                     'exists': True}
            plugin = manager.NeutronManager.get_plugin()
            if (host and
                not plugin.get_port_host(rpc_context, port['id']) == host):
                LOG.debug(_("Device %(device)s not bound to the"
                            " agent host %(host)s"),
                          {'device': device, 'host': host})
            elif port['status'] != q_const.PORT_STATUS_DOWN:
                # Set port status to DOWN
                ovs_db_v2.set_port_status(port['id'],
                                          q_const.PORT_STATUS_DOWN)
        else:
            entry = {'device': device,
                     'exists': False}
            LOG.debug(_("%s can not be found in database"), device)
        return entry

    def update_device_up(self, rpc_context, **kwargs):
        """Device is up on agent."""
        agent_id = kwargs.get('agent_id')
        device = kwargs.get('device')
        host = kwargs.get('host')
        port = ovs_db_v2.get_port(device)
        LOG.debug(_("Device %(device)s up on %(agent_id)s"),
                  {'device': device, 'agent_id': agent_id})
        plugin = manager.NeutronManager.get_plugin()
        if port:
            if (host and
                not plugin.get_port_host(rpc_context, port['id']) == host):
                LOG.debug(_("Device %(device)s not bound to the"
                            " agent host %(host)s"),
                          {'device': device, 'host': host})
                return
            elif port['status'] != q_const.PORT_STATUS_ACTIVE:
                ovs_db_v2.set_port_status(port['id'],
                                          q_const.PORT_STATUS_ACTIVE)
        else:
            LOG.debug(_("%s can not be found in database"), device)

    def tunnel_sync(self, rpc_context, **kwargs):
        """Update new tunnel.

        Updates the datbase with the tunnel IP. All listening agents will also
        be notified about the new tunnel IP.
        """
        tunnel_ip = kwargs.get('tunnel_ip')
        # Update the database with the IP
        tunnel = ovs_db_v2.add_tunnel_endpoint(tunnel_ip)
        tunnels = ovs_db_v2.get_tunnel_endpoints()
        entry = dict()
        entry['tunnels'] = tunnels
        # Notify all other listening agents
        self.notifier.tunnel_update(rpc_context, tunnel.ip_address,
                                    tunnel.id, self.tunnel_type)
        # Return the list of tunnels IP's to the agent
        return entry


class AgentNotifierApi(proxy.RpcProxy,
                       sg_rpc.SecurityGroupAgentRpcApiMixin):
    '''Agent side of the openvswitch rpc API.

    API version history:
        1.0 - Initial version.

    '''

    BASE_RPC_API_VERSION = '1.0'

    def __init__(self, topic):
        super(AgentNotifierApi, self).__init__(
            topic=topic, default_version=self.BASE_RPC_API_VERSION)
        self.topic_network_delete = topics.get_topic_name(topic,
                                                          topics.NETWORK,
                                                          topics.DELETE)
        self.topic_port_update = topics.get_topic_name(topic,
                                                       topics.PORT,
                                                       topics.UPDATE)
        self.topic_tunnel_update = topics.get_topic_name(topic,
                                                         constants.TUNNEL,
                                                         topics.UPDATE)

    def network_delete(self, context, network_id):
        self.fanout_cast(context,
                         self.make_msg('network_delete',
                                       network_id=network_id),
                         topic=self.topic_network_delete)

    def port_update(self, context, port, network_type, segmentation_id,
                    physical_network):
        self.fanout_cast(context,
                         self.make_msg('port_update',
                                       port=port,
                                       network_type=network_type,
                                       segmentation_id=segmentation_id,
                                       physical_network=physical_network),
                         topic=self.topic_port_update)

    def tunnel_update(self, context, tunnel_ip, tunnel_id, tunnel_type):
        self.fanout_cast(context,
                         self.make_msg('tunnel_update',
                                       tunnel_ip=tunnel_ip,
                                       tunnel_id=tunnel_id,
                                       tunnel_type=tunnel_type),
                         topic=self.topic_tunnel_update)


# Bob - Patch to handle trunk ports.

MIN_VLAN=100
MAX_VLAN=4000

class TrunkMapping(model_base.BASEV2):
    """Represents a vlan to network mapping."""
    __tablename__ = 'csr1kv_ovs_trunk_mappings'

    network_id = sa.Column(sa.String(36),
                           sa.ForeignKey('networks.id',
                                         ondelete="CASCADE"),
                           primary_key=True)
    # trunk_network = orm.relationship(models_v2.Network,
    #                                  primaryjoin='Network.id==TrunkMapping.'
    #                                              'network_id',
    #                                  backref=orm.backref('trunk_mappings'))
    trunked_network_id = sa.Column(sa.String(36),
                                   sa.ForeignKey('networks.id',
                                                 ondelete="CASCADE"),
                                   nullable=False,
                                   primary_key=True)
    vlan_tag = sa.Column(sa.Integer, nullable=False, autoincrement=False)


class TrunkPortNetwork(model_base.BASEV2):
    __tablename__ = 'csr1kv_ovs_trunk_port_networks'

    network_id = sa.Column(sa.String(36),
                           sa.ForeignKey('networks.id',
                                         ondelete="CASCADE"),
                           primary_key=True)
# Bob - End of patch


class CSR1kv_OVSNeutronPluginV2(db_base_plugin_v2.NeutronDbPluginV2,
                                external_net_db.External_net_db_mixin,
                                sg_db_rpc.SecurityGroupServerRpcMixin,
                                agentschedulers_db.DhcpAgentSchedulerDbMixin,
                                portbindings_db.PortBindingMixin,
                                extradhcpopt_db.ExtraDhcpOptMixin,
                                addr_pair_db.AllowedAddressPairsMixin):

    """Implement the Neutron abstractions using Open vSwitch.

    Depending on whether tunneling is enabled, either a GRE, VXLAN tunnel or
    a new VLAN is created for each network. An agent is relied upon to
    perform the actual OVS configuration on each host.

    The provider extension is also supported. As discussed in
    https://bugs.launchpad.net/neutron/+bug/1023156, this class could
    be simplified, and filtering on extended attributes could be
    handled, by adding support for extended attributes to the
    NeutronDbPluginV2 base class. When that occurs, this class should
    be updated to take advantage of it.

    The port binding extension enables an external application relay
    information to and from the plugin.
    """

    # This attribute specifies whether the plugin supports or not
    # bulk/pagination/sorting operations. Name mangling is used in
    # order to ensure it is qualified by class
    __native_bulk_support = True
    __native_pagination_support = True
    __native_sorting_support = True

    _supported_extension_aliases = ["provider", "external-net",
                                    "trunkport", "binding", "quotas",
                                    "security-group", "agent",
                                    "dhcp_agent_scheduler",
                                    "extra_dhcp_opt",
                                    "allowed-address-pairs"]

    @property
    def supported_extension_aliases(self):
        if not hasattr(self, '_aliases'):
            aliases = self._supported_extension_aliases[:]
            sg_rpc.disable_security_group_extension_if_noop_driver(aliases)
            self._aliases = aliases
        return self._aliases

    db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
        attributes.NETWORKS, ['_extend_network_dict_provider_ovs'])

    def __init__(self, configfile=None):
        super(CSR1kv_OVSNeutronPluginV2, self).__init__()
        self.base_binding_dict = {
            portbindings.VIF_TYPE: portbindings.VIF_TYPE_OVS,
            portbindings.VIF_DETAILS: {
                # TODO(rkukura): Replace with new VIF security details
                portbindings.CAP_PORT_FILTER:
                'security-group' in self.supported_extension_aliases}}
        self._parse_network_vlan_ranges()
        ovs_db_v2.sync_vlan_allocations(self.network_vlan_ranges)
        self.tenant_network_type = cfg.CONF.OVS.tenant_network_type
        if self.tenant_network_type not in [svc_constants.TYPE_LOCAL,
                                            svc_constants.TYPE_VLAN,
                                            svc_constants.TYPE_GRE,
                                            svc_constants.TYPE_VXLAN,
                                            svc_constants.TYPE_NONE]:
            LOG.error(_("Invalid tenant_network_type: %s. "
                      "Server terminated!"),
                      self.tenant_network_type)
            sys.exit(1)
        self.enable_tunneling = cfg.CONF.OVS.enable_tunneling
        self.tunnel_type = None
        if self.enable_tunneling:
            self.tunnel_type = (cfg.CONF.OVS.tunnel_type or
                                svc_constants.TYPE_GRE)
        elif cfg.CONF.OVS.tunnel_type:
            self.tunnel_type = cfg.CONF.OVS.tunnel_type
            self.enable_tunneling = True
        self.tunnel_id_ranges = []
        if self.enable_tunneling:
            self._parse_tunnel_id_ranges()
            ovs_db_v2.sync_tunnel_allocations(self.tunnel_id_ranges)
        elif self.tenant_network_type in constants.TUNNEL_NETWORK_TYPES:
            LOG.error(_("Tunneling disabled but tenant_network_type is '%s'. "
                      "Server terminated!"), self.tenant_network_type)
            sys.exit(1)
        self.setup_rpc()
        # Bob: Extension path change
        # If no api_extensions_path is provided set the following
        basepath = neutron.plugins.__path__[0]
        ext_path = (basepath + '/cisco/extensions:' +
                    basepath + '/cisco/l3/extensions:' +
                    basepath + '/csr1kv_openvswitch/extensions')
        cfg.CONF.set_override('api_extensions_path', ext_path)
        #TODO(bobmel): Remove this over-ride of router scheduler default
        #TODO(bobmel): setting and make it part of installer instead.
        self.network_scheduler = importutils.import_object(
            cfg.CONF.network_scheduler_driver
        )

    def setup_rpc(self):
        # RPC support
        self.service_topics = {svc_constants.CORE: topics.PLUGIN}
        self.conn = rpc.create_connection(new=True)
        self.notifier = AgentNotifierApi(topics.AGENT)
        self.agent_notifiers[q_const.AGENT_TYPE_DHCP] = (
            dhcp_rpc_agent_api.DhcpAgentNotifyAPI()
        )
        self.callbacks = CSR1kv_OVSRpcCallbacks(self.notifier, self.tunnel_type,
                                                self)
        self.dispatcher = self.callbacks.create_rpc_dispatcher()
        for svc_topic in self.service_topics.values():
            self.conn.create_consumer(svc_topic, self.dispatcher, fanout=False)
        # Consume from all consumers in a thread
        self.conn.consume_in_thread()

    # Bob - Patch to handle trunk ports
    #
    #  { 'network': { 'trunked_networks': {
    #                        'd22e42a2-4412-a32e-7e2e-56dcfbb243cc': 5,
    #                        'bbe2365c-652e-2bef-62ea-b55ed23a33ac': 6,
    #                        '6243eb53-fe6b-6ae2-cd31-b2c351fcb2de': None
    #                                     }
    #               }
    #  }

    def _process_trunk_create(self, context, net_data, net):
        if trunkport.TRUNKED_NETWORKS not in net_data:
            return
        trunked_networks = net_data[trunkport.TRUNKED_NETWORKS]
        if trunked_networks is None:
            return
        # expects to be called within a plugin's session
        context.session.add(TrunkPortNetwork(network_id=net['id']))
        self._update_trunk_mappings(context, net['id'], trunked_networks)
        net[trunkport.TRUNKED_NETWORKS] = trunked_networks

    def _process_trunk_update(self, context, net_data, net):
        if trunkport.TRUNKED_NETWORKS not in net_data:
            return
        trunked_networks = net_data[trunkport.TRUNKED_NETWORKS]
        is_trunk_network = self._network_hosts_trunk_ports(context, net['id'])
        to_be_trunk_network = trunked_networks is not None

        if is_trunk_network != to_be_trunk_network:
            # The network must not have any ports if its
            # trunking status is going to be changed
            port = context.session.query(models_v2.Port).filter_by(
                network_id=net['id']).first()
            if port:
                raise trunkport.NetworkInUse(network_id=net['id'])

        if to_be_trunk_network:
            if not is_trunk_network:
                context.session.add(TrunkPortNetwork(network_id=net['id']))
            self._update_trunk_mappings(context, net['id'], trunked_networks)
            self._notify_agent_about_trunk_update(context, net['id'],
                                                  trunked_networks)
            net[trunkport.TRUNKED_NETWORKS] = trunked_networks
        elif is_trunk_network:
            context.session.query(TrunkPortNetwork).filter_by(
                network_id=net['id']).delete()
            self._update_trunk_mappings(context, net['id'], {})
            self._notify_agent_about_trunk_update(context, net['id'], None)
            net[trunkport.TRUNKED_NETWORKS] = None

    def _update_trunk_mappings(self, context, network_id, trunk_mappings):
        networks_to_trunk = set(trunk_mappings)
        current_mappings = self._get_trunk_mappings_by_network_id(context,
                                                                  network_id)
        trunked_networks = set(current_mappings)
        trunks_to_modify = {k for k, v in trunk_mappings.iteritems() if
                            k in current_mappings and v != current_mappings[k]}

        with context.session.begin(subtransactions=True):
            trunks_to_add = networks_to_trunk - trunked_networks
            trunks_to_remove = trunked_networks - networks_to_trunk
            used_vlans = set(current_mappings[n] for n in (trunked_networks -
                                                           trunks_to_remove))

            LOG.debug(_('Networks trunk to be modified %s'), trunks_to_modify)
            LOG.debug(_('Networks to be added to trunk %s'), trunks_to_add)
            auto_vlan_pool = [x for x in xrange(MIN_VLAN, MAX_VLAN)
                              if x not in used_vlans]
            for net_id in trunks_to_add | trunks_to_modify:
                # Fetch network information here to always catch invalid
                # (= non-existent) networks
                netdb = super(CSR1kv_OVSNeutronPluginV2, self)._get_network(
                    context, net_id)
                # We need to fetch provider network information separately
                # since parent's get_network() method is used above
                net = {}
                self._extend_network_dict_provider_ovs(net, netdb)
                if trunk_mappings[net_id] in used_vlans:
                    # Cannot trunk several networks using same VLAN tag
                    raise trunkport.VlanAlreadyUsedinTrunk(
                        vlan_tag=trunk_mappings[net_id])
                pn_type = net.get(provider.NETWORK_TYPE, '')
                if pn_type == svc_constants.TYPE_VLAN:
                    # VLAN tag of provider network always overrides requested
                    trunk_mappings[net_id] = net.get(provider.SEGMENTATION_ID,
                                                     4094)
                if trunk_mappings[net_id] is None:
                    trunk_mappings[net_id] = auto_vlan_pool[0]
                used_vlans.add(trunk_mappings[net_id])
                if trunk_mappings[net_id] in auto_vlan_pool:
                    auto_vlan_pool.remove(trunk_mappings[net_id])
                if net_id in trunks_to_modify:
                    mapping = self._get_trunk_mapping(context, network_id,
                                                      net_id)
                    if mapping is None:
                        continue
                    mapping.vlan_tag = trunk_mappings[net_id]
                else:
                    mapping = TrunkMapping(
                        network_id=network_id,
                        trunked_network_id=net_id,
                        vlan_tag=trunk_mappings[net_id])
                context.session.add(mapping)

            LOG.debug(_('Networks to be removed from trunk %s'),
                      trunks_to_remove)
            for net_id in trunks_to_remove:
                del_context = context.session.query(TrunkMapping)
                del_context.filter_by(network_id=network_id,
                                      trunked_network_id=net_id).delete()
        return trunk_mappings

    def _notify_agent_about_trunk_update(self, context, net_id,
                                         trunked_networks):
        """Notifies agent about trunk mapping change on trunk network."""
        filters = {'network_id': [net_id]}
        ports = self.get_ports(context, filters)

        binding = ovs_db_v2.get_network_binding(None, net_id)
        for port in ports:
            port[trunkport.TRUNKED_NETWORKS] = (
                self._make_extended_trunk_mapping_dict(trunked_networks))
            self.notifier.port_update(context, port,
                                      binding.network_type,
                                      binding.segmentation_id,
                                      binding.physical_network)

    def _make_trunk_mapping_dict_from_db(self, trunk_mappings_db):
        return dict((mapping['trunked_network_id'], mapping['vlan_tag'])
            for mapping in trunk_mappings_db)

    def _make_extended_trunk_mapping_dict_from_db(self, trunk_mappings_db):
        res = {}
        for mapping in trunk_mappings_db:
            binding = ovs_db_v2.get_network_binding(
                None, mapping['trunked_network_id'])
            res[mapping['trunked_network_id']] = {
                'vlan': mapping['vlan_tag'],
                'network_type': binding.network_type,
                'segmentation_id': binding.segmentation_id,
                'physical_network': binding.physical_network}
        return res

    def _make_extended_trunk_mapping_dict(self, trunk_mappings):
        res = {}
        for network_id, vlan_tag in trunk_mappings.iteritems():
            binding = ovs_db_v2.get_network_binding(None, network_id)
            res[network_id] = {
                'vlan': vlan_tag,
                'network_type': binding.network_type,
                'segmentation_id': binding.segmentation_id,
                'physical_network': binding.physical_network}
        return res

    def extend_resource_dict_trunks(self, context, resource, network_id,
                                    extended_info=False):
        if self._network_hosts_trunk_ports(context, network_id):
            resource[trunkport.TRUNKED_NETWORKS] = (
                self._get_trunk_mappings_by_network_id(context, network_id,
                                                       extended_info))
        else:
            resource[trunkport.TRUNKED_NETWORKS] =  None

    def _network_hosts_trunk_ports(self, context, net_id):
        try:
            context.session.query(TrunkPortNetwork).filter_by(
                network_id=net_id).one()
            return True
        except exc.NoResultFound:
            return False

    def _get_trunk_mappings_by_network_id(self, context, network_id,
                                          extended_info=False):
        trunk_mappings_db = context.session.query(TrunkMapping).filter_by(
            network_id=network_id).all()
        if extended_info:
            return self._make_extended_trunk_mapping_dict_from_db(
                trunk_mappings_db)
        else:
            return self._make_trunk_mapping_dict_from_db(trunk_mappings_db)

    def _get_trunk_mapping(self, context, network_id, trunked_network_id):
        query = context.session.query(TrunkMapping).filter(
            TrunkMapping.network_id == network_id,
            TrunkMapping.trunked_network_id == trunked_network_id)
        try:
            return query.one()
        except exc.NoResultFound, exc.MultipleResultsFound:
            return
    # Bob - End of patch

    def _parse_network_vlan_ranges(self):
        try:
            self.network_vlan_ranges = plugin_utils.parse_network_vlan_ranges(
                cfg.CONF.OVS.network_vlan_ranges)
        except Exception as ex:
            LOG.error(_("%s. Server terminated!"), ex)
            sys.exit(1)
        LOG.info(_("Network VLAN ranges: %s"), self.network_vlan_ranges)

    def _parse_tunnel_id_ranges(self):
        for entry in cfg.CONF.OVS.tunnel_id_ranges:
            entry = entry.strip()
            try:
                tun_min, tun_max = entry.split(':')
                self.tunnel_id_ranges.append((int(tun_min), int(tun_max)))
            except ValueError as ex:
                LOG.error(_("Invalid tunnel ID range: "
                            "'%(range)s' - %(e)s. Server terminated!"),
                          {'range': entry, 'e': ex})
                sys.exit(1)
        LOG.info(_("Tunnel ID ranges: %s"), self.tunnel_id_ranges)

    def _extend_network_dict_provider_ovs(self, network, net_db,
                                          net_binding=None):
        # this method used in two cases: when binding is provided explicitly
        # and when it is a part of db model object
        binding = net_db.binding if net_db else net_binding
        network[provider.NETWORK_TYPE] = binding.network_type
        if binding.network_type in constants.TUNNEL_NETWORK_TYPES:
            network[provider.PHYSICAL_NETWORK] = None
            network[provider.SEGMENTATION_ID] = binding.segmentation_id
        elif binding.network_type == svc_constants.TYPE_FLAT:
            network[provider.PHYSICAL_NETWORK] = binding.physical_network
            network[provider.SEGMENTATION_ID] = None
        elif binding.network_type == svc_constants.TYPE_VLAN:
            network[provider.PHYSICAL_NETWORK] = binding.physical_network
            network[provider.SEGMENTATION_ID] = binding.segmentation_id
        elif binding.network_type == svc_constants.TYPE_LOCAL:
            network[provider.PHYSICAL_NETWORK] = None
            network[provider.SEGMENTATION_ID] = None

    def _process_provider_create(self, context, attrs):
        network_type = attrs.get(provider.NETWORK_TYPE)
        physical_network = attrs.get(provider.PHYSICAL_NETWORK)
        segmentation_id = attrs.get(provider.SEGMENTATION_ID)

        network_type_set = attributes.is_attr_set(network_type)
        physical_network_set = attributes.is_attr_set(physical_network)
        segmentation_id_set = attributes.is_attr_set(segmentation_id)

        if not (network_type_set or physical_network_set or
                segmentation_id_set):
            return (None, None, None)

        if not network_type_set:
            msg = _("provider:network_type required")
            raise n_exc.InvalidInput(error_message=msg)
        elif network_type == svc_constants.TYPE_FLAT:
            if segmentation_id_set:
                msg = _("provider:segmentation_id specified for flat network")
                raise n_exc.InvalidInput(error_message=msg)
            else:
                segmentation_id = constants.FLAT_VLAN_ID
        elif network_type == svc_constants.TYPE_VLAN:
            if not segmentation_id_set:
                msg = _("provider:segmentation_id required")
                raise n_exc.InvalidInput(error_message=msg)
            if not utils.is_valid_vlan_tag(segmentation_id):
                msg = (_("provider:segmentation_id out of range "
                         "(%(min_id)s through %(max_id)s)") %
                       {'min_id': q_const.MIN_VLAN_TAG,
                        'max_id': q_const.MAX_VLAN_TAG})
                raise n_exc.InvalidInput(error_message=msg)
        elif network_type in constants.TUNNEL_NETWORK_TYPES:
            if not self.enable_tunneling:
                msg = _("%s networks are not enabled") % network_type
                raise n_exc.InvalidInput(error_message=msg)
            if physical_network_set:
                msg = _("provider:physical_network specified for %s "
                        "network") % network_type
                raise n_exc.InvalidInput(error_message=msg)
            else:
                physical_network = None
            if not segmentation_id_set:
                msg = _("provider:segmentation_id required")
                raise n_exc.InvalidInput(error_message=msg)
        elif network_type == svc_constants.TYPE_LOCAL:
            if physical_network_set:
                msg = _("provider:physical_network specified for local "
                        "network")
                raise n_exc.InvalidInput(error_message=msg)
            else:
                physical_network = None
            if segmentation_id_set:
                msg = _("provider:segmentation_id specified for local "
                        "network")
                raise n_exc.InvalidInput(error_message=msg)
            else:
                segmentation_id = None
        else:
            msg = _("provider:network_type %s not supported") % network_type
            raise n_exc.InvalidInput(error_message=msg)

        if network_type in [svc_constants.TYPE_VLAN, svc_constants.TYPE_FLAT]:
            if physical_network_set:
                if physical_network not in self.network_vlan_ranges:
                    msg = _("Unknown provider:physical_network "
                            "%s") % physical_network
                    raise n_exc.InvalidInput(error_message=msg)
            elif 'default' in self.network_vlan_ranges:
                physical_network = 'default'
            else:
                msg = _("provider:physical_network required")
                raise n_exc.InvalidInput(error_message=msg)

        return (network_type, physical_network, segmentation_id)

    def create_network(self, context, network):
        (network_type, physical_network,
         segmentation_id) = self._process_provider_create(context,
                                                          network['network'])

        session = context.session
        #set up default security groups
        tenant_id = self._get_tenant_id_for_create(
            context, network['network'])
        self._ensure_default_security_group(context, tenant_id)

        with session.begin(subtransactions=True):
            if not network_type:
                # tenant network
                network_type = self.tenant_network_type
                if network_type == svc_constants.TYPE_NONE:
                    raise n_exc.TenantNetworksDisabled()
                elif network_type == svc_constants.TYPE_VLAN:
                    (physical_network,
                     segmentation_id) = ovs_db_v2.reserve_vlan(session)
                elif network_type in constants.TUNNEL_NETWORK_TYPES:
                    segmentation_id = ovs_db_v2.reserve_tunnel(session)
                # no reservation needed for TYPE_LOCAL
            else:
                # provider network
                if network_type in [svc_constants.TYPE_VLAN,
                                    svc_constants.TYPE_FLAT]:
                    ovs_db_v2.reserve_specific_vlan(session, physical_network,
                                                    segmentation_id)
                elif network_type in constants.TUNNEL_NETWORK_TYPES:
                    ovs_db_v2.reserve_specific_tunnel(session, segmentation_id)
                # no reservation needed for TYPE_LOCAL
            net = super(CSR1kv_OVSNeutronPluginV2, self).create_network(context,
                                                                 network)
            binding = ovs_db_v2.add_network_binding(session, net['id'],
                                                    network_type,
                                                    physical_network,
                                                    segmentation_id)

            self._process_l3_create(context, net, network['network'])
            # passing None as db model to use binding object
            self._extend_network_dict_provider_ovs(net, None, binding)
            # Bob - Patch to handle creation of network that trunk
            # ports belong to
            self._process_trunk_create(context, network['network'], net)
            # Bob - End of patch
            # note - exception will rollback entire transaction
        LOG.debug(_("Created network: %s"), net['id'])
        return net

    def update_network(self, context, id, network):
        provider._raise_if_updates_provider_attributes(network['network'])

        session = context.session
        with session.begin(subtransactions=True):
            net = super(CSR1kv_OVSNeutronPluginV2, self).update_network(
                context, id, network)
            self._process_l3_update(context, net, network['network'])
            # Bob - Patch to handle update of network that trunk
            # ports belong to
            self._process_trunk_update(context, network['network'], net)
            # Bob - End of patch
        return net

    def delete_network(self, context, id):
        session = context.session
        with session.begin(subtransactions=True):
            binding = ovs_db_v2.get_network_binding(session, id)
            super(CSR1kv_OVSNeutronPluginV2, self).delete_network(context, id)
            if binding.network_type in constants.TUNNEL_NETWORK_TYPES:
                ovs_db_v2.release_tunnel(session, binding.segmentation_id,
                                         self.tunnel_id_ranges)
            elif binding.network_type in [svc_constants.TYPE_VLAN,
                                          svc_constants.TYPE_FLAT]:
                ovs_db_v2.release_vlan(session, binding.physical_network,
                                       binding.segmentation_id,
                                       self.network_vlan_ranges)
            # the network_binding record is deleted via cascade from
            # the network record, so explicit removal is not necessary
        self.notifier.network_delete(context, id)

    def get_network(self, context, id, fields=None):
        session = context.session
        with session.begin(subtransactions=True):
            net = super(CSR1kv_OVSNeutronPluginV2, self).get_network(context,
                                                                     id, None)
            # Bob - Patch to add information to networks that trunk
            # ports belong to
            self.extend_resource_dict_trunks(context, net, net['id'])
            # Bob - End of patch

        return self._fields(net, fields)

    def get_networks(self, context, filters=None, fields=None,
                     sorts=None,
                     limit=None, marker=None, page_reverse=False):
        session = context.session
        with session.begin(subtransactions=True):
            nets = super(CSR1kv_OVSNeutronPluginV2,
                         self).get_networks(context, filters, None, sorts,
                                            limit, marker, page_reverse)
            # Bob - Patch to add information to networks that trunk
            # ports belong to
            for net in nets:
                self.extend_resource_dict_trunks(context, net, net['id'])
            # Bob - End of patch

        return [self._fields(net, fields) for net in nets]

    def create_port(self, context, port):
        # Set port status as 'DOWN'. This will be updated by agent
        port['port']['status'] = q_const.PORT_STATUS_DOWN
        port_data = port['port']
        session = context.session
        with session.begin(subtransactions=True):
            self._ensure_default_security_group_on_port(context, port)
            sgids = self._get_security_groups_on_port(context, port)
            dhcp_opts = port['port'].get(edo_ext.EXTRADHCPOPTS, [])
            port = super(CSR1kv_OVSNeutronPluginV2, self).create_port(context, port)
            self._process_portbindings_create_and_update(context,
                                                         port_data, port)
            self._process_port_create_security_group(context, port, sgids)
            self._process_port_create_extra_dhcp_opts(context, port,
                                                      dhcp_opts)
            port[addr_pair.ADDRESS_PAIRS] = (
                self._process_create_allowed_address_pairs(
                    context, port,
                    port_data.get(addr_pair.ADDRESS_PAIRS)))
        self.notify_security_groups_member_updated(context, port)
        return port

    def update_port(self, context, id, port):
        session = context.session
        need_port_update_notify = False
        changed_fixed_ips = 'fixed_ips' in port['port']
        with session.begin(subtransactions=True):
            original_port = super(CSR1kv_OVSNeutronPluginV2, self).get_port(
                context, id)
            updated_port = super(CSR1kv_OVSNeutronPluginV2, self).update_port(
                context, id, port)
            if addr_pair.ADDRESS_PAIRS in port['port']:
                need_port_update_notify |= (
                    self.update_address_pairs_on_port(context, id, port,
                                                      original_port,
                                                      updated_port))
            elif changed_fixed_ips:
                self._check_fixed_ips_and_address_pairs_no_overlap(
                    context, updated_port)
            need_port_update_notify |= self.update_security_group_on_port(
                context, id, port, original_port, updated_port)
            self._process_portbindings_create_and_update(context,
                                                         port['port'],
                                                         updated_port)
            need_port_update_notify |= self._update_extra_dhcp_opts_on_port(
                context, id, port, updated_port)

        need_port_update_notify |= self.is_security_group_member_updated(
            context, original_port, updated_port)
        if original_port['admin_state_up'] != updated_port['admin_state_up']:
            need_port_update_notify = True

        if need_port_update_notify:
            binding = ovs_db_v2.get_network_binding(None,
                                                    updated_port['network_id'])
            # Bob - Patch to add information about trunk mappings
            self.extend_resource_dict_trunks(context, updated_port,
                                             updated_port['network_id'],
                                             extended_info=True)
             # Bob - End of patch
            self.notifier.port_update(context, updated_port,
                                      binding.network_type,
                                      binding.segmentation_id,
                                      binding.physical_network)
            # Bob - Patch to add information about trunk mappings
            updated_port.pop(trunkport.TRUNKED_NETWORKS)
             # Bob - End of patch
        return updated_port

    def delete_port(self, context, id, l3_port_check=True):

        # if needed, check to see if this is a port owned by
        # and l3-router.  If so, we should prevent deletion.
        if l3_port_check:
            self.prevent_l3_port_deletion(context, id)

        session = context.session
        with session.begin(subtransactions=True):
            self.disassociate_floatingips(context, id)
            port = self.get_port(context, id)
            self._delete_port_security_group_bindings(context, id)
            super(CSR1kv_OVSNeutronPluginV2, self).delete_port(context, id)

        self.notify_security_groups_member_updated(context, port)
