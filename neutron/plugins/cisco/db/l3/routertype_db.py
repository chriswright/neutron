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
# @author: Hareesh Puthalath, Cisco Systems, Inc.

from sqlalchemy.orm import exc

from neutron.db import db_base_plugin_v2 as base_db
from neutron.openstack.common import log as logging
from neutron.openstack.common import uuidutils
from neutron.plugins.cisco.db.l3.l3_models import RouterType
import neutron.plugins.cisco.extensions.routertype as routertype

LOG = logging.getLogger(__name__)


class RoutertypeDbMixin(routertype.RoutertypePluginBase,
                        base_db.CommonDbMixin):
    """Mixin class for Router types."""

    def create_routertype(self, context, routertype):
        """Creates a router type.

        Also binds it to the specified hosting device template.
        """
        LOG.debug("create_routertype() called. Contents %s", routertype)
        r = routertype['routertype']
        with context.session.begin(subtransactions=True):
            routertype_db = RouterType(id=uuidutils.generate_uuid(),
                                       name=r['name'],
                                       description=r['description'],
                                       template_id=r['template_id'],
                                       slot_need=r['slot_need'],
                                       scheduler=r['scheduler'],
                                       cfg_agent_driver=r['cfg_agent_driver'])
            context.session.add(routertype_db)
        return self._make_routertype_dict(routertype_db)

    def update_routertype(self, context, id, routertype):
        LOG.debug(_("update_routertype() called"))
        rt = routertype['routertype']
        with context.session.begin(subtransactions=True):
            rt_query = context.session.query(
                RouterType).with_lockmode('update')
            rt_db = rt_query.filter_by(id=id).one()
            rt_db.update(rt)
        return self._make_routertype_dict(rt_db)

    def delete_routertype(self, context, id):
        LOG.debug(_("delete_routertype() called"))
        with context.session.begin(subtransactions=True):
            routertype_query = context.session.query(
                RouterType).with_lockmode('update')
            routertype_db = routertype_query.filter_by(id=id).one()
            context.session.delete(routertype_db)

    def get_routertype(self, context, id, fields=None):
        LOG.debug(_("get_routertype() called"))
        try:
            query = self._model_query(context, RouterType)
            rt = query.filter(RouterType.id == id).one()
            return self._make_routertype_dict(rt, fields)
        except exc.NoResultFound:
            raise routertype.RouterTypeNotFound(routertype_id=id)

    def get_routertypes(self, context, filters=None, fields=None,
                        sorts=None, limit=None, marker=None,
                        page_reverse=False):
        LOG.debug(_("get_routertypes() called"))
        return self._get_collection(context, RouterType,
                                    self._make_routertype_dict,
                                    filters=filters, fields=fields,
                                    sorts=sorts, limit=limit,
                                    marker_obj=marker,
                                    page_reverse=page_reverse)

    def _make_routertype_dict(self, routertype, fields=None):
        res = {'id': routertype['id'],
               'name': routertype['name'],
               'description': routertype['description'],
               'template_id': routertype['template_id'],
               'slot_need': routertype['slot_need'],
               'scheduler': routertype['scheduler'],
               'cfg_agent_driver': routertype['cfg_agent_driver']}
        return self._fields(res, fields)
