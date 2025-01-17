#!/usr/bin/env python
# -*- coding: utf-8 -*-


"""
   Plugin for transfering objects and annotations between servers

"""

from pathlib import Path
import sys
import os
from functools import wraps
import shutil
from collections import defaultdict

from generate_xml import populate_xml
from generate_omero_objects import populate_omero

from ome_types import from_xml
from omero.sys import Parameters
from omero.rtypes import rstring
from omero.cli import CLI, GraphControl
from omero.cli import ProxyStringType
from omero.gateway import BlitzGateway
from omero.model import Image, Dataset, Project
from omero.grid import ManagedRepositoryPrx as MRepo

DIR_PERM = 0o755


HELP = ("""Transfer objects and annotations between servers.

Both subcommands (pack and unpack) will use an existing OMERO session
created via CLI or prompt the user for parameters to create one.
""")

PACK_HELP = ("""Create a transfer packet for moving objects between
OMERO server instances.

The syntax for specifying objects is: <object>:<id>
<object> can be Image, Project or Dataset.
Project is assumed if <object>: is omitted.
A file path needs to be provided; a zip file with the contents of
the packet will be created at the specified path.

Currently, only MapAnnotations and Tags are packaged into the transfer
pack, and only Point, Line, Ellipse, Rectangle and Polygon-type ROIs are
packaged.

Examples:
omero transfer pack Image:123 transfer_pack.zip
omero transfer pack Dataset:1111 /home/user/new_folder/new_pack.zip
omero transfer pack 999 zipfile.zip  # equivalent to Project:999
""")

UNPACK_HELP = ("""Unpacks an existing transfer packet, imports images
as orphans and uses the XML contained in the transfer packet to re-create
links, annotations and ROIs.

--ln_s forces imports to use the transfer=ln_s option, in-place importing
files. Same restrictions of regular in-place imports apply.

--output allows for specifying an optional output folder where the packet
will be unzipped. 

Examples:
omero transfer unpack transfer_pack.zip
omero transfer unpack --output /home/user/optional_folder --ln_s
""")

def gateway_required(func):
    """
    Decorator which initializes a client (self.client),
    a BlitzGateway (self.gateway), and makes sure that
    all services of the Blitzgateway are closed again.
    """
    @wraps(func)
    def _wrapper(self, *args, **kwargs):
        self.client = self.ctx.conn(*args)
        self.gateway = BlitzGateway(client_obj=self.client)

        try:
            return func(self, *args, **kwargs)
        finally:
            if self.gateway is not None:
                self.gateway.close(hard=False)
                self.gateway = None
                self.client = None
    return _wrapper

