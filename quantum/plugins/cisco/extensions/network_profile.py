# Copyright (c) 2012 Cisco Systems, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# @author: Abhishek Raut, Cisco Systems, Inc.
# @author: Sergey Sudakovich, Cisco Systems, Inc.

from quantum.api.v2 import attributes
from quantum.api.v2 import base
from quantum.api import extensions
from quantum import manager

RESOURCE_NAME = "network_profile"
COLLECTION_NAME = "%ss" % RESOURCE_NAME
EXT_ALIAS = RESOURCE_NAME


# Attribute Map
RESOURCE_ATTRIBUTE_MAP = {
    COLLECTION_NAME: {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:regex': attributes.UUID_PATTERN},
               'is_visible': True},
        'name': {'allow_post': True, 'allow_put': True,
                 'is_visible': True, 'default': ''},
        'segment_type': {'allow_post': True, 'allow_put': True,
                         'is_visible': True, 'default': ''},
        'segment_range': {'allow_post': True, 'allow_put': True,
                          'is_visible': True, 'default': ''},
        'multicast_ip_range': {'allow_post': True, 'allow_put': True,
                               'is_visible': True, 'default': '0.0.0.0'},
        'multicast_ip_index': {'allow_post': False, 'allow_put': False,
                               'is_visible': False, 'default': '0'},
        'physical_network': {'allow_post': True, 'allow_put': True,
                             'is_visible': True, 'default': ''},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'is_visible': False, 'default': ''},
        'add_tenant': {'allow_post': True, 'allow_put': True,
                       'is_visible': True, 'default': None},
        'remove_tenant': {'allow_post': True, 'allow_put': True,
                          'is_visible': True, 'default': None},
    },
}


class Network_profile(extensions.ExtensionDescriptor):

    @classmethod
    def get_name(cls):
        return "Cisco N1kv Network Profiles"

    @classmethod
    def get_alias(cls):
        return EXT_ALIAS

    @classmethod
    def get_description(cls):
        return ("Profile includes the type of profile for N1kv")

    @classmethod
    def get_namespace(cls):
        return "http://docs.openstack.org/ext/n1kv/network-profile/api/v2.0"

    @classmethod
    def get_updated(cls):
        return "2012-07-20T10:00:00-00:00"

    @classmethod
    def get_resources(cls):
        """ Returns Ext Resources """
        controller = base.create_resource(
            COLLECTION_NAME,
            RESOURCE_NAME,
            manager.QuantumManager.get_plugin(),
            RESOURCE_ATTRIBUTE_MAP.get(COLLECTION_NAME))
        return [extensions.ResourceExtension(COLLECTION_NAME, controller)]
