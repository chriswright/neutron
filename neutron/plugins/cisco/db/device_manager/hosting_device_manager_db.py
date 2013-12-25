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

import eventlet
import math

from keystoneclient import exceptions as k_exceptions
from keystoneclient.v2_0 import client as k_client
from oslo.config import cfg
from sqlalchemy import func
from sqlalchemy.orm import exc
from sqlalchemy.orm import joinedload
from sqlalchemy.sql import expression as expr

from neutron.common import exceptions as n_exc
from neutron.common import utils
from neutron import context as neutron_context
from neutron import manager
from neutron.openstack.common import importutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import timeutils
from neutron.plugins.cisco.db.device_manager.hd_models import (
    HostingDeviceTemplate)
from neutron.plugins.cisco.db.device_manager.hd_models import HostingDevice
from neutron.plugins.cisco.db.device_manager.hd_models import SlotAllocation
from neutron.plugins.cisco.db.device_manager import hosting_devices_db
from neutron.plugins.cisco.device_manager.rpc import (devmgr_rpc_cfgagent_api
                                                      as devmgr_rpc)
from neutron.plugins.cisco.device_manager import service_vm_lib
from neutron.plugins.cisco.extensions import ciscohostingdevicemanager
from neutron.plugins.common import constants as svc_constants

LOG = logging.getLogger(__name__)


HOSTING_DEVICE_MANAGER_OPTS = [
    cfg.StrOpt('l3_admin_tenant', default='L3AdminTenant',
               help=_("Name of the L3 admin tenant")),
    cfg.StrOpt('management_network', default='osn_mgmt_nw',
               help=_("Name of management network for CSR VM configuration. "
                      "Default value is osn_mgmt_nw")),
    cfg.StrOpt('default_security_group', default='mgmt_sec_grp',
               help=_("Default security group applied on management port. "
                      "Default value is mgmt_sec_grp")),
]

cfg.CONF.register_opts(HOSTING_DEVICE_MANAGER_OPTS)


class HostingDeviceTemplateNotFound(n_exc.NeutronException):
    message = _("Could not find hosting device template %(template)s.")


class MultipleHostingDeviceTemplates(n_exc.NeutronException):
    message = _("Multiple hosting device templates with same name %(name)s. "
                "exist. Id must be used to.")


VM_CATEGORY = ciscohostingdevicemanager.VM_CATEGORY