class TransferControl(GraphControl):

    def _configure(self, parser):
        parser.add_login_arguments()
        sub = parser.sub()
        pack = parser.add(sub, self.pack, PACK_HELP)
        unpack = parser.add(sub, self.unpack, UNPACK_HELP)

        render_type = ProxyStringType("Project")
        obj_help = ("Object to be packed for transfer")
        pack.add_argument("object", type=render_type, help=obj_help)
        file_help = ("Path to where the zip file will be saved")
        pack.add_argument("filepath", type=str, help=file_help)

        file_help = ("Path to where the zip file is saved")
        unpack.add_argument("filepath", type=str, help=file_help)
        unpack.add_argument(
                "--ln_s_import", help="Use in-place import",
                                     action="store_true")
        unpack.add_argument(
            "--output", type=str, help="Output directory where zip "
                                       "file will be extracted"
        )
        
    @gateway_required   
    def pack(self, args):
        """ Implements the 'pack' command """
        self.__pack(args)

    @gateway_required   
    def unpack(self, args):
        """ Implements the 'pack' command """
        self.__unpack(args)

    def _get_path_to_repo(self):
        shared = self.client.sf.sharedResources()
        repos = shared.repositories()
        repos = list(zip(repos.descriptions, repos.proxies))
        mrepos = []
        for _, pair in enumerate(repos):
            desc, prx = pair
            path = "".join([desc.path.val, desc.name.val])
            is_mrepo = MRepo.checkedCast(prx)
            if is_mrepo:
                mrepos.append(path)
        return mrepos

    def _copy_files(self, id_list, folder, repo):
        cli = CLI()
        cli.loadplugins()
        for id in id_list:
            path = id_list[id]
            rel_path = path.split(repo)[-1][1:]
            rel_path = str(Path(rel_path).parent)
            subfolder = str(Path(folder) / rel_path)
            os.makedirs(subfolder, mode=DIR_PERM, exist_ok=True)
            cli.invoke(['download', id, subfolder])


    def __pack(self, args):
        if isinstance(args.object, Image):
            src_datatype, src_dataid = "Image", args.object.id
        elif isinstance(args.object, Dataset):
            src_datatype, src_dataid = "Dataset", args.object.id
        elif isinstance(args.object, Project):
            src_datatype, src_dataid = "Project", args.object.id
        else:
            print("Object is not a project, dataset or image")
            return
        print("Populating xml...")
        zip_path = Path(args.filepath)
        folder = str(zip_path) + "_folder"
        os.makedirs(folder, mode=DIR_PERM, exist_ok=True)
        xml_fp = str(Path(folder) / "transfer.xml")
        repo = self._get_path_to_repo()[0]
        path_id_dict = populate_xml(src_datatype, src_dataid, xml_fp, self.gateway, repo)
        print(f"XML saved at {xml_fp}.")        

        print("Starting file copy...")
        self._copy_files(path_id_dict, folder, repo)
        print("Creating zip file...")
        shutil.make_archive(os.path.splitext(zip_path)[0], 'zip', folder)
        print("Cleaning up...")
        shutil.rmtree(folder)
        return


    def __unpack(self, args):
        print(f"Unzipping {args.filepath}...")
        parent_folder = Path(args.filepath).parent
        filename = os.path.splitext(args.filepath)[0]
        if args.output:
            folder = Path(args.output)
        else:
            folder = parent_folder / filename
        shutil.unpack_archive(args.filepath, str(folder), 'zip')
        ome = from_xml(folder / "transfer.xml")
        print("Generating Image mapping and import filelist...")
        src_img_map, filelist = self._create_image_map(ome)
        print("Importing data as orphans...")
        if args.ln_s_import:
            ln_s = True
        else:
            ln_s = False
        dest_img_map = self._import_files(folder, filelist, ln_s)
        print("Matching source and destination images...")
        img_map = self._make_image_map(src_img_map, dest_img_map)
        print("Creating and linking OMERO objects...")
        populate_omero(ome, img_map, self.gateway)
        return


    def _create_image_map(self, ome):
        img_map = defaultdict(list)
        filelist = []
        for ann in ome.structured_annotations:
            if int(ann.id.split(":")[-1]) < 0:
                img_map[ann.value].append(int(ann.namespace.split(":")[-1]))
                filelist.append(ann.value.split('/./')[-1])
        ome.structured_annotations = [x for x in ome.structured_annotations if int(x.id.split(":")[-1])>0]
        for i in ome.images:
            i.annotation_ref = [x for x in i.annotation_ref if int(x.id.split(":")[-1])>0]
        filelist = list(set(filelist))
        img_map = {x: sorted(img_map[x]) for x in img_map.keys()}
        return img_map, filelist

    def _import_files(self, folder, filelist, ln_s):
        cli = CLI()
        cli.loadplugins()
        dest_map = {}
        for filepath in filelist:
            dest_path = str(os.path.join(folder,  '.', filepath))
            if ln_s:
                cli.invoke(['import',
                            dest_path,
                            '--transfer=ln_s'])
            else:
                cli.invoke(['import',
                            dest_path])
            img_ids = self._get_image_ids(dest_path)
            dest_map[dest_path] = img_ids
        return dest_map

    def _get_image_ids(self, file_path):
        """Get the Ids of imported images.
        Note that this will not find images if they have not been imported.

        Returns
        -------
        image_ids : list of ints
            Ids of images imported from the specified client path, which
            itself is derived from ``file_path``.
        """
        q = self.gateway.getQueryService()
        params = Parameters()
        path_query = str(file_path).strip('/')
        params.map = {"cpath": rstring(path_query)}
        results = q.projection(
            "SELECT i.id FROM Image i"
            " JOIN i.fileset fs"
            " JOIN fs.usedFiles u"
            " WHERE u.clientPath=:cpath",
            params,
            self.gateway.SERVICE_OPTS
            )
        image_ids = sorted([r[0].val for r in results])
        return image_ids


    def _make_image_map(self, source_map, dest_map):
        # using both source and destination file-to-image-id maps,
    # map image IDs between source and destination
        print(source_map)
        print(dest_map)
        src_dict = defaultdict(list)
        imgmap = {}
        for k, v in source_map.items():
            newkey = k.split("/./")[-1]
            src_dict[newkey].extend(v)
        dest_dict = defaultdict(list)
        for k, v in dest_map.items():
            newkey = k.split("/./")[-1]
            dest_dict[newkey].extend(v)
        print(src_dict)
        print(dest_dict)
        for src_k in src_dict.keys():
            src_v = src_dict[src_k]
            dest_v = dest_dict[src_k]
            for count in range(len(src_v)):
                map_key = f"Image:{src_v[count]}"
                imgmap[map_key] = dest_v[count]
        return imgmap


try:
    register("transfer", TransferControl, HELP)
except NameError:
    if __name__ == "__main__":
        cli = CLI()
        cli.register("transfer", TransferControl, HELP)
        cli.invoke(sys.argv[1:])
