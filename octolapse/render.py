# coding=utf-8
##################################################################################
# Octolapse - A plugin for OctoPrint used for making stabilized timelapse videos.
# Copyright (C) 2017  Brad Hochgesang
##################################################################################
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see the following:
# https://github.com/FormerLurker/Octolapse/blob/master/LICENSE
#
# You can contact the author either through the git-hub repository, or at the
# following email address: FormerLurker@pm.me
##################################################################################

from subprocess import CalledProcessError
import logging
import math
import os
import shutil
import sys
import threading
import Queue
import time
import json
from csv import DictReader
# sarge was added to the additional requirements for the plugin
import datetime
from tempfile import mkdtemp

import sarge
from PIL import Image, ImageDraw, ImageFont

import octolapse.utility as utility
from octolapse.snapshot import SnapshotMetadata


# create the module level logger
from octolapse.log import LoggingConfigurator
logging_configurator = LoggingConfigurator()
logger = logging_configurator.get_logger(__name__)


def is_rendering_template_valid(template, options):
    # make sure we have all the replacements we need
    option_dict = {}
    for option in options:
        option_dict[option] = "F"  # use any valid file character, F seems ok
    try:
        filename = template.format(**option_dict)
    except KeyError as e:
        return False, "The following token is invalid: {{{0}}}".format(e.args[0])
    except IndexError as e:
        return False, "Integers as tokens are not allowed."
    except ValueError:
        return False, "A value error occurred when replacing the provided tokens."

    temp_directory = mkdtemp()
    file_path = "{0}{1}.{2}".format(temp_directory, filename, "mp4")
    # see if the filename is valid
    if not os.access(file_path, os.W_OK):
        try:
            open(file_path, 'w').close()
            os.unlink(file_path)
        except (IOError, OSError):
            return False, "The resulting filename is not a valid filename.  Most likely an invalid character was used."

    shutil.rmtree(temp_directory)

    return True, ""


def is_overlay_text_template_valid(template, options):
    # make sure we have all the replacements we need
    option_dict = {}
    for option in options:
        option_dict[option] = "F"  # use any valid file character, F seems ok
    try:
        template.format(**option_dict)
    except KeyError as e:
        return False, "The following token is invalid: {{{0}}}".format(e.args[0])
    except IndexError as e:
        return False, "Integers as tokens are not allowed."
    except ValueError:
        return False, "A value error occurred when replacing the provided tokens."

    return True, ""


def preview_overlay(rendering_profile, image=None):
    if image is None:
        # Create an image with background color inverse to the text color.
        image = Image.new('RGB', (640, 480), color=tuple(255 - c for c in json.loads(rendering_profile.overlay_text_color)))

    if rendering_profile.overlay_font_path is None or len(rendering_profile.overlay_font_path.strip()) == 0:
        # we don't have any overlay path, return
        return None

    font = ImageFont.truetype(rendering_profile.overlay_font_path, size=50)

    def draw_center(i, t, dx=0, dy=0):
        """Draws the text centered in the image, offsets by (dx, dy)."""
        text_image = Image.new('RGBA', i.size, (255, 255, 255, 0))
        d = ImageDraw.Draw(text_image)
        iw, ih = i.size
        tw, th = d.textsize(t, font=font)

        if isinstance(rendering_profile.overlay_text_color, basestring):
            overlay_text_color = json.loads(rendering_profile.overlay_text_color)
        else:
            overlay_text_color = rendering_profile.overlay_text_color
        d.text(xy=(iw / 2 - tw / 2 + dx, ih / 2 - th / 2 + dy), text=t,
               fill=tuple(overlay_text_color), font=font)
        return Image.alpha_composite(i.convert('RGBA'), text_image).convert('RGB')

    image = draw_center(image, "Preview", dy=-20)
    image = draw_center(image, "Click to refresh", dy=20)

    format_vars = {'snapshot_number': 1234,
                   'file_name': 'image.jpg',
                   'time_taken': time.time(),
                   'current_time': datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S"),
                   'time_elapsed': str(datetime.timedelta(seconds=round(9001)))}
    image = TimelapseRenderJob.add_overlay(image,
                                           text_template=rendering_profile.overlay_text_template,
                                           format_vars=format_vars,
                                           font_path=rendering_profile.overlay_font_path,
                                           font_size=rendering_profile.overlay_font_size,
                                           overlay_location=rendering_profile.overlay_text_pos,
                                           overlay_text_alignment=rendering_profile.overlay_text_alignment,
                                           overlay_text_valign=rendering_profile.overlay_text_valign,
                                           overlay_text_halign=rendering_profile.overlay_text_halign,
                                           text_color=rendering_profile.overlay_text_color)
    return image


