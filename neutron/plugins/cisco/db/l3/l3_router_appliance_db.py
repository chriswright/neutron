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

import copy

from oslo.config import cfg
from sqlalchemy.orm import exc
from sqlalchemy.orm import joinedload
from sqlalchemy.sql import expression as expr

from neutron.common import constants as l3_constants
from neutron.common import exceptions as n_exc
from neutron import context as n_context
from neutron.db import extraroute_db
from neutron.db import l3_db
from neutron.db import models_v2
from neutron.extensions import providernet as pr_net
from neutron import manager
from neutron.openstack.common import excutils
from neutron.openstack.common import importutils
from neutron.openstack.common import lockutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import loopingcall
from neutron.plugins.cisco.common import cisco_constants as c_const
from neutron.plugins.cisco.db.device_manager.hd_models import (
    HostedHostingPortBinding)
from neutron.plugins.cisco.db.l3.l3_models import RouterHostingDeviceBinding
from neutron.plugins.cisco.db.l3.l3_models import RouterType
from neutron.plugins.cisco.extensions import routertype
from neutron.plugins.cisco.l3.rpc import (l3_router_rpc_joint_agent_api as
                                          l3_router_rpc_api)
from neutron.plugins.common import constants as svc_constants

LOG = logging.getLogger(__name__)


ROUTER_APPLIANCE_OPTS = [
    cfg.StrOpt('default_router_type',
               default=c_const.CSR1KV_ROUTER_TYPE,
               help=_("Default type of router to create")),
    cfg.StrOpt('namespace_router_type_name',
               default=c_const.NAMESPACE_ROUTER_TYPE,
               help=_("Name of router type used for Linux network namespace "
                      "routers (i.e., Neutron's legacy routers in Network "
                      "nodes).")),
    cfg.IntOpt('backlog_processing_interval',
               default=10,
               help=_('Time in seconds between renewed scheduling attempts of '
                      'non-scheduled routers')),
]

cfg.CONF.register_opts(ROUTER_APPLIANCE_OPTS)


class RouterCreateInternalError(n_exc.NeutronException):
    message = _("Router could not be created due to internal error.")


class RouterInternalError(n_exc.NeutronException):
    message = _("Internal error during router processing.")


class RouterBindingInfoError(n_exc.NeutronException):
    message = _("Could not get binding information for router %(router_id)s.")


class RouterTypeNotFound(n_exc.NeutronException):
    message = _("Could not find router type %(router_type)s.")


class MultipleRouterTypes(n_exc.NeutronException):
    message = _("Multiple router type with same name %(name)s exist. Id "
                "must be used to specify router type.")


