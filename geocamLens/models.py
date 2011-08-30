# __BEGIN_LICENSE__
# Copyright (C) 2008-2010 United States Government as represented by
# the Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
# __END_LICENSE__

import os
import sys
import glob
import shutil
import datetime
import random
import re
from cStringIO import StringIO

import pytz
import PIL.Image
from django.db import models
from django.utils.safestring import mark_safe
from tagging.fields import TagField
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.contrib.contenttypes import generic
import tagging

from geocamUtil import anyjson as json
from geocamUtil.models.ExtrasField import ExtrasField
from geocamUtil.models.UuidField import UuidField
from geocamUtil.models.managers import AbstractModelManager, FinalModelManager
from geocamUtil.icons import ICON_URL_CACHE, getIconSize, getIconUrl
from geocamUtil.gpx import TrackLog
from geocamUtil.Xmp import Xmp
from geocamUtil.TimeUtil import parseUploadTime
from geocamUtil.FileUtil import mkdirP
from geocamUtil import TimeUtil
import geocamCore.models as coreModels

from geocamLens import settings

TIME_ZONES = None
try:
    import pytz
except ImportError:
    TIME_ZONES = ['US/Pacific'] # try to fail gracefully
else:
    TOP_TIME_ZONES = ['US/Pacific', 'US/Eastern', 'US/Central', 'US/Mountain']
    TIME_ZONES = TOP_TIME_ZONES + [tz for tz in pytz.common_timezones if tz not in TOP_TIME_ZONES]
TIME_ZONE_CHOICES = [(x,x) for x in TIME_ZONES]
DEFAULT_TIME_ZONE = TIME_ZONES[0]

PERM_VIEW = 0
PERM_POST = 1
PERM_EDIT = 2
PERM_VALIDATE = 3
PERM_ADMIN = 4

PERMISSION_CHOICES = ((PERM_VIEW, 'view'),
                      (PERM_POST, 'post'),
                      (PERM_VALIDATE, 'validate'),
                      (PERM_ADMIN, 'admin'),
                      )

YAW_REF_CHOICES = (('', 'unknown'),
                   ('T', 'true'),
                   ('M', 'magnetic'),
                   )
YAW_REF_LOOKUP = dict(YAW_REF_CHOICES)
YAW_REF_LOOKUP[''] = None
DEFAULT_YAW_REF = YAW_REF_CHOICES[0][0]

ALTITUDE_REF_CHOICES = (('', 'unknown'),
                        ('S', 'sea level'),
                        ('E', 'ellipsoid wgs84'),
                        ('G', 'ground surface'),
                        )
ALTITUDE_REF_LOOKUP = dict(ALTITUDE_REF_CHOICES)
ALTITUDE_REF_LOOKUP[''] = None
DEFAULT_ALTITUDE_REF = ALTITUDE_REF_CHOICES[0][0]

STATUS_CHOICES = (('p', 'pending'), # in db but not fully processed yet
                  ('a', 'active'),  # active, display this to user
                  ('d', 'deleted'), # deleted but not purged yet
                  )
# define constants like STATUS_PENDING based on above choices
for code, label in STATUS_CHOICES:
    globals()['STATUS_' + label.upper()] = code

WF_NEEDS_EDITS = 0
WF_SUBMITTED_FOR_VALIDATION = 1
WF_VALID = 2
WF_REJECTED = 3
WORKFLOW_STATUS_CHOICES = ((WF_NEEDS_EDITS, 'Needs edits'),
                           (WF_SUBMITTED_FOR_VALIDATION, 'Submitted for validation'),
                           (WF_VALID, 'Valid'),
                           (WF_REJECTED, 'Rejected'),
                           )
DEFAULT_WORKFLOW_STATUS = WF_SUBMITTED_FOR_VALIDATION

CARDINAL_DIRECTIONS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                       'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']

