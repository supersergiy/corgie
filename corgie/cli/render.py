import click
import six

from corgie import scheduling, residuals, helpers, stack

from corgie.log import logger as corgie_logger
from corgie.layers import get_layer_types, DEFAULT_LAYER_TYPE, \
                             str_to_layer_type
from corgie.boundingcube import get_bcube_from_coords
from corgie.argparsers import LAYER_HELP_STR, \
        create_layer_from_spec, corgie_optgroup, corgie_option, \
        create_stack_from_spec

class RenderJob(scheduling.Job):
    def __init__(self, src_stack, dst_stack, mips, pad, render_masks,
                 blackout_masks, seethrough, bcube, chunk_xy, chunk_z,
                 additional_fields=[], seethrough_offset=-1):
        self.src_stack = src_stack
        self.dst_stack = dst_stack
        self.mips = mips
        self.pad = pad
        self.bcube = bcube
        self.chunk_xy = chunk_xy
        self.chunk_z = chunk_z
        self.render_masks = render_masks
        self.blackout_masks = blackout_masks
        self.additional_fields = additional_fields
        self.seethrough = seethrough
        self.seethrough_offset = seethrough_offset

        super().__init__()

    def task_generator(self):
        for mip in self.mips:
            chunks = self.dst_stack.get_layers()[0].break_bcube_into_chunks(
                    bcube=self.bcube,
                    chunk_xy=self.chunk_xy,
                    chunk_z=self.chunk_z,
                    mip=mip)

            tasks = [RenderTask(self.src_stack,
                                self.dst_stack,
                                blackout_masks=self.blackout_masks,
                                render_masks=self.render_masks,
                                mip=mip,
                                pad=self.pad,
                                bcube=input_chunk,
                                additional_fields=self.additional_fields,
                                seethrough=self.seethrough,
                                seethrough_offset=self.seethrough_offset) \
                                        for input_chunk in chunks]
            corgie_logger.info(f"Yielding render tasks for bcube: {self.bcube}, MIP: {mip}")

            yield tasks


class RenderTask(scheduling.Task):
    def __init__(self, src_stack, dst_stack, additional_fields, render_masks,
            blackout_masks, seethrough, seethrough_offset, mip,
            pad, bcube):
        super().__init__(self)
        self.src_stack = src_stack
        self.dst_stack = dst_stack
        self.render_masks = render_masks
        self.blackout_masks = blackout_masks
        self.mip = mip
        self.bcube = bcube
        self.pad = pad
        self.additional_fields = additional_fields
        self.seethrough = seethrough
        self.seethrough_offset = seethrough_offset
        self.blackout_value = 0.0

    def execute(self):
        padded_bcube = self.bcube.uncrop(self.pad, self.mip)
        seethrough_bcube = self.bcube.translate(
                        z_offset=self.seethrough_offset)

        for f in self.additional_fields:
            #just in case the "additional" field is actually already a part of src_stack
            if f not in self.src_stack.layers.values():
                self.src_stack.add_layer(f)

        src_translation, src_data_dict = self.src_stack.read_data_dict(padded_bcube,
                mip=self.mip, add_prefix=False)
        agg_field = src_data_dict[f"agg_field"]
        agg_mask = None

        if self.blackout_masks or self.seethrough:
            mask_layers = self.dst_stack.get_layers_of_type(["mask"])
            mask_layer_names = [l.name for l in mask_layers]
            for n, d in six.iteritems(src_data_dict):
                if n in mask_layer_names:
                    if agg_mask is None:
                        agg_mask = d
                    else:
                        agg_mask = ((agg_mask + d) > 0).byte()

            if agg_mask is not None:
                coarsen_factor = int(2**(6 - self.mip))
                agg_mask = helpers.coarsen_mask(agg_mask, coarsen_factor)
                if agg_field is not None:
                    warped_mask = residuals.res_warp_img(
                        agg_mask.float(),
                        agg_field)
                else:
                    warped_mask = agg_mask

                warped_mask = (warped_mask > 0.4).byte()
            else:
                warped_mask = None

        if self.render_masks:
            write_layers = self.dst_stack.get_layers_of_type(["img", "mask"])
        else:
            write_layers = self.dst_stack.get_layers_of_type("img")


        for l in write_layers:
            src = src_data_dict[f"{l.name}"]
            if agg_field is not None:
                warped_src = residuals.res_warp_img(src.float(), agg_field)
            else:
                warped_src = src

            cropped_out = helpers.crop(warped_src, self.pad)

            if self.blackout_masks or self.seethrough:
                if warped_mask is not None:
                    warped_mask = helpers.crop(warped_mask, self.pad)

                if l.get_layer_type() == "img" and  self.blackout_masks and warped_mask is not None:
                    cropped_out[warped_mask] = self.blackout_value


                if l.get_layer_type() == "img" and self.seethrough and warped_mask is not None:
                    seethrough_data = l.read(
                            bcube=seethrough_bcube,
                            mip=self.mip)

                    coarsen_factor = int(2**(6 - self.mip))
                    seethrough_mask = helpers.coarsen_mask(warped_mask, coarsen_factor)
                    seethrough_mask[cropped_out == 0] = True

                    cropped_out[seethrough_mask] = \
                            seethrough_data[seethrough_mask]

                    seenthru = (cropped_out[seethrough_mask] != 0).sum()
                    corgie_logger.debug(f"Seenthrough {seenthru} pixels")

            l.write(cropped_out, bcube=self.bcube, mip=self.mip)



