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

from datetime import timedelta
from operator import itemgetter
from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.sql import expression as expr

from neutron.openstack.common import log as logging
from neutron.plugins.cisco.db.device_manager.hd_models import HostingDevice
from neutron.plugins.cisco.db.device_manager.hd_models import SlotAllocation

LOG = logging.getLogger(__name__)


# Maximum allowed minute difference in creation time
# for hosting devices to be considered equal
EQUIVALENCE_TIME_DIFF = 7


class L3RouterHostingDeviceScheduler(object):
    """Slot-aware scheduler of Neutron routers on hosting devices."""

    def schedule_router(self, plugin, context, r_hd_binding):
        """Schedules a Neutron router on a hosting device.

        Selection criteria: The longest running hosting device that...
            ... is based on the template required by router's type
            AND
            ... is administratively up
            AND
            ... is bound to tenant owning router OR is unbound
            AND
            ... has enough slots available to host the router

            Hosting devices with creation date/time less than
            EQUIVALENCE_TIME_DIFF are considered equally old.

            Among hosting devices meeting these criteria and
            that are of same age the device with less allocated
            slots is preferred.
        """
        # SELECT hosting_device_id, created_at, sum(num_allocated)
        # FROM hostingdevices AS hd
        # LEFT OUTER JOIN slotallocations AS sa ON hd.id=sa.hosting_device_id
        # WHERE
        #    hd.template_id='11111111-2222-3333-4444-555555555555' AND
        #    hd.admin_state_up=TRUE AND
        # <<<sharing case:>>>
        #    (hd.tenant_bound IS NULL OR hd.tenant_bound='t10')
        # <<<non-sharing case:>>>
        #    (sa.tenant_bound='t10' OR
        #     (sa.tenant_bound IS NULL AND sa.logical_resource_owner='t10') OR
        #     hd.tenant_bound='t10' OR
        #     (hd.tenant_bound IS NULL AND sa.hosting_device_id IS NULL))
        # GROUP BY hosting_device_id
        # HAVING sum(num_allocated) <= 8 OR sum(num_allocated) IS NULL
        # ORDER BY created_at;
        router = r_hd_binding['router']
        tenant_id = router['tenant_id']
        router_type = r_hd_binding['router_type']
        template_id = router_type['template_id']
        template = router_type['template']
        slot_threshold = template['slot_capacity'] - router_type['slot_need']

        query = context.session.query(HostingDevice.id,
                                      HostingDevice.created_at,
                                      func.sum(SlotAllocation.num_allocated))
        query = query.outerjoin(
            SlotAllocation,
            HostingDevice.id == SlotAllocation.hosting_device_id)
        query = query.filter(
            HostingDevice.template_id == template_id,
            HostingDevice.admin_state_up == expr.true())
        if r_hd_binding['share_hosting_device']:
            query = query.filter(
                or_(HostingDevice.tenant_bound == expr.null(),
                    HostingDevice.tenant_bound == tenant_id))
        else:
            query = query.filter(
                or_(SlotAllocation.tenant_bound == tenant_id,
                    and_(SlotAllocation.tenant_bound == expr.null(),
                         SlotAllocation.logical_resource_owner == tenant_id),
                    HostingDevice.tenant_bound == tenant_id,
                    and_(HostingDevice.tenant_bound == expr.null(),
                         SlotAllocation.hosting_device_id == expr.null())))
        query = query.group_by(HostingDevice.id)
        query = query.having(
            or_(func.sum(SlotAllocation.num_allocated) <= slot_threshold,
                func.sum(SlotAllocation.num_allocated == expr.null())))
        query = query.order_by(HostingDevice.created_at)
        candidates = query.all()
        if len(candidates) == 0:
            # report unsuccessful scheduling
            return
        else:
            # determine oldest candidates considered equally old
            oldest_candidates = []
            minute_limit = timedelta(minutes=EQUIVALENCE_TIME_DIFF)
            for candidate in candidates:
                if candidate[1] - candidates[0][1] < minute_limit:
                    oldest_candidates.append(candidate)
                else:
                    # we're only interested in the longest running devices
                    break
            # sort on least number of used slots
            sorted_candidates = sorted(oldest_candidates, key=itemgetter(2))
            return sorted_candidates[0]

    def unschedule_router(self, plugin, context, r_hd_binding):
        return True
