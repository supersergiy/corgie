import click

from corgie import scheduling, residuals, helpers, stack
from corgie.log import logger as corgie_logger
from corgie.layers import get_layer_types, DEFAULT_LAYER_TYPE, \
                             str_to_layer_type
from corgie.boundingcube import get_bcube_from_coords
from corgie.stack import Stack

from corgie.argparsers import LAYER_HELP_STR, \
        create_layer_from_dict, corgie_optgroup, corgie_option
import json

class MergeRenderJob(scheduling.Job):
    def __init__(self, 
                 src_layers, 
                 src_specs,
                 dst_layer, 
                 mip,
                 pad,
                 bcube,
                 chunk_xy):
        """Render multiple images to the same destination image
        
        Args:
            src_layers ({'img':{Layers},
            src_masks ([Layer])
            dst_layer (Stack)
            mip (int)
            pad (int)
            bcube (BoundingCube)
            z_list ([int]): : ranked by layer priority (first image overwrites later images)
            mask_ids ([int]) 
        """

        self.src_layers = src_layers
        self.src_specs = src_specs
        self.dst_layer = dst_layer
        self.mip = mip
        self.pad = pad
        self.bcube = bcube
        self.chunk_xy = chunk_xy
        super().__init__()

    def task_generator(self):
        chunks = self.dst_layer.break_bcube_into_chunks(
                bcube=self.bcube,
                chunk_xy=self.chunk_xy,
                chunk_z=1,
                mip=self.mip)

        tasks = [MergeRenderTask(
                          src_layers=self.src_layers,
                          src_specs=self.src_specs,
                          dst_layer=self.dst_layer,
                          mip=self.mip,
                          pad=self.pad,
                          bcube=input_chunk) for input_chunk in chunks]
        corgie_logger.info(
            f"Yielding render tasks for bcube: {self.bcube}, MIP: {self.mip}")

        yield tasks


class MergeRenderTask(scheduling.Task):
    def __init__(self, 
                 src_layers,
                 src_specs, 
                 dst_layer, 
                 mip,
                 pad,
                 bcube):

        """Render multiple images to the same destination image
        
        Args:
            src_layers ({Layer})
            src_specs (json): list of dicts with img, field, mask, z, & mask_id per island
                ranked by layer priority (first image overwrites later images)
            dst_layer (Stack)
            mip (int)
            pad (int)
            bcube (BoundingCube)
        """
        super().__init__(self)
        self.src_layers = src_layers
        self.src_specs = src_specs
        self.dst_layer = dst_layer
        self.mip = mip
        self.pad = pad
        self.bcube  = bcube

    def execute(self):
        padded_bcube = self.bcube.uncrop(self.pad, self.mip)
        for k, specs in enumerate(self.src_specs[::-1]):
            z = specs['z']
            mask_id = specs['mask_id']
            bcube = padded_bcube.reset_coords(zs=z, ze=z+1, in_place=False)
            imgs = {}
            for name in ['img', 'mask', 'field']:
                layer = self.src_layers[specs[name]]
                if name == 'mask':
                    layer.binarizer = helpers.Binarizer(['eq', mask_id])
                imgs[name] = layer.read(bcube=bcube, mip=self.mip)
            mask = residuals.res_warp_img(imgs['mask'].float(), imgs['field'])
            mask = (mask > 0.4).byte()
            cropped_mask = helpers.crop(mask, self.pad)
            img = residuals.res_warp_img(imgs['img'].float(), imgs['field'])
            cropped_img = helpers.crop(img, self.pad)
            if k == 0:
                dst_img = cropped_img
                dst_img[~cropped_mask] = 0
            else:
                dst_img[cropped_mask] = cropped_img[cropped_mask]

        self.dst_layer.write(dst_img, bcube=self.bcube, mip=self.mip)

@click.command()
@corgie_optgroup('Layer Parameters')
@corgie_option('--spec_path',  nargs=1,
        type=str, required=True,
        help= "JSON spec relating src stacks, src z to dst z")
@corgie_option('--dst_folder',  nargs=1,
        type=str, required=False,
        help= "Destination folder for the copied stack")
@corgie_optgroup('Render Method Specification')
@corgie_option('--chunk_xy',       '-c', nargs=1, type=int, default=1024)
@corgie_option('--pad',                  nargs=1, type=int, default=512)
@corgie_option('--mip',                  nargs=1, type=int, required=True)

@corgie_optgroup('Data Region Specification')
@corgie_option('--start_coord',      nargs=1, type=str, required=True)
@corgie_option('--end_coord',        nargs=1, type=str, required=True)
@corgie_option('--coord_mip',        nargs=1, type=int, default=0)
@corgie_option('--suffix',           nargs=1, type=str, default=None)

@click.pass_context
def merge_render(ctx, 
         spec_path,
         dst_folder, 
         chunk_xy, 
         pad,
         start_coord, 
         end_coord, 
         coord_mip, 
         mip, 
         suffix):

    scheduler = ctx.obj['scheduler']
    if suffix is None:
        suffix = ''
    else:
        suffix = f"_{suffix}"

    corgie_logger.debug("Setting up layers...")
    # create layers
        # collect image paths
        # collect mask paths
    
    with open(spec_path, 'r') as f:
        spec = json.load(f)

    # collect {path: Layer}
    src_layers = {}
    for _, src_specs in spec.items():
        for src_spec in src_specs:
            for name in ['img','field','mask']:
                src_path = src_spec[name]
                if src_path not in src_layers:                        
                    s = {'path': src_path, 'type': name}
                    if name == 'mask':
                        s['args'] = {'data_mip': 6}
                    l = create_layer_from_dict(s, readonly=True)
                    src_layers[src_path] = l

    reference_layer = src_layers[list(src_layers.keys())[0]]
    dst_layer = create_layer_from_dict({'path': dst_folder, 
                                        'type': 'img'},
                                       reference=reference_layer,
                                       overwrite=True)

    bcube = get_bcube_from_coords(start_coord, end_coord, coord_mip)

    for z in range(*bcube.z_range()):
        tgt_z = str(z)
        if tgt_z in spec.keys():
            job_bcube = bcube.reset_coords(zs=z, ze=z+1, in_place=False)
            render_job = MergeRenderJob(
                            src_layers=src_layers,
                            src_specs=spec[tgt_z],
                            dst_layer=dst_layer,
                            mip=mip,
                            pad=pad,
                            bcube=job_bcube,
                            chunk_xy=chunk_xy)
            scheduler.register_job(render_job, 
                        job_name="MergeRender {}".format(job_bcube))
    scheduler.execute_until_completion()
