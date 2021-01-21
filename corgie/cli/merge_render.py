import click

from corgie import scheduling, residuals, helpers, stack
from corgie.log import logger as corgie_logger
from corgie.layers import get_layer_types, DEFAULT_LAYER_TYPE, \
                             str_to_layer_type
from corgie.boundingcube import get_bcube_from_coords

from corgie.argparsers import LAYER_HELP_STR, \
        create_layer_from_spec, corgie_optgroup, corgie_option, \
        create_stack_from_spec
import json

class MergeRenderJob(scheduling.Job):
    def __init__(self, 
                 src_stack, 
                 dst_stack, 
                 mip,
                 pad,
                 bcube,
                 z_list,
                 chunk_xy):
        """Render multiple images to the same destination image
        
        Args:
            src_stack (Stack)
            dst_stack (Stack)
            mip (int)
            pad (int)
            bcube (BoundingCube)
            z_list ([int]): : ranked by layer priority (first image overwrites later images)
        """

        self.src_stack = src_stack
        self.dst_stack = dst_stack
        self.mip = mip
        self.pad = pad
        self.bcube = bcube
        self.chunk_xy = chunk_xy
        self.z_list = z_list

        super().__init__()

    def task_generator(self):
        chunks = self.dst_stack.get_layers()[0].break_bcube_into_chunks(
                bcube=self.bcube,
                chunk_xy=self.chunk_xy,
                chunk_z=1,
                mip=self.mip)

        tasks = [MergeRenderTask(src_stack=self.src_stack,
                          dst_stack=self.dst_stack,
                          mip=self.mip,
                          pad=self.pad,
                          bcube=input_chunk,
                          z_list=self.z_list) for input_chunk in chunks]
        corgie_logger.info(f"Yielding render tasks for bcube: {self.bcube}, MIP: {self.mip}")

        yield tasks


class MergeRenderTask(scheduling.Task):
    def __init__(self, 
                 src_stack, 
                 dst_stack, 
                 mip,
                 pad,
                 bcube,
                 z_list):
        """Render multiple images to the same destination image
        
        Args:
            src_stack (Stack)
            dst_stack (Stack)
            mip (int)
            pad (int)
            bcube (BoundingCube)
            z_list ([int]): : ranked by layer priority (first image overwrites later images)
        """
        super().__init__(self)
        self.src_stack = src_stack
        self.dst_stack = dst_stack
        self.mip = mip
        self.pad = pad
        self.bcube  = bcube
        self.z_list = z_list

    def execute(self):
        padded_bcube = self.bcube.uncrop(self.pad, self.mip)
        for k, z in enumerate(self.z_list[::-1]):
            src_bcube = padded_bcube.reset_coords(zs=z, ze=z+1, in_place=False)
            src_trans, src_data_dict = self.src_stack.read_data_dict(src_bcube,
                                                           mip=self.mip, 
                                                           stack_name='src')
            agg_field = src_data_dict[f"src_agg_field"]
            import pdb; pdb.set_trace()
            img_name = self.src_stack.get_layers_of_type('img')[0].name
            src_img = src_data_dict[f"src_{img_name}"]
            mask_name = self.src_stack.get_layers_of_type('mask')[0].name
            mask = src_data_dict[f"src_{mask_name}"]
            warped_mask = residuals.res_warp_img(
                mask.float(),
                agg_field)
            warped_mask = (warped_mask > 0.4).byte()
            cropped_mask = helpers.crop(warped_mask, self.pad)
            warped_src = residuals.res_warp_img(src_img.float(), agg_field)
            cropped_src = helpers.crop(warped_src, self.pad)
            # mask_layers = src_stack.get_layers_of_type(["mask"])
            # mask = helpers.read_mask_list(mask_layers, src_bcube, self.mip)
            if k == 0:
                dst_image = cropped_src
                dst_image[~cropped_mask] = 0
            else:
                dst_image[cropped_mask] = cropped_src[cropped_mask]

        dst_layer = self.dst_stack.get_layers_of_type("img")[0]
        dst_layer.write(dst_image, bcube=self.bcube, mip=self.mip)

@click.command()
@corgie_optgroup('Layer Parameters')
@corgie_option('--src_layer_spec',  '-s', nargs=1,
        type=str, required=True, multiple=True,
        help='Source layer spec. Order img, mask, img, mask, etc. List must have length of multiple two.' + \
                LAYER_HELP_STR)
#
@corgie_option('--dst_folder',  nargs=1,
        type=str, required=False,
        help= "Destination folder for the copied stack")
@corgie_option('--spec_path',  nargs=1,
        type=str, required=True,
        help= "JSON spec relating src stacks, src z to dst z")

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
         src_layer_spec, 
         dst_folder, 
         spec_path,
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
    assert(len(src_layer_spec) % 2 == 0)
    src_stacks = {}
    group = 3
    for k in range(len(src_layer_spec) // group):
        src_stack = create_stack_from_spec(src_layer_spec[group*k:group*(k+1)],
                                            name='src', 
                                            readonly=True)
        name = src_stack.get_layers_of_type('img')[0].path
        src_stacks[name] = src_stack

    with open(spec_path, 'r') as f:
        spec = json.load(f)

    # if force_chunk_xy:
    #     force_chunk_xy = chunk_xy
    # else:
    #     force_chunk_xy = None

    # if force_chunk_z:
    #     force_chunk_z = chunk_z
    # else:
    #     force_chunk_z = None

    dst_stack = stack.create_stack_from_reference(reference_stack=list(src_stacks.values())[0],
                                                    folder=dst_folder, 
                                                    name="dst", 
                                                    types=["img"],
                                                    readonly=False,
                                                    suffix=suffix, 
                                                    force_chunk_xy=None, 
                                                    force_chunk_z =None,
                                                    overwrite=True)

    bcube = get_bcube_from_coords(start_coord, end_coord, coord_mip)

    for z in range(*bcube.z_range()):
        spec_z = str(z)
        if spec_z in spec.keys():
            src_dict = spec[str(z)]
            job_bcube = bcube.reset_coords(zs=z, ze=z+1, in_place=False)
            src_stack = src_stacks[src_dict['cv_path']]
            z_list = src_dict['z_list']
            render_job = MergeRenderJob(src_stack=src_stack,
                               dst_stack=dst_stack,
                               mip=mip,
                               pad=pad,
                               bcube=job_bcube,
                               chunk_xy=chunk_xy,
                               z_list=z_list)
            # create scheduler and execute the job
            scheduler.register_job(render_job, job_name="MergeRender {}".format(job_bcube))
    scheduler.execute_until_completion()