class RenderJobInfo(object):
    def __init__(
        self, timelapse_job_info, data_directory, current_camera, rendering, ffmpeg_path, job_number=0, jobs_remaining=0
    ):
        self.timelapse_job_info = timelapse_job_info

        self.job_id = timelapse_job_info.JobGuid
        self.job_number = job_number
        self.jobs_remaining = jobs_remaining
        self.camera = current_camera
        self.job_directory = os.path.join(data_directory, "snapshots", timelapse_job_info.JobGuid)
        self.snapshot_directory = os.path.join(data_directory, "snapshots", timelapse_job_info.JobGuid, current_camera.guid)
        self.snapshot_filename_format = os.path.basename(utility.get_snapshot_filename(
            timelapse_job_info.PrintFileName, timelapse_job_info.PrintStartTime, utility.SnapshotNumberFormat)
        )
        self.output_tokens = self._get_output_tokens(data_directory)
        self.rendering = rendering.clone()
        self.ffmpeg_path = ffmpeg_path
        self.cleanup_after_complete = rendering.cleanup_after_render_complete
        self.cleanup_after_fail = rendering.cleanup_after_render_fail

    def _get_output_tokens(self, data_directory):
        job_info = self.timelapse_job_info
        assert(isinstance(job_info, utility.TimelapseJobInfo))

        return {
            "FAILEDFLAG": "FAILED" if job_info.PrintEndState != "COMPLETED" else "",
            "FAILEDSEPARATOR": "_" if job_info.PrintEndState != "COMPLETED" else "",
            "FAILEDSTATE": "" if job_info.PrintEndState == "COMPLETED" else job_info.PrintEndState,
            "PRINTSTATE": job_info.PrintEndState,
            "GCODEFILENAME": job_info.PrintFileName,
            "PRINTENDTIME": time.strftime("%Y%m%d%H%M%S", time.localtime(job_info.PrintEndTime)),
            "PRINTENDTIMESTAMP": "{0:d}".format(math.trunc(round(job_info.PrintEndTime, 2) * 100)),
            "PRINTSTARTTIME": time.strftime("%Y%m%d%H%M%S", time.localtime(job_info.PrintStartTime)),
            "PRINTSTARTTIMESTAMP": "{0:d}".format(math.trunc(round(job_info.PrintStartTime, 2) * 100)),
            "DATETIMESTAMP": "{0:d}".format(math.trunc(round(time.time(), 2) * 100)),
            "DATADIRECTORY": data_directory,
            "TIMEADDED": "{0:d}".format(math.trunc(round(job_info.SecondsAdded))),
            "SNAPSHOTCOUNT": 0,
            "FPS": 0,
        }


class RenderingProcessor(threading.Thread):
    def __init__(
        self, rendering_task_queue, data_directory, octoprint_timelapse_folder,
        on_prerender_start, on_start, on_success, on_error, on_end
    ):
        super(RenderingProcessor, self).__init__()
        self.lock = threading.Lock()

        self.rendering_task_queue = rendering_task_queue
        # make a local copy of everything.
        self.data_directory = data_directory
        self.octoprint_timelapse_folder = octoprint_timelapse_folder
        self.on_prerender_start = on_prerender_start
        self.on_start = on_start
        self.on_success = on_success
        self.on_error = on_error
        self.on_end = on_end
        self.cameras = []
        self.print_state = "unknown"
        self.time_added = 0
        self.daemon = True
        self.job_count = 0
        self._is_processing = False

    def is_processing(self):
        with self.lock:
            return self._is_processing

    def run(self):
        while True:
            try:
                job_info = self.rendering_task_queue.get(timeout=5)
                assert(isinstance(job_info, RenderJobInfo))
                self.job_count += 1
                with self.lock:
                    self._is_processing = True

                job_info.job_number = self.job_count
                job_info.jobs_remaining = self.rendering_task_queue.qsize()
                job = TimelapseRenderJob(
                    job_info.rendering,
                    job_info,
                    self.octoprint_timelapse_folder,
                    job_info.ffmpeg_path,
                    job_info.rendering.thread_count,
                    job_info.rendering.cleanup_after_render_complete,
                    job_info.rendering.cleanup_after_render_fail,
                    self.on_prerender_start,
                    self.on_render_start,
                    self.on_render_error,
                    self.on_render_success
                )
                # send the rendering start message
                try:
                    job.process()
                ## What exceptions can happen here?
                except Exception as e:
                    logger.exception("Failed to process the rendering job")
                    raise e
                finally:
                    self.rendering_task_queue.task_done()

                if self.rendering_task_queue.qsize() == 0:
                    with self.lock:
                        self._is_processing = False
                    logger.info("Sending render end message")
                    self.on_render_end()

            except Queue.Empty:
                if self._is_processing:
                    self._is_processing = False



    def on_prerender_start(self, payload):
        logger.info("Sending prerender start message")
        self.on_prerender_start(payload)

    def on_render_start(self, payload):
        logger.info("Sending render start message")
        self.on_start(payload)

    def on_render_error(self, payload, error):
        logger.info("Sending render fail message")
        self.on_error(payload, error)

    def on_render_success(self, payload):
        logger.info("Sending render complete message")
        self.on_success(payload)

    def on_render_end(self):
        logger.info("Sending render end message")
        self.on_end()