class Snapshot(models.Model):
    imgType = models.ForeignKey(ContentType, editable=False)
    imgId = models.PositiveIntegerField()
    uuid = UuidField()
    xmin = models.FloatField()
    ymin = models.FloatField()
    xmax = models.FloatField()
    ymax = models.FloatField()
    title = models.CharField(max_length=64)
    comment = models.TextField()
    status = models.CharField(max_length=1, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    dateCreated = models.DateTimeField(null=True, blank=True)
    dateUpdated = models.DateTimeField(null=True, blank=True)

    # img is a virtual field, not actually present in the db.  it
    # specifies which image this snapshot is associated with based on
    # imgType (which db table to look in) and imgId (which row in the
    # table).
    img = generic.GenericForeignKey('imgType', 'imgId')

    def save(self, updateDate=True, **kwargs):
        if updateDate:
            self.dateUpdated = datetime.datetime.utcnow()
        super(Snapshot, self).save(**kwargs)

    def __unicode__(self):
        return self.title

class Image(coreModels.PointFeature):
    roll = models.FloatField(blank=True, null=True) # degrees, 0 is level, right-hand rotation about x in NED frame
    pitch = models.FloatField(blank=True, null=True) # degrees, 0 is level, right-hand rotation about y in NED frame
    # compass degrees, 0 = north, increase clockwise as viewed from above
    yaw = models.FloatField(blank=True, null=True,
                            verbose_name='Heading')
    yawRef = models.CharField(blank=True, max_length=1, choices=YAW_REF_CHOICES, default=DEFAULT_YAW_REF,
                              verbose_name='Heading ref.')
    widthPixels = models.PositiveIntegerField()
    heightPixels = models.PositiveIntegerField()
    objects = AbstractModelManager(parentModel=coreModels.PointFeature)

    # snapshot_set is a virtual field, not actually present in the db,
    # which specifies how to look up the snapshots associated with this
    # image.
    snapshot_set = generic.GenericRelation(Snapshot,
                                           content_type_field='imgType',
                                           object_id_field='imgId')

    viewerExtension = '.jpg'

    class Meta:
        abstract = True

    def getActiveSnapshots(self):
        return self.snapshot_set.filter(status=STATUS_ACTIVE)

    def getThumbnailPath(self, width):
        return os.path.join(self.getDir(), 'th%d.jpg' % width)

    def calcThumbSize(self, fullWidth, fullHeight, maxOutWidth, maxOutHeight=None):
        if maxOutHeight == None:
            maxOutHeight = (maxOutWidth * 3) // 4
        if float(maxOutWidth) / fullWidth < float(maxOutHeight) / fullHeight:
            thumbWidth = maxOutWidth
            thumbHeight = int(float(maxOutWidth)/fullWidth * fullHeight)
        else:
            thumbWidth = int(float(maxOutHeight)/fullHeight * fullWidth)
            thumbHeight = maxOutHeight
        return (thumbWidth, thumbHeight)

    def getThumbSize(self, width):
        return self.calcThumbSize(self.widthPixels, self.heightPixels, width)

    def getImagePath(self, version=None):
        return os.path.join(self.getDir(version), 'full.jpg')

    def getThumbnailUrl(self, width):
        return '%s/th%d.jpg' % (self.getDirUrl(), width)

    def makeThumbnail0(self, previewOriginalPath, thumbSize):
        maxOutWidth, maxOutHeight = thumbSize
        if previewOriginalPath is not None and not os.path.exists(self.getThumbnailPath(maxOutWidth)):
            im = PIL.Image.open(previewOriginalPath)
            fullWidth, fullHeight = im.size
            thumbWidth, thumbHeight = self.calcThumbSize(fullWidth, fullHeight, maxOutWidth, maxOutHeight)
            try:
                im.thumbnail((thumbWidth, thumbHeight), PIL.Image.ANTIALIAS)
            except IOError, e:
                # fall back to resize
                im.resize((thumbWidth, thumbHeight), PIL.Image.ANTIALIAS)
            mkdirP(self.getDir())
            im.save(self.getThumbnailPath(maxOutWidth))

    def makeThumbnail(self, thumbSize):
        previewOriginalPath = self.getImagePath()
        self.makeThumbnail0(previewOriginalPath, thumbSize)

    def galleryThumb(self):
        w0, h0 = settings.GEOCAM_CORE_GALLERY_THUMB_SIZE
        w, h = self.getThumbSize(w0)
        return mark_safe('<td style="vertical-align: top; width: %dpx; height: %dpx;"><img src="%s" width="%d" height="%d"/></td>' % (w0, h0, self.getThumbnailUrl(w0), w, h))

    def getRotatedIconDict(self):
        if self.yaw == None:
            return self.getStyledIconDict()
        rot = self.yaw
        rotRounded = 10 * int(0.1 * rot + 0.5)
        if rotRounded == 360:
            rotRounded = 0
        return self.getStyledIconDict(kind='', suffix='%03d' % rotRounded)

    def process(self, importFile=None):
        self.status = STATUS_ACTIVE
        self.processed = True
        if importFile and not os.path.exists(self.getImagePath()):
            if not os.path.exists(self.getDir()):
                mkdirP(self.getDir())
            shutil.copyfile(importFile, self.getImagePath())
        self.makeThumbnail(settings.GEOCAM_CORE_GALLERY_THUMB_SIZE)
        self.makeThumbnail(settings.GEOCAM_CORE_DESC_THUMB_SIZE)
        # remember to call save() after process()

    def getCaptionHtml(self):
        return ''

    def getBalloonHtml(self, request):
        result = ['<div>\n']
        result.append(self.getCaptionHeader())
        
        viewerUrl = request.build_absolute_uri(self.getViewerUrl())
        if self.widthPixels != 0:
            dw, dh = self.getThumbSize(settings.GEOCAM_CORE_DESC_THUMB_SIZE[0])
            thumbnailUrl = request.build_absolute_uri(self.getThumbnailUrl(settings.GEOCAM_CORE_DESC_THUMB_SIZE[0]))
            result.append("""
  <a href="%(viewerUrl)s"
     title="Show high-res view">
    <img
     src="%(thumbnailUrl)s"
     width="%(dw)f"
     height="%(dh)f"
     border="0"
     />
  </a>
""" % dict(viewerUrl=viewerUrl,
           thumbnailUrl=thumbnailUrl,
           dw=dw,
           dh=dh))
        else:
            result.append("""
<div><a href="%(viewerUrl)s">Show detail view</a></div>
""" % dict(viewerUrl=viewerUrl))
        result.append(self.getCaptionInfoTable())
        result.append('</div>\n')
        return ''.join(result)

    def getXmpVals(self, storePath):
        xmp = Xmp(storePath)
        xmpVals = xmp.getDict()
        return xmpVals

    def getUploadImageFormVals(self, formData):
        yaw, yawRef = Xmp.normalizeYaw(formData.get('yaw', None),
                                       formData.get('yawRef', None))
        altitude, altitudeRef = Xmp.normalizeYaw(formData.get('altitude', None),
                                                 formData.get('altitudeRef', None))

        folderName = formData.get('folder', None)
        folder = None
        if folderName:
            folderMatches = coreModels.Folder.objects.filter(name=folderName)
            if folderMatches:
                folder = folderMatches[0]
        if folder == None:
            folder = coreModels.Folder.objects.get(id=1)

        # FIX figure out what time zone to show
        tz = pytz.timezone(settings.TIME_ZONE)
        timestampStr = Xmp.checkMissing(formData.get('cameraTime', None))
        if timestampStr == None:
            timestampUtc = None
        else:
            timestampLocal = parseUploadTime(timestampStr)
            if timestampLocal.tzinfo == None:
                timestampLocal = tz.localize(timestampLocal)
            timestampUtc = timestampLocal.astimezone(pytz.utc).replace(tzinfo=None)

        # special case: remove 'default' tag inserted by older versions of GeoCam Mobile
        tagsOrig = formData.get('tags', None)
        tagsList = [t for t in tagging.utils.parse_tag_input(tagsOrig)
                    if t != 'default']
        tagsStr = self.makeTagsString(tagsList)

        formVals0 = dict(uuid=formData.get('uuid', None),
                         name=formData.get('name', None),
                         author=formData.get('author', None),
                         notes=formData.get('notes', None),
                         tags=tagsStr,
                         latitude=formData.get('latitude', None),
                         longitude=formData.get('longitude', None),
                         altitude=formData.get('altitude', None),
                         altitudeRef=formData.get('altitudeRef', None),
                         timestamp=timestampUtc,
                         folder=folder,
                         yaw=yaw,
                         yawRef=yawRef)
        formVals = dict([(k, v) for k, v in formVals0.iteritems()
                         if Xmp.checkMissing(v) != None])
        return formVals

    @staticmethod
    def makeTagsString(tagsList):
        tagsList = list(set(tagsList))
        tagsList.sort()
        
        # modeled on tagging.utils.edit_string_for_tags
        names = []
        useCommas = False
        for tag in tagsList:
            if ' ' in tag:
                names.append('"%s"' % tag)
            else:
                names.append(tag)
            if ',' in tag:
                useCommas = True
        if useCommas:
            return ', '.join(names)
        else:
            return ' '.join(names)

    def processVals(self, vals):
        if vals.has_key('tags'):
            tagsList = tagging.utils.parse_tag_input(vals['tags'])
        else:
            tagsList = []

        # find any '#foo' hashtags in notes and add them to the tags field
        if vals.has_key('notes'):
            for hashtag in re.finditer('\#([\w0-9_]+)', vals['notes']):
                tagsList.append(hashtag.group(1))
            vals['tags'] = self.makeTagsString(tagsList)

        # if one of the tags is the name of an icon, use that icon
        for t in tagsList:
            if t in ICON_URL_CACHE:
                vals['icon'] = t
                break

    def getImportVals(self, storePath=None, uploadImageFormData=None):
        vals = {'icon': settings.GEOCAM_LENS_DEFAULT_ICON}

        if storePath != None:
            xmpVals = self.getXmpVals(storePath)
            print >>sys.stderr, 'getImportVals: exif/xmp data:', xmpVals
            vals.update(xmpVals)

        if uploadImageFormData != None:
            formVals = self.getUploadImageFormVals(uploadImageFormData)
            print >>sys.stderr, 'getImportVals: UploadImageForm data:', formVals
            vals.update(formVals)

        self.processVals(vals)

        return vals

    def readImportVals(self, *args, **kwargs):
        vals = self.getImportVals(*args, **kwargs)
        for k, v in vals.iteritems():
            setattr(self, k, v)

    def getKmlAdvanced(self):
        # FIX: fix this up and rename it to getKml()
        return ("""
<PhotoOverlay %(uuid)s>
  <name>%(requestId)s</name>
  <Style>
    <IconStyle><Icon></Icon></IconStyle>
    <BalloonStyle>
      <displayMode>hide</displayMode><!-- suppress confusing description balloon -->
    </BalloonStyle>
  </Style>
  <Camera>
    <longitude></longitude>
    <latitude></latitude>
    <altitude></altitude>
    <heading>{{ self.cameraRotation.yawDegrees }}</heading>
    <tilt>90</tilt>
    <roll>{{ self.cameraRotation.rollDegrees }}</roll>
  </Camera>
  <Icon>
    <href>{{ self.hrefBase }}s/photos/{{ self.rollName }}/{{ self.imageFile }}</href>
  </Icon>
  <Point>
    <coordinates>{{ billboardLonLatAlt.commaString }}</coordinates>
    <altitudeMode>relativeToGround</altitudeMode>
  </Point>
  <ViewVolume>
    <near>{{ settings.STYLE.billboard.photoNear }}</near>
    <leftFov>-{{ halfWidthDegrees }}</leftFov>
    <rightFov>{{ halfWidthDegrees }}</rightFov>
    <bottomFov>-{{ halfHeightDegrees }}</bottomFov>
    <topFov>{{ halfHeightDegrees }}</topFov>
  </ViewVolume>
</PhotoOverlay>
""" % dict())

    def getProperties(self):
        result = super(Image, self).getProperties()
        result.update(sizePixels=[self.widthPixels, self.heightPixels],
                      pointIcon=self.getIconDict('Point'),
                      rotatedIcon=self.getRotatedIconDict(),
                      roll=self.roll,
                      pitch=self.pitch,
                      yaw=self.yaw,
                      yawRef=YAW_REF_LOOKUP[self.yawRef])
        return result

    def getCaptionTimeZone(self):
        return pytz.timezone(settings.DEFAULT_TIME_ZONE['code'])

    def getCaptionTimeStamp(self):
        if self.timestamp == None:
            return '(unknown)'
        else:
            tz = self.getCaptionTimeZone()
            tzName = settings.DEFAULT_TIME_ZONE['name']
            localizedDt = pytz.utc.localize(self.timestamp).astimezone(tz).replace(tzinfo=None)
            return '%s %s' % (str(localizedDt), tzName)

    def getCaptionFieldLatLon(self):
        if self.latitude == None:
            val = '(unknown)'
        else:
            val = '%.5f, %.5f' % (self.latitude, self.longitude)
        return ['lat, lon', val]

    def getCaptionFieldHeading(self):
        if self.yaw == None:
            val = '(unknown)'
        else:
            dirIndex = int(round(self.yaw / 22.5)) % 16
            cardinal = CARDINAL_DIRECTIONS[dirIndex]
            
            if self.yawRef == None:
                yawStr = 'unknown'
            else:
                yawStr = YAW_REF_LOOKUP[self.yawRef]
                
            val = ('%s %d&deg; (ref. %s)'
                   % (cardinal, round(self.yaw), yawStr))
        return ['heading', val]

    def getCaptionFieldTime(self):
        if self.timestamp == None:
            val = '(unknown)'
        else:
            val = self.getCaptionTimeStamp()
        return ['time', val]

    def getCaptionField(self, field):
        funcName = 'getCaptionField' + field[0].upper() + field[1:]
        return getattr(self, funcName)()

    def getCaptionFields(self):
        return ['latLon', 'heading', 'time']

    def getCaptionHeader(self):
        if self.timestamp == None:
            tsVal = '(unknown)'
        else:
            tsVal = TimeUtil.getTimeShort(self.timestamp, tz=self.getCaptionTimeZone())
        if self.name == None:
            nameVal = '(untitled)'
        else:
            nameVal = self.name
        tsColor = '#000077' # dark blue
        return ("""
<div style="padding-top: 5px; padding-bottom: 5px;">
  <span class="geocamCaption_name">%(nameVal)s</span>
  <span class="geocamCaption_timeShort">%(tsVal)s</span>
</div>
""" % dict(nameVal=nameVal, tsVal=tsVal, tsColor=tsColor))

    def getCaptionInfoTable(self):
        out = StringIO()
        out.write('<table>')
        for field in self.getCaptionFields():
            label, val = self.getCaptionField(field)
            out.write('<tr><td class="geocamCaption_fieldLabel">%s</td><td>%s</td></tr>' % (label, val))
        out.write('</table>')
        return out.getvalue()

class Photo(Image):
    objects = FinalModelManager(parentModel=Image)

class GoogleEarthSession(models.Model):
    """Session state for a Google Earth client that is requesting periodic updates."""
    sessionId = models.CharField(max_length=256)
    query = models.CharField(max_length=128, default='', help_text="User's query when session was initiated")
    utime = models.DateTimeField(help_text="The last time we sent an update to the client.")
    extras = models.TextField(max_length=1024, default='{}', help_text="A place to add extra fields if we need them but for some reason can't modify the table schema.  Expressed as a JSON-encoded dict.")

    @staticmethod
    def getSessionId(searchQuery=None):
        randomPart = '%08x' % random.getrandbits(32)
        if searchQuery:
            MAX_SEARCH_LEN = 200
            if len(searchQuery) > MAX_SEARCH_LEN:
                raise Exception('due to limitations of current db schema, search queries are limited to %d chars' % MAX_SEARCH_LEN)
            return '%s-%s' % (randomPart, searchQuery)
        else:
            return randomPart
        
    def getSearchQuery(self):
        if '-' in self.sessionId:
            return self.sessionId.split('-', 1)[1]
        else:
            return None

    def __unicode__(self):
        return u'<Session %s (%s)>' % (self.sessionId, self.utime)
    class Meta:
        verbose_name = 'Google Earth session'
        ordering = ['utime']
