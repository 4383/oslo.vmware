# Copyright (c) 2014 VMware, Inc.
# All Rights Reserved.
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

"""
Unit tests for functions and classes for image transfer.
"""

import math

from eventlet import greenthread
import mock

from oslo.vmware import exceptions
from oslo.vmware import image_transfer
from oslo.vmware import rw_handles
from tests import base


class BlockingQueueTest(base.TestCase):
    """Tests for BlockingQueue."""

    def test_read(self):
        max_size = 10
        chunk_size = 10
        max_transfer_size = 30
        queue = image_transfer.BlockingQueue(max_size, max_transfer_size)

        def get_side_effect():
            return [1] * chunk_size

        queue.get = mock.Mock(side_effect=get_side_effect)
        while True:
            data_item = queue.read(chunk_size)
            if not data_item:
                break

        self.assertEqual(max_transfer_size, queue._transferred)
        exp_calls = [mock.call()] * int(math.ceil(float(max_transfer_size) /
                                                  chunk_size))
        self.assertEqual(exp_calls, queue.get.call_args_list)

    def test_write(self):
        queue = image_transfer.BlockingQueue(10, 30)
        queue.put = mock.Mock()
        write_count = 10
        for _ in range(0, write_count):
            queue.write([1])
        exp_calls = [mock.call([1])] * write_count
        self.assertEqual(exp_calls, queue.put.call_args_list)

    def test_tell(self):
        max_transfer_size = 30
        queue = image_transfer.BlockingQueue(10, 30)
        self.assertEqual(max_transfer_size, queue.tell())


class ImageWriterTest(base.TestCase):
    """Tests for ImageWriter class."""

    def _create_image_writer(self):
        self.image_service = mock.Mock()
        self.context = mock.Mock()
        self.input_file = mock.Mock()
        self.image_id = mock.Mock()
        return image_transfer.ImageWriter(self.context, self.input_file,
                                          self.image_service, self.image_id)

    @mock.patch.object(greenthread, 'sleep')
    def test_start(self, mock_sleep):
        writer = self._create_image_writer()
        status_list = ['queued', 'saving', 'active']

        def image_service_show_side_effect(context, image_id):
            status = status_list.pop(0)
            return {'status': status}

        self.image_service.show.side_effect = image_service_show_side_effect
        exp_calls = [mock.call(self.context, self.image_id)] * len(status_list)
        writer.start()
        self.assertTrue(writer.wait())
        self.image_service.update.assert_called_once_with(self.context,
                                                          self.image_id, {},
                                                          data=self.input_file)
        self.assertEqual(exp_calls, self.image_service.show.call_args_list)

    def test_start_with_killed_status(self):
        writer = self._create_image_writer()

        def image_service_show_side_effect(_context, _image_id):
            return {'status': 'killed'}

        self.image_service.show.side_effect = image_service_show_side_effect
        writer.start()
        self.assertRaises(exceptions.ImageTransferException,
                          writer.wait)
        self.image_service.update.assert_called_once_with(self.context,
                                                          self.image_id, {},
                                                          data=self.input_file)
        self.image_service.show.assert_called_once_with(self.context,
                                                        self.image_id)

    def test_start_with_unknown_status(self):
        writer = self._create_image_writer()

        def image_service_show_side_effect(_context, _image_id):
            return {'status': 'unknown'}

        self.image_service.show.side_effect = image_service_show_side_effect
        writer.start()
        self.assertRaises(exceptions.ImageTransferException,
                          writer.wait)
        self.image_service.update.assert_called_once_with(self.context,
                                                          self.image_id, {},
                                                          data=self.input_file)
        self.image_service.show.assert_called_once_with(self.context,
                                                        self.image_id)

    def test_start_with_image_service_show_exception(self):
        writer = self._create_image_writer()
        self.image_service.show.side_effect = RuntimeError()
        writer.start()
        self.assertRaises(exceptions.ImageTransferException, writer.wait)
        self.image_service.update.assert_called_once_with(self.context,
                                                          self.image_id, {},
                                                          data=self.input_file)
        self.image_service.show.assert_called_once_with(self.context,
                                                        self.image_id)


class FileReadWriteTaskTest(base.TestCase):
    """Tests for FileReadWriteTask class."""

    def test_start(self):
        data_items = [[1] * 10, [1] * 20, [1] * 5, []]

        def input_file_read_side_effect(arg):
            self.assertEqual(arg, rw_handles.READ_CHUNKSIZE)
            data = data_items[input_file_read_side_effect.i]
            input_file_read_side_effect.i += 1
            return data

        input_file_read_side_effect.i = 0
        input_file = mock.Mock()
        input_file.read.side_effect = input_file_read_side_effect
        output_file = mock.Mock()
        rw_task = image_transfer.FileReadWriteTask(input_file, output_file)
        rw_task.start()
        self.assertTrue(rw_task.wait())
        self.assertEqual(len(data_items), input_file.read.call_count)

        exp_calls = []
        for i in range(0, len(data_items)):
            exp_calls.append(mock.call(data_items[i]))
        self.assertEqual(exp_calls, output_file.write.call_args_list)

        self.assertEqual(len(data_items),
                         input_file.update_progress.call_count)
        self.assertEqual(len(data_items),
                         output_file.update_progress.call_count)

    def test_start_with_read_exception(self):
        input_file = mock.Mock()
        input_file.read.side_effect = RuntimeError()
        output_file = mock.Mock()
        rw_task = image_transfer.FileReadWriteTask(input_file, output_file)
        rw_task.start()
        self.assertRaises(exceptions.ImageTransferException, rw_task.wait)
        input_file.read.assert_called_once_with(rw_handles.READ_CHUNKSIZE)


class ImageTransferUtilityTest(base.TestCase):
    """Tests for image_transfer utility methods."""

    @mock.patch('oslo.vmware.rw_handles.FileWriteHandle')
    @mock.patch('oslo.vmware.rw_handles.ImageReadHandle')
    @mock.patch.object(image_transfer, '_start_transfer')
    def test_download_flat_image(
            self,
            fake_transfer,
            fake_rw_handles_ImageReadHandle,
            fake_rw_handles_FileWriteHandle):

        context = mock.Mock()
        image_id = mock.Mock()
        image_service = mock.Mock()
        image_service.download = mock.Mock()
        image_service.download.return_value = 'fake_iter'

        fake_ImageReadHandle = 'fake_ImageReadHandle'
        fake_FileWriteHandle = 'fake_FileWriteHandle'
        cookies = []
        timeout_secs = 10
        image_size = 1000
        host = '127.0.0.1'
        dc_path = 'dc1'
        ds_name = 'ds1'
        file_path = '/fake_path'

        fake_rw_handles_ImageReadHandle.return_value = fake_ImageReadHandle
        fake_rw_handles_FileWriteHandle.return_value = fake_FileWriteHandle

        image_transfer.download_flat_image(
            context,
            timeout_secs,
            image_service,
            image_id,
            image_size=image_size,
            host=host,
            data_center_name=dc_path,
            datastore_name=ds_name,
            cookies=cookies,
            file_path=file_path)

        image_service.download.assert_called_once_with(context, image_id)

        fake_rw_handles_ImageReadHandle.assert_called_once_with('fake_iter')

        fake_rw_handles_FileWriteHandle.assert_called_once_with(
            host,
            dc_path,
            ds_name,
            cookies,
            file_path,
            image_size)

        fake_transfer.assert_called_once_with(
            context,
            timeout_secs,
            fake_ImageReadHandle,
            image_size,
            write_file_handle=fake_FileWriteHandle)