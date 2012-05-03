#!/usr/bin/env python
# Copyright 2011 Google Inc. All Rights Reserved.
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

"""Retrieve web resources over http."""

import copy
import httparchive
import httplib
import logging
import os
import pkg_resources
import re
import sys
import time

# from timeit.py
if sys.platform == "win32":
  # On Windows, the best timer is time.clock()
  DEFAULT_TIMER = time.clock
else:
  # On most other platforms the best timer is time.time()
  DEFAULT_TIMER = time.time


HTML_RE = re.compile(r'<html[^>]*>', re.IGNORECASE)
HEAD_RE = re.compile(r'<head[^>]*>', re.IGNORECASE)


class HttpClientException(Exception):
  """Base class for all exceptions in httpclient."""
  pass


def GetInjectScript(scripts):
  """Loads |scripts| from disk and returns a string of their content."""
  lines = []
  for script in scripts:
    if os.path.exists(script):
      lines += open(script).read()
    elif pkg_resources.resource_exists(__name__, script):
      lines += pkg_resources.resource_string(__name__, script)
    else:
      raise HttpClientException('Script does not exist: %s', script)
  return ''.join(lines)


def _InjectScripts(response, inject_script):
  """Injects |inject_script| immediately after <head> or <html>.

  Copies |response| if it is modified.

  Args:
    response: an ArchivedHttpResponse
    inject_script: JavaScript string (e.g. "Math.random = function(){...}")
  Returns:
    an ArchivedHttpResponse
  """
  if type(response) == tuple:
    logging.warn('tuple response: %s', response)
  content_type = response.get_header('content-type')
  if content_type and content_type.startswith('text/html'):
    text = response.get_data_as_text()

    def InsertScriptAfter(matchobj):
      return '%s<script>%s</script>' % (matchobj.group(0), inject_script)

    if text and not inject_script in text:
      text, is_injected = HEAD_RE.subn(InsertScriptAfter, text, 1)
      if not is_injected:
        text, is_injected = HTML_RE.subn(InsertScriptAfter, text, 1)
      if not is_injected:
        logging.warning('Failed to inject scripts.')
        logging.debug('Response content: %s', text)
      else:
        response = copy.deepcopy(response)
        response.set_data(text)
  return response


class DetailedHTTPResponse(httplib.HTTPResponse):
  """Preserve details relevant to replaying responses.

  WARNING: This code uses attributes and methods of HTTPResponse
  that are not part of the public interface.
  """

  def read_chunks(self):
    """Return the response body content and timing data.

    The returned chunks have the chunk size and CRLFs stripped off.
    If the response was compressed, the returned data is still compressed.

    Returns:
      (chunks, delays)
        chunks:
          [response_body]                  # non-chunked responses
          [chunk_1, chunk_2, ...]          # chunked responses
        delays:
          [0]                              # non-chunked responses
          [chunk_1_first_byte_delay, ...]  # chunked responses

      The delay for the first body item should be recorded by the caller.
    """
    buf = []
    chunks = []
    delays = []
    if not self.chunked:
      chunks.append(self.read())
      delays.append(0)
    else:
      start = DEFAULT_TIMER()
      try:
        while True:
          line = self.fp.readline()
          chunk_size = self._read_chunk_size(line)
          if chunk_size is None:
            raise httplib.IncompleteRead(''.join(chunks))
          if chunk_size == 0:
            break
          delays.append(DEFAULT_TIMER() - start)
          chunks.append(self._safe_read(chunk_size))
          self._safe_read(2)  # skip the CRLF at the end of the chunk
          start = DEFAULT_TIMER()

        # Ignore any trailers.
        while True:
          line = self.fp.readline()
          if not line or line == '\r\n':
            break
      finally:
        self.close()
    return chunks, delays

  @classmethod
  def _read_chunk_size(cls, line):
    chunk_extensions_pos = line.find(';')
    if chunk_extensions_pos != -1:
      line = line[:extention_pos]  # strip chunk-extensions
    try:
      chunk_size = int(line, 16)
    except ValueError:
      return None
    return chunk_size


class DetailedHTTPConnection(httplib.HTTPConnection):
  """Preserve details relevant to replaying connections."""
  response_class = DetailedHTTPResponse


class DetailedHTTPSResponse(DetailedHTTPResponse):
  """Preserve details relevant to replaying SSL responses."""
  pass

class DetailedHTTPSConnection(httplib.HTTPSConnection):
  """Preserve details relevant to replaying SSL connections."""
  response_class = DetailedHTTPSResponse


class RealHttpFetch(object):
  def __init__(self, real_dns_lookup, get_server_rtt):
    """Initialize RealHttpFetch.

    Args:
      get_server_rtt: a function that returns the round-trip time of a host.
    """
    self._real_dns_lookup = real_dns_lookup
    self._get_server_rtt = get_server_rtt

  def __call__(self, request):
    """Fetch an HTTP request.

    Args:
      request: an ArchivedHttpRequest
    Returns:
      an ArchivedHttpResponse
    """
    logging.debug('RealHttpFetch: %s %s', request.host, request.path)
    host_ip = self._real_dns_lookup(request.host)
    if not host_ip:
      logging.critical('Unable to find host ip for name: %s', request.host)
      return None
    retries = 3
    while True:
      try:
        if request.is_ssl:
          connection = DetailedHTTPSConnection(host_ip)
        else:
          connection = DetailedHTTPConnection(host_ip)
        start = DEFAULT_TIMER()
        connection.request(
            request.command,
            request.path,
            request.request_body,
            request.headers)
        response = connection.getresponse()
        headers_delay = int((DEFAULT_TIMER() - start) * 1000)
        headers_delay -= self._get_server_rtt(request.host)

        chunks, chunk_delays = response.read_chunks()
        delays = {
            'headers': headers_delay,
            'data': chunk_delays
            }
        archived_http_response = httparchive.ArchivedHttpResponse(
            response.version,
            response.status,
            response.reason,
            response.getheaders(),
            chunks,
            delays)
        return archived_http_response
      except Exception, e:
        if retries:
          retries -= 1
          logging.warning('Retrying fetch %s: %s', request, e)
          continue
        logging.critical('Could not fetch %s: %s', request, e)
        return None