class TimelapseRenderJob(object):
    render_job_lock = threading.RLock()
    def __init__(
        self,
        rendering,
        render_job_info,
        octoprint_timelapse_folder,
        ffmpeg_path,
        threads,
        cleanup_on_success,
        cleanup_on_fail,
        on_prerender_start,
        on_render_start,
        on_render_error,
        on_render_success
    ):
        self._rendering = rendering

        self.render_job_info = render_job_info

        self._octoprintTimelapseFolder = octoprint_timelapse_folder
        self._fps = None
        self._snapshot_metadata = None
        self._imageCount = None
        self._secondsAddedToPrint = render_job_info.timelapse_job_info.SecondsAdded
        self._threads = threads
        self._ffmpeg = None
        if ffmpeg_path is not None:
            self._ffmpeg = ffmpeg_path.strip()
            if sys.platform == "win32" and not (self._ffmpeg.startswith('"') and self._ffmpeg.endswith('"')):
                self._ffmpeg = "\"{0}\"".format(self._ffmpeg)
        ###########
        # callbacks
        ###########
        self._thread = None
        self.cleanAfterSuccess = cleanup_on_success
        self.cleanAfterFail = cleanup_on_fail
        self._synchronize = False
        # full path of the input
        self.temp_rendering_dir = None
        self._output_directory = ""
        self._output_filename = ""
        self._output_extension = ""
        self._rendering_output_file_path = ""
        self._synchronized_directory = ""
        self._synchronized_filename = ""
        self._synchronize = self._rendering.sync_with_timelapse
        # render script errors
        self.before_render_error = None
        self.after_render_error = None
        # callbacks
        self.on_prerender_start = on_prerender_start
        self.on_render_start = on_render_start
        self.on_render_error = on_render_error
        self.on_render_success = on_render_success

    def process(self):
        return self._render()

    def _pre_render(self):

        self._read_snapshot_metadata()


        if self._imageCount == 0:
            raise RenderError(
                'insufficient-images',
                "No snapshots were found for the '{0}' camera profile.".format(self.render_job_info.camera.name)
              )
        if self._imageCount == 1:
            raise RenderError('insufficient-images',
                              "Only 1 frame was captured, cannot make a timelapse with a single frame.")
        # calculate the FPS
        self._calculate_fps()
        if self._fps < 1:
            raise RenderError('insufficient-images', "The calculated FPS is below 1, which is not allowed. "
                                                     "Please check the rendering settings for Min and Max FPS "
                                                     "as well as the number of snapshots captured.")

        # set the outputs - output directory, output filename, output extension
        self._set_outputs()

    def _pre_render_script(self):
        try:
            script = self.render_job_info.camera.on_before_render_script.strip()
            if not script:
                return
            try:
                script_args = [
                    script,
                    self.render_job_info.camera.name,
                    self.render_job_info.snapshot_directory,
                    self.render_job_info.snapshot_filename_format,
                    os.path.join(self.render_job_info.snapshot_directory, self.render_job_info.snapshot_filename_format)
                ]

                logger.info(
                    "Running the following before-render script command: {0} \"{1}\" \"{2}\" \"{3}\" \"{4}\"",
                    script_args[0],
                    script_args[1],
                    script_args[2],
                    script_args[3],
                    script_args[4]
                )
                cmd = utility.POpenWithTimeout()
                return_code = cmd.run(script_args, None)
                console_output = cmd.stdout
                error_message = cmd.stderr
            except utility.POpenWithTimeout.ProcessError as e:
                raise RenderError(
                    'before_render_script_error',
                    "A script occurred while executing executing the before-render script",
                    cause=e
                )
            if error_message:
                if error_message.endswith("\r\n"):
                    error_message = error_message[:-2]
                logger.error(
                    "Error output was returned from the before-rendering script: {0}\nThe console ouput for the "
                    "error:  \n    {1}",
                    error_message,
                    console_output
                )
            if not return_code == 0:
                if error_message:
                    error_message = "The before-render script failed with the following error message: {0}" \
                        .format(error_message)
                else:
                    error_message = (
                        "The before-render script returned {0},"
                        " which indicates an error.".format(return_code)
                    )
                raise RenderError('before_render_script_error', error_message)
        except RenderError as e:
            self.before_render_error = e

    def _post_render_script(self):
        try:
            script = self.render_job_info.camera.on_after_render_script.strip()
            if not script:
                return
            try:
                script_args = [
                    script,
                    self.render_job_info.camera.name,
                    self.render_job_info.snapshot_directory,
                    self.render_job_info.snapshot_filename_format,
                    os.path.join(
                        self.render_job_info.snapshot_directory, self.render_job_info.snapshot_filename_format
                    ),
                    self._output_directory,
                    self._output_filename,
                    self._output_extension,
                    self._rendering_output_file_path,
                    self._synchronized_directory,
                    self._synchronized_filename,

                ]

                logger.info(
                    'Running the following after-render script command: {0} "{1}" "{2}" "{3}" "{4}" "{5}" "{6}" "{7}" '
                    '"{8}" "{9}" "{10}"',

                    script_args[0],
                    script_args[1],
                    script_args[2],
                    script_args[3],
                    script_args[4],
                    script_args[5],
                    script_args[6],
                    script_args[7],
                    script_args[8],
                    script_args[9],
                    script_args[10]
                )

                cmd = utility.POpenWithTimeout()
                return_code = cmd.run(script_args, None)
                console_output = cmd.stdout
                error_message = cmd.stderr
            except utility.POpenWithTimeout.ProcessError as e:
                raise RenderError(
                    'after_render_script_error',
                    "A script occurred while executing executing the after-render script",
                    cause=e
                )
            if error_message:
                if error_message.endswith("\r\n"):
                    error_message = error_message[:-2]
                logger.error(
                    "Error output was returned from the after-rendering script: {0}",
                    error_message
                )
            if not return_code == 0:
                if error_message:
                    error_message = "The after-render script failed with the following error message: {0}" \
                        .format(error_message)
                else:
                    error_message = (
                        "The after-render script returned {0},"
                        " which indicates an error.".format(return_code)
                    )
                raise RenderError('after_render_script_error', error_message)
        except RenderError as e:
            self.after_render_error = e

    def _read_snapshot_metadata(self):
        metadata_path = os.path.join(self.render_job_info.snapshot_directory, SnapshotMetadata.METADATA_FILE_NAME)
        logger.info('Reading snapshot metadata from {}', metadata_path)
        try:
            with open(metadata_path, 'r') as metadata_file:
                dictreader = DictReader(metadata_file, SnapshotMetadata.METADATA_FIELDS)
                self._snapshot_metadata = list(dictreader)
                """Get the number of frames."""
                self._imageCount = len(self._snapshot_metadata)
                logger.info("Found {0} images with metadata.", self._imageCount)
                # add the snapshot count to the output tokens
                self.render_job_info.output_tokens["SNAPSHOTCOUNT"] = "{0}".format(self._imageCount)
                return
        except IOError as e:
            # If we fail to read the metadata, it could be that no snapshots were taken.
            # Let's not throw an error and just render without the metadata
            pass

        # alternative method of counting images without metadata

        if os.path.exists(self.render_job_info.snapshot_directory):
            self._imageCount = len(
                os.listdir(
                    os.path.dirname(
                        os.path.join(
                            self.render_job_info.snapshot_directory, self.render_job_info.snapshot_filename_format))))
        else:
            self._imageCount = 0

        self.render_job_info.output_tokens["SNAPSHOTCOUNT"] = "{0}".format(self._imageCount)
        logger.info("Found {0} images via a manual search.", self._imageCount)

    def _calculate_fps(self):
        self._fps = self._rendering.fps

        if self._rendering.fps_calculation_type == 'duration':

            self._fps = utility.round_to(
                float(self._imageCount) / float(self._rendering.run_length_seconds), 0.001)
            if self._fps > self._rendering.max_fps:
                self._fps = self._rendering.max_fps
            elif self._fps < self._rendering.min_fps:
                self._fps = self._rendering.min_fps
            message = (
                "FPS Calculation Type:{0}, Fps:{1}, NumFrames:{2}, "
                "DurationSeconds:{3}, Max FPS:{4}, Min FPS:{5}"
            )
            logger.info(
                message,
                self._rendering.fps_calculation_type,
                self._fps,
                self._imageCount,
                self._rendering.run_length_seconds,
                self._rendering.max_fps,
                self._rendering.min_fps
            )
        else:
            logger.info("FPS Calculation Type:{0}, Fps:{0}", self._rendering.fps_calculation_type, self._fps)
        # Add the FPS to the output tokens
        self.render_job_info.output_tokens["FPS"] = "{0}".format(int(math.ceil(self._fps)))

    def _set_outputs(self):
        self._output_directory = "{0}{1}{2}{3}".format(
            self.render_job_info.output_tokens["DATADIRECTORY"], os.sep, "timelapse", os.sep
        )
        try:
            self._output_filename = self._rendering.output_template.format(**self.render_job_info.output_tokens)
        except ValueError as e:
            logger.exception("Failed to format the rendering output template.")
            self._output_filename = "RenderingFilenameTemplateError"
        self._output_extension = self._get_extension_from_output_format(self._rendering.output_format)

        # check for a rendered timelapse file collision
        original_output_filename = self._output_filename
        file_number = 0
        while os.path.isfile(
            "{0}{1}.{2}".format(
                self._output_directory,
                self._output_filename,
                self._output_extension)
        ):
            file_number += 1
            self._output_filename = "{0}_{1}".format(original_output_filename, file_number)

        self._rendering_output_file_path = "{0}{1}.{2}".format(
            self._output_directory, self._output_filename, self._output_extension
        )

        synchronized_output_filename = original_output_filename
        # check for a synchronized timelapse file collision
        file_number = 0
        while os.path.isfile("{0}{1}{2}.{3}".format(
            self._octoprintTimelapseFolder,
            os.sep,
            synchronized_output_filename,
            self._output_extension
        )):
            file_number += 1
            synchronized_output_filename = "{0}_{1}".format(original_output_filename, file_number)

        self._synchronized_directory = "{0}{1}".format(self._octoprintTimelapseFolder, os.sep)
        self._synchronized_filename = synchronized_output_filename

    #####################
    # Event Notification
    #####################

    def create_callback_payload(self, return_code, reason):
        return RenderingCallbackArgs(
            reason,
            return_code,
            self.render_job_info.job_id,
            self.render_job_info.job_directory,
            self.render_job_info.snapshot_directory,
            self._output_directory,
            self._output_filename,
            self._output_extension,
            self._synchronized_directory,
            self._synchronized_filename,
            self._synchronize,
            self._imageCount,
            self.render_job_info.timelapse_job_info.SecondsAdded,
            self.render_job_info.job_number,
            self.render_job_info.jobs_remaining,
            self.render_job_info.camera.name,
            self.before_render_error,
            self.after_render_error

        )


    def _render(self):
        """Rendering runnable."""
        # set an error variable to None, we will return None if there are no problems
        r_error = None
        try:
            self.on_prerender_start(self.create_callback_payload(0,"Prerender is starting."))
            self._pre_render_script()

            self._pre_render()

            logger.info("Starting render.")
            self.on_render_start(self.create_callback_payload(0, "Starting to render timelapse."))

            # Temporary directory to store intermediate results of rendering.
            self.temp_rendering_dir = mkdtemp(prefix='octolapse_render')

            if self._ffmpeg is None:
                raise RenderError('ffmpeg_path', "Cannot create movie, path to ffmpeg is unset. "
                                                 "Please configure the ffmpeg path within the "
                                                 "'Features->Webcam & Timelapse' settings tab.")

            if self._rendering.bitrate is None:
                raise RenderError('no-bitrate', "Cannot create movie, desired bitrate is unset. "
                                                "Please set the bitrate within the Octolapse rendering profile.")

            try:
                logger.info("Creating the directory at {0}", self._output_directory)

                if not os.path.exists(self._output_directory):
                    os.makedirs(self._output_directory)
            except Exception as e:
                raise RenderError('create-render-path',
                                  "Render - An exception was thrown when trying to "
                                  "create the rendering path at: {0}.  Please check "
                                  "the logs (plugin_octolapse.log) for details.".format(self._output_directory),
                                  cause=e)

            watermark_path = None
            if self._rendering.enable_watermark:
                watermark_path = self._rendering.selected_watermark
                if watermark_path == '':
                    raise RenderError('watermark-path',
                                      "Render - Watermark was enabled but no watermark file was selected.")
                if not os.path.exists(watermark_path):
                    raise RenderError('watermark-non-existent', "Render - Watermark file does not exist.")

                if sys.platform == "win32":
                    # Because ffmpeg hiccups on windows' drive letters and backslashes we have to give the watermark
                    # path a special treatment. Yeah, I couldn't believe it either...
                    watermark_path = watermark_path.replace(
                        "\\", "/").replace(":", "\\\\:")

            # Do image preprocessing.
            self._preprocess_images(self.temp_rendering_dir)
            # Add pre and post roll.
            self._apply_pre_post_roll(self.temp_rendering_dir)

            # prepare ffmpeg command
            command_str = self._create_ffmpeg_command_string(
                os.path.join(self.temp_rendering_dir, self.render_job_info.snapshot_filename_format),
                self._rendering_output_file_path,
                watermark=watermark_path
            )
            logger.info("Running ffmpeg with command string: {0}", command_str)

            with self.render_job_lock:
                try:
                    p = sarge.run(
                        command_str, stdout=sarge.Capture(), stderr=sarge.Capture())
                except Exception as e:
                    raise RenderError('rendering-exception', "ffmpeg failed during rendering of movie. "
                                                             "Please check plugin_octolapse.log for details.",
                                      cause=e)
                if p.returncode != 0:
                    return_code = p.returncode
                    stderr_text = p.stderr.text
                    raise RenderError('return-code', "Could not render movie, got return code %r: %s" % (
                        return_code, stderr_text))

            # run any post rendering scripts
            self._post_render_script()

            # Delete preprocessed images.
            if self.cleanAfterSuccess:
                shutil.rmtree(self.temp_rendering_dir)

            if self._synchronize:
                # Move the timelapse to the Octoprint timelapse folder.
                try:
                    # get the timelapse folder for the Octoprint timelapse plugin
                    synchronization_path = "{0}{1}.{2}".format(
                        self._synchronized_directory, self._synchronized_filename, self._output_extension
                    )
                    message = (
                        "Synchronizing timelapse with the built in "
                        "timelapse plugin, copying {0} to {1}"
                    )
                    logger.info(message, self._rendering_output_file_path, synchronization_path)
                    shutil.move(self._rendering_output_file_path, synchronization_path)
                    # we've renamed the output due to a sync, update the member
                except Exception as e:
                    raise RenderError('synchronizing-exception',
                                      "Octolapse has failed to synchronize the default timelapse plugin due"
                                      " to an unexpected exception.  Check plugin_octolapse.log for more "
                                      " information.  You should be able to find your video within your "
                                      " OctoPrint  server here:<br/> '{0}'".format(self._rendering_output_file_path),
                                      cause=e)

        except Exception as e:
            if isinstance(e, RenderError):
                r_error = e
            else:
                r_error = RenderError('render-error',
                                      "Unknown render error. Please check plugin_octolapse.log for more details.",
                                      e)
            if self.cleanAfterFail:
                # Delete preprocessed images.
                if self.temp_rendering_dir is not None:
                    shutil.rmtree(self.temp_rendering_dir)

        if r_error is None:
            self.on_render_success(self.create_callback_payload(0, "Timelapse rendering is complete."))
        else:
            self.on_render_error(self.create_callback_payload(0, "The render process failed."), r_error)

    def _preprocess_images(self, preprocessed_directory):
        logger.info("Starting preprocessing of images.")
        if self._snapshot_metadata is None:
            logger.info("Snapshot metadata file missing; skipping preprocessing.")
            # Just copy images over.
            for i in range(self._imageCount):
                file_path = os.path.join(
                    self.render_job_info.snapshot_directory, self.render_job_info.snapshot_filename_format % i)
                output_path = os.path.join(preprocessed_directory, self.render_job_info.snapshot_filename_format % i)
                output_dir = os.path.dirname(output_path)
                if not os.path.exists(output_dir):
                    os.makedirs(output_dir)
                shutil.move(file_path, output_path)
            return

        first_timestamp = float(self._snapshot_metadata[0]['time_taken'])
        for i, data in enumerate(self._snapshot_metadata):
            # TODO:  MAKE SURE THIS WORKS IF THERE ARE ANY ERRORS
            # Variables the user can use in overlay_text_template.format().
            format_vars = {}

            # Extra metadata according to SnapshotMetadata.METADATA_FIELDS.
            format_vars['snapshot_number'] = snapshot_number = int(data['snapshot_number'])
            if i == snapshot_number:
                assert (i == snapshot_number)
                format_vars['file_name'] = data['file_name']
                format_vars['time_taken_s'] = time_taken = float(data['time_taken'])

                # Verify that the file actually exists.
                file_path = os.path.join(
                    self.render_job_info.snapshot_directory,
                    self.render_job_info.snapshot_filename_format % snapshot_number
                )
                if not os.path.isfile(file_path):
                    raise IOError("Cannot find file {}.".format(file_path))

                # Calculate time elapsed since the beginning of the print.
                format_vars['current_time'] = datetime.datetime.fromtimestamp(time_taken).strftime("%Y-%m-%d %H:%M:%S")
                format_vars['time_elapsed'] = str(datetime.timedelta(seconds=round(time_taken - first_timestamp)))

                # Open the image in Pillow and do preprocessing operations.
                image = Image.open(file_path)
                image = self.add_overlay(image,
                                 text_template=self._rendering.overlay_text_template,
                                 format_vars=format_vars,
                                 font_path=self._rendering.overlay_font_path,
                                 font_size=self._rendering.overlay_font_size,
                                 overlay_location=self._rendering.overlay_text_pos,
                                 overlay_text_alignment=self._rendering.overlay_text_alignment,
                                 overlay_text_valign=self._rendering.overlay_text_valign,
                                 overlay_text_halign=self._rendering.overlay_text_halign,
                                 text_color=self._rendering.overlay_text_color)
                # Save processed image.
                output_path = os.path.join(
                    preprocessed_directory, self.render_job_info.snapshot_filename_format % snapshot_number)
                if not os.path.exists(os.path.dirname(output_path)):
                    os.makedirs(os.path.dirname(output_path))
                image.save(output_path)
            else:
                file_path = os.path.join(
                    self.render_job_info.snapshot_directory, self.render_job_info.snapshot_filename_format % i)
                output_path = os.path.join(preprocessed_directory, self.render_job_info.snapshot_filename_format % i)
                output_dir = os.path.dirname(output_path)
                if not os.path.exists(output_dir):
                    os.makedirs(output_dir)
                shutil.move(file_path, output_path)

        logger.info("Preprocessing success!")

    @staticmethod
    def add_overlay(image, text_template, format_vars, font_path, font_size, overlay_location, overlay_text_alignment,
                    overlay_text_valign, overlay_text_halign, text_color):
        """Adds an overlay to an image with the given parameters. The image is not mutated.
        :param image: A Pillow RGB image.
        :returns The image with the overlay added."""
        # No text to draw.
        if not text_template:
            return image
        text = text_template.format(**format_vars)

        # Retrieve the correct font.
        if not font_path:
            raise RenderError('overlay-font', "No overlay font was specified when attempting to add overlay.")
        font = ImageFont.truetype(font_path, size=font_size)

        # Create the image to draw on.
        text_image = Image.new('RGBA', image.size, (255, 255, 255, 0))
        d = ImageDraw.Draw(text_image)

        # Process the text position to improve the alignment.
        if isinstance(overlay_location, basestring):
            overlay_location = json.loads(overlay_location)
        x, y = tuple(overlay_location)
        # valign.
        if overlay_text_valign == 'top':
            pass
        elif overlay_text_valign == 'middle':
            textsize = d.multiline_textsize(text, font=font, spacing=0)
            y += image.size[1] / 2 - textsize[1] / 2
        elif overlay_text_valign == 'bottom':
            textsize = d.multiline_textsize(text, font=font, spacing=0)
            y += image.size[1] - textsize[1]
        else:
            raise RenderError('overlay-text-valign',
                              "An invalid overlay text valign ({}) was specified.".format(overlay_text_valign))
        # halign.
        if overlay_text_halign == 'left':
            pass
        elif overlay_text_halign == 'center':
            textsize = d.multiline_textsize(text, font=font, spacing=0)
            x += image.size[0] / 2 - textsize[0] / 2
        elif overlay_text_halign == 'right':
            textsize = d.multiline_textsize(text, font=font, spacing=0)
            x += image.size[0] - textsize[0]
        else:
            raise RenderError('overlay-text-halign',
                              "An invalid overlay text halign ({}) was specified.".format(overlay_text_halign))

        # Draw overlay text.
        if isinstance(text_color, basestring):
            text_color = json.loads(text_color)
        d.multiline_text(xy=(x, y), text=text, fill=tuple(text_color), font=font, align=overlay_text_alignment)
        return Image.alpha_composite(image.convert('RGBA'), text_image).convert('RGB')

    def _apply_pre_post_roll(self, image_dir):
        logger.info("Starting pre/post roll.")
        # start with pre-roll, since it will require a bunch of renaming
        pre_roll_frames = int(self._rendering.pre_roll_seconds * self._fps)
        if pre_roll_frames > 0:

            # create a variable to hold the new path of the first image
            first_image_path = ""
            # rename all of the current files. The snapshot number should be
            # incremented by the number of pre-roll frames. Start with the last
            # image and work backwards to avoid overwriting files we've already moved
            for image_number in range(self._imageCount - 1, -1, -1):
                current_image_path = os.path.join(image_dir, self.render_job_info.snapshot_filename_format % image_number)
                new_image_path = os.path.join(image_dir,
                                              self.render_job_info.snapshot_filename_format % (image_number + pre_roll_frames))
                if image_number == 0:
                    first_image_path = new_image_path
                shutil.move(current_image_path, new_image_path)
            # get the path of the first image
            # copy the first frame as many times as we need
            for image_index in range(pre_roll_frames):
                new_image_path = os.path.join(image_dir, self.render_job_info.snapshot_filename_format % image_index)
                shutil.copy(first_image_path, new_image_path)
        # finish with post roll since it's pretty easy
        post_roll_frames = int(self._rendering.post_roll_seconds * self._fps)
        if post_roll_frames > 0:
            last_frame_index = self._imageCount + pre_roll_frames - 1
            last_image_path = os.path.join(image_dir, self.render_job_info.snapshot_filename_format % last_frame_index)
            for image_index in range(post_roll_frames):
                image_number = image_index + self._imageCount + pre_roll_frames
                new_image_path = os.path.join(image_dir, self.render_job_info.snapshot_filename_format % image_number)
                shutil.copy(last_image_path, new_image_path)
        logger.info("Pre/post roll generated successfully.")

    @staticmethod
    def _get_extension_from_output_format(output_format):
        EXTENSIONS = {"avi": "avi",
                      "flv": "flv",
                      "h264": "mp4",
                      "vob": "vob",
                      "mp4": "mp4",
                      "mpeg": "mpeg",
                      "gif": "gif"}
        return EXTENSIONS.get(output_format.lower(), "mp4")

    @staticmethod
    def _get_vcodec_from_output_format(output_format):
        VCODECS = {"avi": "mpeg4",
                   "flv": "flv1",
                   "gif": "gif",
                   "h264": "h264",
                   "mp4": "mpeg4",
                   "mpeg": "mpeg2video",
                   "vob": "mpeg2video"}
        return VCODECS.get(output_format.lower(), "mpeg2video")

    def _create_ffmpeg_command_string(self, input_file_format, output_file, watermark=None, pix_fmt="yuv420p"):
        """
        Create ffmpeg command string based on input parameters.
        Arguments:
            input_file_format (str): Absolute path to input files including file mask
            output_file (str): Absolute path to output file
            watermark (str): Path to watermark to apply to lower left corner.
            pix_fmt (str): Pixel format to use for output. Default of yuv420p should usually fit the bill.
        Returns:
            (str): Prepared command string to render `input` to `output` using ffmpeg.
        """

        logger = logging.getLogger(__name__)

        v_codec = self._get_vcodec_from_output_format(self._rendering.output_format)

        command = [self._ffmpeg, '-framerate', str(self._fps), '-loglevel', 'error', '-i',
                   '"{}"'.format(input_file_format)]
        command.extend(
            ['-threads', str(self._threads), '-r', str(self._fps), '-y', '-b', str(self._rendering.bitrate), '-vcodec', v_codec])

        filter_string = self._create_filter_string(watermark=watermark, pix_fmt=pix_fmt)

        if filter_string is not None:
            logger.debug("Applying video filter chain: {}".format(filter_string))
            command.extend(["-vf", sarge.shell_quote(filter_string)])

        # finalize command with output file
        logger.debug("Rendering movie to {}".format(output_file))
        command.append('"{}"'.format(output_file))

        return " ".join(command)

    @classmethod
    def _create_filter_string(cls, watermark=None, pix_fmt="yuv420p"):
        """
        Creates an ffmpeg filter string based on input parameters.
        Arguments:
            watermark (str): Path to watermark to apply to lower left corner.
            pix_fmt (str): Pixel format to use, defaults to "yuv420p" which should usually fit the bill
        Returns:
            (str): filter string
        """

        filters = []

        # apply pixel format
        filters.append('[{{prev_filter}}] format={} [{{next_filter}}]'.format(pix_fmt))

        # add watermark if configured
        if watermark is not None:
            filters.append(
                'movie={} [wm]; [{{prev_filter}}][wm] overlay=10:main_h-overlay_h-10 [{{next_filter}}]'.format(
                    watermark))

        # Apply string format to each filter to chain them together.
        filter_names = ['f' + str(x) for x in range(len(filters))] + ['out']
        for i, previous_filter_name, next_filter_name in zip(range(len(filters)), filter_names, filter_names[1:]):
            filters[i] = filters[i].format(prev_filter=previous_filter_name, next_filter=next_filter_name)
        # Build the final filter string.
        filter_string = "; ".join(filters)

        return filter_string

    @staticmethod
    def _notify_callback(callback, *args, **kwargs):
        """Notifies registered callbacks of type `callback`."""
        if callback is not None and callable(callback):
            callback(*args, **kwargs)