@click.command()
@corgie_optgroup('Layer Parameters')
@corgie_option('--src_layer_spec',  '-s', nargs=1,
        type=str, required=True, multiple=True,
        help='Source layer spec. Use multiple times to include all masks, fields, images. ' + \
                LAYER_HELP_STR)
#
@corgie_option('--dst_folder',  nargs=1,
        type=str, required=True,
        help= "Folder where rendered stack will go")

@corgie_optgroup('Render Method Specification')
@corgie_option('--chunk_xy',       '-c', nargs=1, type=int, default=1024)
@corgie_option('--chunk_z',              nargs=1, type=int, default=1)
@corgie_option('--pad',                  nargs=1, type=int, default=512)
@corgie_option('--mip', 'mips', nargs=1, type=int, required=True, multiple=True)
@corgie_option('--render_masks/--no_render_masks',          default=True)
@corgie_option('--blackout_masks/--no_blackout_masks',      default=False)
@corgie_option('--seethrough/--no_seethrough',              default=False)
@corgie_option('--force_chunk_xy',     is_flag=True)
@corgie_option('--force_chunk_z',      is_flag=True)

@corgie_optgroup('Data Region Specification')
@corgie_option('--start_coord',      nargs=1, type=str, required=True)
@corgie_option('--end_coord',        nargs=1, type=str, required=True)
@corgie_option('--coord_mip',        nargs=1, type=int, default=0)

@click.option('--suffix',            nargs=1, type=str,  default=None)

@click.pass_context
def render(ctx, src_layer_spec, dst_folder, pad, render_masks, blackout_masks,
         seethrough, chunk_xy, chunk_z, start_coord, end_coord, mips,
         coord_mip, force_chunk_xy, force_chunk_z, suffix):
    scheduler = ctx.obj['scheduler']

    if suffix is None:
        suffix = '_rendered'
    else:
        suffix = f"_{suffix}"

    corgie_logger.debug("Setting up layers...")
    src_stack = create_stack_from_spec(src_layer_spec,
            name='src', readonly=True)

    if force_chunk_xy:
        force_chunk_xy = chunk_xy
    else:
        force_chunk_xy = None

    if force_chunk_z:
        force_chunk_z = chunk_z
    else:
        force_chunk_z = None

    dst_stack = stack.create_stack_from_reference(reference_stack=src_stack,
            folder=dst_folder, name="dst", types=["img", "mask"],
            force_chunk_xy=force_chunk_xy, force_chunk_z=force_chunk_z,
            suffix=suffix, overwrite=True)

    bcube = get_bcube_from_coords(start_coord, end_coord, coord_mip)

    render_job = RenderJob(src_stack=src_stack,
                           dst_stack=dst_stack,
                           mips=mips,
                           pad=pad,
                           bcube=bcube,
                           chunk_xy=chunk_xy,
                           chunk_z=chunk_z,
                           render_masks=render_masks,
                           blackout_masks=blackout_masks,
                           seethrough=seethrough)

    # create scheduler and execute the job
    scheduler.register_job(render_job, job_name="Render {}".format(bcube))
    scheduler.execute_until_completion()
