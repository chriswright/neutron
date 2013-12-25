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

from neutron.common import utils
from neutron import manager
from neutron.openstack.common import log as logging
from neutron.openstack.common.rpc import proxy
from neutron.plugins.cisco.common import cisco_constants as c_constants
from neutron.plugins.cisco.extensions import ciscocfgagentscheduler
from neutron.plugins.common import constants as service_constants

LOG = logging.getLogger(__name__)


CFGAGENT_SCHED = ciscocfgagentscheduler.CFG_AGENT_SCHEDULER_ALIAS


class DeviceMgrCfgAgentNotifyAPI(proxy.RpcProxy):
    """API for Device manager service plugin to notify Cisco cfg agent."""
    BASE_RPC_API_VERSION = '1.0'

    def __init__(self, topic=c_constants.CFG_AGENT):
        super(DeviceMgrCfgAgentNotifyAPI, self).__init__(
            topic=topic, default_version=self.BASE_RPC_API_VERSION)

    def _notification_host(self, context, method, payload, host,
                           topic=None):
        """Notify the agent that is handling the hosting device."""

        LOG.debug(_('Notify Cisco cfg agent at %(host)s the message '
                    '%(method)s'), {'host': host, 'method': method})
        self.cast(context,
                  self.make_msg(method, payload=payload),
                  topic='%s.%s' % (self.topic if topic is None else topic,
                                   host))

    def _agent_notification(self, context, method, hosting_devices,
                            operation, data):
        """Notify individual Cisco cfg agents."""
        adminContext = context.is_admin and context or context.elevated()
        dmplugin = manager.NeutronManager.get_service_plugins().get(
            service_constants.DEVICE_MANAGER)
        for hosting_device in hosting_devices:
            if (utils.is_extension_supported(dmplugin, CFGAGENT_SCHED)):
                agents = dmplugin.get_cfg_agents_for_hosting_devices(
                    adminContext, hosting_device['id'], admin_state_up=True,
                    active=True, schedule=True)
            else:
                agents = []
            for agent in agents:
                LOG.debug(_('Notify Cisco cfg agent at %(topic)s.%(host)s '
                            'the message %(method)s'),
                          {'topic': agent.topic,
                           'host': agent.host,
                           'method': method})
                self.cast(context,
                          self.make_msg(method),
                          topic='%s.%s' % (agent.topic, agent.host),
                          version='1.0')

    # def _notification_fanout(self, context, method, router_id):
    #     """Fanout the deleted router to all L3 agents."""
    #     LOG.debug(_('Fanout notify agent at %(topic)s the message '
    #                 '%(method)s on router %(router_id)s'),
    #               {'topic': topics.DHCP_AGENT,
    #                'method': method,
    #                'router_id': router_id})
    #     self.fanout_cast(context,
    #                      self.make_msg(method, router_id=router_id),
    #                      topic=topics.L3_AGENT)

    def agent_updated(self, context, admin_state_up, host):
        """Updates cfg agent on <host> to enable or disable it."""
        self._notification_host(context, 'agent_updated',
                                {'admin_state_up': admin_state_up}, host)

    def hosting_devices_unassigned_from_cfg_agent(self, context, ids, host):
        """Notify cfg agent to no longer handle some hosting devices.

        This notification relieves the cfg agent in <host> of responsibility
        to monitor and configure hosting devices with id specified in <ids>.
        """
        self._notification_host(context, 'devices_unassigned_from_cfg_agent',
                                {'hosting_device_ids': ids}, host)

    def hosting_devices_assigned_to_cfg_agent(self, context, ids, host):
        """Notify cfg agent to now handle some hosting devices.

        This notification relieves the cfg agent in <host> of responsibility
        to monitor and configure hosting devices with id specified in <ids>.
        """
        self._notification_host(context, 'devices_assigned_to_cfg_agent',
                                {'hosting_device_ids': ids}, host)

    def hosting_devices_removed(self, context, hosting_data, deconfigure,
                                host):
        """Notify cfg agent that some hosting devices have been removed.

        This notification informs the cfg agent in <host> that the
        hosting devices in the <hosting_data> dictionary have been removed
        from the hosting device pool. The <hosting_data> dictionary also
        contains the ids of the affected logical resources for each hosting
        devices:
             {'hd_id1': {'routers': [id1, id2, ...],
                         'fw': [id1, ...],
                         ...},
              'hd_id2': {'routers': [id3, id4, ...]},
                         'fw': [id1, ...],
                         ...},
              ...}
        The <deconfigure> argument is True if any configurations for the
        logical resources should be removed from the hosting devices
        """
        if hosting_data:
            self._notification_host(context, 'hosting_device_removed',
                                    {'hosting_data': hosting_data,
                                     'deconfigure': deconfigure}, host)


DeviceMgrCfgAgentNotify = DeviceMgrCfgAgentNotifyAPI()