class RenderError(Exception):
    def __init__(self, type, message, cause=None):
        super(Exception, self).__init__()
        self.type = type
        self.cause = cause if cause is not None else None
        self.message = message

    def __str__(self):
        if self.cause is None:
            return "{}: {}".format(self.type, self.message, str(self.cause))

        return "{}: {}.  Inner Exception: {}".format(self.type, self.message, str(self.cause))


class RenderingCallbackArgs(object):
    def __init__(
        self,
        reason,
        return_code,
        job_id,
        job_directory,
        snapshot_directory,
        rendering_directory,
        rendering_filename,
        rendering_extension,
        synchronized_directory,
        synchronized_filename,
        synchronize,
        snapshot_count,
        seconds_added_to_print,
        job_number,
        jobs_remaining,
        camera_name,
        before_render_error,
        after_render_error
    ):
        self.Reason = reason
        self.ReturnCode = return_code
        self.JobId = job_id
        self.JobDirectory = job_directory
        self.SnapshotDirectory = snapshot_directory
        self.RenderingDirectory = rendering_directory
        self.RenderingFilename = rendering_filename
        self.RenderingExtension = rendering_extension
        self.SynchronizedDirectory = synchronized_directory
        self.SynchronizedFilename = synchronized_filename
        self.Synchronize = synchronize
        self.SnapshotCount = snapshot_count
        self.SecondsAddedToPrint = seconds_added_to_print
        self.JobNumber = job_number
        self.JobsRemaining = jobs_remaining
        self.CameraName = camera_name
        self.BeforeRenderError = before_render_error
        self.AfterRenderError = after_render_error

    def get_rendering_filename(self):
        return "{0}.{1}".format(self.RenderingFilename, self.RenderingExtension)

    def get_synchronization_filename(self):
        return "{0}.{1}".format(self.SynchronizedFilename, self.RenderingExtension)

    def get_rendering_path(self):
        return "{0}{1}".format(self.RenderingDirectory, self.get_rendering_filename())

    def get_synchronization_path(self):
        return "{0}{1}".format(self.SynchronizedDirectory, self.get_synchronization_filename())