class RecordHttpArchiveFetch(object):
  """Make real HTTP fetches and save responses in the given HttpArchive."""

  def __init__(self, http_archive, real_dns_lookup, inject_script,
               cache_misses=None):
    """Initialize RecordHttpArchiveFetch.

    Args:
      http_archive: an instance of a HttpArchive
      real_dns_lookup: a function that resolves a host to an IP.
      inject_script: script string to inject in all pages
      cache_misses: instance of CacheMissArchive
    """
    self.http_archive = http_archive
    self.real_http_fetch = RealHttpFetch(real_dns_lookup,
                                         http_archive.get_server_rtt)
    self.inject_script = inject_script
    self.cache_misses = cache_misses

  def __call__(self, request):
    """Fetch the request and return the response.

    Args:
      request: an ArchivedHttpRequest.
    Returns:
      an ArchivedHttpResponse
    """
    if self.cache_misses:
      self.cache_misses.record_request(
          request, is_record_mode=True, is_cache_miss=False)

    # If request is already in the archive, return the archived response.
    if request in self.http_archive:
      logging.debug('Repeated request found: %s', request)
      response = self.http_archive[request]
    else:
      response = self.real_http_fetch(request)
      if response is None:
        return None
      self.http_archive[request] = response
    if self.inject_script:
      response = _InjectScripts(response, self.inject_script)
    logging.debug('Recorded: %s', request)
    return response


class ReplayHttpArchiveFetch(object):
  """Serve responses from the given HttpArchive."""

  def __init__(self, http_archive, inject_script,
               use_diff_on_unknown_requests=False, cache_misses=None,
               use_closest_match=False):
    """Initialize ReplayHttpArchiveFetch.

    Args:
      http_archive: an instance of a HttpArchive
      inject_script: script string to inject in all pages
      use_diff_on_unknown_requests: If True, log unknown requests
        with a diff to requests that look similar.
      cache_misses: Instance of CacheMissArchive.
        Callback updates archive on cache misses
      use_closest_match: If True, on replay mode, serve the closest match
        in the archive instead of giving a 404.
    """
    self.http_archive = http_archive
    self.inject_script = inject_script
    self.use_diff_on_unknown_requests = use_diff_on_unknown_requests
    self.cache_misses = cache_misses
    self.use_closest_match = use_closest_match

  def __call__(self, request):
    """Fetch the request and return the response.

    Args:
      request: an instance of an ArchivedHttpRequest.
    Returns:
      Instance of ArchivedHttpResponse (if found) or None
    """
    response = self.http_archive.get(request)

    if self.use_closest_match and not response:
      closest_request = self.http_archive.find_closest_request(
          request, use_path=True)
      if closest_request:
        response = self.http_archive.get(closest_request)
        if response:
          logging.info('Request not found: %s\nUsing closest match: %s',
                       request, closest_request)

    if self.cache_misses:
      self.cache_misses.record_request(
          request, is_record_mode=False, is_cache_miss=not response)

    if not response:
      reason = str(request)
      if self.use_diff_on_unknown_requests:
        diff = self.http_archive.diff(request)
        if diff:
          reason += (
              "\nNearest request diff "
              "('-' for archived request, '+' for current request):\n%s" % diff)
      logging.warning('Could not replay: %s', reason)
    else:
      response = _InjectScripts(response, self.inject_script)
    return response


class ControllableHttpArchiveFetch(object):
  """Controllable fetch function that can swap between record and replay."""

  def __init__(self, http_archive, real_dns_lookup,
               inject_script, use_diff_on_unknown_requests,
               use_record_mode, cache_misses, use_closest_match):
    """Initialize HttpArchiveFetch.

    Args:
      http_archive: an instance of a HttpArchive
      real_dns_lookup: a function that resolves a host to an IP.
      inject_script: script string to inject in all pages.
      use_diff_on_unknown_requests: If True, log unknown requests
        with a diff to requests that look similar.
      use_record_mode: If True, start in server in record mode.
      cache_misses: Instance of CacheMissArchive.
      use_closest_match: If True, on replay mode, serve the closest match
        in the archive instead of giving a 404.
    """
    self.record_fetch = RecordHttpArchiveFetch(
        http_archive, real_dns_lookup, inject_script,
        cache_misses)
    self.replay_fetch = ReplayHttpArchiveFetch(
        http_archive, inject_script, use_diff_on_unknown_requests, cache_misses,
        use_closest_match)
    if use_record_mode:
      self.SetRecordMode()
    else:
      self.SetReplayMode()

  def SetRecordMode(self):
    self.fetch = self.record_fetch
    self.is_record_mode = True

  def SetReplayMode(self):
    self.fetch = self.replay_fetch
    self.is_record_mode = False

  def __call__(self, *args, **kwargs):
    """Forward calls to Replay/Record fetch functions depending on mode."""
    return self.fetch(*args, **kwargs)
