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

from neutron.common import constants as q_const
from neutron.common import rpc as q_rpc
from neutron.common import topics
from neutron.db import api as qdbapi
from neutron.db import db_base_plugin_v2
#from neutron.db import l3_gwmode_db
from neutron.db import l3_rpc_base
from neutron.db import model_base
from neutron.openstack.common import importutils
from neutron.openstack.common import rpc
import neutron.plugins
from neutron.plugins.cisco.common import cisco_constants as c_constants
from neutron.plugins.cisco.db.l3 import l3_router_appliance_db
from neutron.plugins.cisco.db.scheduler import (
    l3_routertype_aware_schedulers_db as router_sch_db)
from neutron.plugins.cisco.extensions import routertype
from neutron.plugins.cisco.l3.rpc import (l3_router_cfgagent_rpc_cb as
                                          l3_router_rpc)
from neutron.plugins.cisco.l3.rpc import l3_router_rpc_joint_agent_api
from neutron.plugins.cisco.l3.rpc import l3_rpc_agent_api_noop
from neutron.plugins.common import constants


class CiscoRouterPluginRpcCallbacks(l3_rpc_base.L3RpcCallbackMixin,
                                    l3_router_rpc.L3RouterCfgRpcCallbackMixin):
    # Set RPC API version to 1.0 by default.
    RPC_API_VERSION = '1.0'

    def create_rpc_dispatcher(self):
        """Get the rpc dispatcher for this manager.

        If a manager would like to set an rpc API version, or support more than
        one class as the target of rpc messages, override this method.
        """
        return q_rpc.PluginRpcDispatcher([self])


class CiscoRouterPlugin(db_base_plugin_v2.CommonDbMixin,
                        l3_router_appliance_db.L3RouterApplianceDBMixin,
                        #l3_gwmode_db.L3_NAT_db_mixin,
                        router_sch_db.L3RouterTypeAwareSchedulerDbMixin):

    """Implementation of Cisco L3 Router Service Plugin for Neutron.

    This class implements a L3 service plugin that provides
    router and floatingip resources and manages associated
    request/response.
    All DB functionality is implemented in class
    l3_router_appliance_db.L3RouterApplianceDBMixin.
    """
    supported_extension_aliases = ["router",  # "ext-gw-mode",
                                   "extraroute", "l3_agent_scheduler",
                                   routertype.ROUTERTYPE_ALIAS]

    def __init__(self):
        qdbapi.register_models(base=model_base.BASEV2)
        self.setup_rpc()
        basepath = neutron.plugins.__path__[0]
        ext_paths = [basepath + '/cisco/extensions',
                     basepath + '/cisco/l3/extensions']
        cp = cfg.CONF.api_extensions_path
        to_add = ""
        for ext_path in ext_paths:
            if cp.find(ext_path) == -1:
                to_add += ':' + ext_path
        if to_add != "":
            cfg.CONF.set_override('api_extensions_path', cp + to_add)
        self.router_scheduler = importutils.import_object(
            cfg.CONF.router_type_aware_scheduler_driver)
        self.l3agent_scheduler = importutils.import_object(
            cfg.CONF.router_scheduler_driver)
        # for backlogging of non-scheduled routers
        self._setup_backlog_handling()

    def setup_rpc(self):
        # RPC support
        self.topic = topics.L3PLUGIN
        self.conn = rpc.create_connection(new=True)
        self.agent_notifiers.update(
            {q_const.AGENT_TYPE_L3:
             l3_router_rpc_joint_agent_api.L3JointAgentNotify,
             c_constants.AGENT_TYPE_CFG:
             l3_router_rpc_joint_agent_api.L3JointAgentNotify})
        # Disable notifications from l3 base class to l3 agents
        self.l3_rpc_notifier = l3_rpc_agent_api_noop.L3AgentNotifyNoOp
        self.callbacks = CiscoRouterPluginRpcCallbacks()
        self.dispatcher = self.callbacks.create_rpc_dispatcher()
        self.conn.create_consumer(self.topic, self.dispatcher,
                                  fanout=False)
        self.conn.consume_in_thread()

    def get_plugin_type(self):
        return constants.L3_ROUTER_NAT

    def get_plugin_description(self):
        return ("Cisco Router Service Plugin for basic L3 forwarding"
                " between (L2) Neutron networks and access to external"
                " networks via a NAT gateway.")
