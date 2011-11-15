#!/usr/bin/env python
# 
# Copyright 2011 Google Inc. All Rights Reserved.
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# #
#

"""Apple SUS shared functions."""




import datetime
import logging
import re
from xml.dom import minidom
from google.appengine.api import taskqueue
from google.appengine.ext import deferred
from simian.mac import common
from simian.mac import models
from simian.mac.munki import plist


OS_VERSIONS = ['10.5', '10.6', '10.7']

AUTO_PROMOTE_PHASE_DAYS_MAP = {
    common.TESTING: 4,
    common.STABLE: 7,
}

MON, TUE, WED, THU, FRI, SAT, SUN = range(0, 7)


class Error(Exception):
  """Base error."""


class DocumentFormatError(Error):
  """Error in document format."""


class DistFileDocument(object):
  """Class to hold a Apple SUS distfile document."""

  def __init__(self):
    """Initializer."""
    self.Reset()

  def Reset(self):
    """Reset variables."""
    self._installer_script = {}

  def _ParseInstallerScriptString(self, istr):
    """Parse an installer script string and return its key/value pairs.

    The installer script string appears generally as
      "KEY" = "VALUE"
    and can contain multiple lines. Apparently the quoting chars can be
    double or single quotes, and the alternative quote char is allowed as
    a literal inside the other.

    Standard javascript-style comments are permitted.

    Poorly formed lines will disrupt the parser and incomplete/no values
    will be returned.

    For example:

        // This comment is OK
        "KEY" = "VALUE";
        "KEY2" = "VALUE2";

        // Here's another comment later on.
        "KEY3" = 'VALUE3
        VALUE3MORE "THIS IS VALID"
        ';

    Or, consider:

        "KEY" = ;         # this will break the parser
        "NOTFOUNDKEY" = "NEVER GET HERE";

    Args:
      istr: str, see above format example above.
    Returns:
      dict
    """
    installer_script = {}

    kv_split = re.compile(
        (r'(?:^//[^\n]*$)|'
          '(?:^"(\w+)"\s*=\s*([\"\'])([^\\2]*?)\\2;$)'),
        re.MULTILINE | re.DOTALL)

    for i in re.finditer(kv_split, istr):
      if i.group(1):
        installer_script[i.group(1)] = i.group(3)

    return installer_script

  def LoadDocument(self, distfile_xml):
    """Load an entire distfile XML document and parse it.

    Args:
      distfile_xml: str, xml document
    Raises:
      DocumentFormatError: the XML document is malformed.
    """
    try:
      p = minidom.parseString(distfile_xml)
    except xml.parsers.expat.ExpatError, e:
      raise DocumentFormatError(str(e))

    try:
      l = p.getElementsByTagName('localization')[0]
      s = p.getElementsByTagName('strings')[0]
      cdata = []
      for cn in s.childNodes:
        cdata.append(cn.nodeValue)
      cdata = ''.join(cdata)
    except IndexError:
      raise DocumentFormatError

    # TODO(user): parse out suDisabledGroupID, which is the name that
    # will show up in the softwareupdate binary.

    self._installer_script = self._ParseInstallerScriptString(cdata)

  def GetInstallerScript(self):
    """Return the installer script values.

    Returns:
      dict, e.g. = {
          'SU_VERS': str,
          'SU_TITLE': str,
          ...
      }
    """
    return self._installer_script


def GenerateAppleSUSCatalogs(track=None, tracks=None, delay=0):
  """Generates Apple SUS catalogs for a given track, set of tracks, or all.
  Note: this generates tracks for all os_versions on the given track/tracks.

  Args:
    track: string track to generate catalog for. OR,
    tracks: list of string tracks.
    delay: int. if > 0, defer generating the catalogs by this many seconds.
  """
  if track and tracks:
    raise ValueError('only one of track and tracks is allowed')
  elif not tracks and not track:
    tracks = common.TRACKS
  elif track:
    tracks = [track]

  for track in tracks:
    for os_version in OS_VERSIONS:
      if delay:
        now_str = datetime.datetime.utcnow().strftime('%Y-%m-%d-%H-%M-%S')
        deferred_name = 'gen-applesus-catalog-%s-%s-%s' % (
            os_version, track, now_str)
        deferred_name = re.sub(r'[^\w-]', '', deferred_name)
        try:
          deferred.defer(
              GenerateAppleSUSCatalog, os_version, track, _name=deferred_name)
        except taskqueue.TaskAlreadyExistsError:
          logging.info('Skipping duplicate Apple SUS Catalog generation task.')
      else:
        GenerateAppleSUSCatalog(os_version, track)


