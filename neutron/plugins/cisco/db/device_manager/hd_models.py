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

import sqlalchemy as sa
from sqlalchemy import orm

from neutron.db import agents_db
from neutron.db import model_base
from neutron.db import models_v2


class HostingDeviceTemplate(model_base.BASEV2, models_v2.HasId,
                            models_v2.HasTenant):
    """Represents a template for devices used to host service.

       Such devices may be physical or virtual.
    """
    # name given to hosting devices created using this template
    name = sa.Column(sa.String(255))
    # template enabled if True
    enabled = sa.Column(sa.Boolean, nullable=False, default=True)
    # 'host_category' can be 'VM', 'Hardware', 'NetworkNode'
    host_category = sa.Column(sa.String(255), nullable=False)
    # list of service types hosting devices based on this template support
    service_types = sa.Column(sa.String(255))
    # the image name or uuid in Glance
    image = sa.Column(sa.String(255))
    # the VM flavor or uuid in Nova
    flavor = sa.Column(sa.String(255))
    # id of default credentials (if any) for devices created from this template
    default_credentials_id = sa.Column(sa.String(36),
                                       sa.ForeignKey('devicecredentials.id'))
    # 'configuration_mechanism' indicates how configurations are made
    configuration_mechanism = sa.Column(sa.String(255))
    # 'protocol_port' is udp/tcp port of hosting device. May be empty.
    protocol_port = sa.Column(sa.Integer)
    # Typical time (in seconds) needed for hosting device (created
    # from this template) to boot into operational state.
    booting_time = sa.Column(sa.Integer, default=0)
    # abstract metric specifying capacity to host logical resources
    slot_capacity = sa.Column(sa.Integer, nullable=False, autoincrement=False)
    # desired number of slots to keep available at all times
    desired_slots_free = sa.Column(sa.Integer, nullable=False, default=0,
                                   autoincrement=False)
    # 'tenant_bound' is a (possibly empty) string of ':'-separated tenant ids
    # representing the only tenants allowed to own/place resources on
    # hosting devices created using this template. If string is empty all
    # tenants are allowed.
    tenant_bound = sa.Column(sa.String(512))
    # module to be used as plugging driver for logical resources
    # hosted inside hosting devices created using this template
    device_driver = sa.Column(sa.String(255), nullable=False)
    # module to be used as hosting device driver when creating
    # hosting devices using his template
    plugging_driver = sa.Column(sa.String(255), nullable=False)


class DeviceCredential(model_base.BASEV2, models_v2.HasId):
    """Represents credentials to control Cisco devices."""

    name = sa.Column(sa.String(255))
    description = sa.Column(sa.String(255))
    user_name = sa.Column(sa.String(255))
    password = sa.Column(sa.String(255))
    type = sa.Column(sa.String(255))


class HostingDevice(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Represents an appliance hosting Neutron router(s).

       When the hosting device is a Nova VM 'id' is uuid of that VM.
    """
    # id of hosting device template used to create the hosting device
    template_id = sa.Column(sa.String(36),
                            sa.ForeignKey('hostingdevicetemplates.id'),
                            nullable=False)
    template = orm.relationship(HostingDeviceTemplate)
    # id of credentials for this hosting device
    credentials_id = sa.Column(sa.String(36),
                               sa.ForeignKey('devicecredentials.id'))
    credentials = orm.relationship(DeviceCredential)
    # manufacturer id of the device, e.g., its serial number
    device_id = sa.Column(sa.String(255))
    admin_state_up = sa.Column(sa.Boolean, nullable=False, default=True)
    # 'management_port_id' is the Neutron Port used for management interface
    management_port_id = sa.Column(sa.String(36),
                                   sa.ForeignKey('ports.id',
                                                 ondelete="SET NULL"))
    management_port = orm.relationship(models_v2.Port)
    # 'protocol_port' is udp/tcp port of hosting device. May be empty.
    protocol_port = sa.Column(sa.Integer)
    cfg_agent_id = sa.Column(sa.String(36),
                             sa.ForeignKey('agents.id'),
                             nullable=True)
    cfg_agent = orm.relationship(agents_db.Agent)
    # Service VMs take time to boot so we store creation time
    # so we can give preference to older ones when scheduling
    created_at = sa.Column(sa.DateTime, nullable=False)
    status = sa.Column(sa.String(16))
    # 'tenant_bound' is empty or is id of the only tenant allowed to
    # own/place resources on this hosting device
    tenant_bound = sa.Column(sa.String(36))
    # If 'auto_delete' is True, a VM-based hosting device is subject to
    # deletion as part of hosting device pool management and in case of VM
    # failures. If 'auto_delete' is set to False, the hosting device must be
    # manually unregistered in the device manager and deleted in Nova.
    auto_delete = sa.Column(sa.Boolean, default=False, nullable=False)


class SlotAllocation(model_base.BASEV2):
    """Tracks allocation of slots in hosting devices."""
    template_id = sa.Column(sa.String(36),
                            sa.ForeignKey('hostingdevicetemplates.id'),
                            nullable=False)
    hosting_device_id = sa.Column(sa.String(36),
                                  sa.ForeignKey('hostingdevices.id',
                                                ondelete='CASCADE'),
                                  nullable=False)
    logical_resource_id = sa.Column(sa.String(36), primary_key=True,
                                    nullable=False)
    # id of tenant owning logical resource
    logical_resource_owner = sa.Column(sa.String(36), nullable=False)
    num_allocated = sa.Column(sa.Integer, autoincrement=False, nullable=False)
    tenant_bound = sa.Column(sa.String(36))


class HostedHostingPortBinding(model_base.BASEV2):
    """Represents binding of logical resource's port to its hosting port."""
    logical_resource_id = sa.Column(sa.String(36), primary_key=True)
    logical_port_id = sa.Column(sa.String(36),
                                sa.ForeignKey('ports.id',
                                              ondelete="CASCADE"),
                                primary_key=True)
    logical_port = orm.relationship(
        models_v2.Port,
        primaryjoin='Port.id==HostedHostingPortBinding.logical_port_id',
        backref=orm.backref('hosting_info', cascade='all', uselist=False))
    # type of router port: router_interface, ..._gateway, ..._floatingip
    port_type = sa.Column(sa.String(32))
    # type of network the router port belongs to
    network_type = sa.Column(sa.String(32))
    hosting_port_id = sa.Column(sa.String(36),
                                sa.ForeignKey('ports.id',
                                              ondelete='SET NULL'))
    hosting_port = orm.relationship(
        models_v2.Port,
        primaryjoin='Port.id==HostedHostingPortBinding.hosting_port_id')
    # VLAN tag for trunk ports
    segmentation_tag = sa.Column(sa.Integer, autoincrement=False)
