# -*- coding: utf-8 -*-
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from tempfile import NamedTemporaryFile

from airflow.contrib.hooks.gcs_hook import (GoogleCloudStorageHook,
                                            _parse_gcs_url)
from airflow.contrib.operators.s3_list_operator import S3ListOperator
from airflow.exceptions import AirflowException
from airflow.hooks.S3_hook import S3Hook
from airflow.utils.decorators import apply_defaults


class S3ToGoogleCloudStorageOperator(S3ListOperator):
    """
    Synchronizes an S3 key, possibly a prefix, with a Google Cloud Storage
    destination path.

    :param bucket: The S3 bucket where to find the objects. (templated)
    :type bucket: string
    :param prefix: Prefix string which filters objects whose name begin with
        such prefix. (templated)
    :type prefix: string
    :param delimiter: the delimiter marks key hierarchy. (templated)
    :type delimiter: string
    :param aws_conn_id: The source S3 connection
    :type aws_conn_id: string
    :param dest_gcs_conn_id: The destination connection ID to use
        when connecting to Google Cloud Storage.
    :type dest_gcs_conn_id: string
    :param dest_gcs: The destination Google Cloud Storage bucket and prefix
        where you want to store the files. (templated)
    :type dest_gcs: string
    :param delegate_to: The account to impersonate, if any.
        For this to work, the service account making the request must have
        domain-wide delegation enabled.
    :type delegate_to: string
    :param replace: Whether you want to replace existing destination files
        or not.
    :type replace: bool


    **Example**:
    .. code-block:: python
       s3_to_gcs_op = S3ToGoogleCloudStorageOperator(
            task_id='s3_to_gcs_example',
            bucket='my-s3-bucket',
            prefix='data/customers-201804',
            dest_gcs_conn_id='google_cloud_default',
            dest_gcs='gs://my.gcs.bucket/some/customers/',
            replace=False,
            dag=my-dag)

    Note that ``bucket``, ``prefix``, ``delimiter`` and ``dest_gcs`` are
    templated, so you can use variables in them if you wish.
    """

    template_fields = ('bucket', 'prefix', 'delimiter', 'dest_gcs')
    ui_color = '#e09411'

    @apply_defaults
    def __init__(self,
                 bucket,
                 prefix='',
                 delimiter='',
                 aws_conn_id='aws_default',
                 dest_gcs_conn_id=None,
                 dest_gcs=None,
                 delegate_to=None,
                 replace=False,
                 *args,
                 **kwargs):

        super(S3ToGoogleCloudStorageOperator, self).__init__(
            bucket=bucket,
            prefix=prefix,
            delimiter=delimiter,
            aws_conn_id=aws_conn_id,
            *args,
            **kwargs)
        self.dest_gcs_conn_id = dest_gcs_conn_id
        self.dest_gcs = dest_gcs
        self.delegate_to = delegate_to
        self.replace = replace

        if dest_gcs and not self._gcs_object_is_directory(self.dest_gcs):
            self.log.info(
                'Destination Google Cloud Storage path is not a valid '
                '"directory", define a path that ends with a slash "/" or '
                'leave it empty for the root of the bucket.')
            raise AirflowException('The destination Google Cloud Storage path '
                                   'must end with a slash "/" or be empty.')

    def execute(self, context):
        # use the super method to list all the files in an S3 bucket/key
        files = super(S3ToGoogleCloudStorageOperator, self).execute(context)

        gcs_hook = GoogleCloudStorageHook(
            google_cloud_storage_conn_id=self.dest_gcs_conn_id,
            delegate_to=self.delegate_to)

        if not self.replace:
            # if we are not replacing -> list all files in the GCS bucket
            # and only keep those files which are present in
            # S3 and not in Google Cloud Storage
            bucket_name, object_prefix = _parse_gcs_url(self.dest_gcs)
            existing_files_prefixed = gcs_hook.list(
                bucket_name, prefix=object_prefix)

            existing_files = []

            if existing_files_prefixed:
                # Remove the object prefix itself, an empty directory was found
                if object_prefix in existing_files_prefixed:
                    existing_files_prefixed.remove(object_prefix)

                # Remove the object prefix from all object string paths
                for f in existing_files_prefixed:
                    if f.startswith(object_prefix):
                        existing_files.append(f[len(object_prefix):])
                    else:
                        existing_files.append(f)

            files = set(files) - set(existing_files)
            if len(files) > 0:
                self.log.info('{0} files are going to be synced: {1}.'.format(
                    len(files), files))
            else:
                self.log.info(
                    'There are no new files to sync. Have a nice day!')

        if files:
            hook = S3Hook(aws_conn_id=self.aws_conn_id)

            for file in files:
                # GCS hook builds its own in-memory file so we have to create
                # and pass the path
                file_object = hook.get_key(file, self.bucket)
                with NamedTemporaryFile(mode='wb', delete=True) as f:
                    file_object.download_fileobj(f)
                    f.flush()

                    dest_gcs_bucket, dest_gcs_object_prefix = _parse_gcs_url(
                        self.dest_gcs)
                    # There will always be a '/' before file because it is
                    # enforced at instantiation time
                    dest_gcs_object = dest_gcs_object_prefix + file

                    # Sync is sequential and the hook already logs too much
                    # so skip this for now
                    # self.log.info(
                    #     'Saving file {0} from S3 bucket {1} in GCS bucket {2}'
                    #     ' as object {3}'.format(file, self.bucket,
                    #                             dest_gcs_bucket,
                    #                             dest_gcs_object))

                    gcs_hook.upload(dest_gcs_bucket, dest_gcs_object, f.name)

            self.log.info(
                "All done, uploaded %d files to Google Cloud Storage",
                len(files))
        else:
            self.log.info(
                'In sync, no files needed to be uploaded to Google Cloud'
                'Storage')

        return files

    # Following functionality may be better suited in
    # airflow/contrib/hooks/gcs_hook.py
    def _gcs_object_is_directory(self, object):
        bucket, blob = _parse_gcs_url(object)

        return len(blob) == 0 or blob.endswith('/')
