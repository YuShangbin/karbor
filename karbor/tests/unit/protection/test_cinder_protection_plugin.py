# Licensed under the Apache License, Version 2.0 (the "License"); you may
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

from cinderclient import exceptions as cinder_exc
import collections
import datetime
from karbor.common import constants
from karbor.context import RequestContext
from karbor import exception
from karbor.resource import Resource
from karbor.services.protection import bank_plugin
from karbor.services.protection import client_factory
from karbor.services.protection.protection_plugins.volume. \
    cinder_protection_plugin import CinderBackupProtectionPlugin
from karbor.services.protection.protection_plugins.volume \
    import volume_plugin_cinder_schemas as cinder_schemas
from karbor.services.protection.restore_heat import HeatTemplate
from karbor.tests import base
from karbor.tests.unit.protection import fakes
import mock
from oslo_config import cfg


ResourceNode = collections.namedtuple(
    "ResourceNode",
    ["value",
     "child_nodes"]
)

Image = collections.namedtuple(
    "Image",
    ["disk_format",
     "container_format",
     "status"]
)


def call_hooks(operation, checkpoint, resource, context, parameters, **kwargs):
    def noop(*args, **kwargs):
        pass

    hooks = (
        'on_prepare_begin',
        'on_prepare_finish',
        'on_main',
        'on_complete',
    )
    for hook_name in hooks:
        hook = getattr(operation, hook_name, noop)
        hook(checkpoint, resource, context, parameters, **kwargs)


class FakeCheckpoint(object):
    def __init__(self, section):
        self.bank_section = section
        self.id = "fake_id"

    def get_resource_bank_section(self, resource_id=None):
        return self.bank_section


class BackupResponse(object):
    def __init__(self, bkup_id, final_status, working_status, time_to_work):
        self._final_status = final_status
        self._working_status = working_status
        self._time_to_work = time_to_work
        self._id = bkup_id

    def __call__(self, *args, **kwargs):
        res = mock.Mock()
        res.id = self._id
        if self._time_to_work > 0:
            self._time_to_work -= 1
            res.status = self._working_status
        else:
            res.status = self._final_status
        if res.status == 'not-found':
            raise cinder_exc.NotFound(403)
        return res


