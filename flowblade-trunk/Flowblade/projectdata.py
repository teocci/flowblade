"""
    Flowblade Movie Editor is a nonlinear video editor.
    Copyright 2012 Janne Liljeblad.

    This file is part of Flowblade Movie Editor <http://code.google.com/p/flowblade>.

    Flowblade Movie Editor is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Flowblade Movie Editor is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with Flowblade Movie Editor.  If not, see <http://www.gnu.org/licenses/>.
"""

"""
Module contains objects used to capture project data.
"""

import datetime
import gtk
import multiprocessing
import mlt
import md5
import os
import time
import thread
import threading

import appconsts
import editorpersistance
import editorstate
import medialog
import mltprofiles
import patternproducer
import miscdataobjects
import respaths
import sequence
import utils


SAVEFILE_VERSION = 4 # this is changed when backwards incompatible changes 
                     # are introduced to project files to allow for fixing them at load time

FALLBACK_THUMB = "fallback_thumb.png"

# Project events
EVENT_CREATED_BY_NEW_DIALOG = 0
EVENT_CREATED_BY_SAVING = 1
EVENT_SAVED = 2
EVENT_SAVED_AS = 3
EVENT_RENDERED = 4

thumbnail_thread = None


class Project:
    """
    Collection of all the data edited as a single unit.
    
    Contains collection of media files and one or more sequences
    Only one sequence is edited at a time.
    """
    def __init__(self, profile): #profile is mlt.Profile here, made using file path
        self.name = _("untitled") + appconsts.PROJECT_FILE_EXTENSION
        self.profile = profile
        self.profile_desc = profile.description()
        self.bins = []
        self.media_files = {} # MediaFile.id(key) -> MediaFile object(value)
        self.sequences = []
        self.next_media_file_id = 0 
        self.next_bin_number = 1 # This is for creating name for new bin 
        self.next_seq_number = 1 # This is for creating name for new sequence
        self.last_save_path = None
        self.events = []
        self.media_log = []
        self.proxy_data = miscdataobjects.ProjectProxyEditingData()
        self.SAVEFILE_VERSION = SAVEFILE_VERSION
        
        # c_seq is the currently edited Sequence
        self.add_unnamed_sequence()
        self.c_seq = self.sequences[0]
        
        # c_bin is the currently displayed bin
        self.add_unnamed_bin()
        self.c_bin = self.bins[0]
        
        # We're running a thumbnail thread here.
        self.start_thumbnail_thread()
    
    def start_thumbnail_thread(self):
        # Thumbnails are made in thread to avoid some MLT crashes
        global thumbnail_thread
        if thumbnail_thread == None:
            thumbnail_thread = ThumbnailThread()
            thumbnail_thread.set_context(self.profile)
            thumbnail_thread.start()

    def add_image_sequence_media_object(self, resource_path, name, length):
        media_object = self.add_media_file(resource_path)
        media_object.length = length
        media_object.name = name

    def add_media_file(self, file_path):
        """
        Adds media file to project if exists and file is of right type.
        """
        (dir, file_name) = os.path.split(file_path)
        (name, ext) = os.path.splitext(file_name)
        
        # Get media type
        media_type = sequence.get_media_type(file_path)
        
        # Get length and icon
        if media_type == appconsts.AUDIO:
            icon_path = respaths.IMAGE_PATH + "audio_file.png"
            length = thumbnail_thread.get_file_length(file_path)
        else: # For non-audio we need write a thumbbnail file and get file lengh while we're at it
             (icon_path, length) = thumbnail_thread.write_image(file_path)

          # Create media file object
        media_object = MediaFile(self.next_media_file_id, file_path, 
                               file_name, media_type, length, icon_path)
            
        self._add_media_object(media_object)
        
        return media_object

    def add_pattern_producer_media_object(self, media_object):
        self._add_media_object(media_object)

    def _add_media_object(self, media_object):
        """
        Adds media file or color clip to project data structures.
        """
        self.media_files[media_object.id] = media_object
        self.next_media_file_id += 1

        # Add to bin
        self.c_bin.file_ids.append(media_object.id)

    def media_file_exists(self, file_path):
        for key, media_file in self.media_files.items():
            if media_file.type == appconsts.PATTERN_PRODUCER:
                continue
            if file_path == media_file.path:
                return True
        return False

    def get_media_file_for_path(self, file_path):
        for key, media_file in self.media_files.items():
            if media_file.type == appconsts.PATTERN_PRODUCER:
                continue
            if file_path == media_file.path:
                return media_file
        return None

    def delete_media_file_from_current_bin(self, media_file):
        self.c_bin.file_ids.pop(media_file.id)

    def add_unnamed_bin(self):
        """
        Adds bin with default name.
        """
        name = _("bin_") + str(self.next_bin_number)
        self.bins.append(Bin(name))
        self.next_bin_number += 1
    
    def add_unnamed_sequence(self):
        """
        Adds sequence with default name
        """
        name = _("sequence_") + str(self.next_seq_number)
        self.add_named_sequence(name)
        
    def add_named_sequence(self, name):
        seq = sequence.Sequence(self.profile, name)
        seq.create_default_tracks()
        self.sequences.append(seq)
        self.next_seq_number += 1

    def get_filtered_media_log_events(self, incl_starred, incl_not_starred):
        filtered_events = []
        for media_log_event in self.media_log:
            if self._media_log_included_by_starred(media_log_event.starred, incl_starred, incl_not_starred):
                filtered_events.append(media_log_event)
        return filtered_events

    def _media_log_included_by_starred(self, starred, incl_starred, incl_not_starred):
        if starred == True and incl_starred == True:
            return True
        if starred == False and incl_not_starred == True:
            return True
        return False

    def delete_media_log_events(self, delete_events):
        for e in delete_events:
            self.media_log.remove(e)

    def exit_clip_renderer_process(self):
        pass


