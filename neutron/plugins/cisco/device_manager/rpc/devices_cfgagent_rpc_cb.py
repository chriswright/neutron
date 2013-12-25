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

from neutron import manager
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants as svc_constants

LOG = logging.getLogger(__name__)


class DeviceMgrCfgRpcCallbackMixin(object):
    """Mixin for Cisco cfg agent rpc support in Device mgr service plugin."""

    def report_non_responding_hosting_devices(self, context, **kwargs):
        """Report that a hosting device cannot be contacted.

        @param: context - contains user information
        @param: kwargs - hosting_device_ids: list of non-responding
                                             hosting devices
                         host: originator of callback
        @return: -
        """
        hosting_device_ids = kwargs.get('hosting_device_ids', [])
        cfg_agent_host = kwargs.get('host', None)
        plugin = manager.NeutronManager.get_service_plugins().get(
            svc_constants.DEVICE_MANAGER)
        if plugin is None:
            LOG.error(_('No Device manager service plugin registered!'
                        'Cannot handle non-responding hosting device '
                        'callback'))
        else:
            plugin.handle_non_responding_hosting_devices(
                context, cfg_agent_host, hosting_device_ids)

    def register_for_duty(self, context, **kwargs):
        """Report that Cisco cfg agent is ready for duty.

        This function is supposed to be called when the agent has started,
        is ready to take on assignments and before any callbacks to fetch
        logical resources are issued.

        @param: context - contains user information
        @param: kwargs - hosting_device_ids: list of non-responding
                                             hosting devices
                         host: originator of callback
        @return: True if succesfully registered, False if not successfully
                 registered, None if no handler found
                 If unsuccessful the agent should retry registration a few
                 seconds later
        """
        agent_host = kwargs.get('host', None)
        plugin = manager.NeutronManager.get_service_plugins().get(
            svc_constants.DEVICE_MANAGER)
        if plugin is None:
            LOG.error(_('No Device manager service plugin registered!'
                        'Cannot handle Cisco configuration agent duty '
                        'readiness callback'))
            return
        else:
            # schedule any non-handled hosting devices
            return plugin.auto_schedule_hosting_devices(context, agent_host)
