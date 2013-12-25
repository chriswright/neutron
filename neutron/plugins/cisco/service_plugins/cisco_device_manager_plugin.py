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

from neutron.common import rpc as q_rpc
from neutron.common import topics
from neutron.db import api as qdbapi

from neutron.db import model_base
from neutron.openstack.common import importutils
from neutron.openstack.common import rpc
import neutron.plugins
from neutron.plugins.cisco.common import cisco_constants as c_constants
from neutron.plugins.cisco.db.device_manager import (hosting_device_manager_db
                                                     as dev_mgr_db)
from neutron.plugins.cisco.db.scheduler import (cfg_agentschedulers_db as
                                                agt_sched_db)
from neutron.plugins.cisco.device_manager.rpc import (devices_cfgagent_rpc_cb
                                                      as devices_rpc)
from neutron.plugins.cisco.extensions import ciscocfgagentscheduler
from neutron.plugins.cisco.extensions import ciscohostingdevicemanager
from neutron.plugins.cisco.l3.rpc import l3_router_rpc_joint_agent_api


class CiscoDevMgrPluginRpcCallbacks(devices_rpc.DeviceMgrCfgRpcCallbackMixin):
    # Set RPC API version to 1.0 by default.
    RPC_API_VERSION = '1.0'

    def create_rpc_dispatcher(self):
        """Get the rpc dispatcher for this manager.

        If a manager would like to set an rpc API version, or support more than
        one class as the target of rpc messages, override this method.
        """
        return q_rpc.PluginRpcDispatcher([self])


class CiscoDeviceManagerPlugin(dev_mgr_db.HostingDeviceManagerMixin,
                               agt_sched_db.CfgAgentSchedulerDbMixin):
    """Implementation of Cisco Device Manager Service Plugin for Neutron.

    This class implements a (hosting) device manager service plugin that
    provides hosting device template and hosting device resources. As such
    it manages associated REST API processing. All DB functionality is
    implemented in class hosting_device_manager_db.HostingDeviceManagerMixin.
    """
    supported_extension_aliases = [
        ciscohostingdevicemanager.HOSTING_DEVICE_MANAGER_ALIAS,
        ciscocfgagentscheduler.CFG_AGENT_SCHEDULER_ALIAS]

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
        self.cfg_agent_scheduler = importutils.import_object(
            cfg.CONF.configuration_agent_scheduler_driver)
        self._setup_device_manager()

    def setup_rpc(self):
        # RPC support
        self.topic = topics.DEVICE_MANAGER_PLUGIN
        self.conn = rpc.create_connection(new=True)
        self.agent_notifiers.update(
            {c_constants.AGENT_TYPE_CFG:
             l3_router_rpc_joint_agent_api.L3JointAgentNotify})
        self.callbacks = CiscoDevMgrPluginRpcCallbacks()
        self.dispatcher = self.callbacks.create_rpc_dispatcher()
        self.conn.create_consumer(self.topic, self.dispatcher, fanout=False)
        self.conn.consume_in_thread()