def GenerateAppleSUSCatalog(os_version, track, _datetime=datetime.datetime):
  """Generates an Apple SUS catalog for a given os_version and track.

  This function loads the untouched/raw Apple SUS catalog, removes any
  products/updates that are not approved for the given track, then saves
  a new catalog (plist/xml) to Datastore for client consumption.

  Args:
    os_version: str OS version to generate the catalog for.
    track: str track name to generate the catalog for.
    _datetime: datetime module; only used for stub during testing.
  Returns:
    tuple, new models.AppleSUSCatalog object and plist.ApplePlist object.
  """
  logging.info('Generating catalog: %s_%s', os_version, track)
  approved_product_ids = {}
  products_query = models.AppleSUSProduct.all().filter('tracks =', track)
  for product in products_query:
    approved_product_ids[product.product_id] = True

  untouched_catalog_obj = models.AppleSUSCatalog.get_by_key_name(
      '%s_untouched' % os_version)
  untouched_catalog_plist = plist.ApplePlist(untouched_catalog_obj.plist)
  untouched_catalog_plist.Parse()

  product_ids = untouched_catalog_plist.get('Products', {}).keys()
  new_plist = untouched_catalog_plist
  for product_id in product_ids:
    if product_id not in approved_product_ids:
      del new_plist['Products'][product_id]

  catalog_plist_xml = new_plist.GetXml()

  # Save the catalog using a time-specific key for rollback purposes.
  now = _datetime.utcnow()
  now_str = now.strftime('%Y-%m-%d-%H-%M-%S')
  backup = models.AppleSUSCatalog(
      key_name='backup_%s_%s_%s' % (os_version, track, now_str))
  backup.plist = catalog_plist_xml
  backup.put()
  # Overwrite the catalog being served for this os_version/track pair.
  c = models.AppleSUSCatalog(key_name='%s_%s' % (os_version, track))
  c.plist = catalog_plist_xml
  c.put()
  return c, new_plist


def ParseDist(dist_str):
  """Parses an Apple SUS Dist string and returns dict of meta data.

  Args:
    dist_str: str Apple SUS Dist file.

  Returns:
    dictionary containing title, version, and description.
  """
  dfd = DistFileDocument()
  dfd.LoadDocument(dist_str)
  isd = dfd.GetInstallerScript()
  return {
      'title': isd.get('SU_TITLE'),
      'version': isd.get('SU_VERS') or isd.get('SU_VERSION'),
      'servercomment': isd.get('SU_SERVERCOMMENT'),
      'description': isd.get('SU_DESCRIPTION'),
  }


def GetAutoPromoteDate(track, applesus_product):
  """Returns a date of when a given update will auto-promote.

  Args:
    track: str track to get the auto-promote datetime for.
    applesus_product: models.AppleSUSProduct object.
  Returns:
    datetime.date of when the Apple SUS update will be auto-promoted to track,
    or None if the product will never be auto-promoted due to manual_override or
    the product not being in the unstable track.
  Raises:
    ValueError: an invalid track was specified; only testing/stable supported.
  """
  if applesus_product.manual_override:
    return None
  elif common.UNSTABLE not in applesus_product.tracks:
    return None

  days = AUTO_PROMOTE_PHASE_DAYS_MAP.get(track)
  if not days:
    raise ValueError('Invalid track was specified: %s' % track)

  auto_promote_offset = datetime.timedelta(days=days)
  previous_track_date = applesus_product.mtime.date()

  if track == common.TESTING:
    auto_promote_date = previous_track_date + auto_promote_offset
    if auto_promote_date.weekday() >= SAT:  # Sat or Sun.
      auto_promote_date = _GetNextWeekdayDate(
          weekday=MON, min_date=auto_promote_date)
    return auto_promote_date

  # If we're looking for a stable auto-promotion date but the item is not yet in
  # testing, then we need to first figure out when it will go to testing and set
  # the previous_track_mtime to that.
  if common.TESTING not in applesus_product.tracks:
    previous_track_date = GetAutoPromoteDate('testing', applesus_product)

  # Unstable should only promoted on Wednesdays and only after the grace period.
  min_auto_promote_date = previous_track_date + auto_promote_offset
  return _GetNextWeekdayDate(min_date=min_auto_promote_date)


def _GetNextWeekdayDate(weekday=WED, min_date=None):
  """Returns the date of the current or next weekday on or after min_date.

  Args:
    weekday: int weekday number, where Monday is 0 and Sunday is 6.
    min_date: datetime.date object of the minimum date to find the weekday on
        or after. default of None uses today as the minimum date.
  Returns:
    datetime.date object of the current or next desired weekday.
  """
  if min_date is None:
    min_date = datetime.datetime.utcnow().date()

  next_date = min_date

  if min_date.weekday() > weekday:
    next_date += datetime.timedelta(7 - min_date.weekday() + weekday)
  else:
    next_date += datetime.timedelta(weekday - min_date.weekday())

  return next_date