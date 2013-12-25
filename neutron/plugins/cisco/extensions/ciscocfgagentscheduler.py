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

from abc import abstractmethod

import webob.exc

from neutron.api import extensions
from neutron.api.v2 import base
from neutron.api.v2 import resource
from neutron.common import exceptions
from neutron.extensions import agent
from neutron import manager
from neutron.openstack.common import log as logging
from neutron.plugins.cisco.extensions import ciscohostingdevicemanager
from neutron.plugins.common import constants as svc_constants
from neutron import policy
from neutron import wsgi

LOG = logging.getLogger(__name__)


class InvalidCfgAgent(agent.AgentNotFound):
    message = _("Agent %(id)s is not a Cfg Agent or has been disabled")


class HostingDeviceHandledByCfgAgent(exceptions.Conflict):
    message = _("The hosting device %(device_id)s is already handled"
                " by the Cisco cfg agent %(agent_id)s.")


class HostingDeviceSchedulingFailed(exceptions.Conflict):
    message = _("Failed scheduling hosting device %(device_id)s to"
                " the Cisco cfg Agent %(agent_id)s.")


class HostingDeviceNotHandledByCfgAgent(exceptions.Conflict):
    message = _("The hosting device %(device_id)s is not handled"
                " by Cisco cfg agent %(agent_id)s.")


CFG_AGENT_SCHEDULER_ALIAS = 'cisco-cfg-agent-scheduler'
HOSTING_DEVICE = ciscohostingdevicemanager.DEVICE
HOSTING_DEVICES = ciscohostingdevicemanager.DEVICES
CFG_AGENT = 'cisco_cfg_agent'
CFG_AGENTS = CFG_AGENT + 's'


class HostingDeviceSchedulerController(wsgi.Controller):
    def get_plugin(self):
        plugin = manager.NeutronManager.get_service_plugins().get(
            svc_constants.DEVICE_MANAGER)
        if not plugin:
            LOG.error(_('No Device manager service plugin registered to '
                        'handle hosting device scheduling'))
            msg = _('The resource could not be found.')
            raise webob.exc.HTTPNotFound(msg)
        return plugin

    def index(self, request, **kwargs):
        plugin = self.get_plugin()
        policy.enforce(request.context, "get_%s" % HOSTING_DEVICES, {})
        return plugin.list_hosting_devices_handled_by_cfg_agent(
            request.context, kwargs['agent_id'])

    def create(self, request, body, **kwargs):
        plugin = self.get_plugin()
        policy.enforce(request.context, "create_%s" % HOSTING_DEVICE, {})
        return plugin.assign_hosting_device_to_cfg_agent(
            request.context, kwargs['agent_id'], body['hosting_device_id'])

    def delete(self, request, id, **kwargs):
        plugin = self.get_plugin()
        policy.enforce(request.context, "delete_%s" % HOSTING_DEVICES, {})
        return plugin.unassign_hosting_device_from_cfg_agent(
            request.context, kwargs['agent_id'], id)


class CfgAgentsHandlingHostingDeviceController(wsgi.Controller):
    def get_plugin(self):
        plugin = manager.NeutronManager.get_service_plugins().get(
            svc_constants.DEVICE_MANAGER)
        if not plugin:
            LOG.error(_('No Device manager service plugin registered to '
                        'handle hosting device scheduling'))
            msg = _('The resource could not be found.')
            raise webob.exc.HTTPNotFound(msg)
        return plugin

    def index(self, request, **kwargs):
        plugin = self.get_plugin()
        policy.enforce(request.context, "get_%s" % CFG_AGENTS, {})
        return plugin.list_cfg_agents_handling_hosting_device(
            request.context, kwargs['hosting_device_id'])


class Ciscocfgagentscheduler(extensions.ExtensionDescriptor):
    """Extension class supporting l3 agent scheduler."""
    @classmethod
    def get_name(cls):
        return "Cisco Configuration Agent Scheduler"

    @classmethod
    def get_alias(cls):
        return CFG_AGENT_SCHEDULER_ALIAS

    @classmethod
    def get_description(cls):
        return "Schedule hosting devices among Cisco configuration agents"

    @classmethod
    def get_namespace(cls):
        return ("http://docs.openstack.org/ext/" +
                CFG_AGENT_SCHEDULER_ALIAS + "/api/v1.0")

    @classmethod
    def get_updated(cls):
        return "2014-03-31T10:00:00-00:00"

    @classmethod
    def get_resources(cls):
        """Returns Ext Resources."""
        exts = []
        parent = dict(member_name="agent",
                      collection_name="agents")
        controller = resource.Resource(HostingDeviceSchedulerController(),
                                       base.FAULT_MAP)
        exts.append(extensions.ResourceExtension(HOSTING_DEVICES, controller,
                                                 parent))
        parent = dict(member_name=HOSTING_DEVICE,
                      collection_name=HOSTING_DEVICES)
        controller = resource.Resource(
            CfgAgentsHandlingHostingDeviceController(), base.FAULT_MAP)
        exts.append(extensions.ResourceExtension(CFG_AGENTS, controller,
                                                 parent))
        return exts

    def get_extended_resources(self, version):
        return {}


class CfgAgentSchedulerPluginBase(object):
    """REST API to operate the cfg agent scheduler.

    All of method must be in an admin context.
    """
    @abstractmethod
    def assign_hosting_device_to_cfg_agent(self, context, id,
                                           hosting_device_id):
        pass

    @abstractmethod
    def unassign_hosting_device_from_cfg_agent(self, context, id,
                                               hosting_device_id):
        pass

    @abstractmethod
    def list_hosting_devices_handled_by_cfg_agent(self, context, id):
        pass

    @abstractmethod
    def list_cfg_agents_handling_hosting_device(self, context,
                                                hosting_device_id):
        pass