class L3RouterApplianceDBMixin(extraroute_db.ExtraRoute_db_mixin):
    """Mixin class implementing Neutron's routing service using appliances."""

    # Dictionary with loaded scheduler modules for different router types
    _router_schedulers = {}

    # Id of router type used to represent Neutron's "legacy" Linux network
    # namespace routers
    _namespace_router_type_id = None

    # Dictionary of routers for which new scheduling attempts should
    # be made and the refresh setting and heartbeat for that.
    _backlogged_routers = {}
    _refresh_router_backlog = True
    _heartbeat = None

    @classmethod
    def reset_all(cls):
        cls._router_schedulers = {}
        cls._namespace_router_type_id = None
        cls._backlogged_routers = {}
        cls._refresh_router_backlog = True
        cls._heartbeat = None

    def create_router(self, context, router):
        r = router['router']
        router_type_name = r.get(routertype.ROUTERTYPE,
                                 cfg.CONF.default_router_type)
        # bobmel: Hard coding to shared host for now
        share_host = True
        with context.session.begin(subtransactions=True):
            router_type_id = self.get_router_type(context,
                                                  router_type_name)['id']
            auto_schedule = cfg.CONF.router_auto_schedule
            if (router_type_id != self.get_namespace_router_type_id(context)
                    and self._dev_mgr.mgmt_nw_id() is None):
                raise RouterCreateInternalError()
            router_created = (super(L3RouterApplianceDBMixin, self).
                              create_router(context, router))
            r_hd_b_db = RouterHostingDeviceBinding(
                router_id=router_created['id'],
                router_type_id=router_type_id,
                auto_schedule=auto_schedule,
                share_hosting_device=share_host,
                hosting_device_id=None)
            context.session.add(r_hd_b_db)
            #TODO(bobmel): Remove this line
            self._add_type_and_hosting_device_info(context.elevated(),
                                                   router_created)
        return router_created

    def update_router(self, context, id, router):
        r = router['router']
        # Check if external gateway has changed so we may have to
        # update trunking
        o_r_db = self._get_router(context, id)
        old_ext_gw = (o_r_db.gw_port or {}).get('network_id')
        new_ext_gw = r.get('external_gateway_info', {}).get('network_id')
        with context.session.begin(subtransactions=True):
            e_context = context.elevated()
            if old_ext_gw is not None and old_ext_gw != new_ext_gw:
                o_r = self._make_router_dict(o_r_db, process_extensions=False)
                # no need to schedule now since we're only doing this to
                # tear-down connectivity and there won't be any if not
                # already scheduled.
                self._add_type_and_hosting_device_info(e_context, o_r,
                                                       schedule=False)
                p_drv = self._dev_mgr.get_hosting_device_plugging_driver(
                    context, (o_r['hosting_device'] or {}).get('template_id'))
                if p_drv is not None:
                    p_drv.teardown_logical_port_connectivity(e_context,
                                                             o_r_db.gw_port)
            router_updated = (
                super(L3RouterApplianceDBMixin, self).update_router(
                    context, id, router))
            routers = [copy.deepcopy(router_updated)]
            self._add_type_and_hosting_device_info(e_context, routers[0])
        l3_router_rpc_api.L3JointAgentNotify.routers_updated(context, routers)
        return router_updated

    def delete_router(self, context, id):
        router_db = self._get_router(context, id)
        router = self._make_router_dict(router_db)
        with context.session.begin(subtransactions=True):
            e_context = context.elevated()
            r_hd_binding = self._get_router_binding_info(e_context, id)
            self._add_type_and_hosting_device_info(
                e_context, router, binding_info=r_hd_binding, schedule=False)
            if router_db.gw_port is not None:
                p_drv = self._dev_mgr.get_hosting_device_plugging_driver(
                    context,
                    (router['hosting_device'] or {}).get('template_id'))
                if p_drv is not None:
                    p_drv.teardown_logical_port_connectivity(e_context,
                                                             router_db.gw_port)
            # conditionally remove router from backlog just to be sure
            self.remove_router_from_backlog('id')
            if router['hosting_device'] is not None:
                self.unschedule_router_from_hosting_device(context,
                                                           r_hd_binding)
            #TODO(bobmel): Delay delete from DB until cfgagent acknowledges
            super(L3RouterApplianceDBMixin, self).delete_router(context, id)
        l3_router_rpc_api.L3JointAgentNotify.router_deleted(context, router)

    def add_router_interface(self, context, router_id, interface_info):
        with context.session.begin(subtransactions=True):
            info = (super(L3RouterApplianceDBMixin, self).
                    add_router_interface(context, router_id, interface_info))
            routers = [self.get_router(context, router_id)]
            self._add_type_and_hosting_device_info(context.elevated(),
                                                   routers[0])
        l3_router_rpc_api.L3JointAgentNotify.routers_updated(
            context, routers, 'add_router_interface')
        return info

    def remove_router_interface(self, context, router_id, interface_info):
        if 'port_id' in (interface_info or {}):
            port_db = self._core_plugin._get_port(
                context, interface_info['port_id'])
        elif 'subnet_id' in (interface_info or {}):
            subnet_db = self._core_plugin._get_subnet(
                context, interface_info['subnet_id'])
            port_db = self._get_router_port_db_on_subnet(
                context, router_id, subnet_db)
        else:
            msg = "Either subnet_id or port_id must be specified"
            raise n_exc.BadRequest(resource='router', msg=msg)
        routers = [self.get_router(context, router_id)]
        with context.session.begin(subtransactions=True):
            e_context = context.elevated()
            self._add_type_and_hosting_device_info(e_context, routers[0])
            p_drv = self._dev_mgr.get_hosting_device_plugging_driver(
                context,
                (routers[0]['hosting_device'] or {}).get('template_id'))
            if p_drv is not None:
                p_drv.teardown_logical_port_connectivity(e_context, port_db)
            info = (super(L3RouterApplianceDBMixin, self).
                    remove_router_interface(context, router_id,
                                            interface_info))
        l3_router_rpc_api.L3JointAgentNotify.routers_updated(
            context, routers, 'remove_router_interface')
        return info

    def create_floatingip(
            self, context, floatingip,
            initial_status=l3_constants.FLOATINGIP_STATUS_ACTIVE):
        with context.session.begin(subtransactions=True):
            info = super(L3RouterApplianceDBMixin, self).create_floatingip(
                context, floatingip)
            if info['router_id']:
                routers = [self.get_router(context, info['router_id'])]
                self._add_type_and_hosting_device_info(context.elevated(),
                                                       routers[0])
                l3_router_rpc_api.L3JointAgentNotify.routers_updated(
                    context, routers, 'create_floatingip')
        return info

    def update_floatingip(self, context, id, floatingip):
        orig_fl_ip = super(L3RouterApplianceDBMixin, self).get_floatingip(
            context, id)
        before_router_id = orig_fl_ip['router_id']
        with context.session.begin(subtransactions=True):
            info = super(L3RouterApplianceDBMixin, self).update_floatingip(
                context, id, floatingip)
            router_ids = []
            if before_router_id:
                router_ids.append(before_router_id)
            router_id = info['router_id']
            if router_id and router_id != before_router_id:
                router_ids.append(router_id)
            routers = []
            for router_id in router_ids:
                router = self.get_router(context, router_id)
                self._add_type_and_hosting_device_info(context.elevated(),
                                                       router)
                routers.append(router)
        l3_router_rpc_api.L3JointAgentNotify.routers_updated(
            context, routers, 'update_floatingip')
        return info

    def delete_floatingip(self, context, id):
        floatingip_db = self._get_floatingip(context, id)
        router_id = floatingip_db['router_id']
        with context.session.begin(subtransactions=True):
            super(L3RouterApplianceDBMixin, self).delete_floatingip(
                context, id)
            if router_id:
                routers = [self.get_router(context, router_id)]
                self._add_type_and_hosting_device_info(context.elevated(),
                                                       routers[0])
                l3_router_rpc_api.L3JointAgentNotify.routers_updated(
                    context, routers, 'delete_floatingip')

    def disassociate_floatingips(self, context, port_id):
        with context.session.begin(subtransactions=True):
            try:
                fip_qry = context.session.query(l3_db.FloatingIP)
                floating_ip = fip_qry.filter_by(fixed_port_id=port_id).one()
                router_id = floating_ip['router_id']
                floating_ip.update({'fixed_port_id': None,
                                    'fixed_ip_address': None,
                                    'router_id': None})
            except exc.NoResultFound:
                return
            except exc.MultipleResultsFound:
                # should never happen
                raise Exception(_('Multiple floating IPs found for port %s')
                                % port_id)
            if router_id:
                routers = [self.get_router(context, router_id)]
                self._add_type_and_hosting_device_info(context.elevated(),
                                                       routers[0])
                l3_router_rpc_api.L3JointAgentNotify.routers_updated(
                    context, routers)

    @lockutils.synchronized('routerbacklog', 'neutron-')
    def handle_non_responding_hosting_devices(self, context, hosting_devices,
                                              affected_resources):
        """Handle hosting devices determined to be "dead".

        This function is called by the hosting device manager.
        Service plugins are supposed to extend the 'affected_resources'
        dictionary. Hence, we add the id of Neutron routers that are
        hosted in <hosting_devices>.

        param: hosting_devices - list of dead hosting devices
        param: affected_resources - dict with list of affected logical
                                    resources per hosting device:
             {'hd_id1': {'routers': [id1, id2, ...],
                         'fw': [id1, ...],
                         ...},
              'hd_id2': {'routers': [id3, id4, ...],
                         'fw': [id1, ...],
                         ...},
              ...}
        """
        LOG.debug(_('Processing affected routers in dead hosting devices'))
        with context.session.begin(subtransactions=True):
            for hd in hosting_devices:
                hd_bindings = self._get_hosting_device_bindings(context,
                                                                hd['id'])
                router_ids = []
                for binding in hd_bindings:
                    router_ids.append(binding['router_id'])
                    if binding['auto_schedule']:
                        self.backlog_router(binding['router'])
                    try:
                        affected_resources[hd['id']].update(
                            {'routers': router_ids})
                    except KeyError:
                        affected_resources[hd['id']] = {'routers': router_ids}

    def get_sync_data(self, context, router_ids=None, active=None):
        # ensure only routers of namespace type are returned
        r_f = {'routertype_id': [self.get_namespace_router_type_id(context)]}
        if router_ids is not None:
            r_f['id'] = router_ids
        routers = self.get_routers(context, filters=r_f, fields=['id']) or []
        router_ids = [item['id'] for item in routers]
        return super(L3RouterApplianceDBMixin, self).get_sync_data(
            context, router_ids, active)

    def get_sync_data_ext(self, context, router_ids=None, active=None):
        """Query routers and their related floating_ips, interfaces.

        Adds information about hosting device as well as trunking.
        """
        with context.session.begin(subtransactions=True):
            sync_data = (super(L3RouterApplianceDBMixin, self).
                         get_sync_data(context, router_ids, active))
            for router in sync_data:
                self._add_type_and_hosting_device_info(context, router)
                plg_drv = self._dev_mgr.get_hosting_device_plugging_driver(
                    context,
                    (router.get('hosting_device') or {}).get('template_id'))
                if plg_drv is not None:
                    self._add_hosting_port_info(context, router, plg_drv)
        return sync_data

    def schedule_router_on_hosting_device(self, context, r_hd_binding):
        LOG.info(_('Attempting to schedule router %s.'),
                 r_hd_binding['router']['id'])
        scheduler = self._get_router_type_scheduler(
            context, r_hd_binding['router_type_id'])
        if scheduler is None:
            LOG.debug(_('Aborting scheduling of router %(r_id)s as no '
                        'scheduler was found for its router type %(type)s.'),
                      {'r_id': r_hd_binding['router']['id'],
                       'type': r_hd_binding['router_type_id']})
            return False
        with context.session.begin(subtransactions=True):
            selected_hd = scheduler.schedule_router(self, context,
                                                    r_hd_binding)
            if selected_hd is None:
                # No running hosting device is able to host this router
                # so backlog it for another scheduling attempt later.
                self.backlog_router(r_hd_binding['router'])
                # Inform device manager so that it can take appropriate
                # measures, e.g., spin up more hosting device VMs.
                self._dev_mgr.report_hosting_device_shortage(
                    context, r_hd_binding['router_type']['template'])
                return False
            else:
                router = r_hd_binding['router']
                acquired = self._dev_mgr.acquire_hosting_device_slots(
                    context.elevated(), selected_hd, router,
                    r_hd_binding['router_type']['slot_need'],
                    exclusive=r_hd_binding['share_hosting_device'])
                if acquired:
                    r_hd_binding.hosting_device_id = selected_hd[0]['id']
                    self.remove_router_from_backlog(router['id'])
                else:
                    LOG.debug(_('Could not allocated slots for router '
                                '%(r_id)s in hosting device %(d_id)s.'),
                              {'r_id': r_hd_binding['router']['id'],
                               'd_id': r_hd_binding.hosting_device_id})
                    # we got not slot so backlog it for another scheduling
                    # attempt later.
                    self.backlog_router(router)
                    return False
            if r_hd_binding.hosting_device_id is not None:
                LOG.info(_('Succesfully scheduled router %(r_id)s to hosting '
                           'device %(d_id)s'),
                         {'r_id': r_hd_binding['router']['id'],
                          'd_id': r_hd_binding.hosting_device_id})
                context.session.add(r_hd_binding)
        return True

    def unschedule_router_from_hosting_device(self, context, r_hd_binding):
        LOG.info(_('Attempting to un-schedule router %s.'),
                 r_hd_binding['router']['id'])
        if r_hd_binding['hosting_device'] is None:
            return False
        scheduler = self._get_router_type_scheduler(
            context, r_hd_binding['router_type_id'])
        if scheduler is None:
            return False
        result = scheduler.unschedule_router_from_hosting_device(
            self, context, r_hd_binding)
        if result:
            self._dev_mgr.release_hosting_device_slots(
                context, r_hd_binding['hosting_device'],
                r_hd_binding['router'],
                r_hd_binding['router_type']['slot_need'])
            LOG.info(_('Succesfully un-scheduled router %(r_id)s from '
                       'hosting device %(d_id)s'),
                     {'r_id': r_hd_binding['router']['id'],
                      'd_id': r_hd_binding.hosting_device_id})

    def get_router_type_id(self, context, router_id):
        r_hd_b = self._get_router_binding_info(context, router_id,
                                               load_hd_info=False)
        return r_hd_b['router_type_id']

    def get_router_type(self, context, id_or_name):
        query = context.session.query(RouterType)
        query = query.filter(RouterType.id == id_or_name)
        try:
            return query.one()
        except exc.MultipleResultsFound:
            with excutils.save_and_reraise_exception():
                LOG.error(_('Database inconsistency: Multiple router types '
                            'with same id %s'), id_or_name)
                raise RouterTypeNotFound(router_type=id_or_name)
        except exc.NoResultFound:
            query = context.session.query(RouterType)
            query = query.filter(RouterType.name == id_or_name)
            try:
                return query.one()
            except exc.MultipleResultsFound:
                with excutils.save_and_reraise_exception():
                    LOG.debug(_('Multiple router types with name %s found. '
                                'Id must be specified to allow arbitration.'),
                              id_or_name)
                    raise MultipleRouterTypes(name=id_or_name)
            except exc.NoResultFound:
                with excutils.save_and_reraise_exception():
                    LOG.error(_('No router type with name %s found.'),
                              id_or_name)
                    raise RouterTypeNotFound(router_type=id_or_name)

    def get_namespace_router_type_id(self, context):
        if self._namespace_router_type_id is None:
            try:
                self._namespace_router_type_id = self.get_router_type(
                    context, cfg.CONF.namespace_router_type_name)['id']
            except n_exc.NeutronException:
                return None
        return self._namespace_router_type_id

    @lockutils.synchronized('routers', 'neutron-')
    def backlog_router(self, router):
        if ((router or {}).get('id') is None or
                router['id'] in self._backlogged_routers):
            return
        LOG.info(_('Backlogging router %s for renewed scheduling attempt '
                   'later'), id)
        self._backlogged_routers[router['id']] = router

    @lockutils.synchronized('routers', 'neutron-')
    def remove_router_from_backlog(self, id):
        self._backlogged_routers.pop(id, None)
        LOG.info(_('Router %s removed from backlog'), id)

    @lockutils.synchronized('routerbacklog', 'neutron-')
    def _process_backlogged_routers(self):
        if self._refresh_router_backlog:
            self._sync_router_backlog()
        if not self._backlogged_routers:
            return
        context = n_context.get_admin_context()
        scheduled_routers = []
        LOG.info(_('Processing router (scheduling) backlog'))
        # try to reschedule
        for r_id, router in self._backlogged_routers.items():
            self._add_type_and_hosting_device_info(context, router)
            if router['hosting_device']:
                # scheduling attempt succeeded
                scheduled_routers.append(router)
                self._backlogged_routers.pop(r_id, None)
        # notify cfg agents so the scheduled routers are instantiated
        if scheduled_routers:
            l3_router_rpc_api.L3JointAgentNotify.routers_updated(
                context, scheduled_routers)

    def _setup_backlog_handling(self):
        self._heartbeat = loopingcall.FixedIntervalLoopingCall(
            self._process_backlogged_routers)
        self._heartbeat.start(interval=cfg.CONF.backlog_processing_interval)

    def _sync_router_backlog(self):
        LOG.info(_('Synchronizing router (scheduling) backlog'))
        context = n_context.get_admin_context()
        type_to_exclude = self.get_namespace_router_type_id(context)
        query = context.session.query(RouterHostingDeviceBinding)
        query = query.options(joinedload('router'))
        query = query.filter(
            RouterHostingDeviceBinding.router_type_id != type_to_exclude,
            RouterHostingDeviceBinding.hosting_device_id == expr.null())
        for binding in query:
            router = self._make_router_dict(binding.router,
                                            process_extensions=False)
            self._backlogged_routers[binding.router_id] = router
        self._refresh_router_backlog = False

    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

    @property
    def _dev_mgr(self):
        return manager.NeutronManager.get_service_plugins().get(
            svc_constants.DEVICE_MANAGER)

    def _get_router_binding_info(self, context, id, load_hd_info=True):
        query = context.session.query(RouterHostingDeviceBinding)
        if load_hd_info:
            query = query.options(joinedload('hosting_device'))
        query = query.filter(RouterHostingDeviceBinding.router_id == id)
        try:
            r_hd_b = query.one()
            return r_hd_b
        except exc.NoResultFound:
            # This should not happen
            LOG.error(_('DB inconsistency: No type and hosting info associated'
                        ' with router %s'), id)
            raise RouterBindingInfoError(router_id=id)
        except exc.MultipleResultsFound:
            # This should not happen either
            LOG.error(_('DB inconsistency: Multiple type and hosting info'
                        ' associated with router %s'), id)
            raise RouterBindingInfoError(router_id=id)

    def _get_hosting_device_bindings(self, context, id, load_routers=False,
                                     load_hosting_device=False):
        query = context.session.query(RouterHostingDeviceBinding)
        if load_routers:
            query = query.options(joinedload('router'))
        if load_hosting_device:
            query = query.options(joinedload('hosting_device'))
        query = query.filter(
            RouterHostingDeviceBinding.hosting_device_id == id)
        return query.all()

    def _add_type_and_hosting_device_info(self, context, router,
                                          binding_info=None, schedule=True):
        """Adds type and hosting device information to a router."""
        try:
            if binding_info is None:
                binding_info = self._get_router_binding_info(context,
                                                             router['id'])
        except RouterBindingInfoError:
            LOG.error(_('DB inconsistency: No hosting info associated with '
                        'router %s'), router['id'])
            return
        router['router_type_id'] = binding_info['router_type_id']
        router['share_host'] = binding_info['share_hosting_device']
        if binding_info.router_type_id == self.get_namespace_router_type_id(
                context):
            router['hosting_device'] = None
            return
        if binding_info.hosting_device is None and schedule:
            # This router has not been scheduled to a hosting device
            # so we try to do it now.
            self.schedule_router_on_hosting_device(context, binding_info)
            context.session.expire(binding_info)
        if binding_info.hosting_device is None:
            router['hosting_device'] = None
        else:
            router['router_type'] = {
                'id': binding_info.router_type.id,
                'name': binding_info.router_type.name,
                'cfg_agent_driver': binding_info.router_type.cfg_agent_driver}
            hosting_device = binding_info.hosting_device
            template = binding_info.hosting_device.template
            router['hosting_device'] = {
                'id': hosting_device.id,
                'name': hosting_device.name,
                'template_id': template.id,
                'host_category': template.host_category,
                'service_types': template.service_types,
                'management_ip_address': hosting_device.management_port[
                    'fixed_ips'][0]['ip_address'],
                'protocol_port': hosting_device.protocol_port,
                'created_at': str(hosting_device.created_at),
                'booting_time': template.booting_time}

    def _add_hosting_port_info(self, context, router, plugging_driver):
        """Adds hosting port information to router ports."""
        # We only populate hosting port info, i.e., reach here, if the
        # router has been scheduled to a hosting device. Hence this
        # a good place to allocate hosting ports to the router ports.
        # cache of hosting port information: {mac_addr: {'name': port_name}}
        hosting_pdata = {}
        if router['external_gateway_info'] is not None:
            h_info, did_allocation = self._populate_hosting_info_for_port(
                context, router['id'], router['gw_port'],
                router['hosting_device'], hosting_pdata, plugging_driver)
        for itfc in router.get(l3_constants.INTERFACE_KEY, []):
            h_info, did_allocation = self._populate_hosting_info_for_port(
                context, router['id'], itfc, router['hosting_device'],
                hosting_pdata, plugging_driver)

    def _populate_hosting_info_for_port(self, context, router_id, port,
                                        hosting_device, hosting_pdata,
                                        plugging_driver):
        port_db = self._core_plugin._get_port(context, port['id'])
        h_info = port_db.hosting_info
        new_allocation = False
        if h_info is None:
            # The port does not yet have a hosting port so allocate one now
            h_info = self._allocate_hosting_port(
                context, router_id, port_db, hosting_device['id'],
                plugging_driver)
            if h_info is None:
                # This should not happen but just in case ...
                port['hosting_info'] = None
                return None, new_allocation
            else:
                new_allocation = True
        if hosting_pdata.get('mac') is None:
            p_data = self._core_plugin.get_port(
                context, h_info.hosting_port_id, ['mac_address', 'name'])
            hosting_pdata['mac'] = p_data['mac_address']
            hosting_pdata['name'] = p_data['name']
        # Including MAC address of hosting port so L3CfgAgent can easily
        # determine which VM VIF to configure VLAN sub-interface on.
        port['hosting_info'] = {'hosting_port_id': h_info.hosting_port_id,
                                'hosting_mac': hosting_pdata.get('mac'),
                                'hosting_port_name': hosting_pdata.get('name')}
        plugging_driver.extend_hosting_port_info(
            context, port_db, port['hosting_info'])
        return h_info, new_allocation

    def _allocate_hosting_port(self, context, router_id, port_db,
                               hosting_device_id, plugging_driver):
        net_data = self._core_plugin.get_network(
            context, port_db['network_id'], [pr_net.NETWORK_TYPE])
        network_type = net_data.get(pr_net.NETWORK_TYPE)
        alloc = plugging_driver.allocate_hosting_port(
            context, router_id, port_db, network_type, hosting_device_id)
        if alloc is None:
            LOG.error(_('Failed to allocate hosting port for port %s'),
                      port_db['id'])
            return
        with context.session.begin(subtransactions=True):
            h_info = HostedHostingPortBinding(
                logical_resource_id=router_id,
                logical_port_id=port_db['id'],
                network_type=network_type,
                hosting_port_id=alloc['allocated_port_id'],
                segmentation_tag=alloc['allocated_vlan'])
            context.session.add(h_info)
            context.session.expire(port_db)
        # allocation succeeded so establish connectivity for logical port
        context.session.expire(h_info)
        plugging_driver.setup_logical_port_connectivity(context, port_db)
        return h_info

    def _get_router_port_db_on_subnet(self, context, router_id, subnet):
        try:
            rport_qry = context.session.query(models_v2.Port)
            ports = rport_qry.filter_by(
                device_id=router_id,
                device_owner=l3_db.DEVICE_OWNER_ROUTER_INTF,
                network_id=subnet['network_id'])
            for p in ports:
                if p['fixed_ips'][0]['subnet_id'] == subnet['id']:
                    return p
        except exc.NoResultFound:
            return

    def _get_router_type_scheduler(self, context, id):
        """Returns the scheduler (instance) for a router type."""
        if id is None:
            return
        try:
            return self._router_schedulers[id]
        except KeyError:
            try:
                router_type = self.get_router_type(context, id)
                self._router_schedulers[id] = importutils.import_object(
                    router_type['scheduler'])
            except (ImportError, TypeError, n_exc.NeutronException):
                LOG.exception(_("Error loading scheduler for router type %s"),
                              id)
            return self._router_schedulers.get(id)