class MediaFile:
    """
    Media file that can added to and edited in Sequence.
    """
    def __init__(self, id, file_path, name, media_type, length, icon_path):
        self.id = id
        self.path = file_path
        self.name = name
        self.type = media_type
        self.length = length
        self.icon_path = icon_path
        self.icon = None
        self.create_icon()

        self.mark_in = -1
        self.mark_out = -1

        self.has_proxy_file = False
        self.is_proxy_file = False
        self.second_file_path = None # to proxy when original, to original when proxy

        # Set default length for graphics files
        (f_name, ext) = os.path.splitext(self.name)
        if utils.file_extension_is_graphics_file(ext):
            in_fr, out_fr, l = editorpersistance.get_graphics_default_in_out_length()
            self.mark_in = in_fr
            self.mark_out = out_fr
            self.length = l
 
    def create_icon(self):
        try:
            icon = gtk.gdk.pixbuf_new_from_file(self.icon_path)
            self.icon = icon.scale_simple(appconsts.THUMB_WIDTH, appconsts.THUMB_HEIGHT, \
                                          gtk.gdk.INTERP_BILINEAR)
        except:
            print "failed to make icon from:", self.icon_path
            self.icon_path = respaths.IMAGE_PATH + FALLBACK_THUMB
            icon = gtk.gdk.pixbuf_new_from_file(self.icon_path)
            self.icon = icon.scale_simple(appconsts.THUMB_WIDTH, appconsts.THUMB_HEIGHT, \
                                          gtk.gdk.INTERP_BILINEAR)

    def create_proxy_path(self, proxy_width, proxy_height, file_extesion):
        md_str = md5.new(self.path + str(proxy_width) + str(proxy_height)).hexdigest()
        return str(editorpersistance.prefs.render_folder + "/proxies/" + md_str + "." + file_extesion) # str() because we get unicode here

    def add_proxy_file(self, proxy_path):
        self.has_proxy_file = True
        self.second_file_path = proxy_path

    def add_existing_proxy_file(self, proxy_width, proxy_height, file_extesion):
        proxy_path = self.create_proxy_path(proxy_width, proxy_height, file_extesion)
        self.add_proxy_file(proxy_path)

    def set_as_proxy_media_file(self):
        self.path, self.second_file_path = self.second_file_path, self.path
        self.is_proxy_file = True
        
    def set_as_original_media_file(self):
        self.path, self.second_file_path = self.second_file_path, self.path
        self.is_proxy_file = False


