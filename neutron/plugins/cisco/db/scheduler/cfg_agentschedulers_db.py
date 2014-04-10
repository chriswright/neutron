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

from neutron.db import agents_db
from neutron.db import agentschedulers_db
from neutron.openstack.common import log as logging
from neutron.openstack.common import timeutils
from neutron.plugins.cisco.common import cisco_constants as c_constants
from neutron.plugins.cisco.extensions import ciscocfgagentscheduler

LOG = logging.getLogger(__name__)


COMPOSITE_AGENTS_SCHEDULER_OPTS = [
    cfg.IntOpt('cfg_agent_down_time', default=15,
               help=_('Seconds of no status update until a cfg agent '
                      'is considered down.')),
    cfg.StrOpt('configuration_agent_scheduler_driver',
               default='neutron.plugins.cisco.device_manager.scheduler.'
                       'hosting_device_cfg_agent_scheduler.'
                       'HostingDeviceCfgAgentScheduler',
               help=_('Driver to use for scheduling hosting device to a Cisco '
                      'configuration agent')),
]

cfg.CONF.register_opts(COMPOSITE_AGENTS_SCHEDULER_OPTS)


class CfgAgentSchedulerDbMixin(
        ciscocfgagentscheduler.CfgAgentSchedulerPluginBase,
        agentschedulers_db.AgentSchedulerDbMixin):
    """Mixin class to add cfg agent scheduler extension."""

    cfg_agent_scheduler = None

    @classmethod
    def is_agent_down(cls, heart_beat_time,
                      timeout=cfg.CONF.cfg_agent_down_time):
        return timeutils.is_older_than(heart_beat_time, timeout)

    def auto_schedule_hosting_devices(self, context, host):
        if self.cfg_agent_scheduler:
            return self.cfg_agent_scheduler.auto_schedule_hosting_devices(
                self, context, host)
        return

    def assign_hosting_device_to_cfg_agent(self, context, id,
                                           hosting_device_id):
        #TODO(bobmel): Implement the assign hd to cfg agent
        pass

    def unassign_hosting_device_from_cfg_agent(self, context, id,
                                               hosting_device_id):
        #TODO(bobmel): Implement the un-assign hd from cfg agent
        pass

    def list_hosting_devices_handled_by_cfg_agent(self, context, id):
        #TODO(bobmel): Change so it returns correct hosting devices
        return {'hosting_devices': []}

    def list_cfg_agents_handling_hosting_device(self, context,
                                                hosting_device_id):
        #TODO(bobmel): Change so it returns correct agent
        return {'cfg_agents': []}

    def get_cfg_agents(self, context, active=None, filters=None):
        query = context.session.query(agents_db.Agent)
        query = query.filter(
            agents_db.Agent.agent_type == c_constants.AGENT_TYPE_CFG)
        if active is not None:
            query = (query.filter(agents_db.Agent.admin_state_up == active))
        if filters:
            for key, value in filters.iteritems():
                column = getattr(agents_db.Agent, key, None)
                if column:
                    query = query.filter(column.in_(value))
        cfg_agents = query.all()
        if active is not None:
            cfg_agents = [cfg_agent for cfg_agent in cfg_agents
                          if not self.is_agent_down(
                              cfg_agent['heartbeat_timestamp'])]
        return cfg_agents

    def get_cfg_agents_for_hosting_devices(self, context, hosting_device_ids,
                                           admin_state_up=None, active=None,
                                           schedule=False):
        if not hosting_device_ids:
            return []
        query = self.get_hosting_devices_qry(context, hosting_device_ids)
        if admin_state_up is not None:
            query = query.filter(
                agents_db.Agent.admin_state_up == admin_state_up)
        if schedule:
            agents = []
            for hosting_device in query:
                if hosting_device.cfg_agent is None:
                    agent = self.cfg_agent_scheduler.schedule_hosting_device(
                        self, context, hosting_device)
                    if agent is not None:
                        agents.append(agent)
        else:
            agents = [hosting_device.cfg_agent for hosting_device in query
                      if hosting_device.cfg_agent is not None]
        if active is not None:
            agents = [agent for agent in agents if not
                      self.is_agent_down(agent['heartbeat_timestamp'])]
        return agents