class CinderProtectionPluginTest(base.TestCase):
    def setUp(self):
        super(CinderProtectionPluginTest, self).setUp()
        cls = client_factory.karbor_keystone_plugin.KarborKeystonePlugin
        with mock.patch.object(cls, '_do_init'):
            client_factory.init()

        self.plugin = CinderBackupProtectionPlugin()
        cfg.CONF.set_default('cinder_endpoint',
                             'http://127.0.0.1:8776/v2',
                             'cinder_client')

        self.cntxt = RequestContext(user_id='admin',
                                    project_id='abcd',
                                    auth_token='efgh')
        self.cinder_client = client_factory.ClientFactory.create_client(
            "cinder", self.cntxt)

    def _get_checkpoint(self):
        fake_bank = bank_plugin.Bank(fakes.FakeBankPlugin())
        fake_bank_section = bank_plugin.BankSection(
            bank=fake_bank,
            prefix="fake"
        )
        return FakeCheckpoint(fake_bank_section)

    def test_get_options_schema(self):
        options_schema = self.plugin.get_options_schema(
            'OS::Cinder::Volume')
        self.assertEqual(options_schema, cinder_schemas.OPTIONS_SCHEMA)

    def test_get_restore_schema(self):
        options_schema = self.plugin.get_restore_schema(
            'OS::Cinder::Volume')
        self.assertEqual(options_schema, cinder_schemas.RESTORE_SCHEMA)

    def test_get_saved_info_schema(self):
        options_schema = self.plugin.get_saved_info_schema(
            'OS::Cinder::Volume')
        self.assertEqual(options_schema,
                         cinder_schemas.SAVED_INFO_SCHEMA)

    @mock.patch('karbor.services.protection.clients.cinder.create')
    def test_protect_succeed(self, mock_cinder_create):
        resource = Resource(
            id="123",
            type=constants.VOLUME_RESOURCE_TYPE,
            name="test",
        )
        checkpoint = self._get_checkpoint()
        section = checkpoint.get_resource_bank_section()
        operation = self.plugin.get_protect_operation(resource)
        section.create_object = mock.MagicMock()
        mock_cinder_create.return_value = self.cinder_client
        with mock.patch.multiple(
            self.cinder_client,
            volumes=mock.DEFAULT,
            backups=mock.DEFAULT
        ) as mocks:
            mocks['volumes'].get.return_value = mock.Mock()
            mocks['volumes'].get.return_value.status = 'available'
            mocks['backups'].create = BackupResponse(
                '456', 'creating', '---', 0)
            mocks['backups'].get = BackupResponse(
                '456', 'available', 'creating', 2)
            call_hooks(operation, checkpoint, resource, self.cntxt, {})

    @mock.patch('karbor.services.protection.clients.cinder.create')
    def test_protect_fail_backup(self, mock_cinder_create):
        resource = Resource(
            id="123",
            type=constants.VOLUME_RESOURCE_TYPE,
            name="test",
        )
        checkpoint = self._get_checkpoint()
        operation = self.plugin.get_protect_operation(resource)
        mock_cinder_create.return_value = self.cinder_client
        with mock.patch.multiple(
            self.cinder_client,
            volumes=mock.DEFAULT,
            backups=mock.DEFAULT
        ) as mocks:
            mocks['volumes'].get.return_value = mock.Mock()
            mocks['volumes'].get.return_value.status = 'available'
            mocks['backups'].backups.create = BackupResponse(
                '456', 'creating', '---', 0)
            mocks['backups'].backups.get = BackupResponse(
                '456', 'error', 'creating', 2)
            self.assertRaises(
                exception.CreateBackupFailed,
                call_hooks,
                operation,
                checkpoint,
                resource,
                self.cntxt,
                {}
            )

    @mock.patch('karbor.services.protection.clients.cinder.create')
    def test_protect_fail_volume(self, mock_cinder_create):
        resource = Resource(
            id="123",
            type=constants.VOLUME_RESOURCE_TYPE,
            name="test",
        )
        checkpoint = self._get_checkpoint()
        operation = self.plugin.get_protect_operation(resource)
        mock_cinder_create.return_value = self.cinder_client
        with mock.patch.multiple(
            self.cinder_client,
            volumes=mock.DEFAULT,
            backups=mock.DEFAULT
        ) as mocks:
            mocks['volumes'].get.return_value = mock.Mock()
            mocks['volumes'].get.return_value.status = 'error'
            mocks['backups'].backups.create = BackupResponse(
                '456', 'creating', '---', 0)
            mocks['backups'].backups.get = BackupResponse(
                '456', 'error', 'creating', 2)
            self.assertRaises(
                exception.CreateBackupFailed,
                call_hooks,
                operation,
                checkpoint,
                resource,
                self.cntxt,
                {}
            )

    @mock.patch('karbor.services.protection.clients.cinder.create')
    def test_delete_succeed(self, mock_cinder_create):
        resource = Resource(
            id="123",
            type=constants.VOLUME_RESOURCE_TYPE,
            name="test",
        )
        checkpoint = self._get_checkpoint()
        section = checkpoint.get_resource_bank_section()
        section.create_object('metadata', {
            'backup_id': '456',
        })
        operation = self.plugin.get_delete_operation(resource)
        mock_cinder_create.return_value = self.cinder_client
        with mock.patch.object(self.cinder_client, 'backups') as backups:
            backups.delete = BackupResponse('456', 'deleting', '---', 0)
            backups.get = BackupResponse('456', 'not-found', 'deleting', 2)
            call_hooks(operation, checkpoint, resource, self.cntxt, {})

    @mock.patch('karbor.services.protection.clients.cinder.create')
    def test_delete_fail(self, mock_cinder_create):
        resource = Resource(
            id="123",
            type=constants.VOLUME_RESOURCE_TYPE,
            name="test",
        )
        checkpoint = self._get_checkpoint()
        section = checkpoint.get_resource_bank_section()
        section.create_object('metadata', {
            'backup_id': '456',
        })
        operation = self.plugin.get_delete_operation(resource)
        mock_cinder_create.return_value = self.cinder_client
        with mock.patch.object(self.cinder_client, 'backups') as backups:
            backups.delete = BackupResponse('456', 'deleting', '---', 0)
            backups.get = BackupResponse('456', 'error', 'deleting', 2)
            self.assertRaises(
                exception.DeleteBackupFailed,
                call_hooks,
                operation,
                checkpoint,
                resource,
                self.cntxt,
                {}
            )

    def test_restore(self):
        heat_template = HeatTemplate()
        resource = Resource(
            id="123",
            type=constants.VOLUME_RESOURCE_TYPE,
            name="fake",
        )
        checkpoint = self._get_checkpoint()
        section = checkpoint.get_resource_bank_section()
        section.create_object('metadata', {
            'backup_id': '456',
        })

        parameters = {
            "restore_name": "karbor restore volume",
            "restore_description": "karbor restore",
        }

        operation = self.plugin.get_restore_operation(resource)
        call_hooks(operation, checkpoint, resource, self.cntxt,
                   parameters, heat_template=heat_template)
        self.assertEqual(1, len(heat_template._resources))

        heat_resource_id = heat_template._original_id_resource_map["123"]
        template_dict = {
            "heat_template_version": str(datetime.date(2015, 10, 15)),
            "description": "karbor restore template",
            "resources": {
                heat_resource_id: {
                    "type": "OS::Cinder::Volume",
                    "properties": {
                        "description": "karbor restore",
                        "backup_id": "456",
                        "name": "karbor restore volume",
                    }
                }
            }
        }
        self.assertEqual(template_dict, heat_template.to_dict())

    def test_get_supported_resources_types(self):
        types = self.plugin.get_supported_resources_types()
        self.assertEqual(types,
                         [constants.VOLUME_RESOURCE_TYPE])