class HostingDeviceManagerMixin(hosting_devices_db.HostingDeviceDBMixin):
    """A class implementing a resource manager for hosting devices.

    The caller should make sure that HostingDeviceManagerMixin is a singleton.
    """

    # The all-mighty tenant owning all hosting devices
    _l3_tenant_uuid = None
    # The management network for hosting devices
    _mgmt_nw_uuid = None
    _mgmt_sec_grp_id = None

    # Dictionaries with loaded driver modules for different host types
    _plugging_drivers = {}
    _hosting_device_drivers = {}

    # Scheduler of hosting devices to configuration agent
    _cfgagent_scheduler = None

    # Service VM manager object that interacts with Nova
    _svc_vm_mgr = None

    @classmethod
    def reset_all(cls):
        cls._l3_tenant_uuid = None
        cls._mgmt_nw_uuid = None
        cls._mgmt_sec_grp_id = None
        cls._plugging_drivers = {}
        cls._hosting_device_drivers = {}
        cls._cfgagent_scheduler = None
        cls._svc_vm_mgr = None

    @classmethod
    def l3_tenant_id(cls):
        """Returns id of tenant owning hosting device resources."""
        if cls._l3_tenant_uuid is None:
            auth_url = (cfg.CONF.keystone_authtoken.auth_protocol + "://" +
                        cfg.CONF.keystone_authtoken.auth_host + ":" +
                        str(cfg.CONF.keystone_authtoken.auth_port) + "/v2.0")
            user = cfg.CONF.keystone_authtoken.admin_user
            pw = cfg.CONF.keystone_authtoken.admin_password
            tenant = cfg.CONF.keystone_authtoken.admin_tenant_name
            keystone = k_client.Client(username=user, password=pw,
                                       tenant_name=tenant,
                                       auth_url=auth_url)
            try:
                tenant = keystone.tenants.find(name=cfg.CONF.l3_admin_tenant)
                cls._l3_tenant_uuid = tenant.id
            except k_exceptions.NotFound:
                LOG.error(_('No tenant with a name or ID of %s exists.'),
                          cfg.CONF.l3_admin_tenant)
            except k_exceptions.NoUniqueMatch:
                LOG.error(_('Multiple tenants matches found for %s'),
                          cfg.CONF.l3_admin_tenant)
        return cls._l3_tenant_uuid

    @classmethod
    def mgmt_nw_id(cls):
        """Returns id of the management network."""
        if cls._mgmt_nw_uuid is None:
            tenant_id = cls.l3_tenant_id()
            if not tenant_id:
                return None
            net = manager.NeutronManager.get_plugin().get_networks(
                neutron_context.get_admin_context(),
                {'tenant_id': [tenant_id],
                 'name': [cfg.CONF.management_network]},
                ['id', 'subnets'])
            if len(net) == 1:
                num_subnets = len(net[0]['subnets'])
                if num_subnets == 0:
                    LOG.error(_('The management network has no subnet. '
                                'Please assign one.'))
                    return
                elif num_subnets > 1:
                    LOG.info(_('The management network has %d subnets. The '
                               'first one will be used.'), num_subnets)
                cls._mgmt_nw_uuid = net[0].get('id')
            elif len(net) > 1:
                # Management network must have a unique name.
                LOG.error(_('The management network for does not have unique '
                            'name. Please ensure that it is.'))
            else:
                # Management network has not been created.
                LOG.error(_('There is no virtual management network. Please '
                            'create one.'))
        return cls._mgmt_nw_uuid

    @classmethod
    def mgmt_sec_grp_id(cls):
        """Returns id of security group used by the management network."""
        if not utils.is_extension_supported(
                manager.NeutronManager.get_plugin(), "security-group"):
            return None
        if cls._mgmt_sec_grp_id is None:
            # Get the id for the _mgmt_security_group_id
            tenant_id = cls.l3_tenant_id()
            res = manager.NeutronManager.get_plugin().get_security_groups(
                neutron_context.get_admin_context(),
                {'tenant_id': [tenant_id],
                 'name': [cfg.CONF.default_security_group]},
                ['id'])
            if len(res) == 1:
                sec_grp_id = res[0].get('id', None)
                cls._mgmt_sec_grp_id = sec_grp_id
            elif len(res) > 1:
                # the mgmt sec group must be unique.
                LOG.error(_('The security group for the management network '
                            'does not have unique name. Please ensure that '
                            'it is.'))
            else:
                # CSR Mgmt security group is not present.
                LOG.error(_('There is no security group for the management '
                            'network. Please create one.'))
        return cls._mgmt_sec_grp_id

    def get_hosting_device_driver(self, context, id):
        """Returns device driver for hosting device template with <id>."""
        if id is None:
            return
        try:
            return self._hosting_device_drivers[id]
        except KeyError:
            try:
                template = self._get_hosting_device_template(context, id)
                self._hosting_device_drivers[id] = importutils.import_object(
                    template['device_driver'])
            except (ImportError, TypeError, n_exc.NeutronException):
                LOG.exception(_("Error loading hosting device driver for "
                                "hosting device template %s"), id)
            return self._hosting_device_drivers.get(id)

    def get_hosting_device_plugging_driver(self, context, id):
        """Returns  plugging driver for hosting device template with <id>."""
        if id is None:
            return
        try:
            return self._plugging_drivers[id]
        except KeyError:
            try:
                template = self._get_hosting_device_template(context, id)
                self._plugging_drivers[id] = importutils.import_object(
                    template['plugging_driver'])
            except (ImportError, TypeError, n_exc.NeutronException):
                LOG.exception(_("Error loading plugging driver for hosting "
                                "device template %s"), id)
            return self._plugging_drivers.get(id)

    def report_hosting_device_shortage(self, context, template):
        """Used to report shortage of hosting devices based on <template>."""
        mgr_context = neutron_context.get_admin_context()
        self._gt_pool.spawn_n(self._maintain_hosting_device_pool, mgr_context,
                              template)

    def acquire_hosting_device_slots(self, context, hosting_device, resource,
                                     num, exclusive=False):
        """Assign <num> slots in <hosting_device> to logical <resource>.

        If exclusive is True the hosting device is bound to the resource's
        tenant. Otherwise it is not bound to any tenant.

        Returns True if allocation was granted, False otherwise.
        """
        if ((hosting_device['tenant_bound'] is not None and
             hosting_device['tenant_bound'] != resource['id']) or
            (exclusive and not self._exclusively_used(context, hosting_device,
                                                      resource['tenant_id']))):
            LOG.debug(_('Rejecting allocation of %(num)d slots in hosting '
                        'device %(device)s to logical resource %(id)s due to '
                        'conflict of exclusive usage.'),
                      {'num': num, 'device': hosting_device['id'],
                       'id': resource['id']})
            return False
        with context.session.begin(subtransations=True):
            try:
                slot_info = context.session.query(SlotAllocation).filter_by(
                    logical_resource_id=resource['id'],
                    hosting_device_id=hosting_device['id']).one()
            except exc.MultipleResultsFound:
                # this should not happen
                LOG.error(_('DB inconsistency: Multiple slot allocation '
                            'entries for logical resource %(res)s in hosting '
                            'device %(device)s. Rejecting slot allocation!'),
                          {'res': resource['id'],
                           'device': hosting_device['id']})
                return False
            except exc.NoResultFound:
                slot_info = SlotAllocation(
                    template_id=hosting_device['template_id'],
                    hosting_device_id=hosting_device['id'],
                    logical_resource_id=resource['id'],
                    logical_resource_owner=resource['tenant_id'],
                    allocated=0,
                    tenant_bound=None)
            new_allocation = num + slot_info.allocated
            if hosting_device['template']['slot_capacity'] < new_allocation:
                LOG.debug(_('Rejecting allocation of %(num)d slots in '
                            'hosting device %(device)s to logical resource '
                            '%(id)s due to insufficent slot availability.'),
                          {'num': num, 'device': hosting_device['id'],
                           'id': resource['id']})
                return False
            # handle any changes to exclusive usage by tenant
            if exclusive and hosting_device['tenant_bound'] is None:
                self._update_hosting_device_exclusivity(
                    context, hosting_device, resource['tenant_id'])
            elif not exclusive and hosting_device['tenant_bound'] is not None:
                self._update_hosting_device_exclusivity(
                    context, hosting_device, None)
            slot_info.allocated = new_allocation
            context.session.add(slot_info)
        # report success
        LOG.info(_('Allocated %(num)d additional slots in hosting device '
                   '%(hd_id)s. %(total)d slots are now allocated in that '
                   'hosting device.'), {'num': num, 'total': new_allocation,
                                        'hd_id': hosting_device['id']})
        return True

    def release_hosting_device_slots(self, context, hosting_device, resource,
                                     num):
        """Free <num> slots in <hosting_device> from logical resource <id>.

        Returns True if deallocation was successful. False otherwise.
        """
        with context.session.begin(subtransactions=True):
            try:
                query = context.session.query(SlotAllocation).filter_by(
                    logical_resource_id=resource['id'],
                    hosting_device_id=hosting_device['id'])
                slot_info = query.one()
            except exc.MultipleResultsFound:
                # this should not happen
                LOG.error(_('DB inconsistency: Multiple slot allocation '
                            'entries for logical resource %(res)s in hosting '
                            'device %(dev)s. Rejecting slot deallocation!'),
                          {'res': resource['id'], 'dev': hosting_device['id']})
                return False
            except exc.NoResultFound:
                LOG.error(_('Logical resource %(res)s does not have '
                            'allocated any slots in hosting device %(dev)s. '
                            'Rejecting slot deallocation!'),
                          {'res': resource['id'], 'dev': hosting_device['id']})
                return False
            new_allocation = slot_info.num_allocated - num
            if new_allocation < 0:
                LOG.debug(_('Rejecting deallocation of %(num)d slots in '
                            'hosting device %(device)s for logical resource '
                            '%(id)s since only %(alloc)d slots are '
                            'allocated.'),
                          {'num': num, 'device': hosting_device['id'],
                          'id': resource['id'],
                          'alloc': slot_info.num_allocated})
                return False
            elif new_allocation == 0:
                result = query.delete()
                if (hosting_device['tenant_bound'] is not None and
                    context.session.query(SlotAllocation).filter_by(
                        hosting_device_id=hosting_device['id']).first() is
                        None):
                    # make hosting device tenant unbound if no logical
                    # resource use it anymore
                    hosting_device['tenant_bound'] = None
                    context.session.add(hosting_device)
                return result == 1
            LOG.info(_('Deallocated %(num)d slots from hosting device '
                       '%(hd_id)s. %(total)d slots are now allocated in that '
                       'hosting device.'),
                     {'num': num, 'total': new_allocation,
                      'hd_id': hosting_device['id']})
            slot_info.num_allocated = new_allocation
            context.session.add(slot_info)
        # report success
        return True

    def get_hosting_devices_qry(self, context, hosting_device_ids):
        """Returns hosting devices with <hosting_device_ids>."""
        query = context.session.query(HostingDevice)
        if len(hosting_device_ids) > 1:
            query = query.options(joinedload('cfg_agent')).filter(
                HostingDevice.id.in_(hosting_device_ids))
        else:
            query = query.options(joinedload('cfg_agent')).filter(
                HostingDevice.id == hosting_device_ids[0])
        return query

    def delete_all_hosting_devices(self, context, force_delete=False):
        """Deletes all hosting devices."""
        for item in self._get_collection_query(context, HostingDeviceTemplate):
            self.delete_all_hosting_devices_by_template(
                context, template=item, force_delete=force_delete)

    def delete_all_hosting_devices_by_template(self, context, template,
                                               force_delete=False):
        """Deletes all hosting devices based on <template>."""
        with context.session.begin(subtransactions=True):
            plugging_drv = self.get_hosting_device_plugging_driver(
                context, template['id'])
            hosting_device_drv = self.get_hosting_device_driver(context,
                                                                template['id'])
            if plugging_drv is None or hosting_device_drv is None:
                return
            is_vm = template['host_category'] == VM_CATEGORY
            query = context.session.query(HostingDevice)
            query = query.filter(HostingDevice.template_id == template['id'])
            for hd in query:
                if not (hd.auto_delete or force_delete):
                    # device manager is not responsible for life cycle
                    # management of this hosting device.
                    continue
                res = plugging_drv.get_hosting_device_resources(
                    context, hd.id, self.l3_tenant_id(), self.mgmt_nw_id())
                if is_vm:
                    self._svc_vm_mgr.delete_service_vm(
                        context, hd.id, hosting_device_drv, self.mgmt_nw_id())
                plugging_drv.delete_hosting_device_resources(
                    context, self.l3_tenant_id(), **res)
                # remove all allocations in this hosting device
                context.session.query(SlotAllocation).filter_by(
                    hosting_device_id=hd['id']).delete()
                context.session.delete(hd)

    def handle_non_responding_hosting_devices(self, context, cfg_agent,
                                              hosting_device_ids):
        query = self.get_hosting_devices_qry(context.elevated(),
                                             hosting_device_ids)
        hosting_devices = query.all()
        # 'hosting_info' is dictionary with ids of removed hosting
        # devices and the affected logical resources for each
        # removed hosting device:
        #    {'hd_id1': {'routers': [id1, id2, ...],
        #                'fw': [id1, ...],
        #                 ...},
        #     'hd_id2': {'routers': [id3, id4, ...]},
        #                'fw': [id1, ...],
        #                ...},
        #     ...}
        hosting_info = {}
        with context.session.begin(subtransactions=True):
            #TODO(bobmel): Modify so service plugins register themselves
            try:
                l3plugin = manager.NeutronManager.get_service_plugins().get(
                    svc_constants.L3_ROUTER_NAT)
                l3plugin.handle_non_responding_hosting_devices(
                    context, hosting_devices, hosting_info)
            except AttributeError:
                pass
            e_context = context.elevated()
            for hd in hosting_devices:
                if self._process_non_responsive_hosting_device(e_context, hd):
                    devmgr_rpc.DeviceMgrCfgAgentNotify.hosting_device_removed(
                        context, hosting_info, False, cfg_agent)

    def _process_non_responsive_hosting_device(self, context, hosting_device):
        """Host type specific processing of non responsive hosting devices.

        :param hosting_device: db object for hosting device
        :return: True if hosting_device has been deleted, otherwise False
        """
        if (hosting_device['host_category'] == VM_CATEGORY and
                hosting_device['auto_delete']):
            self._delete_dead_service_vm_hosting_device(context,
                                                        hosting_device)
            return True
        return False

    def _setup_device_manager(self):
        auth_url = (cfg.CONF.keystone_authtoken.auth_protocol + "://" +
                    cfg.CONF.keystone_authtoken.auth_host + ":" +
                    str(cfg.CONF.keystone_authtoken.auth_port) + "/v2.0")
        u_name = cfg.CONF.keystone_authtoken.admin_user
        pw = cfg.CONF.keystone_authtoken.admin_password
        tenant = cfg.CONF.l3_admin_tenant
        self._svc_vm_mgr = service_vm_lib.ServiceVMManager(
            user=u_name, passwd=pw, l3_admin_tenant=tenant, auth_url=auth_url)
        self._gt_pool = eventlet.GreenPool()
        # initialize hosting device pools
        ctx = neutron_context.get_admin_context()
        for template in ctx.session.query(HostingDeviceTemplate):
            self.report_hosting_device_shortage(ctx, template)

    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

    def _maintain_hosting_device_pool(self, context, template):
        """Maintains the pool of hosting devices that are based on <template>.

        Ensures that the number of standby hosting devices (essentially
        service VMs) is kept at a suitable level so that resource creation is
        not slowed down by booting of the hosting device.
        """
        # For now the pool size is only elastic for service VMs.
        if template['host_category'] != VM_CATEGORY:
            return
        # Maintain a pool of approximately 'desired_slots_free' available
        # for allocation. Approximately means:
        # max(0, desired_slots_free - capacity) <= available_slots <=
        #                                         desired_slots_free + capacity
        capacity = template['slot_capacity']
        desired = template['desired_slots_free']
        available = self._get_total_available_slots(context, template['id'],
                                                    capacity)
        if available <= max(0, desired - capacity):
            num_req = int(math.ceil((desired - available) / (1.0 * capacity)))
            num_created = len(self._create_svc_vm_hosting_devices(
                context, num_req, template))
            if num_created < num_req:
                LOG.warn(_('Requested %(requested)d instances based on '
                           'hosting device template %(template)s but could'
                           'only create %(created)d instances'),
                         {'requested': num_req, 'template': template['id'],
                          'created': num_created})
        elif available >= desired + capacity:
            num_req = int(math.ceil((available - desired) / (1.0 * capacity)))
            num_deleted = self._delete_idle_service_vm_hosting_devices(
                context, num_req, template)
            if num_deleted < num_req:
                LOG.warn(_('Tried to delete %(requested)d instances based on '
                           'hosting device template %(template)s but could '
                           'only delete %(deleted)d instances'),
                         {'requested': num_req, 'deleted': num_deleted})

    def _create_svc_vm_hosting_devices(self, context, num, template):
        """Creates <num> or less service VM instances based on <template>.

        These hosting devices can be bound to a certain tenant or for shared
        use. A list with the created hosting device VMs is returned.
        """
        hosting_devices = []
        plugging_drv = self.get_hosting_device_plugging_driver(context,
                                                               template['id'])
        hosting_device_drv = self.get_hosting_device_driver(context,
                                                            template['id'])
        if plugging_drv is None or hosting_device_drv is None:
            return hosting_devices
        # These resources are owned by the L3AdminTenant
        dev_data = {'template_id': template['id'],
                    'credentials_id': template['default_credentials_id'],
                    'admin_state_up': True,
                    'protocol_port': template['protocol_port'],
                    'created_at': timeutils.utcnow(),
                    'booting_time': template['booting_time'],
                    'tenant_bound': template['tenant_bound'],
                    'auto_delete': True}
        #TODO(bobmel): Determine value for max_hosted properly
        max_hosted = 1  # template['slot_capacity']
        for i in xrange(num):
            res = plugging_drv.create_hosting_device_resources(
                context, self.l3_tenant_id(), self.mgmt_nw_id(),
                self.mgmt_sec_grp_id(), max_hosted)
            if res.get('mgmt_port') is None:
                # Required ports could not be created
                return hosting_devices
            vm_instance = self._svc_vm_mgr.dispatch_service_vm(
                context, template['name'] + '_nrouter', template['image'],
                template['flavor'], hosting_device_drv, res['mgmt_port'],
                res.get('ports'))
            with context.session.begin(subtransactions=True):
                if vm_instance is not None:
                    dev_data.update(
                        {'id': vm_instance['id'],
                         'management_port_id': res['mgmt_port']['id']})
                    self.create_hosting_device(context,
                                               {'hosting_device': dev_data})
                    hosting_devices.append(vm_instance)
                else:
                    # Fundamental error like could not contact Nova
                    # Cleanup anything we created
                    plugging_drv.delete_hosting_device_resources(
                        context, self.l3_tenant_id(), **res)
                    return hosting_devices
        LOG.info(_('Created %(num)d hosting device VMs based on template '
                   '%(t_id)s'), {'num': len(hosting_devices),
                                 't_id': template['id']})
        return hosting_devices

    def _delete_idle_service_vm_hosting_devices(self, context, num, template):
        """Deletes <num> or less unused <template>-based service VM instances.

        The number of deleted service vm instances is returned.
        """
        # Delete the "youngest" hosting devices since they are more likely
        # not to have finished booting
        num_deleted = 0
        plugging_drv = self.get_hosting_device_plugging_driver(context,
                                                               template['id'])
        hosting_device_drv = self.get_hosting_device_driver(context,
                                                            template['id'])
        if plugging_drv is None or hosting_device_drv is None:
            return num_deleted
        query = context.session.query(HostingDevice)
        query = query.outerjoin(
            SlotAllocation,
            HostingDevice.id == SlotAllocation.hosting_device_id)
        query = query.filter(HostingDevice.template_id == template['id'],
                             HostingDevice.admin_state_up == expr.true(),
                             HostingDevice.tenant_bound == expr.null(),
                             HostingDevice.auto_delete == expr.true())
        query = query.group_by(HostingDevice.id).having(
            func.count(SlotAllocation.logical_resource_id) == 0)
        query = query.order_by(
            HostingDevice.created_at.desc(),
            func.count(SlotAllocation.logical_resource_id))
        hd_candidates = query.all()
        num_possible_to_delete = min(len(hd_candidates), num)
        with context.session.begin(subtransactions=True):
            for i in xrange(num_possible_to_delete):
                res = plugging_drv.get_hosting_device_resources(
                    context, hd_candidates[i]['id'], self.l3_tenant_id(),
                    self.mgmt_nw_id())
                if self._svc_vm_mgr.delete_service_vm(
                        context, hd_candidates[i]['id'], hosting_device_drv,
                        self.mgmt_nw_id()):
                    context.session.delete(hd_candidates[i])
                    plugging_drv.delete_hosting_device_resources(
                        context, self.l3_tenant_id(), **res)
                    num_deleted += 1
        LOG.info(_('Deleted %(num)d hosting devices based on template '
                   '%(t_id)s'), {'num': num_deleted, 't_id': template['id']})
        return num_deleted

    def _delete_dead_service_vm_hosting_device(self, context, hosting_device):
        """Deletes a presumably dead <hosting_device> service VM.

        This will indirectly make all of its hosted resources unscheduled.
        """
        if hosting_device is None:
            return
        plugging_drv = self.get_hosting_device_plugging_driver(
            context, hosting_device['template_id'])
        hosting_device_drv = self.get_hosting_device_driver(
            context, hosting_device['template_id'])
        if (plugging_drv is None or hosting_device_drv is None):
            return
        res = plugging_drv.get_hosting_device_resources(
            context, hosting_device['id'], self.l3_tenant_id(),
            self.mgmt_nw_id())
        if not self._svc_vm_mgr.delete_service_vm(
                context, hosting_device['id'], hosting_device_drv,
                self.mgmt_nw_id()):
            LOG.error(_('Failed to delete hosting device %s service VM. '
                        'Will un-register it anyway.'),
                      hosting_device['id'])
        plugging_drv.delete_hosting_device_resources(
            context, self.l3_tenant_id(), **res)
        with context.session.begin(subtransactions=True):
            context.session.delete(hosting_device)

    def _get_total_available_slots(self, context, template_id, capacity):
        """Returns available slots sum for devices based on <template_id>."""
        query = context.session.query(func.sum(SlotAllocation.num_allocated))
        total_allocated = query.filter_by(
            template_id=template_id,
            tenant_bound=expr.null()).one()[0] or 0
        query = context.session.query(func.count(HostingDevice.id))
        num_devices = query.filter_by(template_id=template_id,
                                      admin_state_up=True,
                                      tenant_bound=expr.null()).one()[0] or 0
        return num_devices * capacity - total_allocated

    def _exclusively_used(self, context, hosting_device, tenant_id):
        """Checks if only <tenant_id>'s resources use <hosting_device>."""
        return context.session.query(SlotAllocation).filter(
            SlotAllocation.hosting_device_id == hosting_device['id'],
            SlotAllocation.local_resource_owner != tenant_id).first() is None

    def _update_hosting_device_exclusivity(self, context, hosting_device,
                                           tenant_id):
        """Make <hosting device> bound or unbound to <tenant_id>.

        If <tenant_id> is None the device is unbound, otherwise it gets bound
        to that <tenant_id>
        """
        with context.session.begin(subtransactions=True):
            hosting_device['tenant_bound'] = tenant_id
            context.session.add(hosting_device)
            for item in context.session.query(SlotAllocation).filter_by(
                    hosting_device_id=hosting_device['id']):
                item['tenant_bound'] = tenant_id
                context.session.add(item)
