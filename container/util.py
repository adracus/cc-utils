# Copyright (c) 2019 SAP SE or an SAP affiliate company. All rights reserved. This file is licensed
# under the Apache Software License, v. 2 except as noted otherwise in the LICENSE file
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import hashlib
import json
import tarfile
import tempfile

import container.registry
import util


def filter_image(
    source_ref:str,
    target_ref:str,
    remove_files:[str]=[],
):
    with tempfile.NamedTemporaryFile() as in_fh:
        container.registry.retrieve_container_image(image_reference=source_ref, outfileobj=in_fh)

        # XXX enable filter_image_file / filter_container_image to work w/o named files
        with tempfile.NamedTemporaryFile() as out_fh:
            filter_container_image(
                image_file=in_fh.name,
                out_file=out_fh.name,
                remove_entries=remove_files
            )

            container.registry.publish_container_image(
                image_reference=target_ref,
                image_file_obj=out_fh,
            )


def filter_container_image(
    image_file,
    out_file,
    remove_entries,
):
    util.existing_file(image_file)
    if not remove_entries:
        raise ValueError('remove_entries must not be empty')

    with tarfile.open(image_file) as tf:
        manifest = json.load(tf.extractfile('manifest.json'))
        if not len(manifest) == 1:
            raise NotImplementedError()
        manifest = manifest[0]
        cfg_name = manifest['Config']

    with tarfile.open(image_file, 'r') as in_tf, tarfile.open(out_file, 'w') as out_tf:
        _filter_files(
            manifest=manifest,
            cfg_name=cfg_name,
            in_tarfile=in_tf,
            out_tarfile=out_tf,
            remove_entries=set(remove_entries),
        )


def _filter_files(
    manifest,
    cfg_name,
    in_tarfile: tarfile.TarFile,
    out_tarfile: tarfile.TarFile,
    remove_entries,
):
    layer_paths = set(manifest['Layers'])
    changed_layer_hashes = [] # [(old, new),]

    # copy everything that does not need to be patched
    for tar_info in in_tarfile:
        if not tar_info.isfile():
            out_tarfile.addfile(tar_info)
            continue

        # cfg needs to be rewritten - so do not cp
        if tar_info.name in (cfg_name, 'manifest.json'):
            continue

        fileobj = in_tarfile.extractfile(tar_info)

        if tar_info.name not in layer_paths:
            out_tarfile.addfile(tar_info, fileobj=fileobj)
            continue

        # assumption: layers are always tarfiles
        # check if we need to patch
        layer_tar = tarfile.open(fileobj=fileobj)
        have_match = bool(set(layer_tar.getnames()) & remove_entries)
        fileobj.seek(0)

        if not have_match:
            out_tarfile.addfile(tar_info, fileobj=fileobj)
        else:
            old_hash = hashlib.sha256() # XXX hard-code hash algorithm for now
            while fileobj.peek():
                old_hash.update(fileobj.read(2048))
            fileobj.seek(0)

            patched_tar, size = _filter_single_tar(
                in_file=layer_tar,
                remove_entries=remove_entries,
            )
            # patch tar_info to reduced size
            tar_info.size = size

            new_hash = hashlib.sha256() # XXX hard-code hash algorithm for now
            while patched_tar.peek():
                new_hash.update(patched_tar.read(2048))
            patched_tar.seek(0)

            out_tarfile.addfile(tar_info, fileobj=patched_tar)
            print('patched: ' + str(tar_info.name))

            changed_layer_hashes.append((old_hash.hexdigest(), new_hash.hexdigest()))

    # update cfg
    cfg = json.load(in_tarfile.extractfile(cfg_name))
    root_fs = cfg['rootfs']
    if not root_fs['type'] == 'layers':
        raise NotImplementedError()
    # XXX hard-code hash algorithm (assume all entries are prefixed w/ sha256)
    diff_ids = root_fs['diff_ids']
    for old_hash, new_hash in changed_layer_hashes:
        idx = diff_ids.index('sha256:' + old_hash)
        diff_ids[idx] = 'sha256:' + new_hash

    # hash cfg again (as its name is derived from its hash)
    cfg_raw = json.dumps(cfg)
    cfg_hash = hashlib.sha256(cfg_raw.encode('utf-8')).hexdigest()
    cfg_name = cfg_hash + '.json'

    # add cfg to resulting archive
    # unfortunately, tarfile requires us to use a tempfile :-(
    with tempfile.TemporaryFile() as tmp_fh:
        tmp_fh.write(cfg_raw.encode('utf-8'))
        cfg_size = tmp_fh.tell()
        tmp_fh.seek(0)
        cfg_info = tarfile.TarInfo(name=cfg_name)
        cfg_info.type = tarfile.REGTYPE
        cfg_info.size = cfg_size
        out_tarfile.addfile(cfg_info, fileobj=tmp_fh)

    # now new finally need to patch the manifest
    manifest['Config'] = cfg_name
    # wrap it in a list again
    manifest = [manifest]
    with tempfile.TemporaryFile() as fh:
        manifest_raw = json.dumps(manifest)
        fh.write(manifest_raw.encode('utf-8'))
        size = fh.tell()
        fh.seek(0)
        manifest_info = tarfile.TarInfo(name='manifest.json')
        manifest_info.type = tarfile.REGTYPE
        manifest_info.size = size
        out_tarfile.addfile(manifest_info, fh)


def _filter_single_tar(
    in_file: tarfile.TarFile,
    remove_entries,
):
    print('looking for: ' + ', '.join(remove_entries))
    temp_fh = tempfile.TemporaryFile()
    temptar = tarfile.TarFile(fileobj=temp_fh, mode='w')

    for tar_info in in_file:
        if not tar_info.isfile():
            temptar.addfile(tar_info)
            continue

        if tar_info.name in remove_entries:
            print(f'purging entry: {tar_info.name}')
            continue

        # copy entry
        entry = in_file.extractfile(tar_info)
        temptar.addfile(tar_info, fileobj=entry)

    size = temp_fh.tell()
    temp_fh.flush()
    temp_fh.seek(0)

    return temp_fh, size