class BinColorClip:
    # DECPRECATED, this is replaced by patternproducer.BinColorClip.
    # This is kept for project file backwards compatiblity,
    # unpickle fails for color clips if this isn't here.
    # kill 2016-ish
    def __init__(self, id, name, gdk_color_str):
        self.id = id
        self.name = name
        self.gdk_color_str = gdk_color_str
        self.length = 15000
        self.type = appconsts.PATTERN_PRODUCER
        self.icon = None
        self.create_icon()
        self.patter_producer_type = patternproducer.COLOR_CLIP

        self.mark_in = -1
        self.mark_out = -1

    def create_icon(self):
        icon = gtk.gdk.Pixbuf(gtk.gdk.COLORSPACE_RGB, False, 8, appconsts.THUMB_WIDTH, appconsts.THUMB_HEIGHT)
        pixel = utils.gdk_color_str_to_int(self.gdk_color_str)
        icon.fill(pixel)
        self.icon = icon


class Bin:
    """
    Group of media files
    """
    def __init__(self, name="name"):
        self.name  = name # Displayed name
        self.file_ids = [] # List of media files ids in the bin.
                           # Ids are increasing integers given in 
                           # Project.add_media_file(...)
        
        
class ProducerNotValidError(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)


class ThumbnailThread(threading.Thread):

    def run(self):
        """
        Runs and blocks 
        """
        self.file_path = ""
        self.thumbnail_path = ""
        self.consumer = None
        self.producer = None
        self.running = True
        self.stopped = False

        while self.running:
            time.sleep(1)
            
        self.stopped = True
        
    def set_context(self, profile):
        self.profile = profile
    
    def write_image(self, file_path):
        """
        Writes thumbnail image from file producer
        """
        # Get data
        self.file_path = file_path
        md_str = md5.new(file_path).hexdigest()
        self.thumbnail_path = editorpersistance.prefs.thumbnail_folder + "/" + md_str +  ".png"
        
        # Create consumer
        self.consumer = mlt.Consumer(self.profile, "avformat", 
                                     self.thumbnail_path)
        self.consumer.set("real_time", 0)
        self.consumer.set("vcodec", "png")

        # Create one frame producer
        self.producer = mlt.Producer(self.profile, str(self.file_path))
        if self.producer.is_valid() == False:
            raise ProducerNotValidError(file_path)

        length = self.producer.get_length()
        frame = length / 2
        self.producer = self.producer.cut(frame, frame)

        # Connect and write image
        self.consumer.connect(self.producer)
        self.consumer.run()
        
        return (self.thumbnail_path, length)

    def get_file_length(self, file_path):
        # This is used for audio files which don't need a thumbnail written
        # but do need file length known
        # Get data
        self.file_path = file_path

        # Create one frame producer
        self.producer = mlt.Producer(self.profile, str(self.file_path))
        return self.producer.get_length()

    def shutdown(self):
        if self.consumer != None:
            self.consumer.stop()
        self.running = False

# ----------------------------------- project and media log events
class ProjectEvent:
    def __init__(self, event_type, data):
        self.event_type = event_type
        self.timestamp = datetime.datetime.now()
        self.data = data

    def get_date_str(self):
        date_str = self.timestamp.strftime('%d %B, %Y - %H:%M')
        date_str = date_str.lstrip('0')
        return date_str

    def get_desc_and_path(self):
        if self.event_type == EVENT_CREATED_BY_NEW_DIALOG:
            return (_("Created using dialog"), None)
        elif self.event_type == EVENT_CREATED_BY_SAVING:
            return (_("Created using Save As... "), self.data)
        elif self.event_type == EVENT_SAVED:
            return (_("Saved "), self.data)
        elif self.event_type == EVENT_SAVED_AS:
            name, path = self.data
            return (_("Saved as ") + name, path)
        elif self.event_type == EVENT_RENDERED:
            return (_("Rendered "), self.data)
        else:
            return ("Unknown project event, bug or data corruption", None)



# ------------------------------- MODULE FUNCTIONS
def get_default_project():
    """
    Creates the project displayed at start up.
    """
    profile = mltprofiles.get_default_profile()
    project = Project(profile)
    return project


    
    
    
