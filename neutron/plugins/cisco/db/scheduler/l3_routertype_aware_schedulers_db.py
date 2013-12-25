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

from neutron.db import l3_agentschedulers_db as l3agentsched_db
from neutron.openstack.common import log as logging
from neutron.plugins.cisco.common import cisco_constants as c_constants
from neutron.plugins.cisco.db.device_manager.hd_models import HostingDevice
from neutron.plugins.cisco.db.l3.l3_models import RouterHostingDeviceBinding

LOG = logging.getLogger(__name__)


ROUTER_TYPE_AWARE_SCHEDULER_OPTS = [
    cfg.StrOpt('router_type_aware_scheduler_driver',
               default='neutron.plugins.cisco.l3.scheduler.'
                       'l3_routertype_aware_agent_scheduler.'
                       'L3RouterTypeAwareScheduler',
               help=_('Driver to use for router type-aware scheduling of '
                      'router to a default L3 agent')),
]

cfg.CONF.register_opts(ROUTER_TYPE_AWARE_SCHEDULER_OPTS)


class L3RouterTypeAwareSchedulerDbMixin(
        l3agentsched_db.L3AgentSchedulerDbMixin):
    """Mixin class to add L3 router type-aware scheduler capability.

    This class can schedule Neutron routers to hosting devices
    and to L3 agents on network nodes.
    """

    def list_active_sync_routers_on_hosting_devices(self, context, host,
                                                    router_id,
                                                    hosting_device_ids=None):
        if hosting_device_ids is None:
            hosting_device_ids = []
        agent = self._get_agent_by_type_and_host(
            context, c_constants.AGENT_TYPE_CFG, host)
        if not agent.admin_state_up:
            return []
        query = context.session.query(RouterHostingDeviceBinding.router_id)
        query = query.join(HostingDevice)
        query = query.filter(HostingDevice.cfg_agent_id == agent.id)
        if router_id:
            query = query.filter(
                RouterHostingDeviceBinding.router_id == router_id)
        if len(hosting_device_ids) == 1:
            query = query.filter(
                RouterHostingDeviceBinding.hosting_device_id ==
                hosting_device_ids[0])
        elif len(hosting_device_ids) > 1:
            query = query.filter(
                RouterHostingDeviceBinding.hosting_device_id.in_(
                    hosting_device_ids))
        router_ids = [item[0] for item in query]
        if router_ids:
            return self.get_sync_data_ext(context, router_ids=router_ids,
                                          active=True)
        else:
            return []
