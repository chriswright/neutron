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

from sqlalchemy.orm import exc
from sqlalchemy.sql import exists
from sqlalchemy.sql import expression as expr

from neutron.common import constants
from neutron.common import exceptions as n_exc
from neutron.db import agents_db
from neutron.db import l3_agentschedulers_db
from neutron.db import l3_db
from neutron.openstack.common import log as logging
from neutron.plugins.cisco.db.l3 import l3_models
from neutron.scheduler import l3_agent_scheduler

LOG = logging.getLogger(__name__)


AGENT_TYPE_L3 = constants.AGENT_TYPE_L3


class L3RouterTypeAwareScheduler(l3_agent_scheduler.L3Scheduler):
    """A router type aware l3 agent scheduler for Cisco router service plugin.

    It schedules Neutron routers with router type representing network
    namespace based routers to l3 agents.
    """

    def auto_schedule_routers(self, plugin, context, host, router_ids):
        """Schedule non-hosted network namespace-based routers to L3 agent.

        If router_ids is given, each router in router_ids is scheduled
        if it is not hosted yet. Otherwise all unscheduled routers
        are scheduled. Don't schedule the routers which are hosted
        already by active l3 agents.
        """
        with context.session.begin(subtransactions=True):
            # query if we have valid l3 agent on the host
            query = context.session.query(agents_db.Agent)
            query = query.filter(agents_db.Agent.agent_type == AGENT_TYPE_L3,
                                 agents_db.Agent.host == host,
                                 agents_db.Agent.admin_state_up == expr.true())
            try:
                l3_agent = query.one()
            except (exc.MultipleResultsFound, exc.NoResultFound):
                LOG.debug(_('No enabled L3 agent on host %s'),
                          host)
                return False
            if agents_db.AgentDbMixin.is_agent_down(
                    l3_agent.heartbeat_timestamp):
                LOG.warn(_('L3 agent %s is not active'), l3_agent.id)
            # Only network namespace based routers should be scheduled here
            ns_routertype_id = plugin.get_namespace_router_type_id(context)
            # check if each of the specified routers is hosted
            if router_ids:
                unscheduled_router_ids = []
                for router_id in router_ids:
                    try:
                        router_type_id = plugin.get_router_type_id(
                            context, router_id)
                    except n_exc.NeutronException:
                        router_type_id = None
                    if router_type_id != ns_routertype_id:
                        LOG.debug(_('Router %(r_id)s is of type %(t_id)s '
                                    'which is not hosted by l3 agents'),
                                  {'r_id': router_id, 't_id': router_type_id})
                    else:
                        l3_agents = plugin.get_l3_agents_hosting_routers(
                            context, [router_id], admin_state_up=True)
                        if l3_agents:
                            LOG.debug(_('Router %(router_id)s has already been'
                                        ' hosted by L3 agent %(agent_id)s'),
                                      {'router_id': router_id,
                                       'agent_id': l3_agents[0]['id']})
                        else:
                            unscheduled_router_ids.append(router_id)
                if not unscheduled_router_ids:
                    # all (specified) routers are already scheduled
                    return False
            else:
                # get all routers that are not hosted
                #TODO(gongysh) consider the disabled agent's router
                stmt = ~exists().where(
                    l3_db.Router.id ==
                    l3_agentschedulers_db.RouterL3AgentBinding.router_id)
                # Modified to only include routers of network namespace type
                query = context.session.query(l3_db.Router.id)
                query = query.join(l3_models.RouterHostingDeviceBinding)
                query = query.filter(
                    l3_models.RouterHostingDeviceBinding.router_type_id ==
                    ns_routertype_id, stmt)
                unscheduled_router_ids = [router_id_[0] for router_id_ in
                                          query]
            if not unscheduled_router_ids:
                LOG.debug(_('No non-hosted routers'))
                return False

            # check if the configuration of l3 agent is compatible
            # with the router
            routers = plugin.get_routers(
                context, filters={'id': unscheduled_router_ids})
            to_removed_ids = []
            for router in routers:
                candidates = plugin.get_l3_agent_candidates(router, [l3_agent])
                if not candidates:
                    to_removed_ids.append(router['id'])
            router_ids = set([r['id'] for r in routers]) - set(to_removed_ids)
            if not router_ids:
                LOG.warn(_('No routers compatible with L3 agent configuration'
                           ' on host %s'), host)
                return False

            for router_id in router_ids:
                self.bind_router(context, router_id, l3_agent)
        return True

    def schedule(self, plugin, context, router):
        # Only network namespace based routers should be scheduled here
        ns_routertype_id = plugin.get_namespace_router_type_id(context)
        if router['router_type']['id'] == ns_routertype_id:
            # Do the traditional Neutron router scheduling
            return plugin.l3agent_scheduler.schedule(plugin, context,
                                                     router['id'])
        else:
            return
